#!/usr/bin/env python3
"""Fixed-recipient SES alerts, also usable when Codex cannot start."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from maintenance_runtime import STATE_DIR, read_json, save_json

CONFIG = Path("/etc/pkgdb-maintenance-notify.json")


def notify(kind: str, message: str) -> bool:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "notification.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = STATE_DIR / ("notification-test.json" if kind == "test" else "notification.json")
        state = read_json(path, {})
        if kind == "recovery" and not state.get("open"):
            return True
        if kind == "failure" and state.get("open") and state.get("delivered"):
            print("Existing incident already reported")
            return True
        # Persist an outbox before attempting SES. Never claim delivery on failure.
        pending = {"open": True, "delivered": False, "pending_kind": kind,
                   "message": message[:4000], "previous": state.get("message", "")[:4000]}
        save_json(path, pending)
        try:
            config = read_json(CONFIG, {})
            for field in ("sender", "recipient", "region"):
                if not isinstance(config.get(field), str) or not config[field].strip():
                    raise ValueError(f"Set {field} in {CONFIG}")
            payload = {
                "FromEmailAddress": config["sender"],
                "Destination": {"ToAddresses": [config["recipient"]]},
                "Content": {"Simple": {
                    "Subject": {"Data": f"pkg.so maintenance: {kind}"},
                    "Body": {"Text": {"Data": message[:4000] +
                        "\n\nInspect: journalctl -u pkgdb-maintenance.service\n"
                        "State: /apps/pkg.so/cache/maintenance/current.json\n"
                        "Respond through the maintenance Codex task; email replies are not monitored."}},
                }},
            }
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as handle:
                json.dump(payload, handle)
                handle.flush()
                result = subprocess.run(
                    ["aws", "sesv2", "send-email", "--region", config["region"],
                     "--cli-input-json", "file://" + handle.name],
                    capture_output=True, text=True, timeout=30, check=True,
                )
            pending.update(delivered=True, open=kind == "failure",
                           message_id=json.loads(result.stdout)["MessageId"])
            save_json(path, pending)
            print("SES accepted notification", pending["message_id"])
            return True
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            pending["delivery_error"] = str(error)
            save_json(path, pending)
            print(f"NOTIFICATION_UNDELIVERED: {error}; outbox: {path}", file=sys.stderr)
            return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["failure", "recovery", "test", "service-stop"])
    parser.add_argument("--message", default="")
    args = parser.parse_args()
    if args.kind == "service-stop":
        result = os.environ.get("SERVICE_RESULT", "success")
        if result == "success":
            return 0
        args.kind = "failure"
        args.message = f"Maintenance service stopped with result {result}. Inspect the local journal and persisted stage state."
    return 0 if notify(args.kind, args.message or "Maintenance notification delivery test.") else 1


if __name__ == "__main__":
    raise SystemExit(main())
