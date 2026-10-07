"""Verify transparent multi-size assets and real Windows window icon handles."""

import ctypes
import os
import tkinter as tk
import unittest
from PIL import Image
from xlikesaver import app_icon


class AppIconTests(unittest.TestCase):
    def test_transparency_is_preserved_at_every_windows_icon_size(self):
        with Image.open(app_icon.asset_directory() / "xlikesaver.png") as image:
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getpixel((0, 0))[3], 0)
        with Image.open(app_icon.asset_directory() / "xlikesaver.ico") as image:
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
            send = ctypes.windll.user32.SendMessageW
            send.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
            send.restype = ctypes.c_ssize_t
            for window in (root, child):
                handle = int(window.wm_frame(), 16)
                for size in (0,1):
                    self.assertTrue(send(handle, 0x007F, size, 0))
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


if __name__ == "__main__":
    unittest.main()
