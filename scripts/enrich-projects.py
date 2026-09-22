#!/usr/bin/env -S uv run --python 3.10
from __future__ import annotations

import argparse
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT_PATH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_PATH))

from scripts.bootstrap.lib.common import CACHE_DIR, ROOT, ensure_root, git_commit_if_changed, read_json, write_json
from scripts.bootstrap.lib.render import render_combined_tree
from scripts.enrichment import (
    apply_results,
    has_missing_curated_fields,
    load_projects,
    prompt_text,
    review_input,
    run_id,
    select_projects,
    today_iso,
    update_observed_state,
    validation_error_summary,
    validation_rejection_count,
    validate_codex_payload_partial,
    write_run_artifacts,
)


SCHEMA_PATH = ROOT / "schemas" / "codex-project-enrichment-output.schema.json"
DEFAULT_CODEX_TIMEOUT_SECONDS = 30 * 60
PATH_LOCATION_PLATFORMS = ("unix", "linux", "macos", "windows")
ENRICH_BACKENDS = ("codex-cli", "external")
ENRICH_PHASES = ("run", "prepare", "apply")
ENRICH_MODES = ("replace", "new", "review-stale-updated", "history-missing")


def strict_platform_map_schema() -> dict[str, Any]:
    variants: list[dict[str, Any]] = [{"type": "null"}]
    value_schema = {
        "items": {"minLength": 1, "type": "string"},
        "minItems": 1,
        "type": "array",
    }
    for size in range(len(PATH_LOCATION_PLATFORMS) + 1):
        for platforms in itertools.combinations(PATH_LOCATION_PLATFORMS, size):
            variants.append(
                {
                    "additionalProperties": False,
                    "properties": {
                        platform: dict(value_schema)
                        for platform in platforms
                    },
                    "required": list(platforms),
                    "type": "object",
                }
            )
    return {"anyOf": variants}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review and apply curated project enrichment with Codex.")
    parser.add_argument("--mode", choices=ENRICH_MODES, required=True)
    parser.add_argument("--limit", type=int, default=0, help="Limit projects sent for review.")
    parser.add_argument("--batch-size", type=int, default=10, help="Projects to send to Codex per batch.")
    parser.add_argument("--force", action="store_true", help="Re-run Codex even when a valid batch checkpoint exists.")
    parser.add_argument(
        "--include-missing-curated-fields",
        action="store_true",
        help="Also select projects missing any field tracked in CURATED_FIELDS, including newly added curated fields.",
    )
    parser.add_argument(
        "--only-missing-curated-fields",
        action="store_true",
        help="Select only projects missing a field tracked in CURATED_FIELDS.",
    )
    parser.add_argument("--run-id", help="Resume or write artifacts under a specific enrichment run id.")
    parser.add_argument("--dry-run", action="store_true", help="Build cache/input artifacts without invoking Codex or editing YAML.")
    parser.add_argument("--commit-after-batch", action="store_true", help="Commit tracked YAML changes after each applied batch.")
    parser.add_argument("--provider", default="brew", choices=["brew"], help="Project provider to enrich.")
    parser.add_argument("--confidence-threshold", choices=["high", "medium", "low"], default="medium")
    parser.add_argument(
        "--phase",
        choices=ENRICH_PHASES,
        default="run",
        help="run invokes a backend and applies results; prepare only writes batch artifacts; apply only consumes existing outputs.",
    )
    parser.add_argument(
        "--backend",
        choices=ENRICH_BACKENDS,
        default=os.environ.get("AVDB_ENRICH_BACKEND", "codex-cli"),
        help="AI execution backend for --phase run. external requires codex-output.json files prepared by a controller.",
    )
    return parser.parse_args()


def read_codex_output(path: Path) -> object:
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else {}


def codex_timeout_seconds() -> float | None:
    raw = os.environ.get("AVDB_CODEX_TIMEOUT_SECONDS")
    if raw is None or raw.strip() == "":
        return DEFAULT_CODEX_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except ValueError as err:
        raise SystemExit("AVDB_CODEX_TIMEOUT_SECONDS must be a number") from err
    if timeout < 0:
        raise SystemExit("AVDB_CODEX_TIMEOUT_SECONDS must be non-negative")
    return timeout or None


