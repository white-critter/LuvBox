"""Keep transparent PNG and multi-size ICO assets used by the Qt app."""

from importlib.util import find_spec
import os
from pathlib import Path
import subprocess
import sys
import unittest

from PIL import Image
try:
    from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
except ModuleNotFoundError:      # run as a script: the tests folder is the path
    import app_data_isolation  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]
ICONS = ROOT / "assets" / "icons"

_WINDOW_ICON_PROBE = """
import ctypes
from pathlib import Path
import sys
from unittest.mock import patch

from tests import app_data_isolation
from luvbox import launcher, qt_app
from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication

user32 = ctypes.WinDLL('user32')
user32.IsWindowVisible.argtypes = (ctypes.c_void_p,)
user32.SendMessageW.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
user32.SendMessageW.restype = ctypes.c_void_p
start_controller = qt_app._start_controller
mark_window = launcher.mark_window
program = Path(sys.executable)
seen = {}

def start(*args, **kwargs):
    controller = start_controller(*args, **kwargs)
    QTimer.singleShot(300, controller.requestClose)
    return controller

def mark(handle, path):
    marked = mark_window(handle, path)
    if path is not None:
        seen.update(hidden=not user32.IsWindowVisible(handle), marked=marked,
                    icons=all(user32.SendMessageW(handle, 0x007f, size, 96) for size in (0, 1)))
        window = next(w for w in QGuiApplication.topLevelWindows() if int(w.winId()) == handle)
        window.visibleChanged.connect(lambda visible: visible and seen.update(
            first_visible=launcher.window_identity(handle)['icon'] == f'{program},0'))
    return marked

with patch.object(qt_app, '_start_controller', start), \
     patch.object(launcher, 'launcher_path', return_value=program), \
     patch.object(launcher, 'mark_window', mark):
    assert qt_app.launch() == 0
assert seen == dict(hidden=True, marked=True, icons=True, first_visible=True), seen
"""


class AppIconTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt" and find_spec("PySide6"), "needs the Windows Qt platform")
    def test_first_visible_window_already_has_its_native_icons_and_taskbar_identity(self):
        result = subprocess.run([sys.executable, "-c", _WINDOW_ICON_PROBE], cwd=ROOT,
                                env={**os.environ, "QT_QPA_PLATFORM": "windows"}, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_transparency_is_preserved_at_every_windows_icon_size(self):
        with Image.open(ICONS / "luvbox.png") as image:
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getpixel((0, 0))[3], 0)
        with Image.open(ICONS / "luvbox.ico") as image:
            self.assertEqual(image.ico.sizes(), {(n, n) for n in (16, 24, 32, 48, 64, 128, 256)})
            for size in image.ico.sizes():
                with image.ico.getimage(size) as rendered:
                    self.assertEqual(rendered.getpixel((0, 0))[3], 0)

    def test_the_dark_square_fills_the_icon_and_the_heart_keeps_its_original_proportions(self):
        with Image.open(ICONS / "luvbox.png") as image:
            self.assertEqual(image.getpixel((0, image.height // 2)), (18, 18, 18, 255))
            self.assertEqual(image.getpixel((image.width - 1, image.height // 2)), (18, 18, 18, 255))
            pixels = [(x, y) for y in range(image.height) for x in range(image.width)
                      if self.is_heart_red(image.getpixel((x, y)))]
            width = max(x for x, _ in pixels) - min(x for x, _ in pixels) + 1
            self.assertIn(width, range(93, 100))

    def test_heart_is_visible_at_tray_and_extension_toolbar_size(self):
        paths = [ROOT / "assets" / "browser-extension" / browser / "icons" / "icon-16.png"
                 for browser in ("chromium", "firefox")]
        with Image.open(ICONS / "luvbox.ico") as image:
            with image.ico.getimage((16, 16)) as rendered:
                self.assert_heart_visible(rendered)
        for path in paths:
            with self.subTest(icon=path.parent.parent.name), Image.open(path) as image:
                self.assert_heart_visible(image)

    def assert_heart_visible(self, image):
        self.assertEqual(image.getpixel((0, image.height // 2))[3], 255)
        pixels = [(x, y) for y in range(image.height) for x in range(image.width)
                  if self.is_heart_red(image.getpixel((x, y)))]
        self.assertGreaterEqual(len(pixels), 8)
        self.assertLessEqual(max(x for x, _ in pixels) - min(x for x, _ in pixels) + 1, 8)

    @staticmethod
    def is_heart_red(color):
        red, green, _blue, alpha = color
        return red >= 100 and green < red * 0.67 and alpha > 100


if __name__ == "__main__":
    unittest.main()
