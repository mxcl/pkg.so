# Package Metadata Database

The metadata and live SQLite origin behind the pkg.so package catalog.

> [!IMPORTANT]
> Installable CLIs and macOS apps only. Library-only and transitive dependencies don’t belong here.

## Build

```sh
$ scripts/build.py --refresh
$ scripts/build-db.py --refresh --npm-full-scan-parts=7
$ scripts/generate-pkg-sqlite.py
```

Downloaded and intermediate data stays in `cache/`. The committed YAML stages
are:

- `deterministic/`: source-backed generator output
- `agents/`: schema-validated Codex enrichment
- `human-override/`: hand-authored corrections
- `combined/`: the final merged metadata

Precedence is deterministic < agents < human override. Package-page data,
search documents, hubs, and generation metadata are compiled into
`cache/pkg.sqlite`. The aggregate export is generated at
`cache/automic-vault/db.json` and served publicly at `https://pkg.so/db.json`.
HTML, CSS, JavaScript, and sitemaps are served by the Rust origin.

## Run the pkg.so origin

```sh
$ AV_WEB_DB_PATH=cache/pkg.sqlite cargo run --release -p av-web
av-web listening on 127.0.0.1:3004
```

`AV_WEB_DB_PATH` selects the SQLite file. The production service defaults to
`/var/lib/automic-vault-web/pkg.sqlite`; origin-header settings remain in
`/etc/automic-vault-web.env` on Atlas.

## Atlas

Deploy code and systemd units directly from the Atlas checkout. The script
builds the current working tree; it does not SSH, fetch, or require a commit:

```sh
$ cd /apps/pkg.so
$ scripts/deploy-atlas.sh
```

Set `PKGDB_REBUILD_SQLITE=true` when renderer, stylesheet, crawler, or source
inputs changed. The deploy generates and validates a new artifact on Atlas and
coordinates its atomic swap with the matching origin binary. The flag form is
preferred; the environment variable remains supported for compatibility:

```sh
$ scripts/deploy-atlas.sh --rebuild-sqlite
```

`pkgdb-maintenance.timer` refreshes metadata nightly, runs bounded Codex
enrichment, generates and validates `pkg.sqlite.next`, generates `db.json` as
the final daily job step, then atomically replaces both live files. `av-web`
opens the artifacts per request, so successful swaps need no restart. Failed
builds leave the previous files serving.

Inspect it with:

```sh
$ systemctl status pkgdb-maintenance.timer automic-vault-web.service
$ journalctl -u pkgdb-maintenance.service -n 100
```

Atlas has no GitHub credentials. From Pangolin, retrieve Atlas metadata commits
and push them with the external synchronization script:

```sh
$ scripts/sync-atlas.sh
```

`pkg.so` uses a dedicated CloudFront distribution in front of the same Atlas
origin at `origin.pkg.so`. Browser and edge responses are cached for five
minutes, then revalidated with `ETag` or `Last-Modified` so unchanged content
does not need to be retransmitted. CloudFront credentials stay off Atlas;
create or update the distribution from Pangolin with:

```sh
$ AV_WEB_ORIGIN_SECRET=... scripts/deploy-pkg-cloudfront.sh --prepare-only
$ AV_WEB_ORIGIN_SECRET=... scripts/deploy-pkg-cloudfront.sh
```

The deploy requests a DNS-validated ACM certificate in `us-east-1` when one is
not already present. It deploys on the generated `cloudfront.net` hostname
until that certificate is issued, then attaches the `pkg.so` alias on the next
run. The script reports the required ACM CNAME but does not change DNS.

The existing `atomicvault.com/pkg/` CloudFront behaviors stay live during the
migration. Their redirects into the flattened `https://pkg.so/...` catalog are
managed separately in `../av.www`.

## Discover feed

The canonical Discover feed is committed at `www/feed/` and served at
`https://pkg.so/discover/feed/`. Run `scripts/update-discover-feed` to refresh
it; the script validates the rolling v1 snapshot and append-only v2 archive,
then performs its reentrant research handoff when needed. Use
`scripts/update-discover-feed --self-test` before changing the generator and
`--check` before publishing. New v2 archive links use pkg.so; links embedded in
the frozen former `mxcl.dev` archive remain unchanged for historical traversal.

## Checks

```sh
$ python3 -m unittest discover -s tests
$ cargo test --workspace
$ scripts/generate-pkg-sqlite.py --check
```

