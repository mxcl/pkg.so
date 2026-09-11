import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def load_hourly_maintenance():
    path = Path(__file__).resolve().parents[1] / "scripts" / "hourly-maintenance.py"
    spec = importlib.util.spec_from_file_location("hourly_maintenance", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class HourlyMaintenanceTests(unittest.TestCase):
    def run_hourly(self, *args):
        maintenance = load_hourly_maintenance()
        with mock.patch.object(sys, "argv", ["hourly-maintenance.py", "--no-commit", "--skip-sqlite", *args]):
            with (
                mock.patch.object(maintenance, "run") as run,
                mock.patch.object(maintenance, "run_prepare_enrichment", return_value=None) as run_prepare_enrichment,
                mock.patch.object(maintenance, "unresolved_enrichment_run_ids", return_value=[]),
            ):
                self.assertEqual(maintenance.main(), 0)
        return [call.args[0] for call in run.call_args_list], run_prepare_enrichment.call_args_list

    def test_does_not_run_deleted_builder(self):
        commands, _ = self.run_hourly()

        self.assertNotIn("bash", [command[0] for command in commands])

    def test_runs_one_seventh_of_npm_full_scan(self):
        commands, _ = self.run_hourly()

        self.assertIn(
            [sys.executable, "scripts/build-db.py", "--refresh", "--npm-full-scan-parts=7"],
            commands,
        )

    def test_hourly_enrichment_prepares_external_controller_batches(self):
        _, prepare_calls = self.run_hourly()

        self.assertEqual(len(prepare_calls), 1)
        command = prepare_calls[0].args[0]
        self.assertIn("--include-missing-curated-fields", command)
        self.assertIn("--backend", command)
        self.assertIn("external", command)
        self.assertIn("--phase", command)
        self.assertIn("prepare", command)
        self.assertIn("--commit-after-batch", command)

    def test_generates_db_json_as_last_daily_step(self):
        commands, _ = self.run_hourly("--db-json-output", "db.json.next")

        self.assertEqual(
            commands[-1],
            [sys.executable, "scripts/export-automic-vault-db.py", "--output", "db.json.next"],
        )

    def test_nightly_enrichment_uses_daily_capacity(self):
        _, prepare_calls = self.run_hourly()

        command = prepare_calls[0].args[0]
        self.assertEqual(command[command.index("--limit") + 1], "250")
        self.assertEqual(command[command.index("--batch-size") + 1], "5")

    def test_atlas_runs_codex_and_writes_staged_sqlite(self):
        maintenance = load_hourly_maintenance()
        with (
            mock.patch.dict("os.environ", {"AVDB_ENRICH_BACKEND": "codex-cli"}),
            mock.patch.object(sys, "argv", ["hourly-maintenance.py", "--no-commit", "--sqlite-output", "pkg.sqlite.next"]),
            mock.patch.object(maintenance, "run") as run,
        ):
            self.assertEqual(maintenance.main(), 0)

        commands = [call.args[0] for call in run.call_args_list]
        association = next(command for command in commands if "scripts/resolve-cask-app-associations.py" in command)
        self.assertEqual(association[association.index("--limit") + 1], "50")
        self.assertEqual(association[association.index("--batch-size") + 1], "5")
        self.assertIn([sys.executable, "scripts/build.py"], commands)
        enrichment = next(command for command in commands if "scripts/enrich-projects.py" in command)
        self.assertIn("codex-cli", enrichment)
        self.assertIn("run", enrichment)
        self.assertIn(
            [sys.executable, "scripts/generate-pkg-sqlite.py", "--output", "pkg.sqlite.next"],
            commands,
        )

    def test_cask_resolution_does_not_depend_on_project_enrichment_limit(self):
        maintenance = load_hourly_maintenance()
        with (
            mock.patch.dict("os.environ", {"AVDB_ENRICH_BACKEND": "codex-cli"}),
            mock.patch.object(
                sys,
                "argv",
                ["hourly-maintenance.py", "--no-commit", "--skip-sqlite", "--enrich-limit", "0"],
            ),
            mock.patch.object(maintenance, "run") as run,
        ):
            self.assertEqual(maintenance.main(), 0)

        commands = [call.args[0] for call in run.call_args_list]
        self.assertTrue(any("scripts/resolve-cask-app-associations.py" in command for command in commands))
        self.assertFalse(any("scripts/enrich-projects.py" in command for command in commands))

    def test_snapshots_dirty_paths_before_running_commit_flow(self):
        maintenance = load_hourly_maintenance()

        with mock.patch.object(sys, "argv", ["hourly-maintenance.py", "--skip-sqlite"]):
            with (
                mock.patch.object(maintenance, "run"),
                mock.patch.object(maintenance, "run_prepare_enrichment", return_value=None),
                mock.patch.object(maintenance, "unresolved_enrichment_run_ids", return_value=[]),
                mock.patch.object(maintenance, "git_dirty_paths", return_value=(["combined/existing.yml"], [])) as git_dirty_paths,
                mock.patch.object(maintenance, "git_commit_if_changed", return_value="abc123") as git_commit_if_changed,
            ):
                self.assertEqual(maintenance.main(), 0)

        git_dirty_paths.assert_called_once_with(maintenance.COMMIT_PATHS)
        git_commit_if_changed.assert_called_once()
        self.assertEqual(
            git_commit_if_changed.call_args.kwargs["preserved_tracked_dirty"],
            ["combined/existing.yml"],
        )
        self.assertEqual(git_commit_if_changed.call_args.kwargs["preserved_untracked_dirty"], [])

    def test_parse_prepared_run_dir_reads_prepare_output(self):
        maintenance = load_hourly_maintenance()

        run_dir = maintenance.parse_prepared_run_dir(
            "Prepared 10 projects in 4 batches under cache/enrichment/runs/20260623T110419Z\n"
        )

        self.assertEqual(run_dir, maintenance.ROOT / "cache/enrichment/runs/20260623T110419Z")

    def test_hourly_enrichment_health_ignores_empty_prepare_runs(self):
        maintenance = load_hourly_maintenance()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            runs_dir = tmp_root / "cache" / "enrichment" / "runs"
            current_dir = runs_dir / "current-run"
            current_dir.mkdir(parents=True)
            (current_dir / "controller-manifest.json").write_text('{"selected_count": 0}\n', encoding="utf-8")

            with mock.patch.object(maintenance, "ROOT", tmp_root):
                with mock.patch.object(maintenance, "ENRICHMENT_RUNS_DIR", runs_dir):
                    maintenance.assert_hourly_enrichment_progress(current_dir)

    def test_hourly_enrichment_health_fails_when_older_runs_remain_unapplied(self):
        maintenance = load_hourly_maintenance()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            runs_dir = tmp_root / "cache" / "enrichment" / "runs"
            older_dir = runs_dir / "older-run"
            current_dir = runs_dir / "current-run"
            older_dir.mkdir(parents=True)
            current_dir.mkdir(parents=True)
            (older_dir / "controller-manifest.json").write_text('{"selected_count": 5}\n', encoding="utf-8")
            (current_dir / "controller-manifest.json").write_text('{"selected_count": 3}\n', encoding="utf-8")

            with mock.patch.object(maintenance, "ROOT", tmp_root):
                with mock.patch.object(maintenance, "ENRICHMENT_RUNS_DIR", runs_dir):
                    with self.assertRaises(maintenance.EnrichmentHealthError):
                        maintenance.assert_hourly_enrichment_progress(current_dir)

    def test_hourly_enrichment_health_allows_older_runs_that_were_applied(self):
        maintenance = load_hourly_maintenance()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            runs_dir = tmp_root / "cache" / "enrichment" / "runs"
            older_dir = runs_dir / "older-run"
            current_dir = runs_dir / "current-run"
            older_dir.mkdir(parents=True)
            current_dir.mkdir(parents=True)
            (older_dir / "controller-manifest.json").write_text('{"selected_count": 5}\n', encoding="utf-8")
            (older_dir / "apply-summary.json").write_text('{"changed": 2}\n', encoding="utf-8")
            (current_dir / "controller-manifest.json").write_text('{"selected_count": 3}\n', encoding="utf-8")

            with mock.patch.object(maintenance, "ROOT", tmp_root):
                with mock.patch.object(maintenance, "ENRICHMENT_RUNS_DIR", runs_dir):
                    maintenance.assert_hourly_enrichment_progress(current_dir)

    def test_hourly_skips_prepare_when_older_runs_remain_unapplied(self):
        maintenance = load_hourly_maintenance()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            runs_dir = tmp_root / "cache" / "enrichment" / "runs"
            older_dir = runs_dir / "older-run"
            older_dir.mkdir(parents=True)
            (older_dir / "controller-manifest.json").write_text('{"selected_count": 5}\n', encoding="utf-8")

            stderr = io.StringIO()
            with mock.patch.object(maintenance, "ROOT", tmp_root):
                with mock.patch.object(maintenance, "ENRICHMENT_RUNS_DIR", runs_dir):
                    with mock.patch.object(sys, "argv", ["hourly-maintenance.py", "--no-commit", "--skip-sqlite"]):
                        with (
                            mock.patch.object(maintenance, "run") as run,
                            mock.patch.object(maintenance, "run_prepare_enrichment") as run_prepare_enrichment,
                            mock.patch("sys.stderr", stderr),
                        ):
                            self.assertEqual(maintenance.main(), 0)

            run_prepare_enrichment.assert_not_called()
            self.assertIn("skipping hourly enrichment prepare", stderr.getvalue())
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn([sys.executable, "scripts/build.py", "--refresh"], commands)


if __name__ == "__main__":
    unittest.main()
