import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bootstrap.lib import discovery as d

TODAY = dt.date(2026, 10, 17)
IMCP = '''cask "imcp" do
  version "1.6.2"
  sha256 "c99f2aaf79d35cef28a5c7433d1f52abca147dabf64862d72006ee5bed24a62e"
  url "https://github.com/mattt/iMCP/releases/download/#{version}/iMCP.zip"
  name "iMCP"
  desc "MCP server app for your application"
  homepage "https://github.com/mattt/iMCP"
  livecheck do
    url :url
    strategy :github_latest
  end
  app "iMCP.app"
  zap trash: [
    "~/Library/Preferences/co.dododo.iMCP.plist",
  ]
end
'''


def reports(name="mattt/tap/imcp", counts=(141, 319, 1410), today=TODAY):
    return {days: d.parse_report({"start_date": (today-dt.timedelta(days=days)).isoformat(),
                                 "end_date": today.isoformat(),
                                 "items": [{"cask": name, "count": str(count)}]}, "cask", days)
            for days, count in zip(d.WINDOWS, counts)}


class DiscoveryTests(unittest.TestCase):
    def test_imcp_from_analytics_to_catalog(self):
        state = {"schema": 1, "packages": {}}
        d.observe(state, {"cask": reports()}, TODAY)
        d.resolve_candidates(state, lambda key: d.read_definition(IMCP, *key.split(":", 1)), TODAY)
        db = d.overlay({"casks": {}}, state, TODAY)
        package = db["casks"]["mattt/tap/imcp"]
        self.assertEqual(package["applications"], ["iMCP.app"])
        self.assertEqual(package["installCommands"][0]["command"], "brew install --cask mattt/tap/imcp")
        self.assertEqual(package["discovery"]["trend"]["growthPercent"], 58.4)
        self.assertFalse(package["discovery"]["recentlyDiscovered"])
        self.assertEqual(package["url"], "https://github.com/mattt/iMCP/releases/download/1.6.2/iMCP.zip")

    def test_initial_baseline_is_not_new_but_subsequent_admission_is(self):
        state = {"schema": 1, "packages": {}}
        d.observe(state, {"cask": reports("another/tap/tool")}, TODAY)
        tomorrow = TODAY + dt.timedelta(days=1)
        d.observe(state, {"cask": reports(today=tomorrow)}, tomorrow)
        d.resolve_candidates(state, lambda key: d.read_definition(IMCP, "cask", "mattt/tap/imcp"), tomorrow,
                             requested=["cask:mattt/tap/imcp"], limit=1)
        package = d.overlay({}, state, tomorrow)["casks"]["mattt/tap/imcp"]
        self.assertTrue(package["discovery"]["recentlyDiscovered"])

    def test_low_volume_decline_misaligned_and_stale_do_not_trend(self):
        for counts in ((1, 1, 1), (30, 200, 500), (100, 90, 200)):
            state = {"schema": 1, "packages": {}}
            d.observe(state, {"cask": reports(counts=counts)}, TODAY)
            self.assertIsNone(d.trend(state["packages"]["cask:mattt/tap/imcp"]["analytics"], TODAY))
        state = {"schema": 1, "packages": {}}
        d.observe(state, {"cask": reports()}, TODAY)
        analytics = state["packages"]["cask:mattt/tap/imcp"]["analytics"]
        self.assertIsNone(d.trend(analytics, TODAY + dt.timedelta(days=4)))
        analytics["90"]["endDate"] = "2026-10-16"
        self.assertIsNone(d.trend(analytics, TODAY))

    def test_no_baseline_growth_has_no_infinite_percentage(self):
        state = {"schema": 1, "packages": {}}
        d.observe(state, {"cask": reports(counts=(100, 100, 100))}, TODAY)
        result = d.trend(state["packages"]["cask:mattt/tap/imcp"]["analytics"], TODAY)
        self.assertIsNone(result["growthPercent"])
        self.assertGreater(result["score"], 0)

    def test_identity_and_unsupported_ruby(self):
        for name in ("-bad", "a/../x", "a/b/c/d", "foo;id", "a/b/$(id)"):
            self.assertFalse(d.valid_name(name))
        for source in (IMCP.replace('version "1.6.2"', 'version ENV["VERSION"]'),
                       IMCP.replace('app "iMCP.app"', 'if OS.mac?\napp "iMCP.app"\nend'),
                       IMCP.replace('app "iMCP.app"', 'system "touch /tmp/never"'),
                       IMCP.replace('app "iMCP.app"', 'disable! date: "2026-01-01"')):
            with self.assertRaises(ValueError): d.read_definition(source, "cask", "mattt/tap/imcp")

    def test_taps_do_not_collide_and_outage_preserves_metadata(self):
        state = {"schema": 1, "packages": {}}
        d.observe(state, {"cask": reports()}, TODAY)
        resolver = lambda key: d.read_definition(IMCP, "cask", key.split(":", 1)[1])
        d.resolve_candidates(state, resolver, TODAY, requested=["cask:loopwork/tap/imcp", "cask:mattt/tap/imcp"])
        def fail(key): raise OSError("network unavailable")
        d.resolve_candidates(state, fail, TODAY, requested=["cask:mattt/tap/imcp"])
        db = d.overlay({}, state, TODAY)
        self.assertEqual(len(db["casks"]), 2)
        self.assertIsNone(db["casks"]["mattt/tap/imcp"]["discovery"]["trend"])
        self.assertEqual(state["packages"]["cask:mattt/tap/imcp"]["status"], "stale")

    def test_state_survives_restart_and_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = d.load_state(path)
            d.observe(state, {"cask": reports()}, TODAY)
            d.save_state(state, path)
            self.assertEqual(state, d.load_state(path))
            path.write_text('{"schema": 99, "packages": {}}')
            with self.assertRaises(ValueError): d.load_state(path)

    def test_report_validation_and_small_baselines(self):
        for payload in (None, {}, {"start_date": "2026-09-17", "end_date": "2026-10-17", "items": [None]}):
            with self.assertRaises(ValueError): d.parse_report(payload, "cask", 30)
        state = {"schema": 1, "packages": {}}
        d.observe(state, {"cask": reports(counts=(200, 201, 300))}, TODAY)
        result = d.trend(state["packages"]["cask:mattt/tap/imcp"]["analytics"], TODAY)
        self.assertIsNone(result["growthPercent"])

    def test_refreshes_are_not_starved_by_unseen_candidates(self):
        state = {"schema": 1, "packages": {}}
        d.observe(state, {"cask": reports()}, TODAY)
        d.resolve_candidates(state, lambda key: d.read_definition(IMCP, "cask", "mattt/tap/imcp"), TODAY)
        later = TODAY + dt.timedelta(days=8)
        d.observe(state, {"cask": reports("other/tap/new", today=later)}, later)
        calls = []
        def resolve(key):
            calls.append(key)
            return d.read_definition(IMCP, "cask", "mattt/tap/imcp")
        d.resolve_candidates(state, resolve, later, limit=2)
        self.assertIn("cask:mattt/tap/imcp", calls)
        self.assertIn("cask:other/tap/new", calls)

    def test_resolver_pins_commit_and_rejects_ambiguous_paths(self):
        from unittest.mock import patch
        fetcher = d.Fetcher(Path("/unused"))
        urls = []
        def get(url, **kwargs):
            urls.append(url)
            if "/commits/" in url: return {"sha": "abcd"}
            if "/git/trees/" in url:
                return {"sha": "tree-sha", "tree": [{"path": "Casks/imcp.rb", "type": "blob", "mode": "100644"}]}
            return IMCP
        with patch.object(fetcher, "get", side_effect=get):
            item = fetcher.resolve("cask:mattt/tap/imcp")
            self.assertEqual(item["tapSource"]["revision"], "abcd")
            self.assertIn("/abcd/Casks/imcp.rb", urls[-1])
            fetcher.trees["mattt/homebrew-tap"]["tree"].append({"path": "Casks/i/imcp.rb", "type": "blob", "mode": "100644"})
            with self.assertRaises(ValueError): fetcher.resolve("cask:mattt/tap/imcp")

    def test_formula_cli_subset(self):
        source = '''class Example < Formula
 desc "Example CLI"
 homepage "https://example.com"
 url "https://example.com/tool.tar.gz"
 def install
   bin.install "example"
 end
end'''
        metadata = d.read_definition(source, "brew", "owner/tap/example")
        self.assertEqual(metadata["binaries"], [{"source": "example", "target": "example"}])