Raw Codex outputs remain under ignored `cache/enrichment/` paths for resumable
or manual controller runs. See `scripts/codex-enrichment-controller.md` when
using that flow instead of Atlas’s direct CLI backend.

Maintenance runs preserve reusable downloads, indexes, build state, staged
outputs, and every unresolved enrichment run. They remove abandoned hidden
atomic-write files after 24 hours and retain the three newest applied
enrichment runs for diagnostics. Set `AVDB_ENRICHMENT_COMPLETED_RUNS_TO_KEEP`
to change that retention count. Automation logs are capped at 5 MiB; override
that limit with `AVDB_AUTOMATION_MAX_LOG_BYTES`.

## Supervised nightly maintenance

The timer invokes `scripts/maintenance-supervisor.py`, which starts GPT-5.6 Sol
at medium reasoning with `scripts/maintenance-master.md`. The supervisor may
repair scoped pipeline problems, test and commit fixes, and retry the failed
stage. It independently reconciles generated `deterministic/` and `combined/`
changes against authoritative inputs after saving recovery copies, then validates
and commits the result. Unknown authorship of generated output alone does not
require escalation. It preserves hand-authored curation and unrelated code edits,
and cannot bypass checks.

The model repeatedly calls `python3 scripts/maintenance-step.py step`. Each call
runs one stage and returns JSON (`running`, `needs_agent`, `failed`, `complete`).
A `needs_agent` response names the research prompt; the supervisor writes the
requested response and calls the script again. Successful stages persist under
`cache/maintenance/current.json`; interrupted runs resume, including across days.
Enrichment uses a stable run ID and reuses validated research. Logs are bounded
to 2 MiB each. Runtime state and logs are ignored, not published YAML.

The worker locks out overlapping stages. The outer launcher locks out duplicate
supervisors, retries failed Codex processes up to three times (60/120 seconds),
and limits total agent runtime to ten hours. systemd has an independent eleven
hour limit and kills the whole service process tree. GPT-5.6 Sol remains explicit;
there is no automatic model substitution. Overrides for troubleshooting are
`PKG_MAINTENANCE_ATTEMPTS`, `PKG_MAINTENANCE_TIMEOUT` (seconds), and
`PKG_MAINTENANCE_STAGE_TIMEOUT` (seconds; default six hours).

A successful model exit alone is insufficient. The launcher independently runs
`maintenance-step.py verify`: both live data files must match this run's hashes,
SQLite must pass integrity checking, all repository feed files must match their
published copies, the origin must answer health checks, and tracked changes must
be committed. Publication records candidate hashes before replacing either file,
so an interruption between the two atomic renames can resume safely. The two
files are not a single transactional swap.

Inspect the current run with:

```sh
python3 scripts/maintenance-step.py status
journalctl -u pkgdb-maintenance.service -n 100
```

### Human notification

Configure `/etc/pkgdb-maintenance-notify.json` outside the repository, owned by
root and readable by the maintenance user:

```json
{
  "region": "us-east-2",
  "sender": "VERIFIED_SENDER@example.com",
  "recipient": "OPERATOR@example.com"
}
```

The sender must be verified in that SES region; if the account is in the SES
sandbox, the recipient must also be verified. The instance role needs
`ses:SendEmail` on the sender's SES identity ARN. Restrict that grant with
`ses:FromAddress` and `ses:Recipients` conditions matching the configuration.
Do not place AWS access keys in this file; use the instance role.

`maintenance-notify.py` is a narrow fixed-recipient interface: the model supplies
only a kind and concise message, not an address. It sends one failure email per
open incident, retries undelivered notifications on subsequent calls, and sends
one recovery message after verified success. It never attaches logs. Check the
local outbox at `cache/maintenance/notification.json` for SES acceptance or errors.
SES acceptance is not proof of inbox delivery. Replies are not ingested; respond
through the Codex task. Routine successful runs do not send email.

After configuring SES, verify with an actual message:

```sh
python3 scripts/maintenance-notify.py test --message 'pkg.so maintenance alert delivery test'
```

The launcher can notify even if Codex cannot start. `ExecStopPost` supplies an
independent alert path for service timeout/crash. Missing config or denied SES
permissions produce `NOTIFICATION_UNDELIVERED` and a persisted local outbox;
they are never reported as successful delivery. This cannot alert through SES
when the instance itself, networking, or SES is unavailable; external host
monitoring remains separate.
