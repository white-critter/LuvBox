"""The extension setup helper: manager address, and a copy that outlives app versions."""

from importlib.util import find_spec
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch


_QT_AVAILABLE = find_spec("PySide6") is not None
if _QT_AVAILABLE:
    from PySide6.QtCore import QCoreApplication
    from luvbox.qt_browser_setup import _EXTENSION_FILES, BrowserSetupController


@unittest.skipUnless(_QT_AVAILABLE, "optional Qt dependencies are not installed")
class BrowserSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="luvbox-setup-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.folder)})
        environment.start()
        self.addCleanup(environment.stop)
        self.bundled = Path(__file__).resolve().parents[1] / "assets" / "browser-extension" / "chromium"

    def test_extension_is_copied_outside_the_versioned_app_folder_and_refreshed(self):
        target = BrowserSetupController._extension_folder()
        self.assertEqual(target, self.folder / "LuvBox" / "browser-extension" / "chromium")
        for name in _EXTENSION_FILES:
            self.assertEqual((target / name).read_bytes(), (self.bundled / name).read_bytes())
        # An older version's file is replaced; an unchanged file is left alone.
        (target / "background.js").write_bytes(b"// older version")
        before = (target / "manifest.json").stat().st_mtime_ns
        self.assertEqual(BrowserSetupController._extension_folder(), target)
        self.assertEqual((target / "background.js").read_bytes(), (self.bundled / "background.js").read_bytes())
        self.assertEqual((target / "manifest.json").stat().st_mtime_ns, before)
        self.assertEqual([path.name for path in target.iterdir() if path.name.startswith(".")], [])

    def test_every_bundled_extension_file_is_copied(self):
        self.assertEqual(sorted(_EXTENSION_FILES), sorted(path.name for path in self.bundled.iterdir()))

    @unittest.skipUnless(os.name == "nt", "Windows junction")
    def test_a_redirected_extension_folder_is_refused(self):
        import _winapi
        elsewhere = self.folder / "elsewhere"
        elsewhere.mkdir()
        (self.folder / "LuvBox").mkdir()
        _winapi.CreateJunction(str(elsewhere), str(self.folder / "LuvBox" / "browser-extension"))
        with self.assertRaises(OSError):
            BrowserSetupController._extension_folder()
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_manager_opens_the_browser_and_copies_the_address_it_cannot_open(self):
        controller = BrowserSetupController.__new__(BrowserSetupController)
        controller._stop = threading.Event()
        controller._selected_browser = "chrome"
        with patch("luvbox.qt_browser_setup.open_browser_url", return_value=True) as opener:
            result = controller._perform("manager")
        opener.assert_called_once_with("chrome")
        self.assertEqual(result["clipboard"], "chrome://extensions")
        self.assertIn("붙여 넣어", result["status"])

    def test_folder_reveal_selects_the_manifest_instead_of_opening_documents(self):
        controller = BrowserSetupController.__new__(BrowserSetupController)
        controller._stop = threading.Event()
        controller._selected_browser = "chrome"
        folder = self.folder / "LuvBox" / "browser-extension" / "chromium"
        with patch.object(BrowserSetupController, "_extension_folder", return_value=folder):
            result = controller._perform("folder")
        self.assertEqual(result["reveal"], str(folder))
        self.assertEqual(result["path"], str(folder))
        self.assertIn("문서", result["status"])
        self.assertNotIn("url", result)

    def test_reveal_selects_manifest_json(self):
        folder = self.folder / "chromium"
        folder.mkdir()
        (folder / "manifest.json").write_text("{}", encoding="utf-8")
        with patch("luvbox.qt_browser_setup.subprocess.Popen") as popen:
            BrowserSetupController._reveal(folder)
        self.assertEqual(popen.call_args.args[0], ["explorer.exe", "/select," + str(folder / "manifest.json")])

    def test_firefox_uses_the_firefox_manifest_and_persistent_app_folder(self):
        target = BrowserSetupController._extension_folder("firefox")
        bundled = self.bundled.parent / "firefox"
        self.assertEqual(target, self.folder / "LuvBox" / "browser-extension" / "firefox")
        for name in _EXTENSION_FILES:
            self.assertEqual((target / name).read_bytes(), (bundled / name).read_bytes())
        self.assertIn('"scripts"', (target / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn('"service_worker"', (target / "manifest.json").read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            BrowserSetupController._extension_folder("../chromium")

    def test_x_opens_in_the_selected_browser_without_system_default_handoff(self):
        controller = BrowserSetupController.__new__(BrowserSetupController)
        controller._stop = threading.Event()
        controller._selected_browser = "firefox"
        with patch("luvbox.qt_browser_setup.open_browser_url", return_value=True) as opener:
            result = controller._perform("x")
        opener.assert_called_once_with("firefox", "https://x.com/home")
        self.assertNotIn("url", result)
        self.assertIn("일반 프로필", result["status"])

    def test_install_prepares_manual_steps_without_launch_flags_or_browser_process(self):
        controller = BrowserSetupController.__new__(BrowserSetupController)
        controller._stop = threading.Event()
        controller._selected_browser = "edge"
        with patch("luvbox.qt_browser_setup.subprocess.Popen") as popen, \
                patch("luvbox.qt_browser_setup.open_browser_url") as opener:
            result = controller._perform("install")
        popen.assert_not_called()
        opener.assert_not_called()
        self.assertIn("자동으로 설치되지", result["status"])
        self.assertIn("문서", result["status"])
        self.assertEqual(result["clipboard"], result["path"])

    def wait_for(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if condition():
                return
            time.sleep(.002)
        self.fail("browser setup operation did not settle")

    def controller(self, saved="chrome"):
        settings = Mock()
        settings.load.return_value = {"connection_browser": saved, "untouched": "preserved"}
        choices = [{"id": "edge", "label": "Microsoft Edge"},
                   {"id": "chrome", "label": "Google Chrome"},
                   {"id": "firefox", "label": "Mozilla Firefox"}]
        patcher = patch("luvbox.qt_browser_setup.available_browsers", return_value=choices)
        patcher.start()
        self.addCleanup(patcher.stop)
        controller = BrowserSetupController(settings=settings)

        def close():
            controller.shutdown()
            self.wait_for(lambda: controller._shutdown_emitted)

        self.addCleanup(close)
        self.wait_for(lambda: not controller.busy)
        return controller, settings

    def test_saved_browser_is_restored_without_rewriting_preferences(self):
        controller, settings = self.controller("chrome")
        self.assertEqual(controller.selectedBrowser, "chrome")
        self.assertEqual(controller.browserLabel, "Google Chrome")
        self.assertEqual(controller.managerAddress, "chrome://extensions")
        self.assertEqual(controller.extensionKind, "chromium")
        self.assertEqual([item["id"] for item in controller.browsers], ["edge", "chrome", "firefox"])
        settings.update.assert_not_called()

    def test_invalid_saved_browser_uses_the_first_available_without_writing_it(self):
        controller, settings = self.controller("C:/untrusted.exe")
        self.assertEqual(controller.selectedBrowser, "edge")
        settings.update.assert_not_called()
        self.assertFalse(controller.setBrowser("C:/untrusted.exe"))

    def test_selection_is_committed_off_the_ui_thread_and_prepares_correct_variant(self):
        controller, settings = self.controller()
        caller = threading.get_ident()
        threads = []
        settings.update.side_effect = lambda changes: threads.append(threading.get_ident())
        self.assertTrue(controller.setBrowser("firefox"))
        self.assertFalse(controller.setBrowser("edge"))
        self.assertEqual(controller.selectedBrowser, "chrome")
        self.wait_for(lambda: not controller.busy)
        self.assertEqual(controller.selectedBrowser, "firefox")
        self.assertEqual(controller.extensionKind, "firefox")
        self.assertEqual(Path(controller.extensionPath).name, "firefox")
        self.assertEqual(controller.managerAddress, "about:debugging#/runtime/this-firefox")
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], caller)
        settings.update.assert_called_once_with({"connection_browser": "firefox"})

    def test_failed_selection_write_keeps_the_previous_ui_selection_and_folder(self):
        controller, settings = self.controller()
        controller.prepareExtension()
        self.wait_for(lambda: not controller.busy)
        previous = controller.extensionPath
        settings.update.side_effect = OSError("synthetic inaccessible settings")
        self.assertTrue(controller.setBrowser("firefox"))
        self.wait_for(lambda: not controller.busy)
        self.assertEqual(controller.selectedBrowser, "chrome")
        self.assertEqual(controller.extensionPath, previous)
        self.assertIn("이전 선택", controller.error)
        self.assertNotIn("synthetic", controller.error)

    def test_prepare_requested_during_discovery_runs_after_selection_is_known(self):
        settings = Mock()
        settings.load.return_value = {"connection_browser": "firefox"}
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def discover():
            entered.set()
            release.wait(5)
            return [{"id": "firefox", "label": "Mozilla Firefox"}]

        with patch("luvbox.qt_browser_setup.available_browsers", side_effect=discover):
            controller = BrowserSetupController(settings=settings)
            try:
                self.assertTrue(entered.wait(2))
                controller.prepareExtension()
                release.set()
                self.wait_for(lambda: not controller.busy)
                self.assertEqual(controller.selectedBrowser, "firefox")
                self.assertEqual(Path(controller.extensionPath).name, "firefox")
            finally:
                release.set()
                controller.shutdown()
                self.wait_for(lambda: controller._shutdown_emitted)


if __name__ == "__main__":
    unittest.main()
