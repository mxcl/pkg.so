import csv
import datetime as dt
import io
import json
import tarfile
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

from scripts.bootstrap.lib import crates as crates_index
from tests.test_pkg_page_rendering import pkg_pages


def csv_bytes(fieldnames, rows):
    handle = io.StringIO()
    writer = csv.DictWriter(handle, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return handle.getvalue().encode("utf-8")


def add_tar_member(archive, name, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


def crate_archive_bytes(files):
    handle = io.BytesIO()
    with tarfile.open(fileobj=handle, mode="w:gz") as archive:
        for name, data in files.items():
            add_tar_member(archive, name, data)
    return handle.getvalue()


def write_fixture_dump(path: Path, *, irrelevant_crates: int = 0, irrelevant_versions: int = 0) -> None:
    today = dt.date.today().isoformat()
    old_day = (dt.date.today() - dt.timedelta(days=120)).isoformat()
    files = {
        "data/crates.csv": csv_bytes(
            [
                "id",
                "name",
                "updated_at",
                "created_at",
                "description",
                "homepage",
                "documentation",
                "readme",
                "repository",
                "max_upload_size",
                "max_features",
                "trustpub_only",
            ],
            [
                {
                    "id": "1",
                    "name": "ripgrep",
                    "updated_at": "2026-06-01T00:00:00Z",
                    "created_at": "2020-01-01T00:00:00Z",
                    "description": "fast recursive search",
                    "homepage": "https://github.com/BurntSushi/ripgrep",
                    "documentation": "https://docs.rs/ripgrep",
                    "repository": "https://github.com/BurntSushi/ripgrep",
                },
                {"id": "2", "name": "serde", "description": "serialization framework"},
                {"id": "3", "name": "oldcli", "description": "old command"},
                {"id": "4", "name": "yankedcli", "description": "gone"},
                *(
                    {
                        "id": str(index + 100),
                        "name": f"irrelevant-{index}",
                        "description": "x" * 500,
                    }
                    for index in range(irrelevant_crates)
                ),
            ],
        ),
        "data/crate_downloads.csv": csv_bytes(
            ["crate_id", "downloads"],
            [
                {"crate_id": "1", "downloads": "1000"},
                {"crate_id": "2", "downloads": "900"},
                {"crate_id": "3", "downloads": "800"},
                {"crate_id": "4", "downloads": "700"},
            ],
        ),
        "data/default_versions.csv": csv_bytes(
            ["crate_id", "version_id", "num_versions"],
            [
                {"crate_id": "1", "version_id": "10", "num_versions": "3"},
                {"crate_id": "2", "version_id": "20", "num_versions": "4"},
                {"crate_id": "3", "version_id": "30", "num_versions": "1"},
                {"crate_id": "4", "version_id": "40", "num_versions": "1"},
                *(
                    {
                        "crate_id": str(index + 100),
                        "version_id": str(index + 100),
                        "num_versions": "1",
                    }
                    for index in range(irrelevant_versions)
                ),
            ],
        ),
        "data/versions.csv": csv_bytes(
            [
                "id",
                "crate_id",
                "num",
                "updated_at",
                "created_at",
                "downloads",
                "features",
                "yanked",
                "license",
                "crate_size",
                "published_by",
                "checksum",
                "links",
                "rust_version",
                "has_lib",
                "bin_names",
                "edition",
                "description",
                "homepage",
                "documentation",
                "repository",
            ],
            [
                {
                    "id": "10",
                    "crate_id": "1",
                    "num": "15.1.0",
                    "updated_at": "2026-06-01T00:00:00Z",
                    "created_at": "2026-06-01T00:00:00Z",
                    "yanked": "f",
                    "license": "MIT",
                    "checksum": "abc",
                    "rust_version": "1.85",
                    "has_lib": "f",
                    "bin_names": "{rg}",
                    "edition": "2024",
                    "description": "ripgrep command",
                },
                {"id": "20", "crate_id": "2", "num": "1.0.0", "yanked": "f", "bin_names": "{}"},
                {"id": "30", "crate_id": "3", "num": "1.0.0", "yanked": "f", "bin_names": "{oldcli}"},
                {"id": "40", "crate_id": "4", "num": "1.0.0", "yanked": "t", "bin_names": "{yankedcli}"},
                *(
                    {
                        "id": str(index + 100),
                        "crate_id": str(index + 100),
                        "num": "1.0.0",
                        "yanked": "f",
                        "bin_names": f"{{irrelevant-{index}}}",
                        "description": "x" * 500,
                    }
                    for index in range(irrelevant_versions)
                ),
            ],
        ),
        "data/version_downloads.csv": csv_bytes(
            ["version_id", "downloads", "date"],
            [
                {"version_id": "10", "downloads": "75", "date": today},
                {"version_id": "30", "downloads": "1000", "date": old_day},
                {"version_id": "40", "downloads": "1000", "date": today},
            ],
        ),
    }
    with tarfile.open(path, "w:gz") as archive:
        for name, data in files.items():
            add_tar_member(archive, name, data)


def write_internal_binary_fixture_dump(path: Path) -> None:
    today = dt.date.today().isoformat()
    files = {
        "data/crates.csv": csv_bytes(
            [
                "id",
                "name",
                "updated_at",
                "created_at",
                "description",
                "homepage",
                "documentation",
                "readme",
                "repository",
                "max_upload_size",
                "max_features",
                "trustpub_only",
            ],
            [
                {"id": "1", "name": "unsafe-libyaml", "description": "libyaml transpiled to rust by c2rust"},
                {"id": "2", "name": "bat", "description": "A cat(1) clone with wings."},
                {"id": "3", "name": "sourcefail", "description": "library with a helper executable"},
            ],
        ),
        "data/crate_downloads.csv": csv_bytes(
            ["crate_id", "downloads"],
            [
                {"crate_id": "1", "downloads": "1000"},
                {"crate_id": "2", "downloads": "900"},
                {"crate_id": "3", "downloads": "800"},
            ],
        ),
        "data/default_versions.csv": csv_bytes(
            ["crate_id", "version_id", "num_versions"],
            [
                {"crate_id": "1", "version_id": "10", "num_versions": "17"},
                {"crate_id": "2", "version_id": "20", "num_versions": "58"},
                {"crate_id": "3", "version_id": "30", "num_versions": "1"},
            ],
        ),
        "data/versions.csv": csv_bytes(
            [
                "id",
                "crate_id",
                "num",
                "updated_at",
                "created_at",
                "downloads",
                "features",
                "yanked",
                "license",
                "crate_size",
                "published_by",
                "checksum",
                "links",
                "rust_version",
                "has_lib",
                "bin_names",
                "edition",
                "description",
                "homepage",
                "documentation",
                "repository",
            ],
            [
                {
                    "id": "10",
                    "crate_id": "1",
                    "num": "0.2.11",
                    "yanked": "f",
                    "has_lib": "t",
                    "bin_names": "{run-emitter-test-suite,run-parser-test-suite}",
                    "description": "libyaml transpiled to rust by c2rust",
                },
                {
                    "id": "20",
                    "crate_id": "2",
                    "num": "0.26.1",
                    "yanked": "f",
                    "has_lib": "t",
                    "bin_names": "{bat}",
                    "description": "A cat(1) clone with wings.",
                },
                {
                    "id": "30",
                    "crate_id": "3",
                    "num": "1.0.0",
                    "yanked": "f",
                    "has_lib": "t",
                    "bin_names": "{sourcefail-helper}",
                    "description": "library with a helper executable",
                },
            ],
        ),
        "data/version_downloads.csv": csv_bytes(
            ["version_id", "downloads", "date"],
            [
                {"version_id": "10", "downloads": "1000", "date": today},
                {"version_id": "20", "downloads": "900", "date": today},
                {"version_id": "30", "downloads": "800", "date": today},
            ],
        ),
    }
    with tarfile.open(path, "w:gz") as archive:
        for name, data in files.items():
            add_tar_member(archive, name, data)


class CratesIndexTests(unittest.TestCase):
    def test_default_recent_download_gate_includes_established_cli_crates(self):
        self.assertEqual(crates_index.CRATES_IO_MIN_RECENT_DOWNLOADS, 10_000)

    def test_build_index_memory_does_not_scale_with_irrelevant_crates(self):
        with tempfile.TemporaryDirectory() as tmp:
            dump_path = Path(tmp) / "db-dump.tar.gz"
            write_fixture_dump(dump_path, irrelevant_crates=30_000, irrelevant_versions=30_000)

            tracemalloc.start()
            try:
                index = crates_index.build_index_from_dump(
                    dump_path,
                    min_recent_downloads=50,
                    recent_window_days=90,
                    dump_meta={"source_url": "fixture"},
                )
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

        self.assertEqual(list(index["crates"]), ["ripgrep"])
        self.assertLess(peak, 20 * 1024 * 1024)

    def test_build_index_does_not_require_extracted_csv_tempfiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            dump_path = Path(tmp) / "db-dump.tar.gz"
            write_fixture_dump(dump_path)

            with mock.patch.object(
                crates_index.tempfile,
                "TemporaryDirectory",
                side_effect=OSError(28, "No space left on device"),
            ):
                index = crates_index.build_index_from_dump(
                    dump_path,
                    min_recent_downloads=50,
                    recent_window_days=90,
                    dump_meta={"source_url": "fixture"},
                )

        self.assertEqual(list(index["crates"]), ["ripgrep"])

    def test_csv_rows_accepts_large_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "crates.csv"
            csv_path.write_text(f"id,name,description\n1,ripgrep,{('x' * 200000)}\n", encoding="utf-8")

            rows = list(crates_index.csv_rows(csv_path))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "ripgrep")
        self.assertEqual(len(rows[0]["description"]), 200000)

    def test_parse_pg_text_array_handles_quoted_values(self):
        self.assertEqual(
            crates_index.parse_pg_text_array('{"cargo-audit","cargo auditable","rg"}'),
            ["cargo-audit", "cargo auditable", "rg"],
        )
        self.assertTrue(crates_index.valid_executable_name("cargo-audit"))
        self.assertFalse(crates_index.valid_executable_name("cargo auditable"))

    def test_build_index_from_dump_selects_recent_installable_default_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            dump_path = Path(tmp) / "db-dump.tar.gz"
            write_fixture_dump(dump_path)

            index = crates_index.build_index_from_dump(
                dump_path,
                min_recent_downloads=50,
                recent_window_days=90,
                dump_meta={"source_url": "fixture"},
            )

        self.assertEqual(list(index["crates"]), ["ripgrep"])
        ripgrep = index["crates"]["ripgrep"]
        self.assertEqual(ripgrep["version"], "15.1.0")
        self.assertEqual(ripgrep["executables"][0]["name"], "rg")
        self.assertEqual(ripgrep["popularity"]["recent_downloads"], 75)
        self.assertEqual(ripgrep["packageManagerUrl"], "https://crates.io/crates/ripgrep")

    def test_cargo_executable_intent_filters_internal_library_bins(self):
        unsafe_archive = crate_archive_bytes({
            "unsafe-libyaml-0.2.11/Cargo.toml.orig": """
[package]
name = "unsafe-libyaml"
description = "libyaml transpiled to rust by c2rust"

[lib]
""",
            "unsafe-libyaml-0.2.11/README.md": "Rust bindings for libyaml.",
            "unsafe-libyaml-0.2.11/src/bin/run-emitter-test-suite.rs": "fn main() {}",
            "unsafe-libyaml-0.2.11/src/bin/run-parser-test-suite.rs": "fn main() {}",
            "unsafe-libyaml-0.2.11/tests/test_parser.rs": "#[test] fn parser() {}",
        })

        intent = crates_index.cargo_executable_intent(
            "unsafe-libyaml",
            {"description": "libyaml transpiled to rust by c2rust"},
            {"num": "0.2.11", "has_lib": "t", "description": "libyaml transpiled to rust by c2rust"},
            ["run-emitter-test-suite", "run-parser-test-suite"],
            source_archive_fetcher=lambda _name, _version: unsafe_archive,
        )

        self.assertEqual(intent["intent"], "internal_only")

    def test_cargo_executable_intent_keeps_public_binary_shapes(self):
        qrcode_archive = crate_archive_bytes({
            "qrcode-0.14.1/Cargo.toml.orig": """
[package]
name = "qrcode"
description = "QR code encoder in Rust"

[lib]

[[bin]]
name = "qrencode"
""",
            "qrcode-0.14.1/src/bin/qrencode.rs": "fn main() {}",
        })

        explicit_bin = crates_index.cargo_executable_intent(
            "qrcode",
            {"description": "QR code encoder in Rust"},
            {"num": "0.14.1", "has_lib": "t", "description": "QR code encoder in Rust"},
            ["qrencode"],
            source_archive_fetcher=lambda _name, _version: qrcode_archive,
        )

        bat_archive = crate_archive_bytes({
            "bat-0.26.1/Cargo.toml.orig": """
[package]
name = "bat"
description = "A cat(1) clone with wings."

[lib]
""",
            "bat-0.26.1/README.md": "Usage: bat [OPTIONS] [FILE]",
            "bat-0.26.1/src/bin/bat/main.rs": "fn main() {}",
        })

        matching_name = crates_index.cargo_executable_intent(
            "bat",
            {"description": "A cat(1) clone with wings."},
            {"num": "0.26.1", "has_lib": "t", "description": "A cat(1) clone with wings."},
            ["bat"],
            source_archive_fetcher=lambda _name, _version: bat_archive,
        )

        self.assertEqual(explicit_bin["intent"], "public_cli")
        self.assertEqual(matching_name["intent"], "public_cli")

    def test_build_index_from_dump_excludes_internal_only_binary_crates(self):
        archives = {
            ("unsafe-libyaml", "0.2.11"): crate_archive_bytes({
                "unsafe-libyaml-0.2.11/Cargo.toml.orig": """
[package]
name = "unsafe-libyaml"
description = "libyaml transpiled to rust by c2rust"

[lib]
""",
                "unsafe-libyaml-0.2.11/README.md": "Rust bindings for libyaml.",
                "unsafe-libyaml-0.2.11/src/bin/run-emitter-test-suite.rs": "fn main() {}",
                "unsafe-libyaml-0.2.11/src/bin/run-parser-test-suite.rs": "fn main() {}",
                "unsafe-libyaml-0.2.11/tests/test_parser.rs": "#[test] fn parser() {}",
            }),
            ("bat", "0.26.1"): crate_archive_bytes({
                "bat-0.26.1/Cargo.toml.orig": """
[package]
name = "bat"
description = "A cat(1) clone with wings."

[lib]
""",
                "bat-0.26.1/README.md": "Usage: bat [OPTIONS] [FILE]",
                "bat-0.26.1/src/bin/bat/main.rs": "fn main() {}",
            }),
        }

        def fetcher(name: str, version: str) -> bytes:
            if name == "sourcefail":
                raise OSError("fixture fetch failure")
            return archives[(name, version)]

        with tempfile.TemporaryDirectory() as tmp:
            dump_path = Path(tmp) / "db-dump.tar.gz"
            write_internal_binary_fixture_dump(dump_path)

            index = crates_index.build_index_from_dump(
                dump_path,
                min_recent_downloads=50,
                recent_window_days=90,
                dump_meta={"source_url": "fixture"},
                source_archive_fetcher=fetcher,
            )

        self.assertNotIn("unsafe-libyaml", index["crates"])
        self.assertIn("bat", index["crates"])
        self.assertIn("sourcefail", index["crates"])
        self.assertEqual(index["source"]["source_inspection"]["excludedInternalBinaryCrates"], 1)
        self.assertEqual(index["source"]["source_inspection"]["sourceInspectionFailures"], 1)

    def test_build_index_from_dump_reuses_source_inspection_cache(self):
        archives = {
            ("unsafe-libyaml", "0.2.11"): crate_archive_bytes({
                "unsafe-libyaml-0.2.11/Cargo.toml.orig": """
[package]
name = "unsafe-libyaml"

[lib]
""",
                "unsafe-libyaml-0.2.11/src/bin/run-emitter-test-suite.rs": "fn main() {}",
            }),
            ("bat", "0.26.1"): crate_archive_bytes({
                "bat-0.26.1/Cargo.toml.orig": """
[package]
name = "bat"

[lib]

[[bin]]
name = "bat"
""",
                "bat-0.26.1/src/main.rs": "fn main() {}",
            }),
        }
        calls: list[tuple[str, str]] = []

        def fetcher(name: str, version: str) -> bytes:
            calls.append((name, version))
            if name == "sourcefail":
                raise OSError("fixture fetch failure")
            return archives[(name, version)]

        with tempfile.TemporaryDirectory() as tmp:
            dump_path = Path(tmp) / "db-dump.tar.gz"
            write_internal_binary_fixture_dump(dump_path)
            original_cache_path = crates_index.CRATES_IO_SOURCE_INSPECTION_CACHE_PATH
            crates_index.CRATES_IO_SOURCE_INSPECTION_CACHE_PATH = Path(tmp) / "source-inspection-cache.json"
            try:
                crates_index.build_index_from_dump(
                    dump_path,
                    min_recent_downloads=50,
                    recent_window_days=90,
                    dump_meta={"source_url": "fixture"},
                    source_archive_fetcher=fetcher,
                )
                crates_index.build_index_from_dump(
                    dump_path,
                    min_recent_downloads=50,
                    recent_window_days=90,
                    dump_meta={"source_url": "fixture"},
                    source_archive_fetcher=fetcher,
                )
            finally:
                crates_index.CRATES_IO_SOURCE_INSPECTION_CACHE_PATH = original_cache_path

        self.assertEqual(calls, [("unsafe-libyaml", "0.2.11"), ("bat", "0.26.1"), ("sourcefail", "1.0.0")])

    def test_required_features_survive_index_and_rendering(self):
        archive = crate_archive_bytes({
            "ripgrep-15.1.0/Cargo.toml": '\n[[bin]]\nname = "rg"\nrequired-features = ["cli"]\n',
            "ripgrep-15.1.0/Cargo.toml.orig": '[package]\nname = "ignored-original"\n',
        })
        with tempfile.TemporaryDirectory() as tmp:
            dump_path = Path(tmp) / "db-dump.tar.gz"
            write_fixture_dump(dump_path)
            with mock.patch.object(crates_index, "CRATES_IO_SOURCE_INSPECTION_CACHE_PATH", Path(tmp) / "inspection.json"):
                index = crates_index.build_index_from_dump(
                    dump_path, min_recent_downloads=50,
                    source_archive_fetcher=lambda *_: archive,
                )
        record = index["crates"]["ripgrep"]
        self.assertEqual(record["cargoRequiredFeatures"], {"rg": ["cli"]})
        sources = {
            "db": {}, "crates": index, "geiger": {}, "pkg_page_enrichment": {},
            "pkg_version_freshness": {}, "pkg_graph": {}, "pkg_cross_ecosystem": {},
            "pkg_agent_safety_answers": {},
        }
        page = pkg_pages.package_pages_from_sources(sources)["cargo:ripgrep"]
        expected = "cargo install ripgrep --bin=rg --features=cli"
        self.assertEqual(pkg_pages.native_install_command(page), expected)
        # Old runtime cross-ecosystem commands must not reintroduce the broken route.
        page.install_commands = [{"manager": "Cargo", "command": "cargo install ripgrep"}]
        pkg_pages.verify_local_install_commands({page.key: page})
        self.assertEqual([item["command"] for item in page.install_commands], [expected])
        other = pkg_pages.PackagePage(provider="brew", name="ripgrep", install_commands=[
            {"manager": "Cargo", "command": "cargo install ripgrep"},
        ])
        pkg_pages.verify_local_install_commands({page.key: page, other.key: other})
        self.assertEqual(other.install_commands[0]["command"], expected)
        manifest = {"generated_at": "2026-10-25T00:00:00Z"}
        self.assertIn(expected, pkg_pages.render_package_page(page, manifest))
        self.assertIn(expected, pkg_pages.render_package_markdown(page, manifest))

    def test_normalized_binary_does_not_change_internal_cli_classification(self):
        archive = crate_archive_bytes({
            "library/Cargo.toml.orig": '[package]\nname = "library"\n',
            "library/Cargo.toml": '[[bin]]\nname = "run-test-suite"\nrequired-features = ["test"]\n',
            "library/src/bin/run-test-suite.rs": "fn main() {}",
        })
        intent = crates_index.cargo_executable_intent(
            "library", {}, {"num": "1.0.0", "has_lib": "t", "description": "A library"},
            ["run-test-suite"], source_archive_fetcher=lambda *_: archive,
        )
        self.assertEqual(intent["intent"], "internal_only")

    def test_cargo_commands_keep_binary_feature_sets_separate(self):
        commands = crates_index.cargo_install_commands("tool", {
            "client": ["client", "tls"], "server": ["server"], "plain": [],
        })
        self.assertEqual(commands, [
            "cargo install tool --bin=client --features=client,tls",
            "cargo install tool --bin=plain",
            "cargo install tool --bin=server --features=server",
        ])
        self.assertEqual(crates_index.cargo_install_commands("tool", {}), ["cargo install tool"])
        import shlex
        command = crates_index.cargo_install_commands("tool", {"bin": ["$(touch nope)"]})[0]
        self.assertEqual(shlex.split(command)[-1], "--features=$(touch nope)")

    def test_crates_index_creates_cargo_package_pages_without_db_entries(self):
        sources = {
            "db": {"entries": {}, "formulas": {}, "casks": {}, "npms": {}},
            "crates": {
                "crates": {
                    "ripgrep": {
                        "summary": "ripgrep command",
                        "version": "15.1.0",
                        "executables": [{"name": "rg", "kind": "binary"}],
                        "packageManager": "Cargo",
                        "packageManagerUrl": "https://crates.io/crates/ripgrep",
                    }
                }
            },
            "geiger": {},
            "pkg_page_enrichment": {},
            "pkg_version_freshness": {},
            "pkg_graph": {},
            "pkg_cross_ecosystem": {},
            "pkg_agent_safety_answers": {},
        }

        pages = pkg_pages.package_pages_from_sources(sources)
        page = pages["cargo:ripgrep"]

        self.assertEqual(page.provider, "cargo")
        self.assertEqual(page.path, "/cargo/ripgrep/")
        self.assertEqual(pkg_pages.native_install_command(page), "cargo install ripgrep")
        self.assertNotIn("cargo:ripgrep", sources["db"]["entries"].values())


if __name__ == "__main__":
    unittest.main()
