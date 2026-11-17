"""Regression checks for the 1.11.2 inspection fixes."""

from importlib.util import find_spec
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests import app_data_isolation  # noqa: E402,F401


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class ScheduledRunNoticeTests(unittest.TestCase):
    """The program stays in the tray since 1.11.0; a scheduled save ending meanwhile must reach the gallery."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtCore import QCoreApplication

        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        from luvbox.app_settings import default_settings
        from luvbox.qt_app import ApplicationController

        temporary = tempfile.TemporaryDirectory(prefix="luvbox-scheduled-notice-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        settings = Mock()
        settings.load.return_value = {**default_settings(), "account": "example",
                                      "output_dir": str(self.folder / "media")}
        for target, kwargs in (
                ("luvbox.qt_app.SettingsRepository", {"return_value": settings}),
                ("luvbox.qt_accounts.AccountController._read_state",
                 {"new": staticmethod(lambda: {"accounts": [], "identity": {}, "connected": True})}),
                ("luvbox.qt_schedule.task_status", {"return_value": {"registered": False}})):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.folder)})
        environment.start()
        self.addCleanup(environment.stop)
        self.write(0)                               # a run from before this start
        self.controller = ApplicationController()
        self.addCleanup(self.close_controller)
        self.controller._library.refresh = Mock()
        self.controller._library.classifyNewPhotos = Mock()
        self.controller._records.refresh = Mock()

    def close_controller(self):
        self.controller.requestClose()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not self.controller.shutdownReady:
            self.application.processEvents()
            time.sleep(.002)
        self.assertTrue(self.controller.shutdownReady)

    def write(self, saved, *, later=1):
        from luvbox.scheduled import last_run_path

        path = last_run_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"status": "finished", "saved": saved, "seen": False,
                                    "finished_at": "2026-11-18T07:30:00+09:00"}), encoding="utf-8")
        stamp = time.time_ns() + later * 1_000_000_000
        os.utime(path, ns=(stamp, stamp))

    def test_it_looks_now_and_then_and_stops_when_closing(self):
        timer = self.controller._scheduled_timer
        self.assertTrue(timer.isActive())
        self.assertEqual(timer.interval(), 60_000)
        self.controller.requestClose()
        self.assertFalse(timer.isActive())

    def test_nothing_happens_while_no_run_ended(self):
        self.controller.checkScheduledRun()
        self.controller._records.refresh.assert_not_called()
        self.controller._library.refresh.assert_not_called()

    def test_photos_saved_while_another_screen_shows_are_listed_at_once(self):
        self.controller.galleryVisible(False)
        self.write(3)
        self.controller.checkScheduledRun()
        self.controller._library.refresh.assert_called_once_with()
        self.controller._library.classifyNewPhotos.assert_called_once_with()
        self.assertFalse(self.controller.galleryChanged)
        self.controller.checkScheduledRun()          # the same run is not taken twice
        self.controller._library.refresh.assert_called_once_with()

    def test_photos_saved_while_the_gallery_shows_light_the_refresh_button(self):
        self.controller.galleryVisible(True)
        self.write(2)
        self.controller.checkScheduledRun()
        self.controller._library.refresh.assert_not_called()
        self.assertTrue(self.controller.galleryChanged)
        self.controller.refreshGallery()
        self.assertFalse(self.controller.galleryChanged)

    def test_a_run_that_saved_nothing_updates_only_the_records(self):
        self.write(0, later=2)
        self.controller.checkScheduledRun()
        self.controller._records.refresh.assert_called_once_with()
        self.controller._library.refresh.assert_not_called()
        self.controller._library.classifyNewPhotos.assert_not_called()
        self.assertFalse(self.controller.galleryChanged)

    def test_a_closing_program_does_not_look(self):
        self.write(5)
        self.controller.requestClose()
        self.controller.checkScheduledRun()
        self.controller._library.refresh.assert_not_called()

    def test_opening_the_window_from_the_tray_looks_at_once(self):
        from luvbox.qt_tray import TrayController

        window = Mock()
        window.visibility.return_value = "visible"
        tray = TrayController.__new__(TrayController)
        tray._window, tray._controller = window, Mock(closing=False)
        TrayController.show(tray)
        tray._controller.checkScheduledRun.assert_called_once_with()


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class DeleteSummaryTests(unittest.TestCase):
    def summary(self, count):
        from luvbox.qt_management import ManagementController

        rows = [{"path": f"C:/media/photo_{index}.jpg", "filename": f"photo_{index}.jpg"} for index in range(count)]
        shown = type("Shown", (), {"_delete": {"rows": rows}, "deleteCount": count})()
        return ManagementController.deleteSummary.fget(shown).splitlines()

    def test_a_long_selection_says_how_many_names_are_not_listed(self):
        lines = self.summary(7)
        self.assertEqual(lines[0], "선택한 파일 7개를 Windows 휴지통으로 이동합니다.")
        self.assertEqual(lines[1:6], [f"photo_{index}.jpg" for index in range(5)])
        self.assertEqual(lines[6:], ["외 2개"])

    def test_a_short_selection_lists_every_name_and_nothing_more(self):
        lines = self.summary(5)
        self.assertEqual(lines[1:], [f"photo_{index}.jpg" for index in range(5)])
        self.assertEqual(self.summary(1)[1:], ["photo_0.jpg"])

    def test_large_counts_are_grouped(self):
        self.assertEqual(self.summary(1234)[-1], "외 1,229개")


if __name__ == "__main__":
    unittest.main()
