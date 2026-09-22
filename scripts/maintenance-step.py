#!/usr/bin/env python3
"""Run exactly one durable maintenance stage and print the next action as JSON."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import urllib.request

from maintenance_runtime import ROOT, STATE_DIR, read_json, run_logged, save_json

sys.path.insert(0, str(ROOT))
from scripts.bootstrap.lib.common import git_commit_if_changed

STATE = STATE_DIR / "current.json"
LIVE = Path("/var/lib/automic-vault-web")
COMMIT_PATHS = ["deterministic", "combined", "agents", "human-override",
                "data/approval-gates", "data/cask-app-associations.json", "data/pkg-hubs.json",
                "data/pkg-i18n", "data/pkg-pages", "data/pkg-taxonomy.json"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def stages(state):
    py = sys.executable
    def script(name, *args):
        return [py, "scripts/" + name, *args]
    settings = state["settings"]
    return [
        ("checkout", None),
        ("source-refresh", script("build-db.py", "--refresh", "--npm-full-scan-parts=7")),
        ("source-render", script("build.py", "--refresh")),
        ("source-commit", None),
        ("cask-research", script("resolve-cask-app-associations.py", "--limit", "50", "--batch-size", "5")),
        ("cask-render", script("build.py")),
        ("cask-commit", None),
        ("enrichment", script("enrich-projects.py", "--mode", "new", "--include-missing-curated-fields",
                              "--limit", str(settings["enrich_limit"]), "--batch-size", str(settings["batch_size"]),
                              "--commit-after-batch", "--backend", "codex-cli", "--phase", "run",
                              "--run-id", state["enrichment_run_id"])),
        ("page-enrichment", script("generate-pkg-page-enrichment.py", "--refresh", "--registry-cache-only")),
        ("version-freshness", script("generate-pkg-version-freshness.py")),
        ("manager-indexes", script("generate-pkg-manager-indexes.py")),
        ("cross-ecosystem", script("generate-pkg-cross-ecosystem.py")),
        ("graph", script("generate-pkg-graph.py")),
        ("graph-curation", script("generate-pkg-graph-curation.py")),
        ("graph-final", script("generate-pkg-graph.py")),
        ("sqlite", script("generate-pkg-sqlite.py", "--output", str(LIVE / "pkg.sqlite.next"))),
        ("sqlite-check", script("generate-pkg-sqlite.py", "--check", "--output", str(LIVE / "pkg.sqlite.next"))),
        ("data-commit", None),
        ("json", script("export-automic-vault-db.py", "--output", str(LIVE / "db.json.next"))),
        ("publish-data", None),
        ("feed-research", ["scripts/update-discover-feed"]),
        ("feed-check", ["scripts/update-discover-feed", "--check"]),
        ("feed-publish", ["scripts/publish-discover-feed-atlas.sh"]),
        ("verify", None),
    ]


def publish_data(state):
    # Record both candidate hashes BEFORE either rename. A crash between renames
    # can then finish the same publication without rebuilding or losing a file.
    if "artifacts" not in state:
        db = LIVE / "pkg.sqlite.next"
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite integrity check failed")
        json.loads((LIVE / "db.json.next").read_text())
        state["artifacts"] = {name: digest(LIVE / (name + ".next")) for name in ("pkg.sqlite", "db.json")}
        save_json(STATE, state)
    for name, expected in state["artifacts"].items():
        target = LIVE / name
        candidate = LIVE / (name + ".next")
        if candidate.exists():
            if digest(candidate) != expected:
                raise ValueError(f"Candidate changed during publication: {candidate}")
            candidate.chmod(0o640)
            os.replace(candidate, target)
        if digest(target) != expected:
            raise ValueError(f"Published artifact mismatch: {target}")
    health()


def health():
    with urllib.request.urlopen("http://127.0.0.1:3004/healthz", timeout=10) as response:
        if response.status != 200 or response.read().strip() != b"ok":
            raise ValueError("Origin health check failed")


def verify(state):
    if set(state.get("artifacts", {})) != {"pkg.sqlite", "db.json"}:
        raise ValueError("No publication recorded for this run")
    for name, expected in state["artifacts"].items():
        if digest(LIVE / name) != expected:
            raise ValueError(f"Live artifact differs from this run: {name}")
    source_feed = ROOT / "www/feed"
    if not all((source_feed / name).is_file() for name in ("v1.json", "v2.json")):
        raise ValueError("Discover feed manifests missing")
    for source in source_feed.rglob("*"):
        if source.is_file():
            name = source.relative_to(source_feed)
            if digest(source) != digest(LIVE / "feed/current" / name):
                raise ValueError(f"Discover feed has not been published: {name}")
    with sqlite3.connect(f"file:{LIVE / 'pkg.sqlite'}?mode=ro", uri=True) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Live SQLite integrity check failed")
    health()
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True)
    if dirty.strip():
        raise ValueError("Tracked changes remain; inspect and commit verified work before completion")


def load_state():
    state = read_json(STATE, {})
    today = now()[:10]
    if not state or (state.get("status") == "complete" and state["started_at"][:10] != today):
        if state:
            save_json(STATE_DIR / "runs" / (state["run_id"] + ".json"), state)
        state = {"schema": 1, "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                 "started_at": now(), "status": "running", "completed": [], "attempts": {},
                 "settings": {"enrich_limit": int(os.environ.get("AVDB_ENRICH_LIMIT", "250")),
                              "batch_size": int(os.environ.get("AVDB_ENRICH_BATCH_SIZE", "5"))}}
        # Adopt the newest interrupted CLI run on upgrade; future retries retain
        # this exact manifest and selection rather than regenerating 250 projects.
        state["enrichment_run_id"] = "maintenance-" + state["run_id"]
        runs = ROOT / "cache/enrichment/runs"
        for manifest_path in sorted(runs.glob("*/controller-manifest.json"), key=lambda path: path.stat().st_mtime, reverse=True):
            manifest = read_json(manifest_path, {})
            if (manifest.get("backend") == "codex-cli" and manifest.get("mode") == "new"
                    and not (manifest_path.parent / "apply-summary.json").exists()):
                state["enrichment_run_id"] = manifest_path.parent.name
                break
        save_json(STATE, state)
    return state


def step(state):
    if state["status"] == "complete":
        verify(state)
        return {"status": "complete", "run_id": state["run_id"]}
    name, command = next((name, command) for name, command in stages(state) if name not in state["completed"])
    state.update(stage=name, status="running", updated_at=now())
    state["attempts"][name] = state["attempts"].get(name, 0) + 1
    save_json(STATE, state)
    log = STATE_DIR / "logs" / state["run_id"] / (name + ".log")
    try:
        if name == "checkout":
            dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True)
            if dirty.strip():
                raise ValueError("Checkout has tracked changes. Inspect git diff and git diff --cached; preserve unrelated work. " + dirty[:1000])
            if run_logged(["git", "pull", "--ff-only", "origin", "main"], log, 120):
                raise ValueError(f"Git update failed; inspect {log}")
        elif name.endswith("-commit"):
            git_commit_if_changed("nightly: " + name, COMMIT_PATHS)
        elif name == "publish-data":
            publish_data(state)
        elif name == "verify":
            verify(state)
        else:
            env = {**os.environ, "AVDB_ENRICH_BACKEND": "codex-cli"}
            result = run_logged(command, log, float(os.environ.get("PKG_MAINTENANCE_STAGE_TIMEOUT", "21600")), env)
            if result:
                raise ValueError(f"Stage exited {result}; inspect {log}")
            if name == "feed-research":
                output = log.read_text()
                statuses = [line.split("=", 1)[1] for line in output.splitlines() if line.startswith("PMM_FEED_STATUS=")]
                if not statuses:
                    raise ValueError("Feed generator returned no status")
                if statuses[-1] == "NEEDS_AGENT":
                    state["status"] = "needs_agent"
                    save_json(STATE, state)
                    return {"status": "needs_agent", "stage": name, "prompt_file": str(log),
                            "instruction": "Read the prompt file, research and write its requested response. Then call step again. Do not run the feed generator yourself."}
                if statuses[-1] not in {"NOOP", "COMMITTED"}:
                    raise ValueError(f"Unexpected feed status: {statuses[-1]}")
        state["completed"].append(name)
        state.pop("error", None)
        state["status"] = "complete" if name == "verify" else "running"
        state["updated_at"] = now()
        save_json(STATE, state)
        return {"status": state["status"], "completed_stage": name, "instruction": "Call step again until complete."}
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        state.update(status="failed", error=str(error), updated_at=now())
        save_json(STATE, state)
        return {"status": "failed", "stage": name, "error": str(error), "log": str(log),
                "instruction": "Diagnose and repair within the master prompt's limits, then retry this same step. Do not modify the state to skip failures."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["begin", "step", "status", "verify"])
    args = parser.parse_args()
    if args.action == "status":
        print(json.dumps(read_json(STATE, {"status": "not_started"})))
        return 0
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "worker.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "busy", "instruction": "Another step is still running. Wait for it."}))
            return 1
        if args.action == "begin":
            print(json.dumps(load_state()))
            return 0
        if args.action == "verify":
            state = read_json(STATE, {})
            if state.get("status") != "complete":
                raise SystemExit("Maintenance has not completed")
            verify(state)
            print(json.dumps({"status": "complete", "run_id": state["run_id"]}))
            return 0
        result = step(load_state())
        print(json.dumps(result))
        return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
