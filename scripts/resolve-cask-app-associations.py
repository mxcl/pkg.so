#!/usr/bin/env -S uv run --python 3.10
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "bootstrap"))

from lib.casks import (  # noqa: E402
    CASK_APP_ASSOCIATIONS_PATH,
    is_bundle_identifier,
    read_cask_app_associations,
    read_cask_cache,
    unresolved_cask_app_associations,
)
from lib.common import CACHE_DIR, read_json, write_json  # noqa: E402


SCHEMA_PATH = ROOT / "schemas" / "codex-cask-app-association-output.schema.json"
DEFAULT_LIMIT = int(os.environ.get("AVDB_CASK_APP_ASSOCIATION_LIMIT", "50"))
DEFAULT_BATCH_SIZE = int(os.environ.get("AVDB_CASK_APP_ASSOCIATION_BATCH_SIZE", "5"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve ambiguous Homebrew cask app bundle identifiers with Codex.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--token", action="append", default=[], help="Resolve only this cask token; repeatable.")
    parser.add_argument("--force", action="store_true", help="Review matching casks even when current decisions exist.")
    return parser.parse_args()


def batches(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    if size < 1:
        raise SystemExit("--batch-size must be at least 1")
    return [items[index:index + size] for index in range(0, len(items), size)]


def input_record(candidate: dict[str, Any]) -> dict[str, Any]:
    token = candidate["token"]
    return {
        **candidate,
        "homebrew_cask_page": f"https://formulae.brew.sh/cask/{token}",
        "homebrew_cask_source": f"https://github.com/Homebrew/homebrew-cask/blob/HEAD/Casks/{token[0]}/{token}.rb",
    }


def prompt_text(input_path: Path) -> str:
    return f"""/goal Identify the primary macOS application bundle identifier for every Homebrew cask in the input.

Read {input_path}. It contains a JSON object with a `casks` array. Return exactly one result for every token and match the provided output schema.

These casks could not be resolved deterministically because their uninstall and zap metadata contains either no repeated bundle identifier or several. Determine the bundle identifier of the primary `.app` artifact, not a helper, updater, extension, group identifier, preference domain, or filename mistaken for an identifier.

Use official sources only: the linked Homebrew cask page/source, the vendor's official documentation or repository, or an official app manifest. Use `high` confidence only with direct, convincing evidence. Return `bundle_identifier: null` when uncertain. Every result needs at least one source URL and a concise reason. Do not edit files.
"""


def write_output_schema(path: Path, tokens: list[str]) -> None:
    schema = read_json(SCHEMA_PATH, {})
    results = schema["properties"]["results"]
    results["minItems"] = len(tokens)
    results["maxItems"] = len(tokens)
    results["items"]["properties"]["token"]["enum"] = sorted(tokens)
    write_json(path, schema)


def invoke_codex(input_path: Path, output_path: Path, schema_path: Path) -> None:
    command = [
        "codex",
        "--search",
        "--ask-for-approval",
        "never",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--color",
        "never",
        "--sandbox",
        "read-only",
        "--output-schema",
        str(schema_path),
        "-C",
        str(ROOT),
        "-o",
        str(output_path),
        prompt_text(input_path),
    ]
    timeout_raw = os.environ.get("AVDB_CODEX_TIMEOUT_SECONDS", "1800").strip()
    timeout = float(timeout_raw) if timeout_raw else None
    subprocess.run(command, cwd=ROOT, check=True, timeout=timeout, stdin=subprocess.DEVNULL)


def validated_results(payload: Any, expected_tokens: set[str]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("Codex output must contain a results array")
    results = payload["results"]
    tokens = [item.get("token") for item in results if isinstance(item, dict)]
    if len(results) != len(expected_tokens) or set(tokens) != expected_tokens or len(tokens) != len(set(tokens)):
        raise ValueError("Codex output must contain exactly one result for every requested token")
    for item in results:
        bundle_identifier = item.get("bundle_identifier")
        confidence = item.get("confidence")
        sources = item.get("sources")
        reason = item.get("reason")
        if bundle_identifier is not None and not is_bundle_identifier(bundle_identifier):
            raise ValueError(f"{item['token']}: invalid bundle identifier")
        if confidence not in {"high", "medium", "low"}:
            raise ValueError(f"{item['token']}: invalid confidence")
        if not isinstance(sources, list) or not sources or not all(
            isinstance(source, str) and source.startswith("https://") for source in sources
        ):
            raise ValueError(f"{item['token']}: sources must be non-empty HTTPS URLs")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{item['token']}: reason is required")
    return results


def apply_results(
    associations: dict[str, dict[str, Any]],
    candidates: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    candidates_by_token = {candidate["token"]: candidate for candidate in candidates}
    for result in results:
        token = result["token"]
        associations[token] = {
            "bundle_identifier": result["bundle_identifier"],
            "confidence": result["confidence"],
            "evidence_hash": candidates_by_token[token]["evidence_hash"],
            "reason": result["reason"].strip(),
            "sources": sorted(set(result["sources"])),
        }


def main() -> int:
    args = parse_args()
    if args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    associations = read_cask_app_associations()
    candidates = unresolved_cask_app_associations(
        read_cask_cache(),
        {} if args.force else associations,
    )
    if args.token:
        requested = set(args.token)
        candidates = [candidate for candidate in candidates if candidate["token"] in requested]
    candidates = candidates[:args.limit]
    if not candidates:
        print("No unresolved cask app associations selected")
        return 0

    work_root = CACHE_DIR / "cask-app-association-agent"
    work_root.mkdir(parents=True, exist_ok=True)
    for index, batch in enumerate(batches(candidates, args.batch_size), start=1):
        with tempfile.TemporaryDirectory(prefix="batch-", dir=work_root) as temporary:
            directory = Path(temporary)
            input_path = directory / "input.json"
            output_path = directory / "output.json"
            schema_path = directory / "output-schema.json"
            write_json(input_path, {"casks": [input_record(candidate) for candidate in batch]})
            write_output_schema(schema_path, [candidate["token"] for candidate in batch])
            invoke_codex(input_path, output_path, schema_path)
            results = validated_results(read_json(output_path, {}), {candidate["token"] for candidate in batch})
            apply_results(associations, batch, results)
            write_json(CASK_APP_ASSOCIATIONS_PATH, {"schema": 1, "associations": associations})
            accepted = sum(
                result["bundle_identifier"] is not None and result["confidence"] == "high"
                for result in results
            )
            print(f"Resolved cask app batch {index}: {accepted}/{len(batch)} high-confidence associations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
