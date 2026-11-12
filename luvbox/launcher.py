"""What the application knows about LuvBox.exe, the launcher that carries it.

Releases are one file: a small launcher (tools/launcher/Launcher.cs) with the
compressed application appended. The launcher unpacks a version once into
%LOCALAPPDATA%\\LuvBox\\app\\<version>\\ and starts the program there, so
`sys.executable` is that unpacked copy and changes with every version. The
path that stays the same is the launcher's own; it is what the scheduled task
runs and what an update replaces.

The last 84 bytes of LuvBox.exe locate the archive and carry its SHA-256 and
version. This module owns that format for the build tools and the updater.
"""

import ctypes
import hashlib
import os
from pathlib import Path
import re
import struct
import subprocess
import sys


FOOTER = struct.Struct("<8sIQQ32s16s8s")
FOOTER_HEAD = b"LUVBOXAR"
FOOTER_TAIL = b"RAXOBVUL"
FOOTER_FORMAT = 1
# The smallest launcher is far larger; anything shorter cannot hold a program.
_SMALLEST_PROGRAM = 4096
VERSION_SHAPE = re.compile(r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}\Z")
APP_IDENTIFIER = "WhiteCritter.LuvBox"
INNER_NAME = "LuvBox.exe"
MARKER_NAME = ".luvbox-app"
LAUNCHER_VARIABLE = "LUVBOX_LAUNCHER"
READY_VARIABLE = "LUVBOX_READY_EVENT"
_READY_NAME = re.compile(r"Local\\LuvBox-ready-[0-9a-f]{32}\Z")


def make_footer(offset: int, length: int, digest: bytes, version: str) -> bytes:
    if not VERSION_SHAPE.match(version) or len(digest) != 32 or offset < _SMALLEST_PROGRAM or length < 22:
        raise ValueError("Invalid launcher footer values")
    return FOOTER.pack(FOOTER_HEAD, FOOTER_FORMAT, offset, length, digest,
                       version.encode("ascii").ljust(16, b"\0"), FOOTER_TAIL)


