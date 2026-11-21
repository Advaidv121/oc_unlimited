"""Watchdog decisions: bring a dead worker back, leave a healthy one alone."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import ouc_watchdog


class WatchdogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.log_file = base / "watchdog.log"
        self.hb = base / "heartbeat.txt"
        self.hb.write_text(str(int(time.time())), encoding="utf-8")
        self.worker = {
            "name": "guard",
            "script": "zen_guard.py",
            "mutex": "OpencodeUnlimitedGuard",
            "task": "OpencodeUnlimited-Guard",
            "heartbeat": self.hb,
        }
        self._patches = [
            mock.patch.object(ouc_watchdog, "WATCHDOG_LOG_FILE", self.log_file),
            mock.patch.object(ouc_watchdog, "task_enabled", return_value=True),
            mock.patch.object(ouc_watchdog, "_start_task", return_value=True),
            mock.patch.object(ouc_watchdog, "_kill_script", return_value=1),
            mock.patch.object(ouc_watchdog, "time"),
        ]
        self.mocks = [p.start() for p in self._patches]
        self.time = self.mocks[4]
        self.time.time.return_value = time.time()
        self.time.sleep.return_value = None

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_running_worker_with_a_fresh_heartbeat_is_left_alone(self) -> None:
        with mock.patch.object(ouc_watchdog, "is_mutex_held", return_value=True):
            self.assertEqual(ouc_watchdog.check(self.worker), "")
        ouc_watchdog._start_task.assert_not_called()
        ouc_watchdog._kill_script.assert_not_called()

    def test_dead_worker_is_started(self) -> None:
        with mock.patch.object(ouc_watchdog, "is_mutex_held", return_value=False):
            action = ouc_watchdog.check(self.worker)

        self.assertIn("started guard", action)
        ouc_watchdog._start_task.assert_called_once_with("OpencodeUnlimited-Guard")
        ouc_watchdog._kill_script.assert_not_called()

    def test_wedged_worker_is_killed_then_started(self) -> None:
        stamp = time.time() - ouc_watchdog.STALE_SECONDS - 60
        import os

        os.utime(self.hb, (stamp, stamp))

        with mock.patch.object(ouc_watchdog, "is_mutex_held", return_value=True):
            action = ouc_watchdog.check(self.worker)

        self.assertIn("restarted guard", action)
        ouc_watchdog._kill_script.assert_called_once_with("zen_guard.py")
        ouc_watchdog._start_task.assert_called_once_with("OpencodeUnlimited-Guard")

    def test_disabled_task_is_never_started(self) -> None:
        with mock.patch.object(ouc_watchdog, "task_enabled", return_value=False):
            self.assertEqual(ouc_watchdog.check(self.worker), "")
        ouc_watchdog._start_task.assert_not_called()

    def test_missing_heartbeat_on_a_running_worker_counts_as_healthy(self) -> None:
        self.hb.unlink()
        with mock.patch.object(ouc_watchdog, "is_mutex_held", return_value=True):
            self.assertEqual(ouc_watchdog.check(self.worker), "")

    def test_main_writes_one_line_per_action(self) -> None:
        with mock.patch.object(ouc_watchdog, "WORKERS", (self.worker,)), mock.patch.object(
            ouc_watchdog, "is_mutex_held", return_value=False
        ), mock.patch.object(ouc_watchdog, "ensure_dirs"):
            self.assertEqual(ouc_watchdog.main(), 0)

        text = self.log_file.read_text(encoding="utf-8")
        self.assertEqual(text.count("started guard"), 1)


if __name__ == "__main__":
    unittest.main()
