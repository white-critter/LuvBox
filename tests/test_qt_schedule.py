"""Schedule edits use a fresh, verified native state without touching real tasks."""

from copy import deepcopy
import importlib.util
import threading
import time
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # noqa: F401

_QT = importlib.util.find_spec("PySide6") is not None
if _QT:
    from PySide6.QtCore import QCoreApplication
    from luvbox.qt_schedule import ScheduleController


@unittest.skipUnless(_QT, "Qt dependencies are not installed")
class ScheduleControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.native = {"registered": False}
        reader = patch("luvbox.qt_schedule.task_status", side_effect=lambda: deepcopy(self.native))
        self.reader = reader.start()
        self.addCleanup(reader.stop)
        register = patch("luvbox.qt_schedule.register_task", side_effect=self.register)
        self.registered = register.start()
        self.addCleanup(register.stop)
        remove = patch("luvbox.qt_schedule.unregister_task", side_effect=self.remove)
        self.removed = remove.start()
        self.addCleanup(remove.stop)
        self.controller = ScheduleController()
        self.addCleanup(self.close)
        self.wait(lambda: self.controller.known and not self.controller.busy)

    def wait(self, condition, seconds=4):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(0.005)
        self.fail("schedule fixture did not finish")

    def close(self):
        self.controller.shutdown()
        self.wait(lambda: not self.controller._worker.is_alive())
        self.app.processEvents()

    def register(self, value):
        self.native = {"registered": True, "enabled": True, "current": True, "running": False,
                       "time": value, "program": "fixture.exe", "catch_up": True, "on_battery": True,
                       "next_run": "2026-10-28T09:00:00", "last_run": "2026-10-27T09:00:00"}

    def remove(self):
        self.native = {"registered": False}

    def test_create_and_time_edit_verify_registered_task(self):
        self.assertFalse(self.controller.registered)
        self.assertTrue(self.controller.create("7:30"))
        self.wait(lambda: not self.controller.busy)
        self.assertEqual((self.controller.registered, self.controller.current, self.controller.time), (True, True, "07:30"))
        self.assertEqual((self.controller.catchUp, self.controller.onBattery), (True, True))
        self.assertEqual(self.controller.nextRun, "2026-10-28T09:00:00")
        self.assertEqual(self.controller.lastRun, "2026-10-27T09:00:00")
        self.assertTrue(self.controller.changeTime("18:45"))
        self.wait(lambda: not self.controller.busy)
        self.assertEqual(self.controller.time, "18:45")
        self.assertEqual([call.args[0] for call in self.registered.call_args_list], ["07:30", "18:45"])

    def test_invalid_time_never_calls_native_scheduler(self):
        self.assertFalse(self.controller.create("24:00"))
        self.assertFalse(self.controller.busy)
        self.assertIn("24시간", self.controller.error)
        self.registered.assert_not_called()

    def test_locked_mutations_are_refused_but_reading_still_works(self):
        self.controller.setLocked(True)
        self.assertFalse(self.controller.create("09:00"))
        self.assertTrue(self.controller.refresh())
        self.wait(lambda: not self.controller.busy)
        self.assertTrue(self.controller.known)
        self.registered.assert_not_called()

    def test_stale_state_cannot_overwrite_another_windows_task_change(self):
        expected = dict(self.controller._state)
        self.register("11:00")
        state, message = self.controller._perform("create", "09:00", expected)
        self.assertEqual(state["time"], "11:00")
        self.assertIn("상태가 바뀌었습니다", message)
        self.registered.assert_not_called()

    def test_changed_task_name_or_arguments_are_not_overwritten(self):
        self.register("09:00")
        self.native.update(current=False, name="old-task", arguments="old-arguments")
        self.controller.refresh()
        self.wait(lambda: not self.controller.busy)
        for field, value in (("name", "changed-task"), ("arguments", "changed-arguments")):
            with self.subTest(field=field):
                expected = dict(self.controller._state)
                original = self.native[field]
                self.native[field] = value
                _, message = self.controller._perform("update", "10:00", expected)
                self.assertIn("상태가 바뀌었습니다", message)
                self.registered.assert_not_called()
                self.native[field] = original

    def test_running_task_allows_future_removal_without_stopping_it(self):
        self.register("09:00")
        self.native["running"] = True
        self.controller.refresh()
        self.wait(lambda: not self.controller.busy)
        self.assertFalse(self.controller.changeTime("10:00"))
        self.assertFalse(self.controller.useCurrentVersion())
        self.assertTrue(self.controller.remove())
        self.wait(lambda: not self.controller.busy)
        self.assertFalse(self.controller.registered)
        self.assertIn("진행 중인 저장은 계속", self.controller.status)
        self.removed.assert_called_once()

    def test_old_version_can_be_migrated_with_time_preserved(self):
        self.register("06:30")
        self.native.update(current=False, program="older.exe")
        self.controller.refresh()
        self.wait(lambda: not self.controller.busy)
        self.assertTrue(self.controller.useCurrentVersion())
        self.wait(lambda: not self.controller.busy)
        self.registered.assert_called_once_with("06:30")
        self.assertTrue(self.controller.current)

    def test_failed_refresh_preserves_the_previous_state_and_recovers(self):
        self.register("09:00")
        self.controller.refresh()
        self.wait(lambda: not self.controller.busy)
        with patch("luvbox.qt_schedule.task_status", side_effect=OSError("private content")):
            self.controller.refresh()
            self.wait(lambda: not self.controller.busy)
        self.assertEqual((self.controller.known, self.controller.registered, self.controller.time), (False, True, "09:00"))
        self.assertNotIn("private content", self.controller.error)
        self.assertFalse(self.controller.changeTime("11:00"))
        self.controller.refresh()
        self.wait(lambda: not self.controller.busy)
        self.assertTrue(self.controller.known)
        self.assertEqual(self.controller.error, "")

    def test_registration_must_be_reflected_before_success(self):
        with patch("luvbox.qt_schedule.register_task"):
            self.controller.create("09:00")
            self.wait(lambda: not self.controller.busy)
        self.assertFalse(self.controller.known)
        self.assertFalse(self.controller.registered)
        self.assertTrue(self.controller.error)


if __name__ == "__main__":
    unittest.main()
