"""Use bundled fonts in this Windows process, without installing them."""

import ctypes
from functools import lru_cache
import os
from pathlib import Path


FAMILY = "Pretendard"
FALLBACK = "맑은 고딕"
FILES = ("Pretendard-Regular.ttf", "Pretendard-Bold.ttf")
FR_PRIVATE = 0x10


def asset_directory() -> Path:
    # PyInstaller preserves this path beneath its extracted application root.
    return Path(__file__).resolve().parents[1] / "assets" / "fonts" / "Pretendard"


@lru_cache(maxsize=1)
def load_family() -> str:
    """Register full Korean Regular/Bold fonts; keep a readable failure fallback.

    FR_PRIVATE limits use to this process. Windows removes these registrations
    when the process exits, so no admin access or system font changes are needed.
    """
    if os.name != "nt":
        return FALLBACK
    registered = []
    try:
        gdi = ctypes.WinDLL("gdi32")
        add = gdi.AddFontResourceExW
        add.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p)
        add.restype = ctypes.c_int
        remove = gdi.RemoveFontResourceExW
        remove.argtypes = add.argtypes
        remove.restype = ctypes.c_int
        for name in FILES:
            path = asset_directory() / name
            if not path.is_file() or not add(str(path), FR_PRIVATE, None):
                for previous in registered:
                    remove(previous, FR_PRIVATE, None)
                return FALLBACK
            registered.append(str(path))
        return FAMILY
    except (OSError, AttributeError):
        for previous in registered:
            try:
                remove(previous, FR_PRIVATE, None)
            except (OSError, AttributeError):
                pass
        return FALLBACK