def read_footer(path) -> dict | None:
    """Where the application archive sits in a LuvBox.exe; None for any other file.

    A file that claims to carry an archive but describes it inconsistently is
    damaged, not merely "something else", and raises ValueError.
    """
    with open(path, "rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        if size < FOOTER.size + _SMALLEST_PROGRAM:
            return None
        stream.seek(size - FOOTER.size)
        head, layout, offset, length, digest, version, tail = FOOTER.unpack(stream.read(FOOTER.size))
    if head != FOOTER_HEAD or tail != FOOTER_TAIL:
        return None
    try:
        text = version.rstrip(b"\0").decode("ascii")
    except UnicodeDecodeError:
        text = ""
    if (layout != FOOTER_FORMAT or offset < _SMALLEST_PROGRAM or length < 22
            or offset + length + FOOTER.size != size or not VERSION_SHAPE.match(text)):
        raise ValueError("The launcher's archive footer is damaged")
    return {"offset": offset, "length": length, "sha256": digest.hex(), "version": text}


def payload_digest(path, footer: dict) -> str:
    """SHA-256 of the appended archive itself, as the launcher checks it."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        stream.seek(footer["offset"])
        remaining = footer["length"]
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise ValueError("The launcher's archive is shorter than its footer says")
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def launcher_path() -> Path | None:
    """The LuvBox.exe that started this program, while it is still there.

    None when running from source or when the unpacked program was started
    directly. The file may already be a newer release: an update replaces it
    in place while this version keeps running from its own folder.
    """
    if not getattr(sys, "frozen", False):
        return None
    value = os.environ.get(LAUNCHER_VARIABLE, "")
    if not value or "\0" in value:
        return None
    path = Path(value)
    try:
        if not path.is_absolute() or not path.is_file():
            return None
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(sys.executable)):
            return None
        if read_footer(path) is None:
            return None
    except (OSError, ValueError):
        return None
    return path


def program_path() -> str:
    """What to start for "this LuvBox": the launcher, or this program without one."""
    launcher = launcher_path()
    return str(launcher) if launcher is not None else sys.executable


def spawn_detached(command) -> None:
    """Start a program that outlives this one: LuvBox again after an update or from a notice.

    A packaged Python program changes two things its children inherit: the
    folder Windows searches for DLLs, and variables that tell a packaged child
    it belongs to this instance. Neither must leak into the launcher or the
    version it starts next.
    """
    environment = os.environ.copy()
    for name in (READY_VARIABLE, LAUNCHER_VARIABLE):
        environment.pop(name, None)
    packaged = bool(getattr(sys, "frozen", False))
    if packaged:
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    kernel32 = None
    if packaged and os.name == "nt":
        try:
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.SetDllDirectoryW.argtypes = (ctypes.c_wchar_p,)
            kernel32.SetDllDirectoryW(None)
        except (OSError, AttributeError):
            kernel32 = None
    try:
        subprocess.Popen(command, env=environment, close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    finally:
        bundle = getattr(sys, "_MEIPASS", None)
        if kernel32 is not None and bundle:
            kernel32.SetDllDirectoryW(bundle)


def signal_ready() -> bool:
    """Tell the launcher its splash can go: the window has drawn, or an error box is up."""
    name = os.environ.pop(READY_VARIABLE, "")
    if os.name != "nt" or not _READY_NAME.match(name):
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenEventW.restype = ctypes.c_void_p
        kernel32.OpenEventW.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p)
        kernel32.SetEvent.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.OpenEventW(0x0002, 0, name)  # EVENT_MODIFY_STATE
        if not handle:
            return False
        try:
            return bool(kernel32.SetEvent(handle))
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError):
        return False


def set_process_identity() -> bool:
    """Group the splash and the application's windows under one taskbar button."""
    if os.name != "nt":
        return False
    try:
        shell32 = ctypes.WinDLL("shell32")
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = (ctypes.c_wchar_p,)
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        return shell32.SetCurrentProcessExplicitAppUserModelID(APP_IDENTIFIER) >= 0
    except (OSError, AttributeError):
        return False


class _Guid(ctypes.Structure):
    _fields_ = [("first", ctypes.c_uint32), ("second", ctypes.c_uint16), ("third", ctypes.c_uint16),
                ("rest", ctypes.c_ubyte * 8)]


class _PropertyKey(ctypes.Structure):
    _fields_ = [("format", _Guid), ("identifier", ctypes.c_uint32)]


class _PropVariant(ctypes.Structure):
    _fields_ = [("kind", ctypes.c_uint16), ("reserved", ctypes.c_uint16 * 3),
                ("pointer", ctypes.c_void_p), ("padding", ctypes.c_void_p)]


def _guid(text: str) -> _Guid:
    first, second, third, fourth, fifth = text.split("-")
    rest = bytes.fromhex(fourth + fifth)
    return _Guid(int(first, 16), int(second, 16), int(third, 16), (ctypes.c_ubyte * 8)(*rest))


# System.AppUserModel.*: the taskbar's name, command and icon for a window.
_APP_MODEL = "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"
_KEYS = {"command": 2, "icon": 3, "name": 4, "identifier": 5}
_STRING = 31  # VT_LPWSTR


class _WindowProperties:
    """The shell's property store of one window (IPropertyStore), for a few string values."""

    def __init__(self, window: int):
        self._store = ctypes.c_void_p()
        shell32 = ctypes.WinDLL("shell32")
        shell32.SHGetPropertyStoreForWindow.argtypes = (ctypes.c_void_p, ctypes.POINTER(_Guid),
                                                        ctypes.POINTER(ctypes.c_void_p))
        shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long
        interface = _guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")
        if shell32.SHGetPropertyStoreForWindow(window, ctypes.byref(interface), ctypes.byref(self._store)) < 0:
            raise OSError("The window has no property store")
        table = ctypes.cast(ctypes.cast(self._store, ctypes.POINTER(ctypes.c_void_p))[0],
                            ctypes.POINTER(ctypes.c_void_p))
        access = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_PropertyKey),
                                    ctypes.POINTER(_PropVariant))
        self._release = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(table[2])
        self._get = access(table[5])
        self._set = access(table[6])
        self._commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(table[7])

    def close(self):
        if self._store:
            self._release(self._store)
            self._store = ctypes.c_void_p()

    @staticmethod
    def _key(name):
        return _PropertyKey(_guid(_APP_MODEL), _KEYS[name])

    def set(self, name, text):
        value = _PropVariant()
        buffer = None
        if text is not None:
            # The store copies the string; this buffer only has to outlive the call.
            buffer = ctypes.create_unicode_buffer(text)
            value.kind = _STRING
            value.pointer = ctypes.cast(buffer, ctypes.c_void_p)
        if self._set(self._store, ctypes.byref(self._key(name)), ctypes.byref(value)) < 0:
            raise OSError("The window property was not accepted")

    def get(self, name):
        value = _PropVariant()
        if self._get(self._store, ctypes.byref(self._key(name)), ctypes.byref(value)) < 0:
            raise OSError("The window property could not be read")
        try:
            return ctypes.wstring_at(value.pointer) if value.kind == _STRING and value.pointer else None
        finally:
            ctypes.WinDLL("ole32").PropVariantClear(ctypes.byref(value))

    def commit(self):
        if self._commit(self._store) < 0:
            raise OSError("The window properties were not saved")


def mark_window(window: int, launcher) -> bool:
    """Make "pin to taskbar" on the application's window pin the launcher.

    Without this the pin points into this version's folder: it would keep
    starting the old version after an update, and break once that folder is
    cleaned up. None for `launcher` removes the values again before the
    window closes, as the shell asks.
    """
    if os.name != "nt" or not window:
        return False
    try:
        properties = _WindowProperties(int(window))
    except (OSError, AttributeError, ValueError):
        return False
    try:
        if launcher is None:
            values = {"command": None, "name": None, "icon": None, "identifier": None}
        else:
            path = str(launcher)
            values = {"command": f'"{path}"', "name": "LuvBox", "icon": f"{path},0",
                      "identifier": APP_IDENTIFIER}
        for name, text in values.items():
            properties.set(name, text)
        properties.commit()
        return True
    except OSError:
        return False
    finally:
        properties.close()


def window_identity(window: int) -> dict:
    """The taskbar values currently set on a window; for checks."""
    properties = _WindowProperties(int(window))
    try:
        return {name: properties.get(name) for name in _KEYS}
    finally:
        properties.close()
