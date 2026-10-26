"""Classify a transient local read lock without weakening file safeguards."""

import ctypes
import os
from pathlib import Path

from .recycling import file_signature
from .thumbcache import cloud_only


def is_read_locked(value: Path | str) -> bool:
    """Whether Windows currently refuses this regular file with error 32/33.

    Python's buffered readers may expose only errno EACCES for these failures.
    Check the native error rather than treating every permission error as a
    lock. The probe reads at most one byte and never returns its contents.
    """
    if os.name != "nt":
        return False
    from ctypes import wintypes

    try:
        path = Path(value)
        if not path.is_absolute():
            return False
        before = file_signature(path)
        if cloud_only(path):
            return False

        class Attributes(ctypes.Structure):
            _fields_ = (("st_file_attributes", wintypes.DWORD), ("st_reparse_tag", wintypes.DWORD))

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.GetFileInformationByHandleEx.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                                        ctypes.c_void_p, wintypes.DWORD)
        kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
        kernel.ReadFile.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
        kernel.ReadFile.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        # Read-only, share read/write/delete, OPEN_EXISTING, and do not follow
        # a leaf reparse point that could appear after the path validation.
        handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x80 | 0x200000, None)
        if handle in (None, wintypes.HANDLE(-1).value):
            error = ctypes.get_last_error()
            return error in {32, 33} and file_signature(path) == before
        try:
            attributes = Attributes()
            if not kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
                return False
            # Reject a redirected or cloud-only handle before reading data.
            if attributes.st_reparse_tag & 0x20000000 or cloud_only(path, status=attributes):
                return False
            buffer = ctypes.create_string_buffer(1)
            count = wintypes.DWORD()
            if kernel.ReadFile(handle, buffer, 1, ctypes.byref(count), None):
                return False
            error = ctypes.get_last_error()
            return error in {32, 33} and file_signature(path) == before
        finally:
            kernel.CloseHandle(handle)
    except (OSError, TypeError, ValueError):
        return False
