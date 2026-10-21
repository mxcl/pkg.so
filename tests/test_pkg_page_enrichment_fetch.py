import importlib.util
import http.client
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "generate-pkg-page-enrichment.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("pkg_page_enrichment", MODULE_PATH)
pkg_page_enrichment = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(pkg_page_enrichment)


class FetchJsonTests(unittest.TestCase):
    def test_registry_cache_only_still_refreshes_homebrew(self):
        module = pkg_page_enrichment
        stale = {"token": "notchnook", "version": "1.6.2", "homepage": "https://lo.cafe/notchnook"}
        read_json = module.read_json

        def read_source(path):
            if path in (module.PACKAGE_INDEX_PATH, Path("data/pip.json")):
                return {}
            return read_json(path)

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(module, "CACHE_DIR", Path(tmp)),
            mock.patch.object(module, "read_json", side_effect=read_source),
            mock.patch.object(module, "fetch_url", return_value=(200, {}, b"[]")) as fetch,
        ):
            for url, payload in ((module.FORMULA_URL, []), (module.CASK_URL, [stale])):
                module.write_cache(module.cache_path_for(url, module.ECOSYSTEM), payload, "old", 0)
            result = module.expected_enrichment(force_refresh=True, registry_cache_only=True)

        self.assertNotIn("cask:notchnook", result["packages"])
        self.assertEqual([call.args[0] for call in fetch.call_args_list],
                         [module.FORMULA_URL, module.CASK_URL])

    def test_fetch_json_retries_incomplete_read(self):
        original_cache_dir = pkg_page_enrichment.CACHE_DIR
        calls = []

        def fetch_url(_url, *, headers):
            calls.append(headers)
            if len(calls) == 1:
                raise http.client.IncompleteRead(b'{"partial"', 10)
            return 200, {"etag": "abc"}, b'{"ok": true}'

        with tempfile.TemporaryDirectory() as tmp:
            try:
                pkg_page_enrichment.CACHE_DIR = Path(tmp)
                with (
                    mock.patch.object(pkg_page_enrichment, "fetch_url", side_effect=fetch_url),
                    mock.patch.object(pkg_page_enrichment.time, "sleep"),
                ):
                    data = pkg_page_enrichment.fetch_json(
                        "https://example.com/index.json",
                        ecosystem="test",
                        force_refresh=True,
                    )
            finally:
                pkg_page_enrichment.CACHE_DIR = original_cache_dir

        self.assertEqual(data, {"ok": True})
        self.assertEqual(len(calls), 2)

    def test_fetch_json_uses_cached_payload_after_retryable_refresh_failure(self):
        original_cache_dir = pkg_page_enrichment.CACHE_DIR

        with tempfile.TemporaryDirectory() as tmp:
            try:
                pkg_page_enrichment.CACHE_DIR = Path(tmp)
                url = "https://example.com/index.json"
                path = pkg_page_enrichment.cache_path_for(url, "test")
                pkg_page_enrichment.write_cache(path, {"cached": True}, "abc", 0)

                with (
                    mock.patch.object(
                        pkg_page_enrichment,
                        "fetch_url",
                        side_effect=pkg_page_enrichment.urllib.error.URLError("timeout"),
                    ) as fetch_url,
                    mock.patch.object(pkg_page_enrichment.time, "sleep"),
                ):
                    data = pkg_page_enrichment.fetch_json(
                        url,
                        ecosystem="test",
                        force_refresh=True,
                    )
            finally:
                pkg_page_enrichment.CACHE_DIR = original_cache_dir

        self.assertEqual(data, {"cached": True})
        self.assertEqual(fetch_url.call_count, 1)

    def test_npm_enrichment_prefers_db_version_when_cached_packument_lags(self):
        payload = {
            "dist-tags": {"latest": "7.8.4"},
            "description": "The semantic version parser used by npm.",
            "homepage": "https://github.com/npm/node-semver#readme",
            "time": {
                "modified": "2026-06-09T23:50:03.928Z",
                "7.8.4": "2026-06-09T23:50:03.612Z",
            },
            "versions": {
                "7.8.4": {
                    "name": "semver",
                    "version": "7.8.4",
                    "description": "The semantic version parser used by npm.",
                    "homepage": "https://github.com/npm/node-semver#readme",
                    "bin": {"semver": "bin/semver.js"},
                    "dist": {
                        "tarball": "https://registry.npmjs.org/semver/-/semver-7.8.4.tgz",
                    },
                },
            },
        }
        db_info = {
            "version": "7.8.5",
            "summary": "The semantic version parser used by npm.",
            "homepage": "https://github.com/npm/node-semver#readme",
            "executable": "semver",
            "last_updated_at": "2026-06-19T18:32:48.972Z",
        }

        key, entry = pkg_page_enrichment.npm_enrichment("semver", db_info, payload)

        self.assertEqual(key, "npm:semver")
        self.assertEqual(entry["version"], "7.8.5")
        self.assertNotIn("sourceArchive", entry)
        self.assertEqual(entry["publishedAt"], "2026-06-19T18:32:48.972Z")
        self.assertEqual(entry["registryInsights"]["distTags"]["latest"], "7.8.5")
        self.assertEqual(entry["registryInsights"]["latestPublishedAt"], "2026-06-19T18:32:48.972Z")

    def test_expected_enrichment_processes_registry_payloads_incrementally(self):
        events = []
        db = {"npms": {"first": {}, "second": {}}}

        def fetch_json(url, **_kwargs):
            if url == pkg_page_enrichment.FORMULA_URL:
                return []
            if url == pkg_page_enrichment.CASK_URL:
                return []
            events.append(f"fetch:{url.rsplit('/', 1)[-1]}")
            return {"name": url}

        def read_json(path):
            return db if path == pkg_page_enrichment.PACKAGE_INDEX_PATH else {}

        def npm_enrichment(name, _info, _payload):
            events.append(f"enrich:{name}")
            return f"npm:{name}", {"package": {"name": name}}

        with (
            mock.patch.object(pkg_page_enrichment, "fetch_json", side_effect=fetch_json),
            mock.patch.object(pkg_page_enrichment, "read_json", side_effect=read_json),
            mock.patch.object(pkg_page_enrichment, "npm_enrichment", side_effect=npm_enrichment),
        ):
            result = pkg_page_enrichment.expected_enrichment()

        self.assertEqual(
            events,
            ["fetch:first", "enrich:first", "fetch:second", "enrich:second"],
        )
        self.assertEqual(set(result["packages"]), {"npm:first", "npm:second"})


if __name__ == "__main__":
    unittest.main()