class DiscoveryIntegrationTests(unittest.TestCase):
    @staticmethod
    def module(name, filename):
        import importlib.util
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(name, root / "scripts" / filename)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    def test_imcp_catalog_sqlite_search_and_editorial_candidates(self):
        import json
        import sqlite3
        import importlib.machinery
        import importlib.util
        from unittest.mock import patch
        pages = self.module("discovery_test_pages", "generate-pkg-pages.py")
        sql = self.module("discovery_test_sqlite", "generate-pkg-sqlite.py")
        loader = importlib.machinery.SourceFileLoader("discovery_test_feed", str(d.ROOT / "scripts/update-discover-feed"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        feed = importlib.util.module_from_spec(spec)
        loader.exec_module(feed)
        state = {"schema": 1, "packages": {}}
        d.observe(state, {"cask": reports()}, TODAY)
        def resolve(key):
            metadata = d.read_definition(IMCP, "cask", "mattt/tap/imcp")
            metadata["tapSource"] = {"tap": "mattt/tap", "revision": "abc123",
                "repository": "https://github.com/mattt/homebrew-tap", "path": "Casks/imcp.rb",
                "url": "https://github.com/mattt/homebrew-tap/blob/abc123/Casks/imcp.rb"}
            metadata["packageManagerUrl"] = metadata["tapSource"]["url"]
            return metadata
        d.resolve_candidates(state, resolve, TODAY)
        db = d.overlay({"generated_at": "2026-10-17T00:00:00Z", "formulas": {}, "casks": {}, "npms": {}}, state, TODAY)
        with patch.object(pages, "apply_package_page_supplements"):
            catalog = pages.package_pages_from_sources({"db": db}, cross_ecosystem_only=True)
        key = "cask:mattt/tap/imcp"
        page = catalog[key]
        self.assertTrue(pages.is_indexable_package_page(page))
        self.assertEqual(page.display_name, "iMCP")
        self.assertEqual(pages.install_command(page), "brew install --cask mattt/tap/imcp")
        routes = sql.package_routes([page])
        public = sql.discovery_document(pages, [page], routes)
        self.assertEqual(public["trending"][0]["packageKey"], key)
        self.assertEqual(public["recentlyDiscovered"], [])
        # A qualifying tap remains visible when official packages fill the global shelf.
        import dataclasses
        crowded = [dataclasses.replace(page, provider="brew", name=f"tool{i}", repository=f"https://example.com/{i}",
            extra={**page.extra, "tapSource": {}, "discovery": {**page.extra["discovery"],
                "trend": {**page.extra["discovery"]["trend"], "score": 100}}}) for i in range(60)]
        all_pages = [page, *crowded]
        shelves = sql.discovery_document(pages, all_pages, sql.package_routes(all_pages))
        self.assertNotIn(key, [item["packageKey"] for item in shelves["trending"]])
        self.assertEqual([item["packageKey"] for item in shelves["independentTaps"]], [key])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.sqlite"
            record = sql.package_record(pages, page, routes[key], "iMCP mattt/tap/imcp MCP server")
            search = sql.SearchDocument(routes[key].path, "en", "iMCP", page.summary, "cask", key, None, record.search_text)
            sql.write_sqlite(path, [search], [record], [], [], {"discovery": public})
            with sqlite3.connect(path) as connection:
                row = connection.execute("SELECT native_install_command, data_json FROM packages WHERE package_key=?", [key]).fetchone()
                self.assertEqual(row[0], "brew install --cask mattt/tap/imcp")
                self.assertEqual(json.loads(row[1])["full"]["tapSource"]["tap"], "mattt/tap")
                self.assertEqual(connection.execute("SELECT title FROM search_documents WHERE search_text LIKE '%imcp%'").fetchone()[0], "iMCP")
        _, normalized = feed.normalize_catalog(db)
        # Even 200 more-popular official discoveries cannot consume the tap quota.
        for i in range(200):
            normalized[f"brew:tool{i}"] = {**normalized["brew:cask:mattt/tap/imcp"], "id": f"brew:tool{i}",
                "discovery": {"trend": {"score": 1000}}, "popularityRank": i}
        pool = feed.candidate_pool(normalized, [])
        self.assertIn("brew:cask:mattt/tap/imcp", [p["id"] for p in pool])
        _, curated = next(d.project_records(state))
        rendered = json.dumps(curated)
        for volatile in ("1.6.2", "sha256", "1410", "lastVerifiedAt", "abc123"):
            self.assertNotIn(volatile, rendered)

if __name__ == "__main__": unittest.main()
