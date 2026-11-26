import sys; sys.dont_write_bytecode = True
"""Nightly dream defers when the machine is busy; synthetic vaults, patched clock and CPU."""
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from epitype import dream, memspec


def local(day, hour, minute, second=0):
    return datetime(2026, 11, day, hour, minute, second).astimezone()


class DreamDeferRegression(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epitype-dream-defer-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.vault = root / "vault"
        self.vault.mkdir()
        (self.vault / memspec.WORK_LEDGER_FILENAME).write_text("ledger\n", encoding="utf-8")
        home = root / "home"
        (home / ".claude").mkdir(parents=True)  # 合成家目錄：免得設定檔位置推出真機的家
        self.config = home / ".epitype" / "config.json"
        self.config.parent.mkdir()
        self.write_config(memspec.DREAM_MODE_NIGHTLY, "03:30")
        saved = os.environ.get(memspec.EPITYPE_CONFIG_ENV)
        os.environ[memspec.EPITYPE_CONFIG_ENV] = os.fspath(self.config)
        self.addCleanup(lambda: os.environ.__setitem__(memspec.EPITYPE_CONFIG_ENV, saved)
                        if saved is not None else os.environ.pop(memspec.EPITYPE_CONFIG_ENV, None))
        self.state = self.vault / memspec.DREAM_DIRECTORY / memspec.DREAM_STATE_FILENAME
        self.cpu_calls = []
        self.lock_calls = []

    def write_config(self, mode, at):
        self.config.write_text(json.dumps({
            memspec.CONFIG_VAULTS_FIELD: [os.fspath(self.vault)],
            memspec.DREAM_CONFIG_FIELD: {memspec.DREAM_MODE_FIELD: mode, memspec.DREAM_AT_FIELD: at},
        }), encoding="utf-8")

    def write_completed(self, moment):
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(json.dumps({memspec.DREAM_STATE_COMPLETED_EPOCH_FIELD: moment.timestamp()}),
                              encoding="utf-8")

    def log_text(self):
        path = self.vault / memspec.DREAM_DIRECTORY / memspec.DREAM_LOG_FILENAME
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def run_scheduled(self, now, cpu):
        def fake_cpu(seconds):
            self.cpu_calls.append(seconds)
            return cpu

        def fake_lock(governance, *args, **kwargs):
            # 走到 lock 就證明閘門放行；回 None 讓主程式在這裡停，不跑整場夢。
            self.lock_calls.append(governance)
            return None

        with patch.object(dream, "_local_now", return_value=now), \
                patch.object(dream, "system_cpu_percent", side_effect=fake_cpu), \
                patch.object(dream, "acquire_lock", side_effect=fake_lock):
            return dream.main([memspec.DREAM_SCHEDULED_FLAG], output=io.StringIO())

    def test_busy_before_last_attempt_defers_without_taking_the_lock(self):
        self.assertEqual(self.run_scheduled(local(18, 4, 10), 95.0), 0)
        self.assertEqual(self.lock_calls, [])
        self.assertEqual(self.cpu_calls, [memspec.DREAM_BUSY_SAMPLE_SECONDS])
        self.assertIn("deferred: system CPU 95% ≥ 60%; next try in 20 min", self.log_text())

    def test_busy_on_the_first_trigger_waits_a_full_interval(self):
        self.run_scheduled(local(18, 3, 30, 5), 60.0)
        self.assertEqual(self.lock_calls, [])
        self.assertIn("next try in 30 min", self.log_text())

    def test_busy_on_the_last_attempt_runs_and_says_so(self):
        for moment in (local(18, 7, 0, 0), local(18, 6, 59, 30), local(18, 7, 0, 40)):
            self.lock_calls.clear()
            self.run_scheduled(moment, 99.0)
            self.assertEqual(len(self.lock_calls), 1, moment)
        self.assertNotIn("deferred", self.log_text())
        self.assertIn("running busy: system CPU 99% ≥ 60% on the last attempt", self.log_text())

    def test_attempt_before_the_last_one_still_defers(self):
        self.run_scheduled(local(18, 6, 30, 20), 80.0)
        self.assertEqual(self.lock_calls, [])

    def test_not_busy_or_unmeasurable_runs(self):
        for cpu in (59.9, None):
            self.lock_calls.clear()
            self.run_scheduled(local(18, 4, 0), cpu)
            self.assertEqual(len(self.lock_calls), 1, cpu)
        self.assertNotIn("deferred", self.log_text())

    def test_already_ran_this_night_skips_without_measuring(self):
        self.write_completed(local(18, 3, 35))
        self.assertEqual(self.run_scheduled(local(18, 4, 0), 10.0), 0)
        self.assertEqual((self.lock_calls, self.cpu_calls), ([], []))
        self.assertIn("skipped: already ran this night", self.log_text())

    def test_last_night_or_unreadable_state_does_not_count(self):
        self.write_completed(local(17, 3, 40))
        self.run_scheduled(local(18, 4, 0), 10.0)
        self.assertEqual(len(self.lock_calls), 1)
        self.state.write_text("{broken", encoding="utf-8")
        self.run_scheduled(local(18, 4, 0), 10.0)
        self.assertEqual(len(self.lock_calls), 2)
        self.assertNotIn("already ran", self.log_text())

    def test_outside_the_window_is_never_deferred(self):
        for moment in (local(18, 12, 0), local(18, 3, 0), local(18, 7, 5)):
            self.lock_calls.clear()
            self.run_scheduled(moment, 100.0)
            self.assertEqual(len(self.lock_calls), 1, moment)
        self.assertEqual(self.cpu_calls, [])
        self.assertNotIn("deferred", self.log_text())

    def test_window_crossing_midnight_belongs_to_the_previous_night(self):
        self.write_config(memspec.DREAM_MODE_NIGHTLY, "22:00")
        self.run_scheduled(local(18, 0, 30), 90.0)
        self.assertEqual(self.lock_calls, [])
        self.assertIn("next try in 30 min", self.log_text())
        self.write_completed(local(17, 22, 5))
        self.run_scheduled(local(18, 1, 0), 10.0)
        self.assertEqual(self.lock_calls, [])
        self.assertIn("skipped: already ran this night", self.log_text())

    def test_only_nightly_mode_has_a_retry_to_wait_for(self):
        self.write_config(memspec.DREAM_MODE_PIGGYBACK, "03:30")
        self.run_scheduled(local(18, 4, 0), 100.0)
        self.assertEqual(len(self.lock_calls), 1)
        self.assertEqual(self.cpu_calls, [])

    def test_lock_handoff_path_is_not_gated(self):
        with patch.object(dream, "_local_now", return_value=local(18, 4, 0)), \
                patch.object(dream, "system_cpu_percent", side_effect=AssertionError("measured")), \
                patch.object(dream, "_handoff_lock", return_value=False):
            self.assertEqual(dream.main([memspec.DREAM_SCHEDULED_FLAG, memspec.DREAM_LOCK_HELD_FLAG,
                                         "--lock-token", "x"], output=io.StringIO()), 0)
        self.assertIn("lock handoff unavailable", self.log_text())

    def test_cpu_measurement_returns_a_percentage(self):
        value = dream.system_cpu_percent(0.2)
        self.assertIsNotNone(value)
        self.assertGreaterEqual(value, 0.0)
        self.assertLessEqual(value, 100.0)

    def test_cpu_measurement_failure_is_not_busy(self):
        target = "ctypes.windll" if os.name == "nt" else "os.getloadavg"
        with patch(target, side_effect=OSError("unavailable"), create=True):
            self.assertIsNone(dream.system_cpu_percent(0))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
