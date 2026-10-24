"""The extension setup helper: manager address, and a copy that outlives app versions."""

from importlib.util import find_spec
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


_QT_AVAILABLE = find_spec("PySide6") is not None
if _QT_AVAILABLE:
    from luvbox.qt_browser_setup import _EXTENSION_FILES, BrowserSetupController


@unittest.skipUnless(_QT_AVAILABLE, "optional Qt dependencies are not installed")
class BrowserSetupTests(unittest.TestCase):
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
        chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        controller = BrowserSetupController.__new__(BrowserSetupController)
        controller._stop = __import__("threading").Event()
        with patch("luvbox.qt_browser_setup.find_default_browser", return_value=chrome), \
                patch("luvbox.qt_browser_setup.subprocess.Popen") as popen:
            result = controller._perform("manager")
        self.assertEqual(popen.call_args.args[0], [str(chrome)])
        self.assertEqual(result["clipboard"], "chrome://extensions")
        self.assertIn("붙여 넣어", result["status"])


if __name__ == "__main__":
    unittest.main()
