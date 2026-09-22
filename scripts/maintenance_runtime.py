"""Small runtime shared by the unattended launcher and reentrant worker."""
from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "cache" / "maintenance"


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def run_logged(command: list[str], log: Path, timeout: float, env=None) -> int:
    """Drain output without unbounded logs; kill the whole child tree on timeout."""
    log.parent.mkdir(parents=True, exist_ok=True)
    tail = bytearray()
    limit = 2 * 1024 * 1024
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        with log.open("wb") as handle:
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                for key, _ in selector.select(timeout=min(1, max(0, deadline - time.monotonic()))):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    tail.extend(chunk)
                    if len(tail) > limit:
                        del tail[:-limit]
                        handle.seek(0)
                        handle.truncate()
                        handle.write(tail)
                    else:
                        handle.write(chunk)
                    handle.flush()
            return process.wait(timeout=max(0.01, deadline - time.monotonic()))
    finally:
        # Also reap descendants that outlive the main command.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()
        selector.close()
