import pathlib
import os
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MAINTENANCE = ROOT / "scripts" / "atlas-maintenance.sh"
CONTROLLER = ROOT / "scripts" / "nightly-discover-feed.sh"
GENERATOR = ROOT / "scripts" / "update-discover-feed"


class NightlyDiscoverFeedTests(unittest.TestCase):
    def test_maintenance_defaults_to_250_enrichments(self):
        maintenance = MAINTENANCE.read_text()
        service = (ROOT / "systemd" / "pkgdb-maintenance.service").read_text()
        self.assertIn('${AVDB_ENRICH_LIMIT:-250}', maintenance)
        self.assertIn('Environment=AVDB_ENRICH_LIMIT=250', service)

    def test_maintenance_runs_feed_after_database_health_check(self):
        script = MAINTENANCE.read_text()
        health_check = 'curl -fsS http://127.0.0.1:3004/healthz >/dev/null'
        feed_stage = 'scripts/nightly-discover-feed.sh'
        self.assertIn(health_check, script)
        self.assertIn(feed_stage, script)
        self.assertLess(script.index(health_check), script.index(feed_stage))

    def test_controller_reenters_codex_and_publishes_only_after_validation(self):
        script = CONTROLLER.read_text()
        self.assertIn('PMM_FEED_STATUS=NEEDS_AGENT', script)
        self.assertIn('codex --model gpt-5.6-sol', script)
        self.assertIn('--search --ask-for-approval never exec', script)
        self.assertIn('"${repo_root}/scripts/update-discover-feed" --check', script)
        self.assertNotIn('git push', script)
        self.assertIn('"${repo_root}/scripts/publish-discover-feed-atlas.sh"', script)
        self.assertLess(
            script.index('"${repo_root}/scripts/update-discover-feed" --check'),
            script.index('"${repo_root}/scripts/publish-discover-feed-atlas.sh"'),
        )

    def test_final_research_pass_is_checked_before_reporting_exhaustion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            controller = scripts / "nightly-discover-feed.sh"
            controller.write_text(CONTROLLER.read_text())
            generator = scripts / "update-discover-feed"
            generator.write_text("""#!/usr/bin/env bash
if [[ "${1:-}" == --check ]]; then exit 0; fi
count=$(cat count 2>/dev/null || echo 0)
if ((count < 3)); then echo PMM_FEED_STATUS=NEEDS_AGENT; else echo PMM_FEED_STATUS=COMMITTED; fi
""")
            codex = root / "codex"
            codex.write_text("""#!/usr/bin/env bash
count=$(cat count 2>/dev/null || echo 0)
echo $((count + 1)) > count
""")
            publisher = scripts / "publish-discover-feed-atlas.sh"
            publisher.write_text("#!/usr/bin/env bash\ntouch published\n")
            for path in (generator, codex, publisher):
                path.chmod(0o755)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            result = subprocess.run(["bash", str(controller)], cwd=root,
                env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]},
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "published").exists())
            self.assertEqual((root / "count").read_text().strip(), "3")

    def test_atlas_feed_prompt_does_not_instruct_agent_to_push(self):
        script = GENERATOR.read_text()
        self.assertNotIn("push main normally to origin", script)
        self.assertNotIn("Never force-push", script)


if __name__ == "__main__":
    unittest.main()
