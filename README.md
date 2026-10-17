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

Build and test binaries locally. Atlas does not have enough resources for
compilation. Cross-compile the origin on the development machine:

```sh
$ cargo test --workspace
$ cargo zigbuild --release --target aarch64-unknown-linux-gnu -p av-web
```

Copy `target/aarch64-unknown-linux-gnu/release/av-web` to a new release directory
under `/apps/automic-vault-web/releases/` on Atlas. Retain the previous release,
switch `/apps/automic-vault-web/current`, restart `automic-vault-web`, and check
`http://127.0.0.1:3004/healthz`. Restore the previous symlink and restart if the
health check fails. Verify the public site after deployment.

Do not run the legacy `scripts/deploy-atlas.sh`: it compiles on Atlas. Scheduled
metadata refresh and SQLite generation on Atlas remain separate from binary
compilation.

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

Discover editorials are published as the [pkg.so blog](https://pkg.so/blog/),
with permanent article URLs and a blog sitemap linked from the main sitemap.
The web origin reads the current feed release and its linked v2 archive, so the
nightly feed publication also updates the blog without a rebuild.

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

## New and trending

`/new-and-trending/` and `/new-and-trending.json` publish Homebrew discovery
alongside the catalog. The existing `/new.json` keeps its seven-day
added-or-updated semantics. Discovery is English-only; package pages continue to
use the catalog's existing locale routes. The Independent taps section ranks
qualifying third-party packages separately using the same criteria, so the global
top 60 cannot crowd them out.

The nightly discovery step reads 30/90/365-day cask-install and formula
install-on-request analytics, including third-party taps. It compares the latest
30 days with the preceding 60 days, requires at least 30 recent events and ten
additional monthly events, and ranks by
`log2((recent + 10) / (prior + 10)) * log1p(recent - prior)`, requiring a smoothed
ratio of at least 1.2. Reports must have aligned dates and be at most three days
old. Counts are recorded installation events, not unique users. These thresholds
are selection policy, not a claim of statistical significance.

First observation and catalog admission are separate from upstream release age.
The first successful report set per provider establishes a baseline and does not
create a "new" cohort. Later admissions remain recently discovered for 30 days.
Editorial research still needs primary evidence before calling a project newly
released. Dedicated candidate slots let tap discoveries reach editorial research.

Run a bounded refresh locally with:

```sh
uv run --python 3.12 scripts/update-homebrew-discovery.py --limit 20
uv run --python 3.12 scripts/update-homebrew-discovery.py \
  --limit 1 --package cask:mattt/tap/imcp
```

Persistent state defaults to `state/homebrew-discovery.json`, outside disposable
`cache/`; set `PKGDB_DISCOVERY_STATE` to an absolute persistent path when operating
multiple checkouts. Back up this file with the service's persistent data. Never
remove it to clear an HTTP cache: doing so loses first-seen dates. The adjacent
`http/` directory is disposable. State writes are atomic and refreshes take an
exclusive lock. A corrupt state fails closed instead of silently resetting dates.

Each refresh resolves at most 20 packages by default, reserving a third of the
batch for rechecking admitted packages. Previously unresolved packages retry
after three days; admitted packages after seven. Candidates require ten events
in 30 days or 100 in a year. Package and tap identity are retained throughout.
Packages that fall out of analytics remain in the catalog. Failed source checks
preserve their last verified metadata but remove them from discovery shelves.
Metadata older than 14 days is also excluded from shelves. Old published shelves
expire at request time if maintenance stops.

The resolver reads public GitHub tap trees and pins definition URLs to a commit;
it never runs Ruby, installs packages, or downloads their binaries. Supported
literal definitions include app/binary casks and formulas with literal
`bin.install` artifacts. Dynamic definitions, ambiguous paths, and disabled or
deprecated packages are not admitted. Failure reasons and source-report errors
are recorded in state; the command prints status counts. GitHub rate limiting
leaves candidates available for later retries. Coverage is intentionally partial,
and unsupported syntax should be expanded using real fixtures rather than eval.

Stable tap curation enters the normal deterministic/agent/human-override stages.
Versions, source archives, checksums, telemetry, and verification timestamps stay
in runtime artifacts. Qualified package routes include an identity hash, and
shelves collapse confirmed shared upstream repository identities without merging
installation identities. Package pages, search, public export, and discovery
shelves are built from the same authority. The origin serves shelves from the
published SQLite snapshot; no network requests or ranking work happen at request
time. Build/test binaries locally, and use the release-directory deployment
procedure above when deploying the origin.
