"""The 업데이트 click reads the latest release again, and a release taken down is said to be so.

A release found by an earlier check is shown at start without asking the
server again. By the time the person clicks, the owner may have published a
newer one and deleted the release that was found (the tag stays). The click
must fetch what the latest release says, not the remembered one, and a
release whose checksum file is no longer published is reported as taken down,
never as a broken internet connection.
"""

from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox import updater
from tests.release_server import ReleaseServer, make_program


_QT_AVAILABLE = find_spec("PySide6") is not None
if _QT_AVAILABLE:
    from PySide6.QtCore import QCoreApplication
    from luvbox.app_settings import default_settings
    from luvbox.qt_updater import UpdateController


class ChecksumFileTakenDownTests(unittest.TestCase):
    def setUp(self):
        self.server = ReleaseServer()
        self.addCleanup(self.server.close)
        self.folder = Path(tempfile.mkdtemp(prefix="luvbox-update-reread-"))
        self.addCleanup(shutil.rmtree, self.folder, True)

    def kind(self, version="1.9.0"):
        with self.assertRaises(updater.UpdateError) as caught:
            updater.download(version, self.folder / "updates", self.server.source)
        self.assertEqual(str(caught.exception), updater.MESSAGES[caught.exception.kind])
        return caught.exception.kind

    def test_a_release_whose_checksum_file_is_gone_is_not_a_network_failure(self):
        self.server.publish("1.9.0")
        del self.server.files["/download/v1.9.0/SHA256-1.9.0.txt"]
        self.assertEqual(self.kind(), "gone")
        # Nothing large is asked for once the release is known to be gone.
        self.assertEqual([path for path, _headers in self.server.requests], ["/download/v1.9.0/SHA256-1.9.0.txt"])
        self.assertNotIn("인터넷", updater.MESSAGES["gone"])
        for status, kind in ((410, "gone"), (404, "gone"), (500, "download"), (503, "download"), (403, "download")):
            with self.subTest(status=status):
                with patch.object(updater, "_open", side_effect=updater._Refused(status)):
                    self.assertEqual(self.kind(), kind)

    def test_a_missing_program_after_its_checksum_is_still_a_download_failure(self):
        # Only a missing checksum file tells that the release itself is no longer published.
        self.server.publish("1.9.0")
        del self.server.files["/download/v1.9.0/LuvBox.exe"]
        self.assertEqual(self.kind(), "download")

    def test_a_release_removed_between_requests_is_rechecked_as_withdrawn(self):
        self.server.publish("1.9.0")
        checksum_path = "/download/v1.9.0/SHA256-1.9.0.txt"
        program_path = "/download/v1.9.0/LuvBox.exe"
        open_asset = updater._open

        def remove_before_program(source, url, accept, timeout):
            if url.endswith(program_path):
                self.server.files.clear()
            return open_asset(source, url, accept, timeout)

        with patch.object(updater, "_open", side_effect=remove_before_program):
            self.assertEqual(self.kind(), "gone")
        self.assertEqual([path for path, _headers in self.server.requests],
                         [checksum_path, program_path, checksum_path])
        self.assertFalse(list((self.folder / "updates").iterdir()))


