import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import pkg_hub_data


ROOT = Path(__file__).resolve().parents[1]


def load_categorizer():
    path = ROOT / "scripts" / "categorize-ecosystem-packages.py"
    spec = importlib.util.spec_from_file_location("categorize_ecosystem_packages", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PkgHubDataTaxonomyTests(unittest.TestCase):
    def tearDown(self):
        pkg_hub_data.load_pkg_taxonomy_data.cache_clear()
        pkg_hub_data.load_pkg_ecosystem_taxonomy_data.cache_clear()
        pkg_hub_data.load_pkg_taxonomy_index.cache_clear()

    def test_ecosystem_overlay_is_visible_to_taxonomy_lookup(self):
        original_base = pkg_hub_data.PKG_TAXONOMY_PATH
        original_overlay = pkg_hub_data.PKG_ECOSYSTEM_TAXONOMY_PATH
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "pkg-taxonomy.json"
            overlay = root / "pkg-ecosystem-taxonomy.json"
            base.write_text(
                json.dumps({
                    "schema": 1,
                    "packages": {
                        "brew:bat": {
                            "id": "brew:bat",
                            "category": "developer-tools",
                            "categoryPath": ["developer-tools"],
                            "packageManagerAliases": {"npm": "bat-npm"},
                        }
                    },
                }),
                encoding="utf-8",
            )
            overlay.write_text(
                json.dumps({
                    "schema": 1,
                    "packages": {
                        "cargo:ripgrep": {
                            "id": "cargo:ripgrep",
                            "category": "developer-tools",
                            "categoryPath": ["developer-tools", "search"],
                            "tags": ["cli", "search"],
                        }
                    },
                }),
                encoding="utf-8",
            )
            try:
                pkg_hub_data.PKG_TAXONOMY_PATH = base
                pkg_hub_data.PKG_ECOSYSTEM_TAXONOMY_PATH = overlay
                pkg_hub_data.load_pkg_taxonomy_data.cache_clear()
                pkg_hub_data.load_pkg_ecosystem_taxonomy_data.cache_clear()
                pkg_hub_data.load_pkg_taxonomy_index.cache_clear()

                index = pkg_hub_data.load_pkg_taxonomy_index()
            finally:
                pkg_hub_data.PKG_TAXONOMY_PATH = original_base
                pkg_hub_data.PKG_ECOSYSTEM_TAXONOMY_PATH = original_overlay

        self.assertEqual(pkg_hub_data.taxonomy_for_package(index, "npm", "bat-npm")["id"], "brew:bat")
        self.assertEqual(pkg_hub_data.taxonomy_for_package(index, "cargo", "ripgrep")["categoryPath"], ["developer-tools", "search"])


class EcosystemCategorizerTests(unittest.TestCase):
    def setUp(self):
        self.module = load_categorizer()

    def test_selects_uncategorized_npm_and_cargo_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "package-index.json"
            crates = root / "crates.json"
            db.write_text(json.dumps({"npms": {"0x": {"summary": "flamegraph profiler", "executable": "0x"}}}), encoding="utf-8")
            crates.write_text(json.dumps({"crates": {"ripgrep": {"summary": "line search", "executables": [{"name": "rg"}]}}}), encoding="utf-8")

            with mock.patch.object(self.module, "read_cask_catalog", return_value=({}, {}, {})):
                candidates = self.module.load_candidates("all", db, crates)
            selected = self.module.selected_candidates(
                candidates,
                {"schema": 1, "packages": {"npm:0x": {"id": "npm:0x"}}},
                include_existing=False,
            )

        self.assertEqual([item["id"] for item in selected], ["cargo:ripgrep"])

    def test_selects_casks_and_respects_base_taxonomy(self):
        candidates = self.module.cask_packages({
            "chatgpt": {"displayName": "ChatGPT", "applications": ["ChatGPT.app"]},
            "vlc": {"displayName": "VLC", "applications": ["VLC.app"]},
        })

        selected = self.module.selected_candidates(
            candidates,
            {"schema": 1, "packages": {}},
            include_existing=False,
            existing_ids={"cask:chatgpt"},
        )

        self.assertEqual([item["id"] for item in selected], ["cask:vlc"])

    def test_rejects_invalid_category_roots(self):
        _entries, errors = self.module.validate_payload(
            {
                "results": [
                    {
                        "id": "npm:0x",
                        "displayName": "0x",
                        "category": "nope",
                        "categoryPath": ["nope"],
                        "categoryConfidence": "high",
                        "tags": ["profiler"],
                        "tagsConfidence": "high",
                        "categorySources": ["summary"],
                        "tagsSources": ["summary"],
                    }
                ]
            },
            {"npm:0x"},
        )

        self.assertTrue(any("categoryPath must start" in error for error in errors))

    def test_merge_keeps_existing_entries_and_sorts_keys(self):
        merged = self.module.merge_taxonomy(
            {"schema": 1, "packages": {"npm:z": {"id": "npm:z", "category": "other"}}},
            [
                {
                    "id": "cargo:a",
                    "displayName": "a",
                    "category": "developer-tools",
                    "categoryPath": ["developer-tools"],
                    "categoryConfidence": "high",
                    "categorySources": ["fixture"],
                    "tags": ["cli"],
                    "tagsConfidence": "high",
                    "tagsSources": ["fixture"],
                }
            ],
        )

        self.assertEqual(list(merged["packages"]), ["cargo:a", "npm:z"])
        self.assertEqual(merged["packages"]["npm:z"]["category"], "other")

    def test_write_json_preserves_deterministic_sorted_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "taxonomy.json"
            self.module.write_json(
                path,
                {"schema": 1, "packages": {"npm:z": {"id": "npm:z"}, "cargo:a": {"id": "cargo:a"}}},
            )

            text = path.read_text(encoding="utf-8")

        self.assertLess(text.index('"cargo:a"'), text.index('"npm:z"'))

    def test_run_skips_a_valid_codex_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = root / "batch"
            batch.mkdir()
            result = {
                "id": "cask:vlc",
                "displayName": "VLC",
                "category": "media",
                "categoryPath": ["media"],
                "categoryConfidence": "high",
                "categorySources": ["summary"],
                "tags": ["video-player"],
                "tagsConfidence": "high",
                "tagsSources": ["summary"],
            }
            (batch / "codex-output.json").write_text(json.dumps({"results": [result]}), encoding="utf-8")
            manifest = {"batches": [{
                "batch": "0001",
                "batch_dir": "batch",
                "codex_output_path": "batch/codex-output.json",
                "expected_ids": ["cask:vlc"],
                "prompt_path": "batch/prompt.md",
            }]}
            original_root = self.module.ROOT
            try:
                self.module.ROOT = root
                with mock.patch.object(self.module.subprocess, "run") as run:
                    self.module.run_codex_batches(manifest)
            finally:
                self.module.ROOT = original_root

        run.assert_not_called()

    def test_apply_is_atomic_when_a_batch_output_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            batch = root / "batch"
            run_dir.mkdir()
            batch.mkdir()
            output = root / "taxonomy.json"
            original = {"schema": 1, "packages": {"cask:existing": {"id": "cask:existing"}}}
            output.write_text(json.dumps(original), encoding="utf-8")
            (run_dir / "controller-manifest.json").write_text(json.dumps({"batches": [{
                "batch_dir": "batch",
                "codex_output_path": "batch/missing.json",
                "expected_ids": ["cask:vlc"],
            }]}), encoding="utf-8")
            original_root = self.module.ROOT
            try:
                self.module.ROOT = root
                code = self.module.apply_run(run_dir, output)
            finally:
                self.module.ROOT = original_root

            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), original)


if __name__ == "__main__":
    unittest.main()
