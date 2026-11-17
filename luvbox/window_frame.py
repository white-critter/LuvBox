"""The window's own edge on Windows 11: rounded corners and the system's thin border.

The main window draws its own title bar, so it is a frameless window, and Windows
then leaves out its corners and border. The Desktop Window Manager can still draw
them on such a window: rounded corners, the thin border (in the accent colour on
the active window when "Show accent colour on title bars and window borders" is on,
as on other windows) and a soft shadow. A maximized window gets square corners and
no border, as Windows does for its own windows.

Elsewhere (Windows 10, other platforms, the offscreen test platform) nothing here
applies and the window draws a hairline itself (`nativeFrame` stays false).
"""

import sys

from PySide6.QtCore import QObject, Slot
from PySide6.QtGui import QGuiApplication, QWindow

# dwmapi.h
_CORNER_PREFERENCE = 33
_BORDER_COLOR = 34
_ROUND = 2
_DO_NOT_ROUND = 1
_COLOR_DEFAULT = 0xFFFFFFFF
_COLOR_NONE = 0xFFFFFFFE


def _accent_border():
    """The accent colour as a COLORREF when Windows puts it on window borders, else None."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM") as key:
            if winreg.QueryValueEx(key, "ColorPrevalence")[0] != 1:
                return None
            accent = winreg.QueryValueEx(key, "AccentColor")[0]
    except (OSError, ValueError, TypeError):
        return None
    # Stored as 0xAABBGGRR; a COLORREF is 0x00BBGGRR.
    return accent & 0xFFFFFF if isinstance(accent, int) else None


class WindowFrame(QObject):
    def __init__(self, window, parent=None):
        super().__init__(parent or window)
        self._window = window
        self._set = None
        self.native = False
        if sys.platform == "win32" and QGuiApplication.platformName() == "windows":
            try:
                import ctypes
                from ctypes import wintypes

                dwm = ctypes.WinDLL("dwmapi")
                dwm.DwmSetWindowAttribute.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
                dwm.DwmSetWindowAttribute.restype = ctypes.c_long

                def set_attribute(attribute, value):
                    data = ctypes.c_uint32(value)
                    return dwm.DwmSetWindowAttribute(int(window.winId()), attribute, ctypes.byref(data), 4) == 0

                # Windows 10 does not know the corner attribute: the window keeps its own hairline there.
                if set_attribute(_CORNER_PREFERENCE, _ROUND):
                    self._set = set_attribute
                    self.native = True
            except (OSError, AttributeError):
                self._set = None
        window.setProperty("nativeFrame", self.native)
        if self.native:
            window.visibilityChanged.connect(self.refresh)
            window.activeChanged.connect(self.refresh)
            self.refresh()

    @Slot()
    def refresh(self):
        if self._set is None:
            return
        try:
            full = self._window.visibility() in (QWindow.Visibility.Maximized, QWindow.Visibility.FullScreen)
            active = self._window.isActive()
            self._set(_CORNER_PREFERENCE, _DO_NOT_ROUND if full else _ROUND)
            if full:
                border = _COLOR_NONE
            else:
                accent = _accent_border() if active else None
                border = _COLOR_DEFAULT if accent is None else accent
            self._set(_BORDER_COLOR, border)
        except RuntimeError:
            self._set = None  # the window is being torn down
