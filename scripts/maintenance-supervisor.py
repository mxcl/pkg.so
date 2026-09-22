#!/usr/bin/env python3
"""Bounded, single-instance Codex supervisor with independent verification/alerts."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time

from maintenance_runtime import ROOT, STATE_DIR, read_json, run_logged


def notify(kind, message):
    return subprocess.run([sys.executable, str(ROOT / "scripts/maintenance-notify.py"),
                           kind, "--message", message], timeout=45).returncode


def supervise():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "supervisor.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Maintenance supervisor already running", flush=True)
            return 0
        if run_logged([sys.executable, "scripts/maintenance-step.py", "begin"],
                      STATE_DIR / "begin.log", 30):
            notify("failure", "Could not initialize or resume maintenance state. Inspect begin.log.")
            return 1
        deadline = time.monotonic() + float(os.environ.get("PKG_MAINTENANCE_TIMEOUT", "36000"))
        attempts = int(os.environ.get("PKG_MAINTENANCE_ATTEMPTS", "3"))
        environment = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")}
        prompt = (ROOT / "scripts/maintenance-master.md").read_text()
        for attempt in range(1, attempts + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            command = ["codex", "--model", "gpt-5.6-sol", "--config", 'model_reasoning_effort="medium"',
                       "--search", "--ask-for-approval", "never", "exec", "--ephemeral",
                       "--ignore-user-config", "--color", "never", "--sandbox", "danger-full-access",
                       "-C", str(ROOT), prompt]
            print(f"Starting maintenance supervisor attempt {attempt}/{attempts}", flush=True)
            try:
                result = run_logged(command, STATE_DIR / "supervisor.log", remaining, environment)
                if result == 0:
                    verification = run_logged([sys.executable, "scripts/maintenance-step.py", "verify"],
                                              STATE_DIR / "verification.log", 300)
                    if verification == 0:
                        notify("recovery", "Maintenance recovered. Live SQLite/JSON hashes, SQLite integrity, Discover feed publication, clean checkout and origin health verified.")
                        return 0
                    # A normal agent exit without verified completion is an escalation,
                    # not a reason to silently launch more repair attempts.
                    break
            except (OSError, subprocess.SubprocessError) as error:
                print(f"Supervisor attempt failed: {error}", flush=True)
            if attempt < attempts:
                delay = min(60 * 2 ** (attempt - 1), max(0, deadline - time.monotonic()))
                time.sleep(delay)
        state = read_json(STATE_DIR / "current.json", {})
        stage = state.get("stage", "supervisor startup")
        notify("failure", f"Maintenance did not complete within its bounded attempts. Stage: {stage}. "
               "Inspect cache/maintenance/current.json, supervisor.log and stage logs. "
               "The last successful publication remains available; current health requires verification.")
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(supervise())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Maintenance launcher failed: {error}", file=sys.stderr)
        notify("failure", "Maintenance launcher failed before completion. Inspect the service journal.")
        raise SystemExit(1)
