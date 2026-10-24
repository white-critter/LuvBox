"""A save stopped by a damaged records database can be repaired from the window."""

from contextlib import closing
from importlib.util import find_spec
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from tests.test_recovery import damage_media_constraint
from luvbox.core import JobOptions, run_worker
from luvbox.history import HistoryStore
from luvbox.storage import resolve_storage


_QT_AVAILABLE = find_spec("PySide6") is not None
if _QT_AVAILABLE:
    from PySide6.QtCore import QCoreApplication
    from luvbox.app_settings import default_settings
    from luvbox.qt_app import ApplicationController


def damaged_records(output: Path, account: str) -> Path:
    """Saved records whose media table now fails SQLite's own checks."""
    paths = resolve_storage(output, account)
    with closing(HistoryStore.for_storage(paths)) as store:
        for index in range(3):
            path = paths.media_dir / f"{1000 + index}_1.jpg"
            path.write_bytes(b"offline")
            store.record_media("flat", {"tweet_id": str(1000 + index), "num": 1, "type": "photo",
                                        "author": {"name": "alice"}}, path)
    live = paths.record_dir / ".luvbox-history.sqlite3"
    damage_media_constraint(live)
    return live


class WorkerDamageFlagTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        state = patch("luvbox.protection.state_directory", return_value=self.base / "protection")
        state.start()
        self.addCleanup(state.stop)
        self.output = self.base / "photos"

    def test_damaged_catalog_is_reported_with_the_repair_flag(self):
        damaged_records(self.output, "myaccount")
        events = []
        with patch("gallery_dl.extractor.find", side_effect=AssertionError("no request")):
            code = run_worker(JobOptions("myaccount", str(self.output)), events.append)
        self.assertEqual(code, 1)
        self.assertEqual((events[-1]["type"], events[-1]["status"]), ("finished", "error"))
        self.assertIs(events[-1]["catalog_damaged"], True)
        self.assertIn("기록 복구", events[-1]["message"])

    def test_other_failures_do_not_offer_repair(self):
        events = []
        run_worker(JobOptions("not a handle!", str(self.output)), events.append)
        self.assertEqual(events[-1]["status"], "error")
        self.assertIs(events[-1]["catalog_damaged"], False)


@unittest.skipUnless(_QT_AVAILABLE, "optional Qt dependencies are not installed")
class WindowRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-qt-repair-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.folder)})
        environment.start()
        self.addCleanup(environment.stop)
        self.output = self.folder / "media"
        self.live = damaged_records(self.output, "myaccount")
        self.settings = Mock()
        self.settings.load.return_value = {**default_settings(), "account": "myaccount",
                                           "output_dir": str(self.output)}
        for target, kwargs in (
                ("luvbox.qt_app.SettingsRepository", {"return_value": self.settings}),
                ("luvbox.qt_accounts.AccountController._read_state", {"new": staticmethod(lambda: {
                    "accounts": [], "identity": {}, "connected": True})}),
                ("luvbox.qt_schedule.task_status", {"return_value": {"registered": False}})):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.controller = ApplicationController()
        self.addCleanup(self.close_controller)

    def wait_for(self, condition, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if condition():
                return
            time.sleep(.005)
        self.fail("Qt repair did not settle")

    def close_controller(self):
        self.controller.requestClose()
        self.wait_for(lambda: self.controller.shutdownReady)

    def report_damage(self):
        # Start a real save up to the worker, which then reports the damage.
        self.controller.download.start = Mock(return_value=True)
        self.controller.startSave()
        self.wait_for(lambda: self.controller.download.start.called and not self.controller.busy)
        self.controller.download._handle({"type": "finished", "status": "error", "catalog_damaged": True,
                                          "message": "저장 기록 DB가 손상되어 기록 복구가 필요합니다."})
        self.assertTrue(self.controller.download.repairNeeded)

    def test_repair_is_offered_only_after_the_damage_report(self):
        self.controller.repairRecords()
        self.assertFalse(self.controller.busy)
        self.controller.download._handle({"type": "finished", "status": "error", "message": "다른 오류"})
        self.assertFalse(self.controller.download.repairNeeded)

    def test_repair_targets_the_failed_save_even_after_a_folder_change(self):
        self.report_damage()
        other = self.folder / "elsewhere"
        self.controller.setOutputFolder(str(other))
        self.wait_for(lambda: not self.controller.busy)
        self.controller.repairRecords()
        self.wait_for(lambda: not self.controller.busy)
        self.assertEqual(self.controller.error, "")
        with closing(HistoryStore(self.live.parent)) as store:
            self.assertEqual(len(store.records()), 2)
        self.assertFalse(resolve_storage(other, "myaccount").record_dir.exists())

    def test_repair_rebuilds_the_records_and_clears_the_error(self):
        self.report_damage()
        self.controller.repairRecords()
        self.assertTrue(self.controller.busy)
        self.wait_for(lambda: not self.controller.busy)
        self.assertEqual(self.controller.error, "")
        self.assertFalse(self.controller.download.repairNeeded)
        self.assertTrue(self.controller.status.startswith("기록 복구를 마쳤습니다"), self.controller.status)
        with closing(HistoryStore(self.live.parent)) as store:
            # The fixture destroyed one row's identity and no backup exists
            # yet; the intact rows are rebuilt into a healthy database.
            self.assertEqual(sorted(row["tweet_id"] for row in store.records()), ["1001", "1002"])
        kept = [path for path in (self.folder / "LuvBox" / "backups").glob("myaccount-*-damaged")]
        self.assertEqual(len(kept), 1)
        self.assertTrue((kept[0] / self.live.name).is_file())

    def test_a_running_save_of_the_account_keeps_the_damaged_records(self):
        from luvbox.protection import _BUSY_HELP, AccountGuard

        self.report_damage()
        before = self.live.read_bytes()
        guard = AccountGuard("myaccount", sleep=lambda *_: None)
        guard.acquire(respect_cooldown=False)
        try:
            self.controller.repairRecords()
            self.wait_for(lambda: not self.controller.busy)
        finally:
            guard.close()
        self.assertEqual(self.controller.error, _BUSY_HELP)
        self.assertTrue(self.controller.download.repairNeeded)
        self.assertEqual(self.live.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
