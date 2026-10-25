#!/usr/bin/env -S uv run --python 3.11
from __future__ import annotations

import argparse
import json
from pathlib import Path

from lib.common import ensure_root
from lib.crates import CRATES_IO_INDEX_PATH, CRATES_IO_MIN_RECENT_DOWNLOADS, CRATES_IO_RECENT_WINDOW_DAYS, build_crates_index


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the crates.io CLI package index used by package pages.")
    parser.add_argument("--refresh", action="store_true", help="Refresh the cached crates.io database dump.")
    parser.add_argument("--dump", type=Path, help="Use an existing db-dump.tar.gz file instead of downloading one.")
    parser.add_argument("--output", type=Path, default=CRATES_IO_INDEX_PATH, help=f"Output path. Defaults to {CRATES_IO_INDEX_PATH}.")
    parser.add_argument(
        "--min-recent-downloads",
        type=int,
        default=CRATES_IO_MIN_RECENT_DOWNLOADS,
        help=f"Minimum recent downloads for inclusion. Defaults to {CRATES_IO_MIN_RECENT_DOWNLOADS}.",
    )
    parser.add_argument(
        "--recent-window-days",
        type=int,
        default=CRATES_IO_RECENT_WINDOW_DAYS,
        help=f"Recent-download window in days. Defaults to {CRATES_IO_RECENT_WINDOW_DAYS}.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_root()
    index = build_crates_index(
        refresh=args.refresh,
        dump_path=args.dump,
        output_path=args.output,
        min_recent_downloads=args.min_recent_downloads,
        recent_window_days=args.recent_window_days,
    )
    print(json.dumps({"ok": True, "path": str(args.output), "crates": len(index.get("crates") or {})}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