def batch_status_path(batch_dir: Path) -> Path:
    return batch_dir / "status.json"


def write_batch_status(batch_dir: Path, status: dict[str, Any]) -> None:
    write_json(batch_status_path(batch_dir), status)


def write_output_schema(output_schema_path: Path, expected_ids: set[str]) -> None:
    schema = read_json(SCHEMA_PATH, default={})
    if not isinstance(schema, dict):
        raise SystemExit(f"{SCHEMA_PATH} must contain a JSON object")
    results_schema = schema.get("properties", {}).get("results", {})
    if not isinstance(results_schema, dict):
        raise SystemExit(f"{SCHEMA_PATH} must define properties.results")
    results_schema["minItems"] = len(expected_ids)
    results_schema["maxItems"] = len(expected_ids)
    item_schema = results_schema.get("items", {})
    if isinstance(item_schema, dict):
        properties = item_schema.get("properties", {})
        id_schema = properties.get("id", {}) if isinstance(properties, dict) else {}
        if isinstance(id_schema, dict):
            id_schema["enum"] = sorted(expected_ids)
        if isinstance(properties, dict):
            properties["config-file-location"] = strict_platform_map_schema()
            properties["credentials-file-location"] = strict_platform_map_schema()
    write_json(output_schema_path, schema)


