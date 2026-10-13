"""Application identity and shared window icons, also in the frozen build."""

import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tkinter as tk
import uuid
import weakref


APP_ID = "LuvBox.Desktop"
_native_icons = None


class _Guid(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]


class _PropertyKey(ctypes.Structure):
    _fields_ = [("format", _Guid), ("pid", ctypes.c_uint32)]


class _ValueUnion(ctypes.Union):
    _fields_ = [("text", ctypes.c_void_p), ("storage", ctypes.c_ubyte * (16 if ctypes.sizeof(ctypes.c_void_p) == 8 else 8)),
                ("alignment", ctypes.c_uint64)]


class _PropertyValue(ctypes.Structure):
    _fields_ = [("kind", ctypes.c_uint16), ("reserved", ctypes.c_uint16 * 3), ("value", _ValueUnion)]


def _taskbar_values(icon: str) -> dict[int, str]:
    if getattr(sys, "frozen", False):
        command = [sys.executable]
    else:
        executable = Path(sys.executable)
        windowed = executable.with_name("pythonw.exe")
        command = [str(windowed if windowed.is_file() else executable),
                   str(Path(__file__).resolve().parents[1] / "entry.py")]
    # Relaunch properties precede the window's explicit AppUserModelID.
    return {2: subprocess.list2cmdline(command), 3: icon + ",0",
            4: "러브박스 (LuvBox)", 5: APP_ID}


def _taskbar_properties(handle: int, values: dict[int, str] | None) -> dict[int, str]:
    """Set (or read for diagnostics) the window's own taskbar group properties."""
    iid = _Guid.from_buffer_copy(uuid.UUID("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99").bytes_le)
    format_id = _Guid.from_buffer_copy(uuid.UUID("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3").bytes_le)
    shell = ctypes.WinDLL("shell32")
    shell.SHGetPropertyStoreForWindow.argtypes = (ctypes.c_void_p, ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p))
    shell.SHGetPropertyStoreForWindow.restype = ctypes.c_long
    store = ctypes.c_void_p()
    if shell.SHGetPropertyStoreForWindow(handle, ctypes.byref(iid), ctypes.byref(store)) < 0:
        raise OSError("taskbar property store unavailable")
    table = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    method = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropertyValue))
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(table[2])
    clear = ctypes.WinDLL("ole32").PropVariantClear
    clear.argtypes = (ctypes.POINTER(_PropertyValue),)
    result = {}
    try:
        for pid in (2, 3, 4, 5):
            key, value = _PropertyKey(format_id, pid), _PropertyValue()
            if values is None:
                try:
                    if method(table[5])(store, ctypes.byref(key), ctypes.byref(value)) < 0:
                        raise OSError("taskbar property read failed")
                    result[pid] = ctypes.wstring_at(value.value.text) if value.kind == 31 else ""
                finally:
                    clear(ctypes.byref(value))
            else:
                text = ctypes.create_unicode_buffer(values[pid]) if values.get(pid) else None
                if text is not None:
                    value.kind = 31  # VT_LPWSTR; SetValue copies this borrowed string.
                    value.value.text = ctypes.cast(text, ctypes.c_void_p)
                if method(table[6])(store, ctypes.byref(key), ctypes.byref(value)) < 0:
                    raise OSError("taskbar property write failed")
    finally:
        release(store)
    return result


def _windows_taskbar(root: tk.Tk, icon: str) -> None:
    handle = int(root.wm_frame(), 16)
    if getattr(root, "_xlikesaver_taskbar_handle", None) == handle:
        return
    _taskbar_properties(handle, _taskbar_values(icon))
    root._xlikesaver_taskbar_handle = handle
    if not getattr(root, "_xlikesaver_taskbar_cleanup", False):
        root_ref = weakref.ref(root)
        destroy_ref = weakref.WeakMethod(root.destroy)
        def close():
            owner = root_ref()
            destroy = destroy_ref()
            if owner is None or destroy is None:
                return
            try:
                # Free the shell's property values before Tk destroys the HWND.
                _taskbar_properties(owner._xlikesaver_taskbar_handle, {})
            except (OSError, AttributeError, tk.TclError):
                pass
            destroy()
        root.destroy = close
        root._xlikesaver_taskbar_cleanup = True


def _windows_icon(window: tk.Misc, icon: str) -> None:
    """Give the current HWND and Tk's shared window class the same icon.

    Preserve both window icons and their class fallback. Keep independent
    native handles alive for the process: Tk owns and destroys its own icons
    when a window is closed or its wrapper is recreated.
    """
    global _native_icons
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.LoadImageW.argtypes = (wintypes.HINSTANCE, ctypes.c_wchar_p, wintypes.UINT,
                               ctypes.c_int, ctypes.c_int, wintypes.UINT)
    user.LoadImageW.restype = wintypes.HANDLE
    user.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)
    user.SendMessageW.restype = ctypes.c_ssize_t
    set_class = user.SetClassLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user.SetClassLongW
    set_class.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
    set_class.restype = ctypes.c_size_t
    if _native_icons is None:
        handles = []
        for x_metric, y_metric in ((11, 12), (49, 50)):
            handle = user.LoadImageW(None, icon, 1, user.GetSystemMetrics(x_metric),
                                     user.GetSystemMetrics(y_metric), 0x0010)
            if not handle:
                user.DestroyIcon.argtypes = (wintypes.HANDLE,)
                for loaded in handles:
                    user.DestroyIcon(loaded)
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
        _native_icons = tuple(handles)
    handle = int(window.wm_frame(), 16)
    big, small = _native_icons
    for slot, native in ((1, big), (0, small)):
        user.SendMessageW(handle, 0x0080, slot, native)  # WM_SETICON
    for slot, native in ((-14, big), (-34, small)):
        set_class(handle, slot, native)  # GCLP_HICON / GCLP_HICONSM


def asset_directory() -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / "icons"


def install(root: tk.Tk) -> bool:
    """Apply the icon to this root and its current/future child windows."""
    try:
        if os.name == "nt":
            shell = ctypes.WinDLL("shell32")
            identify = shell.SetCurrentProcessExplicitAppUserModelID
            identify.argtypes = (ctypes.c_wchar_p,)
            identify.restype = ctypes.c_long
            identify(APP_ID)
            icon = str(asset_directory() / "luvbox.ico")
            # Create Tk's native wrapper without loading the multi-image ICO
            # again for every window/map. Use our two cached native icons below.
            if not getattr(root, "_xlikesaver_icon_binding", False):
                root.iconbitmap("")
            _windows_icon(root, icon)
            _windows_taskbar(root, icon)
            # Map may follow creation of a new native wrapper. Reapply to the
            # root too, and preserve the fallback on independently mapped children.
            if not getattr(root, "_xlikesaver_icon_binding", False):
                def mapped(event):
                    if isinstance(event.widget, (tk.Tk, tk.Toplevel)):
                        try:
                            _windows_icon(event.widget, icon)
                            if isinstance(event.widget, tk.Tk):
                                _windows_taskbar(event.widget, icon)
                        except (OSError, AttributeError, ValueError, tk.TclError):
                            pass
                for window_class in ("Tk", "Toplevel"):
                    root.bind_class(window_class, "<Map>", mapped, add="+")
                root._xlikesaver_icon_binding = True
        else:
            image = tk.PhotoImage(master=root, file=str(asset_directory() / "luvbox.png"))
            root.iconphoto(True, image)
        return True
    except (OSError, AttributeError, ValueError, tk.TclError):
        return False
