"""Bounded Windows process locks for atomic file read/modify/write operations.

Callers retain their thread locks. Other platforms keep that existing thread
protection; this app's cross-process store protection targets Windows.
"""

from contextlib import contextmanager
import hashlib
import math
import os
from pathlib import Path


_LOCK_ERROR = "다른 LuvBox 창이 정보를 저장하고 있습니다. 잠시 뒤 다시 시도해 주세요."


@contextmanager
def file_mutex(path, timeout=5.0):
    """Serialize writers to one resolved path without creating a lock file."""
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 <= timeout <= 60):
        raise ValueError("The store lock timeout must be between 0 and 60 seconds.")
    if os.name != "nt":
        yield
        return
    import ctypes
    from ctypes import wintypes

    try:
        key = os.path.normcase(str(Path(path).resolve())).encode("utf-8", "surrogatepass")
    except (OSError, RuntimeError, ValueError):
        raise OSError(_LOCK_ERROR) from None
    name = "Local\\LuvBoxStore-" + hashlib.sha256(key).hexdigest()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise OSError(_LOCK_ERROR)
    acquired = False
    try:
        outcome = kernel.WaitForSingleObject(handle, math.ceil(timeout * 1000))
        # WAIT_ABANDONED grants ownership after a previous writer crashed. File
        # replacement remains atomic, so its last complete snapshot is readable.
        if outcome not in (0, 0x80):
            raise OSError(_LOCK_ERROR)
        acquired = True
        yield
    finally:
        try:
            if acquired:
                kernel.ReleaseMutex(handle)
        finally:
            kernel.CloseHandle(handle)
