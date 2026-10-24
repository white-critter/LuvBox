"""Verify transparent multi-size assets and real Windows window icon handles."""

import ctypes
import os
import tkinter as tk
import unittest
from PIL import Image
from luvbox import app_icon
from tests.legacy_ui import previous_window


def rendered_icon(handle, size):
    """Compare actual shell icon pixels, not just nonzero (possibly Tk) handles."""
    from ctypes import wintypes as w
    class Header(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("width", w.LONG), ("height", w.LONG),
                    ("planes", w.WORD), ("bits", w.WORD), ("compression", w.DWORD),
                    ("image_size", w.DWORD), ("x", w.LONG), ("y", w.LONG),
                    ("used", w.DWORD), ("important", w.DWORD)]
    class Info(ctypes.Structure):
        _fields_ = [("header", Header), ("colors", w.DWORD * 3)]
    gdi = ctypes.WinDLL("gdi32")
    gdi.CreateCompatibleDC.argtypes = (w.HDC,)
    gdi.CreateCompatibleDC.restype = w.HDC
    gdi.CreateDIBSection.argtypes = (w.HDC, ctypes.POINTER(Info), w.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD)
    gdi.CreateDIBSection.restype = w.HBITMAP
    gdi.SelectObject.argtypes = (w.HDC, w.HGDIOBJ)
    gdi.SelectObject.restype = w.HGDIOBJ
    gdi.DeleteObject.argtypes = (w.HGDIOBJ,)
    gdi.DeleteDC.argtypes = (w.HDC,)
    user = ctypes.WinDLL("user32")
    user.DrawIconEx.argtypes = (w.HDC, ctypes.c_int, ctypes.c_int, w.HICON,
                               ctypes.c_int, ctypes.c_int, w.UINT, w.HBRUSH, w.UINT)
    info = Info(Header(ctypes.sizeof(Header), size, -size, 1, 32))
    bits = ctypes.c_void_p()
    dc = gdi.CreateCompatibleDC(None)
    bitmap = gdi.CreateDIBSection(dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
    previous = gdi.SelectObject(dc, bitmap)
    try:
        ctypes.memset(bits, 0, size * size * 4)
        if not user.DrawIconEx(dc, 0, 0, handle, size, size, 0, None, 3):
            raise ctypes.WinError()
        gdi.GdiFlush()
        return ctypes.string_at(bits, size * size * 4)
    finally:
        gdi.SelectObject(dc, previous)
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(dc)


@previous_window
class AppIconTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows GUI handle accounting")
    def test_repeated_window_mapping_does_not_accumulate_gui_handles(self):
        import gc
        user = ctypes.WinDLL("user32")
        user.GetGuiResources.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        kernel = ctypes.WinDLL("kernel32")
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        process = kernel.GetCurrentProcess()
        baseline = None
        for _ in range(20):
            root = tk.Tk()
            root.attributes("-alpha", 0)
            try:
                self.assertTrue(app_icon.install(root))
                child = tk.Toplevel(root)
                child.attributes("-alpha", 0)
                root.update()
                root.withdraw()
                child.withdraw()
                root.update()
                root.deiconify()
                child.deiconify()
                root.update()
                self.assertTrue(app_icon.install(root))
            finally:
                root.destroy()
            del child, root
            gc.collect()
            observed = [user.GetGuiResources(process, kind) for kind in (0, 1)]
            if baseline is None:
                baseline = observed
            self.assertTrue(all(now <= before + 4 for now, before in zip(observed, baseline)),
                            (baseline, observed))

    def test_transparency_is_preserved_at_every_windows_icon_size(self):
        with Image.open(app_icon.asset_directory() / "luvbox.png") as image:
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getpixel((0, 0))[3], 0)
        with Image.open(app_icon.asset_directory() / "luvbox.ico") as image:
            self.assertEqual(image.ico.sizes(), {(n,n) for n in (16,24,32,48,64,128,256)})
            for size in image.ico.sizes():
                self.assertEqual(image.ico.getimage(size).getpixel((0,0))[3], 0)

    @unittest.skipUnless(os.name == "nt", "Windows taskbar and title bar")
    def test_root_and_child_windows_have_native_icon_handles(self):
        root = tk.Tk()
        try:
            root.attributes("-alpha", 0)
            self.assertTrue(app_icon.install(root))
            child = tk.Toplevel(root)
            child.attributes("-alpha", 0)
            root.update()
            user = ctypes.WinDLL("user32")
            send = user.SendMessageW
            send.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
            send.restype = ctypes.c_ssize_t
            get_class = user.GetClassLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user.GetClassLongW
            get_class.argtypes = (ctypes.c_void_p, ctypes.c_int)
            get_class.restype = ctypes.c_size_t
            user.LoadImageW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_uint)
            user.LoadImageW.restype = ctypes.c_void_p
            user.DestroyIcon.argtypes = (ctypes.c_void_p,)
            expected = [user.LoadImageW(None, str(app_icon.asset_directory() / "luvbox.ico"),
                                       1, size, size, 0x10) for size in (16, 32)]
            try:
                for remap in (False, True):
                    if remap:
                        root.withdraw()
                        child.withdraw()
                        root.update()
                        root.deiconify()
                        child.deiconify()
                        root.update()
                    for window in (root, child):
                        handle = int(window.wm_frame(), 16)
                        for slot, size, class_slot, reference in zip((0, 1), (16, 32), (-34, -14), expected):
                            with self.subTest(window=window, remap=remap, size=size):
                                wanted = rendered_icon(reference, size)
                                self.assertEqual(rendered_icon(send(handle, 0x007F, slot, 0), size), wanted)
                                self.assertEqual(rendered_icon(get_class(handle, class_slot), size), wanted)
                    for window in (root, child):
                        self.assertEqual(app_icon._taskbar_properties(int(window.wm_frame(), 16), None),
                                         app_icon._taskbar_values(str(app_icon.asset_directory() / "luvbox.ico")))
            finally:
                for icon in expected:
                    user.DestroyIcon(icon)
            get_id = ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID
            get_id.argtypes = (ctypes.POINTER(ctypes.c_void_p),)
            pointer = ctypes.c_void_p()
            self.assertEqual(get_id(ctypes.byref(pointer)), 0)
            try:
                self.assertEqual(ctypes.wstring_at(pointer), app_icon.APP_ID)
            finally:
                free = ctypes.windll.ole32.CoTaskMemFree
                free.argtypes = (ctypes.c_void_p,)
                free(pointer)
        finally:
            root.destroy()

    @unittest.skipUnless(os.name == "nt", "Windows wrapper lifecycle")
    def test_recreated_wrappers_and_dialogs_keep_the_application_identity(self):
        root = tk.Tk()
        root.attributes("-alpha", 0)
        try:
            self.assertTrue(app_icon.install(root))
            child = tk.Toplevel(root)
            child.attributes("-alpha", 0)
            root.update()
            expected = app_icon._taskbar_values(str(app_icon.asset_directory() / "luvbox.ico"))
            transitions = (
                lambda: root.attributes("-toolwindow", True),
                lambda: root.attributes("-toolwindow", False),
                lambda: root.overrideredirect(True),
                lambda: root.overrideredirect(False),
                lambda: child.transient(root),
                lambda: child.transient(""),
            )
            for change in transitions:
                change()
                root.update()
                for window in (root, child):
                    handle = int(window.wm_frame(), 16)
                    self.assertEqual(window._luvbox_taskbar_handle, handle)
                    self.assertEqual(app_icon._taskbar_properties(handle, None), expected)
            # A child gets the same cleanup as the root, before its HWND dies.
            from unittest.mock import patch
            original = app_icon._taskbar_properties
            cleared = []
            def tracked(handle, values):
                if values == {}:
                    cleared.append((handle, bool(child.winfo_exists())))
                return original(handle, values)
            child_handle = int(child.wm_frame(), 16)
            with patch.object(app_icon, "_taskbar_properties", side_effect=tracked):
                child.destroy()
            self.assertEqual(cleared, [(child_handle, True)])
        finally:
            root.destroy()

    def test_default_title_bar_close_clears_taskbar_properties_before_destroying(self):
        from unittest.mock import patch
        root = tk.Tk()
        root.withdraw()
        try:
            child = tk.Toplevel(root)
            calls = []
            def tracked(handle, values):
                calls.append((values, bool(child.winfo_exists())))
            with patch.object(app_icon, "_taskbar_properties", side_effect=tracked), \
                    patch.object(app_icon, "_taskbar_values", return_value={5: app_icon.APP_ID}):
                app_icon._windows_taskbar(child, "luvbox.ico")
                # Tk's default WM_DELETE_WINDOW would bypass the cleanup.
                child.tk.call(child.wm_protocol("WM_DELETE_WINDOW"))
            self.assertEqual(calls, [({5: app_icon.APP_ID}, True), ({}, True)])
            self.assertFalse(child.winfo_exists())
            # A window's own close handler is never replaced.
            other = tk.Toplevel(root)
            other.protocol("WM_DELETE_WINDOW", other.withdraw)
            handler = other.wm_protocol("WM_DELETE_WINDOW")
            with patch.object(app_icon, "_taskbar_properties"), \
                    patch.object(app_icon, "_taskbar_values", return_value={}):
                app_icon._windows_taskbar(other, "luvbox.ico")
            self.assertEqual(other.wm_protocol("WM_DELETE_WINDOW"), handler)
        finally:
            root.destroy()

    @unittest.skipUnless(os.name == "nt", "Windows taskbar properties")
    def test_taskbar_properties_are_removed_before_the_window_is_destroyed(self):
        from unittest.mock import patch
        root = tk.Tk()
        root.attributes("-alpha", 0)
        try:
            self.assertTrue(app_icon.install(root))
            clear = app_icon._taskbar_properties
            calls = []
            def tracked(handle, values):
                calls.append((values, bool(root.winfo_exists())))
                return clear(handle, values)
            with patch.object(app_icon, "_taskbar_properties", side_effect=tracked):
                root.destroy()
            self.assertEqual(calls, [({}, True)])
        finally:
            if root.tk.call("info", "commands", "."):
                root.destroy()


if __name__ == "__main__":
    unittest.main()
