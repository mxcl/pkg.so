Commit at sensible intervals.

Build and test binaries locally, never on Atlas: it does not have enough resources
for compilation. Cross-compile the production origin here with
`cargo zigbuild --release --target aarch64-unknown-linux-gnu -p av-web`, then copy
`target/aarch64-unknown-linux-gnu/release/av-web` to Atlas. Deploy into a new release
directory, retain the previous release for rollback, switch the `current` symlink,
restart `automic-vault-web`, and verify health. Do not install a compiler on Atlas
or run the legacy `scripts/deploy-atlas.sh`, which compiles there. Atlas's scheduled
metadata refresh and SQLite generation are separate from binary compilation.

Published YAML is for rarely changing curation data. Never add fields that change
regularly, including package versions, source archive or download URLs, checksums,
download counts, and refresh timestamps. Runtime artifacts such as JSON and SQLite
must obtain volatile package metadata directly from the relevant authoritative source.
