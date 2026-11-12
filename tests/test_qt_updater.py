"""The update notice's controller: when it checks, what it shows, and the install on a click."""

from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from luvbox import updater
from tests.release_server import ReleaseServer, make_program


_QT_AVAILABLE = find_spec("PySide6") is not None
if _QT_AVAILABLE:
    from PySide6.QtCore import QCoreApplication
    from luvbox.app_settings import default_settings
    from luvbox.qt_updater import UpdateController


@unittest.skipUnless(_QT_AVAILABLE, "optional Qt dependencies are not installed")
class UpdateControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.server = ReleaseServer()
        self.addCleanup(self.server.close)
        self.folder = Path(tempfile.mkdtemp(prefix="luvbox-qt-updater-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.launcher = self.folder / "place" / "LuvBox.exe"
        self.launcher.parent.mkdir()
        self.old = make_program("1.8.0")
        self.launcher.write_bytes(self.old)
        self.settings = Mock()
        self.controllers = []

    def controller(self, *, launcher=True, current="1.8.0", **preferences):
        made = UpdateController(settings=self.settings, preferences={**default_settings(), **preferences},
                                current=current, source=self.server.source,
                                launcher=(lambda: self.launcher) if launcher else (lambda: None),
                                folder=self.folder / "updates")
        self.controllers.append(made)
        self.addCleanup(self.close, made)
        return made

    def close(self, controller):
        controller.shutdown()
        self.wait(lambda: controller._shutdown_complete)

    def wait(self, condition, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if condition():
                return
            time.sleep(0.005)
        self.fail("The update controller did not settle")

    def paths(self):
        return [path for path, _headers in self.server.requests]

    def test_nothing_is_requested_until_asked_and_never_when_the_daily_check_is_off_or_recent(self):
        self.server.publish("1.9.0")
        recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        for preferences in ({"update_check": False}, {"update_checked_at": recent}):
            with self.subTest(preferences=preferences):
                controller = self.controller(**preferences)
                controller.startAutomaticCheck()
                for _ in range(40):
                    self.application.processEvents()
                    time.sleep(0.005)
                self.assertEqual(self.paths(), [])
                self.assertEqual(controller.state, "idle")
                self.assertFalse(controller.noticeVisible)
        # Making the controller, reading its properties and closing it sends nothing either.
        self.assertEqual(self.controller().status, "아직 확인하지 않았어요")
        self.assertEqual(self.paths(), [])

    def test_the_daily_check_finds_a_release_shows_it_and_remembers_it(self):
        program = self.server.publish("1.9.0")
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        controller = self.controller(update_checked_at=old)
        controller.startAutomaticCheck()
        self.wait(lambda: controller.state == "available")
        self.assertEqual(self.paths(), ["/latest"])
        self.assertTrue(controller.noticeVisible)
        self.assertEqual((controller.version, controller.notes, controller.canInstall),
                         ("1.9.0", "· 첫째: 고친 것\n· 둘째", True))
        self.assertEqual(controller.status, "새 버전이 나왔어요 · 1.9.0")
        self.wait(lambda: self.settings.update.called)
        saved = self.settings.update.call_args.args[0]
        self.assertEqual(saved["update_found"], {"version": "1.9.0", "notes": "· 첫째: 고친 것\n· 둘째",
                                                 "installable": True, "size": len(program)})
        self.assertFalse(updater.check_due(True, saved["update_checked_at"]))
        # Hidden for this session by "later"; settings still show it.
        controller.dismiss()
        self.assertFalse(controller.noticeVisible)
        self.assertEqual(controller.status, "새 버전이 나왔어요 · 1.9.0")

    def test_a_release_found_earlier_is_shown_at_start_without_asking_again(self):
        recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        remembered = {"version": "1.9.0", "notes": "· 기억한 변경", "installable": True, "size": 2_000_000}
        controller = self.controller(update_checked_at=recent, update_found=remembered)
        self.assertFalse(controller.noticeVisible)  # only once the window is up
        controller.startAutomaticCheck()
        self.assertTrue(controller.noticeVisible)
        self.assertEqual((controller.state, controller.version, controller.notes), ("available", "1.9.0", "· 기억한 변경"))
        self.assertEqual(self.paths(), [])
        # Already running that version or a later one: the stale note is ignored.
        for current in ("1.9.0", "1.10.0"):
            other = self.controller(current=current, update_checked_at=recent, update_found=remembered)
            other.startAutomaticCheck()
            self.assertFalse(other.noticeVisible)
            self.assertEqual(other.version, "")
        for damaged in ("1.9.0", ["1.9.0"], {"version": "v1.9.0"}, {"version": None}, {"notes": "x"}, None):
            other = self.controller(update_checked_at=recent, update_found=damaged)
            other.startAutomaticCheck()
            self.assertFalse(other.noticeVisible, damaged)

    def test_checking_by_hand_reports_the_latest_version_and_failures(self):
        self.server.publish("1.8.0")
        controller = self.controller()
        controller.checkNow()
        self.assertEqual(controller.state, "checking")
        self.assertTrue(controller.busy)
        self.wait(lambda: controller.state == "current")
        self.assertEqual(controller.status, "최신 버전이에요")
        self.assertFalse(controller.noticeVisible)
        self.server.status = 403
        controller.checkNow()
        self.wait(lambda: controller.state == "failed")
        self.assertEqual(controller.status, updater.MESSAGES["limited"])
        self.assertFalse(controller.noticeVisible)

    def test_a_failed_check_by_hand_keeps_the_release_found_earlier(self):
        self.server.publish("1.9.0")
        controller = self.controller()
        controller.checkNow()
        self.wait(lambda: controller.state == "available")
        self.server.status = 500
        controller.checkNow()
        self.wait(lambda: controller.state == "available" and controller.error)
        self.assertEqual(controller.status, updater.MESSAGES["network"])
        self.assertTrue(controller.noticeVisible)
        self.assertEqual(controller.version, "1.9.0")
        self.server.status = 200
        controller.checkNow()
        self.wait(lambda: controller.state == "available" and not controller.error)
        self.assertEqual(controller.status, "새 버전이 나왔어요 · 1.9.0")

    def test_a_failed_check_nobody_asked_for_stays_quiet(self):
        self.server.status = 500
        controller = self.controller()
        controller.startAutomaticCheck()
        self.wait(lambda: self.paths() == ["/latest"] and controller.state == "idle")
        self.assertEqual(controller.error, "")
        self.assertFalse(controller.noticeVisible)
        self.assertFalse(self.settings.update.called)

    def test_one_click_fetches_verifies_and_replaces_the_launcher(self):
        program = self.server.publish("1.9.0")
        controller = self.controller()
        controller.checkNow()
        self.wait(lambda: controller.state == "available")
        states = []
        controller.changed.connect(lambda: states.append(controller.state))
        controller.install()
        self.assertEqual(controller.state, "downloading")
        self.wait(lambda: controller.state == "ready")
        self.assertEqual(self.launcher.read_bytes(), program)
        self.assertEqual(sorted(path.name for path in self.launcher.parent.iterdir()), ["LuvBox.exe"])
        self.assertEqual(list((self.folder / "updates").iterdir()), [])
        self.assertEqual(controller.progress, 1.0)
        self.assertTrue(controller.noticeVisible)
        self.assertEqual(controller.status, "1.9.0 준비 완료 · 다시 시작하면 바뀌어요")
        self.assertEqual(self.paths(), ["/latest", "/download/v1.9.0/SHA256-1.9.0.txt", "/download/v1.9.0/LuvBox.exe"])
        self.assertNotIn("failed", states)
        # Restarting is the application's decision; nothing is pending until it says so.
        self.assertFalse(controller.restart_pending)
        controller.confirm_restart()
        self.assertTrue(controller.restart_pending)
        # A second click or check cannot start another download over the finished one.
        controller.install()
        controller.checkNow()
        self.assertEqual(controller.state, "ready")

    def test_a_download_that_does_not_match_its_checksum_changes_nothing(self):
        self.server.publish("1.9.0", checksum="b" * 64)
        controller = self.controller()
        controller.checkNow()
        self.wait(lambda: controller.state == "available")
        controller.install()
        self.wait(lambda: controller.state == "failed")
        self.assertEqual(controller.error, updater.MESSAGES["checksum"])
        self.assertTrue(controller.noticeVisible)
        self.assertEqual(self.launcher.read_bytes(), self.old)
        self.assertEqual(list((self.folder / "updates").iterdir()), [])
        self.assertFalse(controller.restart_pending)
        # "다시 시도" runs the same install again once the release is right.
        program = self.server.publish("1.9.0")
        controller.install()
        self.wait(lambda: controller.state == "ready")
        self.assertEqual(self.launcher.read_bytes(), program)

    def test_cancelling_returns_to_the_notice_and_leaves_the_launcher(self):
        self.server.publish("1.9.0", make_program("1.9.0", os.urandom(2 * 1024 * 1024)))
        controller = self.controller()
        controller.checkNow()
        self.wait(lambda: controller.state == "available")
        self.server.slow.clear()   # the server holds the transfer until released
        controller.install()
        self.wait(lambda: "/download/v1.9.0/LuvBox.exe" in self.paths())
        controller.cancel()
        self.server.slow.set()
        self.wait(lambda: controller.state == "available")
        self.assertEqual(controller.progress, 0.0)
        self.assertEqual(self.launcher.read_bytes(), self.old)
        self.assertEqual(list((self.folder / "updates").iterdir()), [])

    def test_without_a_launcher_or_the_release_files_the_page_is_offered_instead(self):
        self.server.publish("1.9.0")
        source_run = self.controller(launcher=False)
        source_run.checkNow()
        self.wait(lambda: source_run.state == "available")
        self.assertFalse(source_run.canInstall)
        source_run.install()
        self.assertEqual(source_run.state, "available")
        self.server.publish("1.9.0", with_program=False)
        controller = self.controller()
        controller.checkNow()
        self.wait(lambda: controller.state == "available")
        self.assertFalse(controller.canInstall)
        with patch("luvbox.qt_updater.QDesktopServices.openUrl") as opened:
            controller.openReleasePage()
        self.assertEqual(opened.call_args.args[0].toString(), "https://github.com/white-critter/LuvBox/releases/latest")

    def test_the_switch_is_remembered(self):
        controller = self.controller()
        self.assertTrue(controller.autoCheck)
        controller.setAutoCheck(False)
        self.assertFalse(controller.autoCheck)
        self.wait(lambda: self.settings.update.called)
        self.assertEqual(self.settings.update.call_args.args[0], {"update_check": False})
        controller.startAutomaticCheck()
        self.assertEqual(self.paths(), [])
        # A damaged saved value falls back to checking, the default.
        self.assertTrue(self.controller(update_check="yes").autoCheck)

    def test_closing_during_a_download_does_not_wait_for_the_network(self):
        self.server.publish("1.9.0", make_program("1.9.0", os.urandom(2 * 1024 * 1024)))
        controller = self.controller()
        controller.checkNow()
        self.wait(lambda: controller.state == "available")
        self.server.slow.clear()
        controller.install()
        self.wait(lambda: "/download/v1.9.0/LuvBox.exe" in self.paths())
        started = time.monotonic()
        finished = []
        controller.shutdownFinished.connect(lambda: finished.append(True))
        controller.shutdown()
        self.wait(lambda: finished, timeout=3)
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(controller.noticeVisible)
        self.assertEqual(self.launcher.read_bytes(), self.old)
        self.server.slow.set()


@unittest.skipUnless(_QT_AVAILABLE, "optional Qt dependencies are not installed")
class ApplicationUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def test_the_application_owns_an_update_part_that_stays_offline_and_closes_with_it(self):
        from luvbox.qt_app import ApplicationController

        folder = tempfile.TemporaryDirectory(prefix="luvbox-qt-update-app-")
        self.addCleanup(folder.cleanup)
        settings = Mock()
        settings.load.return_value = {**default_settings(), "account": "example",
                                      "output_dir": str(Path(folder.name) / "media")}
        patches = [patch("luvbox.qt_app.SettingsRepository", return_value=settings),
                   patch("luvbox.qt_accounts.AccountController._read_state", new=staticmethod(lambda: {
                       "accounts": [], "identity": {}, "connected": True})),
                   patch("luvbox.qt_schedule.task_status", return_value={"registered": False}),
                   patch.dict(os.environ, {"LOCALAPPDATA": folder.name}),
                   patch.object(updater, "check", side_effect=AssertionError("no request at start")),
                   patch.object(updater.urllib.request, "build_opener", side_effect=AssertionError("no connection"))]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        controller = ApplicationController()
        self.assertIs(controller.updates, controller._updates)
        self.assertEqual(controller.updates.state, "idle")
        # Restarting is only possible once a fetched version is in place.
        self.assertFalse(controller.prepareRestart())
        self.assertFalse(controller.updates.restart_pending)
        controller.updates._state = "ready"
        self.assertTrue(controller.prepareRestart())
        self.assertTrue(controller.updates.restart_pending)
        controller.requestClose()
        deadline = time.monotonic() + 10
        while not controller.shutdownReady and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(0.005)
        self.assertTrue(controller.shutdownReady)
        self.assertIn("updates", controller._shutdown_parts)
        self.assertFalse(controller.prepareRestart())


if __name__ == "__main__":
    unittest.main()
