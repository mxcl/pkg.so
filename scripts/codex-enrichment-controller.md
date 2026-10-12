# Codex Enrichment Controller

Use this workflow from a Codex-hosted automation when curated enrichment needs AI research. Do not call `codex exec` from inside the maintenance scripts for this path.

To inspect the next unclaimed prepared run without taking ownership, use:

```sh
python3 scripts/enrichment-controller.py next-run --json
```

It emits the oldest unresolved run plus the exact `apply_command` for that run.

Workers must atomically claim work instead:

```sh
python3 scripts/enrichment-controller.py claim-next --owner codex-nightly-monolith --json
```

`claim-next` records a 12-hour lease in the run directory and emits the claimed run plus its exact
`apply_command`. Active claims are hidden from both `claim-next` and `next-run`, so overlapping or
manually retried workers cannot select the same run. An interrupted claim becomes eligible again
after its lease expires.

1. Prepare the run.

For nightly newly observed projects:

```sh
python3 scripts/enrich-projects.py --mode new --limit 50 --batch-size 5 --backend external --phase prepare --run-id "$(date -u +%Y%m%dT%H%M%SZ)"
```

For nightly stale or upstream-updated projects:

```sh
python3 scripts/enrich-projects.py --mode review-stale-updated --limit 50 --batch-size 5 --backend external --phase prepare --run-id "$(date -u +%Y%m%dT%H%M%SZ)"
```

For nightly new and missing curated fields:

```sh
python3 scripts/enrich-projects.py --mode new --include-missing-curated-fields --limit 250 --batch-size 5 --backend external --phase prepare --commit-after-batch --run-id "$(date -u +%Y%m%dT%H%M%SZ)"
```

2. Claim the oldest run and read `cache/enrichment/runs/<run-id>/controller-manifest.json`.

3. For each batch with `"status": "pending"`, spawn one sub-agent. Give it only:

- `prompt_path`
- `input_path`
- `output_schema_path`
- `codex_output_path`

The sub-agent must read the prompt and input, research official sources only, and write JSON matching `output_schema_path` to `codex_output_path`. It must not edit repo files.

Each research batch also curates `related-packages`: up to six ranked alternatives or
concrete workflow companions, with a useful reason and official evidence URLs for each.
Resolve exact target IDs against `combined/*.yml`. Shared tags, similar names, and
build dependencies are not sufficient evidence. Return `[]` when no recommendation
is strong enough. The missing-fields run backfills this field; an explicit empty
array counts as reviewed. Older prepared outputs remain applicable and leave the
field missing for a later pass. Published pages use reviewed recommendations in
place of automatic related-package suggestions and omit unavailable targets.

4. Apply completed outputs.

You can use the helper's emitted `apply_command`, or run the equivalent command manually.

Use the same mode, limits, and batch size as the prepare command. For nightly newly observed projects:

```sh
python3 scripts/enrich-projects.py --mode new --limit 50 --batch-size 5 --backend external --phase apply --run-id "<run-id>" --commit-after-batch
```

For stale review, use `--mode review-stale-updated`. For nightly new and missing curated fields, include `--include-missing-curated-fields --limit 250 --batch-size 5 --commit-after-batch`.

5. Apply preserves valid results and processes the remaining batches, then exits non-zero if any timeout, validation error, or rejected project remains. The maintenance supervisor treats this as a failed stage and retries or escalates it. Leave completed batch commits in place and rerun only the failed batches with the same `--run-id` after their `codex-output.json` files are fixed.

6. After a successful apply, call `claim-next` again and repeat until it exits non-zero. Do not
delegate a run returned by the inspection-only `next-run` command.

Package enrichment conflict repair is out of scope for this controller.
