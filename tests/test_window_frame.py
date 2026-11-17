"""The main window's edge: Windows draws rounded corners and its thin border, or the window draws a hairline."""

from importlib.util import find_spec
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)

_QT_AVAILABLE = find_spec("PySide6") is not None
if _QT_AVAILABLE:
    from PySide6.QtGui import QWindow
    from luvbox import window_frame


class _Window:
    def __init__(self, visibility, active):
        self._visibility, self._active = visibility, active

    def visibility(self):
        return self._visibility

    def isActive(self):
        return self._active


@unittest.skipUnless(_QT_AVAILABLE, "optional Qt dependencies are not installed")
class WindowFrameTests(unittest.TestCase):
    def frame(self, visibility, active):
        frame = window_frame.WindowFrame.__new__(window_frame.WindowFrame)
        calls = []
        frame._window = _Window(visibility, active)
        frame._set = lambda attribute, value: calls.append((attribute, value)) or True
        frame.refresh()
        return calls

    def test_a_normal_window_is_round_with_the_system_border(self):
        with patch.object(window_frame, "_accent_border", return_value=None):
            self.assertEqual(self.frame(QWindow.Visibility.Windowed, True),
                             [(window_frame._CORNER_PREFERENCE, window_frame._ROUND),
                              (window_frame._BORDER_COLOR, window_frame._COLOR_DEFAULT)])

    def test_the_active_window_takes_the_accent_border_only_when_windows_shows_it(self):
        with patch.object(window_frame, "_accent_border", return_value=0x0C63F7):
            self.assertEqual(self.frame(QWindow.Visibility.Windowed, True)[1], (window_frame._BORDER_COLOR, 0x0C63F7))
            # An inactive window has the plain border, as other windows do.
            self.assertEqual(self.frame(QWindow.Visibility.Windowed, False)[1],
                             (window_frame._BORDER_COLOR, window_frame._COLOR_DEFAULT))

    def test_a_maximized_window_has_square_corners_and_no_border(self):
        for visibility in (QWindow.Visibility.Maximized, QWindow.Visibility.FullScreen):
            self.assertEqual(self.frame(visibility, True),
                             [(window_frame._CORNER_PREFERENCE, window_frame._DO_NOT_ROUND),
                              (window_frame._BORDER_COLOR, window_frame._COLOR_NONE)])

    def test_the_accent_colour_is_read_as_a_colorref(self):
        values = {"ColorPrevalence": (1, 4), "AccentColor": (0xFF0C63F7, 4)}

        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with patch("winreg.OpenKey", return_value=Key()), \
                patch("winreg.QueryValueEx", side_effect=lambda key, name: values[name]):
            self.assertEqual(window_frame._accent_border(), 0x0C63F7)
            values["ColorPrevalence"] = (0, 4)
            self.assertIsNone(window_frame._accent_border())
        with patch("winreg.OpenKey", side_effect=OSError):
            self.assertIsNone(window_frame._accent_border())


if __name__ == "__main__":
    unittest.main()
