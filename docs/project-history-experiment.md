# Frozen project-history SEO experiment

Max approved using the top 25 ranked, deduplicated projects that already have history. Frozen on 2026-09-30. Exactly 25 English project routes are active locally; no history generation occurred.

## Selection and evidence

Approved by Max: take the first 25 underlying open-source projects WITH existing cited history, ordered by existing discovery source popularityRank ascending, then discovery package ID ascending (brew:NAME, brew:cask:NAME, npm:NAME). Deduplicate evidenced repository identities and ecosystem/version representations. No statistical normalization, no generated history, no filtering solely on unsupported repository/license fields.

The policy comes from `scripts/update-discover-feed::candidate_pool`: source `popularityRank` ascending, then discovery package ID. The catalog uses rank plus display-name ties. npm uses monthly downloads; Homebrew uses annual installs. No new normalization or rank mixing formula was introduced. The feed already combines those source ranks.

Use repository identities to collapse versions and ecosystem representations; public npm registry repository metadata connects npm semver/esbuild/TypeScript/json5/Vite/ESLint to existing Homebrew histories. Do not merge wrappers by name alone. The cross-ecosystem snapshot supplies install evidence and existing local links; normalized-name external matches are not universal project-identity proof.

The selected ranks reach gettext at rank 20; FFmpeg is selected at rank 16. curl (86) and jq (74) remain below this cutoff. No curl/jq history routes are created. Projects without existing cited history are skipped only under Max’s approved adjustment.

Python and PCRE2 use their existing legacy narrative lists verbatim, with URLs from `agents/*` history provenance. They do not gain invented timeline/adoption sections. All other selected projects retain their existing section records, timelines, adoption, usage, related-project text and sources. Source strings containing descriptions retain their visible wording while extracted URL evidence remains clickable and valid in JSON-LD.

Existing history is frozen in `data/project-history-cohort.json` with source paths, content hashes, repository aliases, ecosystem keys, rank evidence and artifact timestamps/hashes. This project-level snapshot is independent of the install-page model. ICU has no installable catalog page in this snapshot; its cited project history still renders without inventing a package/install route. Package versions/install commands remain in SQLite. The frozen material must be reviewed if intentionally refreshed during the experiment.

## Frozen cohort and URLs

| Position | Project | Source rank | History URL |
| --- | --- | --- | --- |
| 1 | OpenSSL | 1 | https://pkg.so/openssl/history/ |
| 2 | semver | 1 | https://pkg.so/semver/history/ |
| 3 | Codex | 2 | https://pkg.so/codex/history/ |
| 4 | SQLite | 3 | https://pkg.so/sqlite/history/ |
| 5 | XZ Utils | 4 | https://pkg.so/xz/history/ |
| 6 | Python | 5 | https://pkg.so/python/history/ |
| 7 | esbuild | 5 | https://pkg.so/esbuild/history/ |
| 8 | Android SDK Platform-Tools | 6 | https://pkg.so/android-platform-tools/history/ |
| 9 | Node.js | 6 | https://pkg.so/node/history/ |
| 10 | TypeScript | 6 | https://pkg.so/typescript/history/ |
| 11 | GitHub CLI | 7 | https://pkg.so/gh/history/ |
| 12 | Zstandard | 8 | https://pkg.so/zstd/history/ |
| 13 | GLib | 10 | https://pkg.so/glib/history/ |
| 14 | JSON5 | 10 | https://pkg.so/json5/history/ |
| 15 | PCRE2 | 11 | https://pkg.so/pcre2/history/ |
| 16 | AWS CLI | 12 | https://pkg.so/awscli/history/ |
| 17 | ICU | 13 | https://pkg.so/icu/history/ |
| 18 | LZ4 | 14 | https://pkg.so/lz4/history/ |
| 19 | Vite | 14 | https://pkg.so/vite/history/ |
| 20 | HarfBuzz | 15 | https://pkg.so/harfbuzz/history/ |
| 21 | FFmpeg | 16 | https://pkg.so/ffmpeg/history/ |
| 22 | uv | 17 | https://pkg.so/uv/history/ |
| 23 | pkgconf | 19 | https://pkg.so/pkgconf/history/ |
| 24 | ESLint | 19 | https://pkg.so/eslint/history/ |
| 25 | gettext | 20 | https://pkg.so/gettext/history/ |

## Measurement over 4–8 weeks

Use `project-history-measurement.csv` for per-project page and query filters. Export Search Console Performance data manually; keep property, search type, country/device filters and complete-week windows consistent. Set deployment day as day zero and review weeks 4 and 8. No baseline metrics were collected or fabricated.

Group history pages separately from matching install pages, then compare all project queries with history/origin/timeline intent. Track impressions, clicks, CTR and average position. The CSV is RE2-compatible; review broad or ambiguous terms manually, especially gh, adb and node. Do not present before/after changes as causal proof without a control. Include localized package URLs separately if relevant; the history pages have no translated variants.

Combined history-page filter:

```text
^(?:https://pkg\.so/openssl/history/|https://pkg\.so/semver/history/|https://pkg\.so/codex/history/|https://pkg\.so/sqlite/history/|https://pkg\.so/xz/history/|https://pkg\.so/python/history/|https://pkg\.so/esbuild/history/|https://pkg\.so/android\-platform\-tools/history/|https://pkg\.so/node/history/|https://pkg\.so/typescript/history/|https://pkg\.so/gh/history/|https://pkg\.so/zstd/history/|https://pkg\.so/glib/history/|https://pkg\.so/json5/history/|https://pkg\.so/pcre2/history/|https://pkg\.so/awscli/history/|https://pkg\.so/icu/history/|https://pkg\.so/lz4/history/|https://pkg\.so/vite/history/|https://pkg\.so/harfbuzz/history/|https://pkg\.so/ffmpeg/history/|https://pkg\.so/uv/history/|https://pkg\.so/pkgconf/history/|https://pkg\.so/eslint/history/|https://pkg\.so/gettext/history/)$
```

No Search Console changes, monitors, schedules or extra analytics services were added. Existing site analytics/advertising behavior is retained.

## Validation and deployment

Run `python3 scripts/project_history.py`, the new Python tests, `cargo test --workspace`, and `cargo build --workspace`. For snapshot tests set `PKG_HISTORY_TEST_DB` to the absolute existing SQLite path. Run `scripts/check-project-history.py --base-url http://127.0.0.1:3004 --db cache/pkg.sqlite`; use `--baseline-url` to compare original install pages.

After deployment is authorized, cross-compile locally with `cargo zigbuild --release --target aarch64-unknown-linux-gnu -p av-web`. The frozen cohort/history snapshot is embedded in the binary. Include the source validation change in the existing deployed checkout. Retain the previous release, copy the new binary into a new release directory, switch the existing current symlink, restart the origin, and verify health plus the 25-history sitemap and mapped package routes. Restore the old symlink/restart on failure. Do not run legacy deploy-atlas.sh or compile on Atlas. Normal five-minute cache revalidation applies. No push, merge or deployment occurred in this task.

Readable artifact source: `/apps/pkg.so/cache/` on Atlas. Access to `/var/lib/automic-vault-web/pkg.sqlite` was denied and not bypassed. Local SQLite was rebuilt from existing readable metadata and YAML only; no paid generation or history regeneration was run.
