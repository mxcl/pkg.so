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


if __name__ == "__main__": unittest.main()
