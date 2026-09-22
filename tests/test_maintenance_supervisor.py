import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from maintenance_runtime import run_logged, save_json


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.worker = load("maintenance-step")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in {"ROOT": self.root, "STATE_DIR": self.root / "state",
                            "STATE": self.root / "state/current.json", "LIVE": self.root / "live"}.items():
            patcher = mock.patch.object(self.worker, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.worker.LIVE.mkdir()

    def test_status_does_not_create_state_or_locks(self):
        with mock.patch.object(sys, "argv", ["maintenance-step.py", "status"]):
            self.assertEqual(self.worker.main(), 0)
        self.assertFalse(self.worker.STATE_DIR.exists())

    def test_failed_stage_resumes_without_repeating_completed_work(self):
        state = self.worker.load_state()
        sequence = [("first", ["first"]), ("second", ["second"])]
        with mock.patch.object(self.worker, "stages", return_value=sequence), mock.patch.object(self.worker, "run_logged", side_effect=[0, 1, 0]) as run:
            self.assertEqual(self.worker.step(state)["completed_stage"], "first")
            self.assertEqual(self.worker.step(state)["status"], "failed")
            resumed = self.worker.load_state()
            self.assertEqual(self.worker.step(resumed)["completed_stage"], "second")
        self.assertEqual([call.args[0] for call in run.call_args_list], [["first"], ["second"], ["second"]])

    def test_feed_handoff_is_not_marked_complete(self):
        state = self.worker.load_state()
        def fake_run(command, log, *args):
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("Research this request\nPMM_FEED_STATUS=NEEDS_AGENT\n")
            return 0
        with mock.patch.object(self.worker, "stages", return_value=[("feed-research", ["feed"])]), mock.patch.object(self.worker, "run_logged", side_effect=fake_run):
            result = self.worker.step(state)
        self.assertEqual(result["status"], "needs_agent")
        self.assertEqual(state["completed"], [])
        self.assertTrue(Path(result["prompt_file"]).is_file())

    def test_publication_recovers_crash_between_renames(self):
        state = self.worker.load_state()
        db = self.worker.LIVE / "pkg.sqlite.next"
        with sqlite3.connect(db) as connection:
            connection.execute("create table example (id integer)")
        (self.worker.LIVE / "db.json.next").write_text('{"valid": true}')
        real_replace = os.replace
        def crash(source, target):
            if str(source).endswith("db.json.next"):
                raise OSError("simulated interruption")
            real_replace(source, target)
        with mock.patch.object(self.worker, "health"), mock.patch.object(self.worker.os, "replace", side_effect=crash):
            with self.assertRaises(OSError):
                self.worker.publish_data(state)
        self.assertFalse(db.exists())
        resumed = self.worker.load_state()
        with mock.patch.object(self.worker, "health"):
            self.worker.publish_data(resumed)
        self.assertFalse((self.worker.LIVE / "db.json.next").exists())
        self.assertEqual(self.worker.digest(self.worker.LIVE / "db.json"), resumed["artifacts"]["db.json"])

    def test_changed_candidate_cannot_be_published_on_resume(self):
        state = {"artifacts": {"pkg.sqlite": "unexpected", "db.json": "unexpected"}}
        candidate = self.worker.LIVE / "pkg.sqlite.next"
        candidate.write_text("wrong")
        with self.assertRaisesRegex(ValueError, "Candidate changed"):
            self.worker.publish_data(state)
        self.assertTrue(candidate.exists())

    def test_verification_rejects_stale_or_missing_artifacts(self):
        with self.assertRaisesRegex(ValueError, "No publication"):
            self.worker.verify({})

    def test_new_day_archives_completed_run_but_resumes_incomplete_run(self):
        state = self.worker.load_state()
        state.update(started_at="2020-01-01T00:00:00+00:00", status="failed")
        save_json(self.worker.STATE, state)
        self.assertEqual(self.worker.load_state()["started_at"], state["started_at"])
        state["status"] = "complete"
        save_json(self.worker.STATE, state)
        self.assertNotEqual(self.worker.load_state()["started_at"], state["started_at"])
        self.assertTrue((self.worker.STATE_DIR / "runs" / (state["run_id"] + ".json")).exists())


class NotificationTests(unittest.TestCase):
    def test_failed_send_remains_pending_then_deduplicates_success_and_recovers(self):
        module = load("maintenance-notify")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.json"
            save_json(config, {"sender": "ops@example.com", "recipient": "owner@example.com", "region": "us-east-2"})
            failed = subprocess.CalledProcessError(1, "aws")
            accepted = subprocess.CompletedProcess([], 0, '{"MessageId":"test-id"}', '')
            with mock.patch.object(module, "STATE_DIR", root), mock.patch.object(module, "CONFIG", config), mock.patch.object(module.subprocess, "run", side_effect=[failed, accepted, accepted]) as send:
                self.assertFalse(module.notify("failure", "Problem"))
                self.assertFalse(json.loads((root / "notification.json").read_text())["delivered"])
                self.assertTrue(module.notify("failure", "Problem"))
                self.assertTrue(module.notify("failure", "Problem again"))
                self.assertEqual(send.call_count, 2)
                self.assertTrue(module.notify("recovery", "Verified healthy"))
                self.assertFalse(json.loads((root / "notification.json").read_text())["open"])
                self.assertEqual(send.call_count, 3)

    def test_fallback_does_not_overwrite_actionable_undelivered_diagnosis(self):
        module = load("maintenance-notify")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(module, "STATE_DIR", Path(tmp)), mock.patch.object(module, "CONFIG", Path(tmp) / "missing"):
            module.notify("failure", "Checkout blocked: decide whether the staged deletion is intentional.")
            module.notify("failure", "Service failed", fallback=True)
            state = json.loads((Path(tmp) / "notification.json").read_text())
            self.assertIn("staged deletion", state["message"])

    def test_no_config_is_a_visible_delivery_failure(self):
        module = load("maintenance-notify")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(module, "STATE_DIR", Path(tmp)), mock.patch.object(module, "CONFIG", Path(tmp) / "missing"):
            self.assertFalse(module.notify("failure", "Failed before model startup"))


class LauncherTests(unittest.TestCase):
    def test_capacity_retry_keeps_model_and_requires_verification(self):
        module = load("maintenance-supervisor")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(module, "STATE_DIR", Path(tmp)), mock.patch.object(module, "run_logged", side_effect=[0, 1, 0, 0]) as run, mock.patch.object(module.time, "sleep"), mock.patch.object(module, "notify") as notify:
            self.assertEqual(module.supervise(), 0)
            calls = [call.args[0] for call in run.call_args_list]
            self.assertEqual(calls[1][2], "gpt-5.6-sol")
            self.assertEqual(calls[2][2], "gpt-5.6-sol")
            self.assertEqual(calls[-1][-1], "verify")
            notify.assert_called_once()
            self.assertEqual(notify.call_args.args[0], "recovery")

    def test_successful_agent_exit_without_publication_escalates(self):
        module = load("maintenance-supervisor")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(module, "STATE_DIR", Path(tmp)), mock.patch.object(module, "run_logged", side_effect=[0, 0, 1]) as run, mock.patch.object(module, "notify") as notify:
            self.assertEqual(module.supervise(), 1)
            self.assertEqual(run.call_count, 3)
            self.assertEqual(notify.call_args.args[0], "failure")

    def test_concurrent_supervisor_does_not_launch_another_job(self):
        import fcntl
        module = load("maintenance-supervisor")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(module, "STATE_DIR", Path(tmp)), mock.patch.object(module, "run_logged") as run:
            with (Path(tmp) / "supervisor.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(module.supervise(), 0)
            run.assert_not_called()


class RuntimeTests(unittest.TestCase):
    def test_timeout_reaps_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(subprocess.TimeoutExpired):
                run_logged([sys.executable, "-c", "import time; time.sleep(10)"], Path(tmp) / "log", 0.1)

    def test_log_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log"
            self.assertEqual(run_logged([sys.executable, "-c", "print('x' * 3000000)"], path, 10), 0)
            self.assertLessEqual(path.stat().st_size, 2 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