def invoke_codex(prompt_path: Path, output_path: Path, output_schema_path: Path) -> bool:
    prompt = prompt_path.read_text(encoding="utf-8")
    command = [
        "codex",
        "--model",
        "gpt-5.6-sol",
        "--config",
        'model_reasoning_effort="medium"',
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
        str(output_schema_path),
        "-C",
        str(ROOT),
        "-o",
        str(output_path),
        prompt,
    ]
    timeout = codex_timeout_seconds()
    try:
        subprocess.run(command, cwd=ROOT, check=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as err:
        seconds = int(timeout) if timeout is not None else 0
        print(f"WARN: Codex enrichment timed out after {seconds}s for {prompt_path}", file=sys.stderr)
        return False
    return True


def batches(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    if size < 1:
        raise SystemExit("--batch-size must be at least 1")
    return [items[index : index + size] for index in range(0, len(items), size)]


def empty_summary(reviewed: int = 0) -> dict[str, int]:
    return {"reviewed": reviewed, "changed": 0, "rejected": 0, "skipped_low_confidence": 0, "no_op": 0}


def add_summary(target: dict[str, int], source: dict[str, int]) -> None:
    for key in ("changed", "rejected", "skipped_low_confidence", "no_op"):
        target[key] += source.get(key, 0)


def load_valid_checkpoint(
    normalized_path: Path,
    expected_ids: set[str],
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]] | None:
    if not normalized_path.exists():
        return None
    payload = read_json(normalized_path, default={})
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    if not isinstance(results, list):
        return None
    normalized, errors, invalid = validate_codex_payload_partial({"results": results}, expected_ids)
    if errors:
        return None
    return normalized, errors, invalid


def validate_and_write_batch(
    codex_output_path: Path,
    normalized_path: Path,
    expected_ids: set[str],
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    payload = read_codex_output(codex_output_path)
    normalized, errors, invalid = validate_codex_payload_partial(payload, expected_ids)
    error_summary = validation_error_summary(errors)
    write_json(
        normalized_path,
        {"results": normalized, "errors": errors, "error_summary": error_summary, "invalid": invalid},
    )
    return normalized, errors, invalid, error_summary


def print_summary(summary: dict[str, int]) -> None:
    print(
        "Reviewed: {reviewed}\nChanged: {changed}\nRejected: {rejected}\n"
        "Skipped low-confidence: {skipped_low_confidence}\nNo-op: {no_op}".format(**summary)
    )


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def run_manifest_path(run_dir: Path) -> Path:
    return run_dir / "controller-manifest.json"


def load_run_manifest(run_dir: Path) -> dict[str, Any]:
    manifest = read_json(run_manifest_path(run_dir), default={})
    if not isinstance(manifest, dict):
        raise SystemExit(f"{run_manifest_path(run_dir)} must contain a JSON object")
    return manifest


def prepare_run(
    args: argparse.Namespace,
    selected: list[dict[str, Any]],
    run_dir: Path,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    input_payload = review_input(selected)
    input_path = run_dir / "input.json"
    prompt = prompt_text(input_path, len(selected))
    write_run_artifacts(run_dir, input_payload, prompt)

    manifest: dict[str, Any] = {
        "schema": 1,
        "backend": args.backend,
        "batch_size": args.batch_size,
        "batches": [],
        "commit_after_batch": bool(args.commit_after_batch),
        "include_missing_curated_fields": bool(args.include_missing_curated_fields),
        "mode": args.mode,
        "phase": "prepare",
        "provider": args.provider,
        "run_dir": rel(run_dir),
        "run_id": run_dir.name,
        "selected_count": len(selected),
    }

    batch_root = run_dir / "batches"
    batch_root.mkdir(parents=True, exist_ok=True)
    for batch_index, batch_projects in enumerate(batches(selected, args.batch_size), start=1):
        batch_dir = batch_root / f"{batch_index:04d}"
        batch_dir.mkdir(parents=True, exist_ok=True)
        batch_input = review_input(batch_projects)
        batch_input_path = batch_dir / "input.json"
        batch_prompt = prompt_text(batch_input_path, len(batch_projects))
        write_run_artifacts(batch_dir, batch_input, batch_prompt)

        expected_ids = {str(item.get("id")) for item in batch_projects}
        output_schema_path = batch_dir / "output-schema.json"
        normalized_path = batch_dir / "normalized-output.json"
        codex_output_path = batch_dir / "codex-output.json"
        write_output_schema(output_schema_path, expected_ids)

        checkpoint = None if args.force else load_valid_checkpoint(normalized_path, expected_ids)
        status = "checkpointed" if checkpoint is not None else "pending"
        batch_record = {
            "apply_summary_path": rel(batch_dir / "apply-summary.json"),
            "batch": f"{batch_index:04d}",
            "batch_dir": rel(batch_dir),
            "codex_output_path": rel(codex_output_path),
            "expected_ids": sorted(expected_ids),
            "input_path": rel(batch_input_path),
            "normalized_output_path": rel(normalized_path),
            "output_schema_path": rel(output_schema_path),
            "prompt_path": rel(batch_dir / "prompt.md"),
            "status": status,
        }
        manifest["batches"].append(batch_record)
        write_batch_status(batch_dir, {"batch": batch_record["batch"], "status": status})
        print(f"PREPARED batch {batch_index:04d} {status} ids={len(expected_ids)}")

    write_json(run_manifest_path(run_dir), manifest)
    return manifest


def write_empty_dry_run_outputs(run_dir: Path, manifest: dict[str, Any], state: dict[str, Any], state_path: Path) -> None:
    summary = empty_summary(int(manifest.get("selected_count") or 0))
    for batch in manifest.get("batches", []):
        batch_dir = ROOT / str(batch["batch_dir"])
        batch_summary = empty_summary(len(batch.get("expected_ids") or []))
        write_json(batch_dir / "normalized-output.json", {"results": [], "errors": [], "error_summary": [], "invalid": []})
        write_json(batch_dir / "apply-summary.json", batch_summary)
        write_batch_status(batch_dir, {"batch": batch["batch"], "status": "dry-run"})
    write_json(run_dir / "normalized-output.json", {"results": [], "errors": [], "error_summary": [], "invalid": []})
    write_json(run_dir / "apply-summary.json", summary)
    write_json(state_path, state)
    print_summary(summary)


def invoke_codex_cli_for_pending_batches(args: argparse.Namespace, manifest: dict[str, Any]) -> None:
    for batch in manifest.get("batches", []):
        batch_dir = ROOT / str(batch["batch_dir"])
        expected_ids = set(batch.get("expected_ids") or [])
        normalized_path = ROOT / str(batch["normalized_output_path"])
        checkpoint = None if args.force else load_valid_checkpoint(normalized_path, expected_ids)
        if checkpoint is not None:
            write_batch_status(batch_dir, {"batch": batch["batch"], "status": "checkpointed"})
            print(f"SKIP batch {batch['batch']}; valid checkpoint exists")
            continue
        # A previous process may have exited after writing raw output but before
        # normalizing it. Recover valid work without paying for research again.
        raw_path = ROOT / str(batch["codex_output_path"])
        if raw_path.exists() and not args.force:
            try:
                validate_and_write_batch(raw_path, normalized_path, expected_ids)
            except json.JSONDecodeError:
                pass  # An interrupted JSON write must be researched again.
            if load_valid_checkpoint(normalized_path, expected_ids) is not None:
                write_batch_status(batch_dir, {"batch": batch["batch"], "status": "checkpointed"})
                continue
        completed = invoke_codex(
            ROOT / str(batch["prompt_path"]),
            ROOT / str(batch["codex_output_path"]),
            ROOT / str(batch["output_schema_path"]),
        )
        if not completed:
            write_batch_status(batch_dir, {"batch": batch["batch"], "status": "timed-out"})
            print(f"TIMEOUT batch {batch['batch']}; continuing with remaining batches", file=sys.stderr)
            continue
        if raw_path.exists():
            try:
                validate_and_write_batch(raw_path, normalized_path, expected_ids)
            except json.JSONDecodeError:
                pass  # Preserve the raw evidence; apply reports the malformed output.
        write_batch_status(batch_dir, {"batch": batch["batch"], "status": "codex-cli-completed"})
        print(f"CODEX-CLI batch {batch['batch']} completed")


def apply_prepared_batches(
    args: argparse.Namespace,
    manifest: dict[str, Any],
    projects: list[dict[str, Any]],
    state: dict[str, Any],
    state_path: Path,
    run_dir: Path,
    today: str,
) -> int:
    summary = empty_summary(int(manifest.get("selected_count") or 0))
    all_normalized: list[dict[str, Any]] = []
    all_errors: list[str] = []
    all_invalid: list[dict[str, Any]] = []
    failed_batches = 0
    projects_by_id = {str(record.get("id")): record for record in projects}

    for batch in manifest.get("batches", []):
        batch_dir = ROOT / str(batch["batch_dir"])
        batch_name = str(batch["batch"])
        expected_ids = set(str(item) for item in batch.get("expected_ids") or [])
        batch_summary = empty_summary(len(expected_ids))
        normalized_path = ROOT / str(batch["normalized_output_path"])
        codex_output_path = ROOT / str(batch["codex_output_path"])
        batch_status = read_json(batch_status_path(batch_dir), default={})
        timed_out = isinstance(batch_status, dict) and batch_status.get("status") == "timed-out"

        checkpoint = None if args.force else load_valid_checkpoint(normalized_path, expected_ids)
        if checkpoint is not None:
            normalized, errors, invalid = checkpoint
            error_summary = []
            write_batch_status(batch_dir, {"batch": batch_name, "status": "checkpointed"})
            print(f"SKIP batch {batch_name}; valid checkpoint exists")
        elif codex_output_path.exists() and not timed_out:
            normalized, errors, invalid, error_summary = validate_and_write_batch(
                codex_output_path,
                normalized_path,
                expected_ids,
            )
            write_batch_status(batch_dir, {"batch": batch_name, "status": "validated", "errors": len(errors)})
            print(f"VALIDATED batch {batch_name} valid={len(normalized)} errors={len(errors)}")
        else:
            error = (
                f"codex timed out for batch {batch_name}"
                if timed_out
                else f"missing codex output for batch {batch_name}: {codex_output_path}"
            )
            errors = [error]
            normalized = []
            invalid = []
            error_summary = validation_error_summary(errors)
            write_json(
                normalized_path,
                {"results": [], "errors": errors, "error_summary": error_summary, "invalid": []},
            )
            status = "timed-out" if timed_out else "missing-output"
            write_batch_status(batch_dir, {"batch": batch_name, "status": status, "errors": len(errors)})
            print(f"Batch {batch_name} {status}; see {codex_output_path}", file=sys.stderr)

        batch_summary["rejected"] += validation_rejection_count(expected_ids, normalized)
        all_normalized.extend(normalized)
        all_errors.extend(errors)
        all_invalid.extend(invalid)

        if not normalized:
            if not timed_out:
                failed_batches += 1
            write_json(batch_dir / "apply-summary.json", batch_summary)
            write_json(state_path, state)
            outcome = "timed out" if timed_out else "failed validation"
            print(f"Batch {batch_name} {outcome}; see {normalized_path}", file=sys.stderr)
            if error_summary:
                top_error = error_summary[0]
                print(f"Top validation error: {top_error['error']} ({top_error['count']})", file=sys.stderr)
            summary["rejected"] += batch_summary["rejected"]
            continue

        apply_summary = apply_results(
            projects_by_id,
            state,
            normalized,
            confidence_threshold=args.confidence_threshold,
            today=today,
            dry_run=args.dry_run,
            preserve_existing_fields=args.include_missing_curated_fields,
        )
        add_summary(batch_summary, apply_summary)
        add_summary(summary, batch_summary)
        if not args.dry_run:
            render_combined_tree()
        write_json(batch_dir / "apply-summary.json", batch_summary)
        write_json(state_path, state)
        write_batch_status(batch_dir, {"batch": batch_name, "status": "applied", "errors": len(errors)})
        if args.commit_after_batch and not args.dry_run:
            commit = git_commit_if_changed(
                f"nightly: enrich {args.mode} batch {batch_name}",
                ["agents", "combined"],
            )
            print(f"COMMIT batch {batch_name} {commit}" if commit else f"SKIP commit batch {batch_name}; no tracked changes")

        if errors:
            print(
                f"Batch {batch_name} partially failed validation; applied {len(normalized)} valid results; "
                f"see {normalized_path}",
                file=sys.stderr,
            )
            if error_summary:
                top_error = error_summary[0]
                print(f"Top validation error: {top_error['error']} ({top_error['count']})", file=sys.stderr)

    write_json(
        run_dir / "normalized-output.json",
        {
            "results": all_normalized,
            "errors": all_errors,
            "error_summary": validation_error_summary(all_errors),
            "invalid": all_invalid,
        },
    )
    write_json(run_dir / "apply-summary.json", summary)
    write_json(state_path, state)
    print_summary(summary)
    return 1 if failed_batches else 0


def selected_projects_for_args(args: argparse.Namespace, state: dict[str, Any], today: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    projects = load_projects(args.provider)
    update_observed_state(state, projects, today)
    selection_mode = "new" if args.mode == "history-missing" else args.mode
    include_missing_curated_fields = (
        args.include_missing_curated_fields
        or args.only_missing_curated_fields
        or args.mode == "history-missing"
    )
    selected = select_projects(
        projects,
        state,
        mode=selection_mode,
        today=today,
        include_missing_curated_fields=include_missing_curated_fields,
    )
    if args.only_missing_curated_fields:
        selected = [record for record in selected if has_missing_curated_fields(record)]
    selected.sort(
        key=lambda record: (
            not any(
                (state.get(str(record.get("id") or "")) or {}).get(field) == today
                for field in ("first_observed", "last_source_change")
            ),
            str(record.get("id") or ""),
        )
    )
    if args.limit:
        selected = selected[: args.limit]
    return projects, selected


def main() -> int:
    args = parse_args()
    ensure_root()
    today = today_iso()
    state_path = CACHE_DIR / "enrichment" / "state.json"
    state = read_json(state_path, default={})
    if not isinstance(state, dict):
        raise SystemExit(f"{state_path} must contain a JSON object")

    if args.phase == "apply" and not args.run_id:
        raise SystemExit("--run-id is required with --phase apply")

    current_run = args.run_id or run_id()
    run_dir = CACHE_DIR / "enrichment" / "runs" / current_run

    projects, selected = selected_projects_for_args(args, state, today)

    if args.phase in {"prepare", "run"}:
        if args.run_id and run_manifest_path(run_dir).exists() and not args.force:
            manifest = load_run_manifest(run_dir)
        else:
            manifest = prepare_run(args, selected, run_dir)
        write_json(state_path, state)
    else:
        manifest = load_run_manifest(run_dir)

    if args.dry_run and args.phase != "apply":
        write_empty_dry_run_outputs(run_dir, manifest, state, state_path)
        return 0

    if args.phase == "prepare":
        print(f"Prepared {manifest['selected_count']} projects in {len(manifest['batches'])} batches under {rel(run_dir)}")
        return 0

    if args.phase == "run" and args.backend == "codex-cli":
        invoke_codex_cli_for_pending_batches(args, manifest)
    elif args.phase == "run" and args.backend == "external":
        print("Using external backend; applying existing codex-output.json files")

    return apply_prepared_batches(args, manifest, projects, state, state_path, run_dir, today)


if __name__ == "__main__":
    raise SystemExit(main())
