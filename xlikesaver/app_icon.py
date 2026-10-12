"""Application identity and shared window icons, also in the frozen build."""

import ctypes
import os
from pathlib import Path
import tkinter as tk


APP_ID = "LuvBox.Desktop"


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
            root.iconbitmap(icon)
            root.iconbitmap(default=icon)
            # Tk/Windows can keep its stock class icon on independently mapped
            # Toplevels despite -default. Set each window's native icon as well.
            if not getattr(root, "_xlikesaver_icon_binding", False):
                def mapped(event):
                    if isinstance(event.widget, tk.Toplevel):
                        event.widget.iconbitmap(icon)
                root.bind_class("Toplevel", "<Map>", mapped, add="+")
                root._xlikesaver_icon_binding = True
        else:
            image = tk.PhotoImage(master=root, file=str(asset_directory() / "luvbox.png"))
            root.iconphoto(True, image)
        return True
    except (OSError, AttributeError, tk.TclError):
        return False
