"""Actual encrypted account selection and shell preferences stay coherent."""

from copy import deepcopy
from importlib.util import find_spec
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

_QT = find_spec("PySide6") is not None
if _QT:
    from PySide6.QtCore import QCoreApplication
    from luvbox import login
    from luvbox.app_settings import default_settings
    from luvbox.core import JobOptions
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_download import DownloadController


class _MemorySettings:
    def __init__(self, folder):
        self.lock = threading.Lock()
        self.calls = []
        self.fail_all = False
        self.fail_calls = set()
        self.values = {**default_settings(), "account": "oldaccount",
                       "output_dir": str(folder / "old-output"),
                       "record_locations": {"oldaccount": str(folder / "old-output"),
                                            "newaccount": str(folder / "new-output")}}

    def load(self):
        with self.lock:
            return deepcopy(self.values)

    def update(self, changes):
        with self.lock:
            self.calls.append(dict(changes))
            if self.fail_all or len(self.calls) in self.fail_calls:
                raise OSError("synthetic settings failure")
            self.values.update(changes)
            return deepcopy(self.values)


class _OfflineService:
    job_id = 7
    def __init__(self):
        self.pending = []
        self.started = []
    def start(self, options): self.started.append(options); return True
    def drain(self, _limit): return [self.pending.pop(0)] if self.pending else []
    def stop(self): pass
    def set_review_enabled(self, _enabled): pass
    def close(self):
        if self.started:
            self.pending.extend([{"type": "finished", "job_id": self.job_id, "status": "stopped"},
                                 {"type": "process_exit", "job_id": self.job_id, "code": 0}])


@unittest.skipUnless(_QT and os.name == "nt", "Windows DPAPI and Qt are required")
class AccountSettingsConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-account-consistency-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": str(self.folder),
                                                  "APPDATA": str(self.folder / "roaming")}))
        self.old = {"user_id": "101", "handle": "oldaccount", "display_name": "Old account"}
        self.new = {"user_id": "202", "handle": "newaccount", "display_name": "New account"}
        login.save_authenticated_session("synthetic-old-cookie", self.old)
        login.save_authenticated_session("synthetic-new-cookie", self.new)
        login.select_account(self.old["user_id"])
        self.settings = _MemorySettings(self.folder)
        self.service = _OfflineService()
        self.enter_context(patch("luvbox.qt_app.SettingsRepository", return_value=self.settings))
        self.enter_context(patch("luvbox.qt_app.DownloadController", side_effect=lambda parent:
                                 DownloadController(parent, service=self.service)))
        self.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        self.enter_context(patch("luvbox.qt_browser_setup.available_browsers", return_value=[]))
        self.enter_context(patch.object(ApplicationController, "_prepare_save", new=staticmethod(
            lambda snapshot, _consents: {"options": JobOptions(snapshot["account"], snapshot["output_dir"])})))
        self.controller = ApplicationController()
        self.addCleanup(self.close)
        self.wait(lambda: not self.controller.busy and not self.controller.browserSetup.busy)

    def enter_context(self, context):
        value = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return value

    def wait(self, predicate, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.002)
        self.fail("account consistency operation did not settle")

    def close(self):
        self.controller.requestClose()
        self.wait(lambda: self.controller.shutdownReady)

    def switch(self):
        self.assertTrue(self.controller.accounts.selectAccount(self.new["user_id"]))
        self.wait(lambda: not self.controller.busy)

    def assert_active_new(self):
        self.assertEqual(self.controller.account, "newaccount")
        self.assertEqual(self.controller.accounts.identity["handle"], "newaccount")
        self.assertEqual(login.active_account()["handle"], "newaccount")
        self.assertEqual(self.controller._preferences["account"], "newaccount")

    def test_failed_settings_keep_actual_login_and_retry_the_visible_folder_safely(self):
        previous_output = self.controller.outputFolder
        self.settings.fail_all = True
        self.switch()
        self.assert_active_new()
        self.assertEqual(self.controller.outputFolder, previous_output)
        self.assertEqual(self.settings.values["account"], "oldaccount")
        self.assertEqual(self.controller._committed_preferences["account"], "oldaccount")
        self.assertIn("설정을 저장하지 못했습니다", self.controller.error)
        self.assertEqual(len(self.settings.calls), 1)
        self.controller.startSave()
        self.wait(lambda: not self.controller.busy)
        self.assertFalse(self.service.started)
        self.assertNotIn("다시 선택", self.controller.error)
        self.assert_active_new()
        self.settings.fail_all = False
        self.controller.startSave()
        self.wait(lambda: bool(self.service.started))
        self.assertEqual(len(self.service.started), 1)
        self.assertEqual(self.service.started[0].account, "newaccount")
        self.assertEqual(self.service.started[0].output_dir, previous_output)
        self.assertEqual(self.settings.values["account"], "newaccount")
        self.assertEqual(self.settings.values["output_dir"], previous_output)

    def test_account_and_folder_are_one_settings_commit_without_a_second_write(self):
        # The prior implementation wrote the account once, then account+folder
        # again. Failure of that second write left the UI and DPAPI disagreeing.
        self.settings.fail_calls = {2}
        self.switch()
        self.assert_active_new()
        self.assertEqual(len(self.settings.calls), 1)
        self.assertEqual(self.settings.calls[0]["account"], "newaccount")
        self.assertEqual(self.settings.calls[0]["output_dir"], str(self.folder / "new-output"))
        self.assertEqual(self.controller.outputFolder, str(self.folder / "new-output"))
        self.assertEqual(self.settings.values["output_dir"], str(self.folder / "new-output"))
        self.assertEqual(self.controller._committed_preferences["account"], "newaccount")
        self.assertEqual(self.controller.error, "")

    def test_mismatched_screen_identity_cannot_enable_save(self):
        self.switch()
        self.controller._account = "oldaccount"
        self.assertFalse(self.controller.canStart)
        self.controller.startSave()
        self.assertIn("다시 선택", self.controller.error)
        self.assertFalse(self.service.started)

    def test_single_commit_also_remembers_the_chosen_browser(self):
        self.controller.browserSetup._selected_browser = "chrome"
        self.switch()
        self.assertEqual(len(self.settings.calls), 1)
        self.assertEqual(self.settings.calls[0]["connection_browser"], "chrome")
        self.assertEqual(self.settings.values["connection_browser"], "chrome")


if __name__ == "__main__":
    unittest.main()
