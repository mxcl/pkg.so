#!/usr/bin/env python3
"""Collect Homebrew discovery leads and resolve a bounded batch of tap packages."""
import argparse
import datetime as dt
import fcntl
import json
from collections import Counter

from bootstrap.lib.discovery import (
    CATEGORIES, WINDOWS, Fetcher, analytics_url, load_state, observe,
    parse_report, resolve_candidates, save_state, state_path,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--package", action="append", default=[], help="Prioritize provider:owner/tap/package")
    args = parser.parse_args()
    if args.limit < 1: parser.error("--limit must be positive")
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state(path)
        fetcher = Fetcher(path.parent / "http")
        today = dt.datetime.now(dt.timezone.utc).date()
        errors = {}
        for provider in CATEGORIES:
            try:
                reports = {days: parse_report(fetcher.get(analytics_url(provider, days)), provider, days) for days in WINDOWS}
                observe(state, {provider: reports}, today)
            except (ValueError, OSError, KeyError) as error:
                errors[provider] = str(error)
        resolve_candidates(state, fetcher.resolve, today, args.limit, args.package)
        state["reportErrors"] = errors
        save_state(state, path)
        print(json.dumps({"state": str(path), "reportErrors": errors,
                          "statuses": dict(Counter(r["status"] for r in state["packages"].values())),
                          "requested": {k: state["packages"].get(k, {}) for k in args.package}}, indent=2))
        if not state.get("baselines"):
            raise SystemExit("No valid analytics baseline; discovery publication cannot start")


if __name__ == "__main__":
    main()