class _ControllerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.server = ReleaseServer()
        self.addCleanup(self.server.close)
        self.folder = Path(tempfile.mkdtemp(prefix="luvbox-update-reread-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.launcher = self.folder / "place" / "LuvBox.exe"
        self.launcher.parent.mkdir()
        self.old = make_program("1.8.0")
        self.launcher.write_bytes(self.old)
        self.settings = Mock()

    def controller(self, **preferences):
        made = UpdateController(settings=self.settings, preferences={**default_settings(), **preferences},
                                current="1.8.0", source=self.server.source, launcher=lambda: self.launcher,
                                folder=self.folder / "updates")
        self.addCleanup(self.close, made)
        return made

    def remembered(self, version="1.9.0"):
        """A start after a check that found `version` (automatic checks off): shown from memory."""
        recent = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        found = {"version": version, "notes": "· 기억한 변경", "installable": True,
                 "size": len(make_program(version))}
        controller = self.controller(update_checked_at=recent, update_found=found, update_check=False)
        controller.startAutomaticCheck()
        self.assertEqual((controller.state, controller.version), ("available", version))
        self.assertTrue(controller.noticeVisible and controller.canInstall)
        self.settle()
        self.assertEqual(self.paths(), [])
        return controller

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

    def settle(self):
        for _ in range(40):
            self.application.processEvents()
            time.sleep(0.005)

    def paths(self):
        return [path for path, _headers in self.server.requests]

    def saved_found(self):
        """What the last write said the next start should offer."""
        writes = [call.args[0] for call in self.settings.update.call_args_list if "update_found" in call.args[0]]
        return writes[-1]["update_found"] if writes else None


@unittest.skipUnless(_QT_AVAILABLE, "optional Qt dependencies are not installed")
class UpdateClickRereadsTests(_ControllerCase):
    def test_the_click_fetches_the_release_that_replaced_the_remembered_one(self):
        program = self.server.publish("1.9.1")      # 1.9.0 was deleted when 1.9.1 came out
        controller = self.remembered("1.9.0")
        controller.install()
        self.assertEqual(controller.state, "downloading")
        self.wait(lambda: controller.state in {"ready", "failed"})
        self.assertEqual((controller.state, controller.error), ("ready", ""))
        self.assertEqual(controller.version, "1.9.1")
        self.assertEqual(self.launcher.read_bytes(), program)
        self.assertEqual(self.paths(), ["/latest", "/download/v1.9.1/SHA256-1.9.1.txt", "/download/v1.9.1/LuvBox.exe"])
        # The next start offers what the latest release is, not the deleted one.
        self.assertEqual(self.saved_found()["version"], "1.9.1")

    def test_the_click_fetches_the_remembered_release_when_it_is_still_the_latest(self):
        program = self.server.publish("1.9.0")
        controller = self.remembered("1.9.0")
        controller.install()
        self.wait(lambda: controller.state in {"ready", "failed"})
        self.assertEqual(controller.state, "ready")
        self.assertEqual(self.launcher.read_bytes(), program)
        self.assertEqual(self.paths(), ["/latest", "/download/v1.9.0/SHA256-1.9.0.txt", "/download/v1.9.0/LuvBox.exe"])

    def test_a_remembered_release_taken_down_with_nothing_newer_is_said_to_be_gone(self):
        self.server.publish("1.8.0")                # the latest release is the running version again
        controller = self.remembered("1.9.0")
        controller.install()
        self.wait(lambda: controller.state not in {"downloading"})
        self.assertEqual(controller.state, "failed")
        self.assertEqual(controller.error, updater.MESSAGES["gone"])
        self.assertNotEqual(controller.error, updater.MESSAGES["download"])
        # The notice says so where the person clicked, with nothing left to fetch.
        self.assertTrue(controller.noticeVisible)
        self.assertFalse(controller.canInstall)
        self.assertEqual(controller.version, "")
        self.assertEqual(controller.status, updater.MESSAGES["gone"])
        self.assertEqual(self.paths(), ["/latest"])
        self.assertEqual(self.saved_found(), {})
        self.assertEqual(self.launcher.read_bytes(), self.old)
        controller.install()                        # nothing to fetch: the click does nothing
        self.settle()
        self.assertEqual(self.paths(), ["/latest"])
        controller.dismiss()
        self.assertFalse(controller.noticeVisible)
        controller.checkNow()
        self.wait(lambda: controller.state == "current")
        self.assertEqual(controller.error, "")
        self.assertFalse(controller.noticeVisible)

    def test_a_newer_release_without_the_program_is_shown_instead_of_fetching_the_remembered_one(self):
        self.server.publish("1.9.1", with_program=False)
        controller = self.remembered("1.9.0")
        controller.install()
        self.wait(lambda: controller.state != "downloading")
        self.assertEqual((controller.state, controller.version, controller.error), ("available", "1.9.1", ""))
        self.assertFalse(controller.canInstall)
        self.assertTrue(controller.noticeVisible)
        self.assertEqual(self.paths(), ["/latest"])
        self.assertEqual(self.launcher.read_bytes(), self.old)

    def test_a_checksum_file_taken_down_clears_the_find_and_looks_again(self):
        # The latest release cannot be read just then (rate limited), so the remembered one is
        # fetched; its checksum file answers 404. A look right after finds what replaced it.
        program = self.server.publish("1.9.1")
        real_check = updater.check
        calls = []

        def limited_once(*arguments, **options):
            calls.append(arguments)
            if len(calls) == 1:
                raise updater.UpdateError("limited")
            return real_check(*arguments, **options)

        controller = self.remembered("1.9.0")
        with patch.object(updater, "check", side_effect=limited_once):
            controller.install()
            self.wait(lambda: controller.state != "downloading")
        self.assertEqual(len(calls), 2)
        self.assertEqual(controller.state, "available")
        self.assertEqual((controller.version, controller.error), ("1.9.1", updater.MESSAGES["gone"]))
        self.assertEqual(controller.status, updater.MESSAGES["gone"])
        self.assertTrue(controller.noticeVisible and controller.canInstall)
        self.assertEqual(self.paths(), ["/download/v1.9.0/SHA256-1.9.0.txt", "/latest"])
        self.assertEqual(self.saved_found()["version"], "1.9.1")
        controller.install()
        self.wait(lambda: controller.state in {"ready", "failed"})
        self.assertEqual((controller.state, controller.error), ("ready", ""))
        self.assertEqual(self.launcher.read_bytes(), program)

    def test_a_checksum_file_taken_down_from_the_latest_release_is_not_offered_again(self):
        self.server.publish("1.9.0")
        del self.server.files["/download/v1.9.0/SHA256-1.9.0.txt"]
        controller = self.remembered("1.9.0")
        controller.install()
        self.wait(lambda: controller.state != "downloading")
        self.assertEqual((controller.state, controller.error), ("failed", updater.MESSAGES["gone"]))
        self.assertTrue(controller.noticeVisible)
        self.assertFalse(controller.canInstall)
        self.assertEqual(self.saved_found(), {})
        self.assertEqual(self.paths(), ["/latest", "/download/v1.9.0/SHA256-1.9.0.txt", "/latest"])
        self.assertNotIn("/download/v1.9.0/LuvBox.exe", self.paths())

    def test_a_look_again_that_cannot_reach_the_server_says_why_and_still_forgets_the_find(self):
        controller = self.remembered("1.9.0")
        self.server.publish("1.9.1")
        with patch.object(updater, "check", side_effect=updater.UpdateError("limited")):
            controller.install()
            self.wait(lambda: controller.state != "downloading")
        self.assertEqual((controller.state, controller.error), ("failed", updater.MESSAGES["limited"]))
        self.assertEqual(self.saved_found(), {})
        self.assertEqual(self.paths(), ["/download/v1.9.0/SHA256-1.9.0.txt"])
        # "다시 시도" reads the latest release again once it answers.
        controller.install()
        self.wait(lambda: controller.state in {"ready", "failed"})
        self.assertEqual((controller.state, controller.version), ("ready", "1.9.1"))


@unittest.skipUnless(_QT_AVAILABLE, "optional Qt dependencies are not installed")
class CheckByHandAfterLaterTests(_ControllerCase):
    """Only the low finding: 나중에 hides the notice for the session, 지금 확인 is the person asking again."""

    def test_a_check_by_hand_that_finds_a_release_shows_the_notice_again(self):
        self.server.publish("1.9.0")
        controller = self.controller()
        controller.checkNow()
        self.wait(lambda: controller.state == "available")
        controller.dismiss()
        self.assertFalse(controller.noticeVisible)
        self.assertEqual(controller.status, "새 버전이 나왔어요 · 1.9.0")
        controller.checkNow()
        self.assertFalse(controller.noticeVisible)
        self.wait(lambda: controller.state == "available")
        self.assertTrue(controller.noticeVisible)
        self.assertEqual(controller.version, "1.9.0")

    def test_a_remembered_release_put_off_with_later_comes_back_on_a_check_by_hand(self):
        self.server.publish("1.9.0")
        controller = self.remembered("1.9.0")
        controller.dismiss()
        self.assertFalse(controller.noticeVisible)
        controller.checkNow()
        self.wait(lambda: controller.state == "available" and self.paths() == ["/latest"])
        self.assertTrue(controller.noticeVisible)



if __name__ == "__main__":
    unittest.main()
