"""Settings failures must not silently change a save or the live controls."""

from importlib.util import find_spec
import os
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch


_QT_AVAILABLE = find_spec("PySide6") is not None
if _QT_AVAILABLE:
    from PySide6.QtCore import QCoreApplication
    from luvbox.app_settings import default_settings
    from luvbox.core import JobOptions
    from luvbox.qt_app import ApplicationController


@unittest.skipUnless(_QT_AVAILABLE, "optional Qt dependencies are not installed")
class SettingsCommitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-qt-settings-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.entered, self.release = Event(), Event()
        self.addCleanup(self.release.set)
        self.settings = Mock()
        self.settings.load.return_value = {
            **default_settings(), "account": "example", "output_dir": str(self.folder / "media")}
        self.patch("luvbox.qt_app.SettingsRepository", return_value=self.settings)
        self.patch("luvbox.qt_accounts.AccountController._read_state", new=staticmethod(lambda: {
            "accounts": [], "identity": {}, "connected": True}))
        self.patch("luvbox.qt_schedule.task_status", return_value={"registered": False})
        self.patch_environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.folder)})
        self.patch_environment.start()
        self.addCleanup(self.patch_environment.stop)
        self.controller = ApplicationController()
        self.controller.download.start = Mock(return_value=True)
        self.addCleanup(self.close_controller)

    def patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def wait_for(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if condition():
                return
            time.sleep(.002)
        self.fail("Qt settings operation did not settle")

    def close_controller(self):
        self.release.set()
        self.controller.requestClose()
        self.wait_for(lambda: self.controller.shutdownReady)

    def blocked_update(self, changes):
        self.entered.set()
        if not self.release.wait(4):
            raise TimeoutError("fixture was not released")
        raise OSError("private settings contents must not be displayed")

    def test_failed_folder_change_restores_previous_folder_and_unlocks_controls(self):
        previous = self.controller.outputFolder
        self.settings.update.side_effect = self.blocked_update
        self.controller.setOutputFolder(str(self.folder / "other"))
        self.assertTrue(self.entered.wait(1))
        self.assertTrue(self.controller.busy)
        self.assertFalse(self.controller.canStart)
        self.release.set()
        self.wait_for(lambda: not self.controller.busy)
        self.assertEqual(self.controller.outputFolder, previous)
        self.assertEqual(self.controller._preferences["output_dir"], previous)
        self.assertTrue(self.controller.canStart)
        self.assertIn("설정을 저장하지 못했습니다", self.controller.error)
        self.assertNotIn("private", self.controller.error)

    def test_failed_save_preferences_never_start_the_download(self):
        options = JobOptions("example", self.controller.outputFolder)
        self.patch("luvbox.qt_app.ApplicationController._prepare_save",
                   new=staticmethod(lambda *_args: {"options": options}))
        self.settings.update.side_effect = self.blocked_update
        self.controller.startSave()
        self.wait_for(lambda: self.entered.is_set())
        self.controller.download.start.assert_not_called()
        self.release.set()
        self.wait_for(lambda: not self.controller.busy)
        self.controller.download.start.assert_not_called()
        self.assertIsNone(self.controller._pending_download)

    def test_successful_preferences_commit_starts_the_download_once(self):
        options = JobOptions("example", self.controller.outputFolder)
        self.patch("luvbox.qt_app.ApplicationController._prepare_save",
                   new=staticmethod(lambda *_args: {"options": options}))
        self.controller.startSave()
        self.wait_for(lambda: self.controller.download.start.called)
        self.controller.download.start.assert_called_once_with(options)
        self.assertFalse(self.controller.busy)

    def test_save_click_waits_for_the_account_edit_to_commit(self):
        self.patch("luvbox.qt_app.ApplicationController._prepare_save", new=staticmethod(
            lambda snapshot, _consents: {"options": JobOptions(snapshot["account"], snapshot["output_dir"])}))
        self.controller.setAccount("another")
        self.controller.startSave()
        self.wait_for(lambda: self.controller.download.start.called)
        options = self.controller.download.start.call_args.args[0]
        self.assertEqual(options.account, "another")
        self.controller.download.start.assert_called_once()

    def test_failed_account_edit_cancels_the_waiting_save_click(self):
        self.settings.update.side_effect = self.blocked_update
        self.controller.setAccount("another")
        self.assertTrue(self.entered.wait(1))
        self.controller.startSave()
        self.release.set()
        self.wait_for(lambda: not self.controller.busy)
        self.assertEqual(self.controller.account, "example")
        self.controller.download.start.assert_not_called()
        self.assertFalse(self.controller._save_after_settings)

    def test_close_cancels_a_download_waiting_for_settings_commit(self):
        options = JobOptions("example", self.controller.outputFolder)
        self.patch("luvbox.qt_app.ApplicationController._prepare_save",
                   new=staticmethod(lambda *_args: {"options": options}))

        def commit(changes):
            self.entered.set()
            self.release.wait(4)

        self.settings.update.side_effect = commit
        self.controller.startSave()
        self.wait_for(lambda: self.entered.is_set())
        self.controller.requestClose()
        self.release.set()
        self.wait_for(lambda: self.controller.shutdownReady)
        self.controller.download.start.assert_not_called()

    def test_failed_older_toggle_does_not_replace_a_newer_successful_toggle(self):
        self.settings.update.side_effect = self.blocked_update
        self.controller.setPreference("confirm_each", True)
        self.assertTrue(self.entered.wait(1))
        self.controller.setPreference("confirm_each", False)
        # The first call already entered blocked_update; only the second call
        # uses this replacement side effect after the first is released.
        self.settings.update.side_effect = lambda changes: None
        self.release.set()
        self.wait_for(lambda: not self.controller.busy)
        self.assertFalse(self.controller.preferences["confirm_each"])
        self.assertFalse(self.controller.download._service._review_enabled)
        self.assertEqual(self.controller.error, "")

    def test_two_failed_toggles_restore_the_last_committed_value(self):
        self.settings.update.side_effect = self.blocked_update
        self.controller.setPreference("confirm_each", True)
        self.assertTrue(self.entered.wait(1))
        self.controller.setPreference("confirm_each", False)
        self.release.set()
        self.wait_for(lambda: not self.controller.busy)
        self.assertFalse(self.controller.preferences["confirm_each"])
        self.assertFalse(self.controller.download._service._review_enabled)
