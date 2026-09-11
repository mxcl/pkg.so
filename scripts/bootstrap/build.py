#!/usr/bin/env -S uv run --python 3.10
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from lib.common import CACHE_DIR, ROOT, ensure_root, stable_json, write_json


STATE_PATH = CACHE_DIR / "build-state.json"


@dataclass(frozen=True)
class Step:
    name: str
    command: list[str]
    inputs: list[Path]
    outputs: list[Path]
    refresh_sensitive: bool = False


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.as_posix().encode("utf-8"))
    digest.update(b"\0")
    if path.is_dir():
        for child in sorted(item for item in path.rglob("*") if item.is_file()):
            digest.update(child.relative_to(path).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(child.read_bytes())
            digest.update(b"\0")
    else:
        digest.update(path.read_bytes())
    return digest.hexdigest()


def fingerprint(step: Step, *, refresh: bool) -> str:
    payload = {
        "name": step.name,
        "command": step.command,
        "refresh": refresh if step.refresh_sensitive else False,
        "inputs": [
            {"path": path.as_posix(), "sha256": file_hash(path)}
            for path in step.inputs
            if path.exists()
        ],
    }
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def steps(refresh: bool, fetch_manifests: bool, manifest_limit: int) -> list[Step]:
    py = sys.executable
    brew_cmd = [py, "scripts/bootstrap/01-brew-fetch.py"]
    manager_cmd = [py, "scripts/bootstrap/02-manager-indexes.py"]
    crates_cmd = [py, "scripts/bootstrap/02-crates-index.py"]
    if refresh:
        brew_cmd.append("--refresh")
        crates_cmd.append("--refresh")
    if fetch_manifests:
        brew_cmd.append("--fetch-manifests")
    if manifest_limit:
        brew_cmd.extend(["--manifest-limit", str(manifest_limit)])
    return [
        Step(
            "brew-fetch",
            brew_cmd,
            [
                Path("scripts/bootstrap/01-brew-fetch.py"),
                Path("scripts/bootstrap/lib/brew.py"),
                Path("scripts/bootstrap/lib/common.py"),
                Path("scripts/bootstrap/lib/casks.py"),
                Path("scripts/bootstrap/lib/executables.py"),
            ],
            [
                Path("cache/brew/formulae.json"),
                Path("cache/brew/casks.json"),
                Path("cache/brew/cask-entries.json"),
                Path("cache/brew/casks.json"),
                Path("cache/brew/executables.json"),
                Path("cache/brew/executable-entries.json"),
            ],
            refresh_sensitive=True,
        ),
        Step(
            "manager-indexes",
            manager_cmd,
            [
                Path("scripts/bootstrap/02-manager-indexes.py"),
                Path("scripts/bootstrap/lib/managers.py"),
                Path("scripts/bootstrap/lib/common.py"),
            ],
            [Path("cache/pkg-manager-indexes.json.gz")],
        ),
        Step(
            "crates-index",
            crates_cmd,
            [
                Path("scripts/bootstrap/02-crates-index.py"),
                Path("scripts/bootstrap/lib/crates.py"),
                Path("scripts/bootstrap/lib/common.py"),
            ],
            [Path("cache/cratesio/index.json")],
            refresh_sensitive=True,
        ),
        Step(
            "render-projects",
            [py, "scripts/bootstrap/03-render-projects.py"],
            [
                Path("scripts/bootstrap/03-render-projects.py"),
                Path("scripts/bootstrap/lib/render.py"),
                Path("scripts/bootstrap/lib/yaml_writer.py"),
                Path("scripts/bootstrap/lib/brew.py"),
                Path("scripts/bootstrap/lib/casks.py"),
                Path("scripts/bootstrap/lib/managers.py"),
                Path("cache/brew/formulae.json"),
                Path("cache/brew/casks.json"),
                Path("cache/brew/executables.json"),
                Path("cache/brew/cask-entries.json"),
                Path("cache/pkg-manager-indexes.json.gz"),
                Path("data/cask-app-associations.json"),
            ],
            [Path("cache/stage/deterministic")],
        ),
        Step(
            "publish-projects",
            [py, "scripts/bootstrap/04-publish-projects.py"],
            [
                Path("scripts/bootstrap/04-publish-projects.py"),
                Path("scripts/bootstrap/lib/render.py"),
                Path("scripts/bootstrap/lib/common.py"),
                Path("cache/stage/deterministic"),
                Path("agents"),
                Path("human-override"),
            ],
            [Path("deterministic"), Path("combined")],
        ),
    ]


def output_exists(path: Path) -> bool:
    if path.is_dir():
        return any(item.is_file() for item in path.rglob("*"))
    return path.exists()


def should_run(step: Step, state: dict[str, str], *, refresh: bool, force: bool) -> tuple[bool, str]:
    fp = fingerprint(step, refresh=refresh)
    if force:
        return True, fp
    if refresh and step.refresh_sensitive:
        return True, fp
    if state.get(step.name) != fp:
        return True, fp
    if not all(output_exists(path) for path in step.outputs):
        return True, fp
    return False, fp


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the pkgdb package data pipeline incrementally.")
    parser.add_argument("--refresh", action="store_true", help="Refresh remote source caches.")
    parser.add_argument("--force", action="store_true", help="Run every step even if fingerprints match.")
    parser.add_argument("--fetch-manifests", action="store_true", help="Fetch GHCR manifests to discover executables missing from the local seed.")
    parser.add_argument("--manifest-limit", type=int, default=0, help="Limit GHCR manifest fetches for debugging.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_root()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    next_state = dict(state)
    for step in steps(args.refresh, args.fetch_manifests, args.manifest_limit):
        run, fp = should_run(step, state, refresh=args.refresh, force=args.force)
        if not run:
            print(f"SKIP {step.name}", flush=True)
            continue
        print(f"RUN  {step.name}", flush=True)
        subprocess.run(step.command, cwd=ROOT, check=True)
        next_state[step.name] = fp
        write_json(STATE_PATH, next_state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
