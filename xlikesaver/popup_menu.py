"""A right-click menu drawn with the app's own theme and font.

Tk's Windows menu keeps the native frame, check-mark gutter and embossed
disabled text around custom colors, which looked out of place in the dark
theme. This menu is a borderless window with themed rows instead.
"""

from __future__ import annotations

import os
import tkinter as tk

from . import theme


def _work_area(widget, x: int, y: int) -> tuple[int, int, int, int]:
    """The usable screen rectangle (without the taskbar) under a point."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class MonitorInfo(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

            user32 = ctypes.windll.user32
            user32.MonitorFromPoint.argtypes = (wintypes.POINT, wintypes.DWORD)
            user32.MonitorFromPoint.restype = wintypes.HANDLE
            user32.GetMonitorInfoW.argtypes = (wintypes.HANDLE, ctypes.POINTER(MonitorInfo))
            user32.GetMonitorInfoW.restype = wintypes.BOOL
            monitor = user32.MonitorFromPoint(wintypes.POINT(x, y), 2)  # nearest monitor
            info = MonitorInfo()
            info.cbSize = ctypes.sizeof(MonitorInfo)
            if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                work = info.rcWork
                return work.left, work.top, work.right, work.bottom
        except (OSError, AttributeError, ValueError):
            pass
    return 0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight()


def _round_corners(window) -> None:
    """Windows 11 rounds small popups like its own menus; older Windows ignores it."""
    if os.name != "nt":
        return
    try:
        import ctypes
        preference = ctypes.c_int(3)  # DWMWCP_ROUNDSMALL
        ctypes.windll.dwmapi.DwmSetWindowAttribute(int(window.wm_frame(), 16), 33, ctypes.byref(preference),
                                                   ctypes.sizeof(preference))
    except (OSError, AttributeError, ValueError, tk.TclError):
        pass


class PopupMenu(tk.Toplevel):
    """Rows of (label, callback, enabled) at a screen point.

    Click a row, or use the arrow keys and Enter. A click elsewhere, Escape
    or switching to another program closes it. A command runs only after the
    menu is gone, so it can open a dialog of its own.
    """

    def __init__(self, owner, commands, x_root: int, y_root: int):
        super().__init__(owner)
        self.withdraw()
        self.overrideredirect(True)
        self.owner = owner
        palette = theme.DARK if theme.mode(owner) == "dark" else theme.LIGHT
        self.palette = palette
        self.configure(background=palette["LINE"])
        self.body = tk.Frame(self, background=palette["CARD"], padx=4, pady=4)
        self.body.pack(fill="both", expand=True, padx=1, pady=1)
        self.rows = []
        self.active = None
        self.closed = False
        for index, (label, callback, enabled) in enumerate(commands):
            row = tk.Label(self.body, text=label, anchor="w", font=theme.SMALL, padx=14, pady=6,
                           background=palette["CARD"],
                           foreground=palette["INK"] if enabled else palette["DISABLED"],
                           cursor="hand2" if enabled else "")
            row.pack(fill="x")
            self.rows.append((row, callback, bool(enabled)))
            if enabled:
                row.bind("<Enter>", lambda _event, index=index: self._activate(index))
                row.bind("<ButtonRelease-1>", lambda _event, index=index: self.invoke(index))
            row.bind("<Leave>", lambda _event, index=index: self._activate(None) if self.active == index else None)
        self.bind("<Escape>", lambda _event: self.close())
        self.bind("<Up>", lambda _event: self._step(-1))
        self.bind("<Down>", lambda _event: self._step(1))
        self.bind("<Return>", lambda _event: self.invoke(self.active))
        self.bind("<space>", lambda _event: self.invoke(self.active))
        self.bind("<ButtonPress>", self._press_elsewhere)
        self.bind("<FocusOut>", self._focus_out)
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        left, top, right, bottom = _work_area(self, int(x_root), int(y_root))
        x = max(left, min(int(x_root), right - width))
        y = int(y_root) if int(y_root) + height <= bottom else max(top, int(y_root) - height)
        self.geometry(f"+{x}+{y}")
        try:
            self.transient(owner.winfo_toplevel())
        except tk.TclError:
            pass
        _round_corners(self)
        self.deiconify()
        self.lift()
        try:
            self.focus_force()
            self.grab_set()
        except tk.TclError:
            pass

    def _activate(self, index):
        if self.closed:
            return
        self.active = index
        for position, (row, _callback, enabled) in enumerate(self.rows):
            chosen = position == index
            row.configure(background=self.palette["BUTTON_HOVER"] if chosen else self.palette["CARD"],
                          foreground=self.palette["INK"] if enabled else self.palette["DISABLED"])

    def _step(self, direction):
        enabled = [index for index, (_row, _callback, usable) in enumerate(self.rows) if usable]
        if not enabled:
            return "break"
        if self.active not in enabled:
            self._activate(enabled[0] if direction > 0 else enabled[-1])
        else:
            self._activate(enabled[(enabled.index(self.active) + direction) % len(enabled)])
        return "break"

    def _inside(self, x_root, y_root):
        return (self.winfo_rootx() <= x_root < self.winfo_rootx() + self.winfo_width()
                and self.winfo_rooty() <= y_root < self.winfo_rooty() + self.winfo_height())

    def _press_elsewhere(self, event):
        # With the grab, a click on any other part of the app arrives here.
        if not self._inside(event.x_root, event.y_root):
            self.close()
            return "break"

    def _focus_out(self, _event):
        # Check once focus has settled: another program, or a click on the
        # library window's title bar, closes the menu like a native one.
        if not self.closed:
            self.after(30, self._check_focus)

    def _check_focus(self):
        if self.closed:
            return
        try:
            focus = self.focus_get()
            inside = focus is not None and focus.winfo_toplevel() is self
        except (KeyError, tk.TclError):
            inside = False
        if not inside:
            self.close()

    def invoke(self, index):
        if self.closed or index is None or not 0 <= index < len(self.rows):
            return "break"
        _row, callback, enabled = self.rows[index]
        if not enabled:
            return "break"
        owner = self.owner
        self.close()
        try:
            owner.after_idle(callback)
        except tk.TclError:
            pass
        return "break"

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    def destroy(self):
        self.closed = True
        self.owner = None
        self.rows = []
        super().destroy()
