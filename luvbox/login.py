"""Locally validate and encrypt manually supplied X authentication cookies.

This module does not launch a browser or connect to X. New sessions use the
LuvBox DPAPI format; prior headers and entropy remain readable for migration.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path


_HEADER = b"LuvBox-DPAPI-v1\0"
_ENTROPY = b"LuvBox.session.v1"
_LEGACY_HEADER = b"XlikeSaver-DPAPI-v1\0"
_LEGACY_ENTROPY = b"XlikeSaver.session.v1"
_MAX_SESSION_BYTES = 1024 * 1024
_COOKIE_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_SESSION_HELP = "저장한 auth_token 연결을 사용할 수 없습니다. auth_token을 다시 입력해 주세요."
_TOKEN_HELP = "auth_token 값을 확인해 주세요. 쿠키의 값만 입력하고 공백이나 따옴표는 포함하지 마세요."
_STORE_HELP = "auth_token 연결 정보를 암호화해 저장하지 못했습니다. Windows 사용자 폴더의 쓰기 권한을 확인해 주세요."
_MAX_TOKEN_LENGTH = 4096


def session_path() -> Path:
    """Return this app's per-user encrypted session file, never a Chrome file."""
    from .settings import app_directory
    return app_directory() / "session.bin"


def _dpapi(data: bytes, *, protect: bool, entropy: bytes | None = None) -> bytes:
    """Protect/unprotect bytes with Windows DPAPI's current-user scope."""
    if os.name != "nt":
        raise ValueError("auth_token 연결 정보 저장은 Windows에서 사용할 수 있습니다.")
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_pointer = ctypes.POINTER(DataBlob)
    protect_data = crypt32.CryptProtectData
    protect_data.argtypes = [blob_pointer, wintypes.LPCWSTR, blob_pointer,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_pointer]
    protect_data.restype = wintypes.BOOL
    unprotect_data = crypt32.CryptUnprotectData
    unprotect_data.argtypes = [blob_pointer, ctypes.c_void_p, blob_pointer,
                               ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_pointer]
    unprotect_data.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    source_buffer = ctypes.create_string_buffer(data)
    entropy_value = _ENTROPY if entropy is None else entropy
    entropy_buffer = ctypes.create_string_buffer(entropy_value)
    source = DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = DataBlob(len(entropy_value), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = DataBlob()
    try:
        # UI_FORBIDDEN; deliberately do not use LOCAL_MACHINE scope.
        if protect:
            ok = protect_data(ctypes.byref(source), "LuvBox X login",
                              ctypes.byref(entropy), None, None, 1, ctypes.byref(target))
        else:
            ok = unprotect_data(ctypes.byref(source), None, ctypes.byref(entropy),
                                None, None, 1, ctypes.byref(target))
        if not ok:
            raise ValueError(_SESSION_HELP)
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.memset(source_buffer, 0, len(source_buffer))
        if target.pbData:
            if not protect:
                ctypes.memset(target.pbData, 0, target.cbData)
            kernel32.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))


def _x_cookies(cookies: object) -> list[dict]:
    """Keep well-formed unexpired cookies usable by the x.com host only."""
    if not isinstance(cookies, list):
        return []
    result = []
    now = time.time()
    for cookie in cookies:
        if (not isinstance(cookie, dict) or not {
                "name", "value", "domain", "path", "expires", "secure", "httpOnly"
                }.issubset(cookie)):
            continue
        name, value = cookie.get("name"), cookie.get("value")
        domain, path = cookie.get("domain"), cookie.get("path", "/")
        expires = cookie.get("expires", -1)
        secure, http_only = cookie.get("secure", False), cookie.get("httpOnly", False)
        if (not isinstance(name, str) or not _COOKIE_NAME.fullmatch(name) or
                not isinstance(value, str) or not value or
                any(ord(char) < 32 or ord(char) == 127 for char in value) or
                not isinstance(domain, str) or domain.lower() not in {"x.com", ".x.com"} or
                not isinstance(path, str) or not path.startswith("/") or
                any(ord(char) < 32 or ord(char) == 127 for char in path) or
                isinstance(expires, bool) or not isinstance(expires, (int, float)) or
                expires < -1 or expires > 10**13 or
                not math.isfinite(expires) or (expires != -1 and expires <= now) or
                not isinstance(secure, bool) or not isinstance(http_only, bool)):
            continue
        result.append({"name": name, "value": value, "domain": domain.lower(),
                       "path": path, "expires": expires,
                       "secure": secure, "httpOnly": http_only})
    return result


def _has_auth(cookies: list[dict]) -> bool:
    return any(cookie["name"] == "auth_token" and cookie["path"] == "/" for cookie in cookies)


def _store_session_cookies(cookies: list[dict]) -> None:
    clean = _x_cookies(cookies)
    if not _has_auth(clean):
        raise ValueError(_SESSION_HELP)
    raw = json.dumps({"version": 1, "cookies": clean}, ensure_ascii=True,
                     separators=(",", ":")).encode("utf-8")
    encrypted = _HEADER + _dpapi(raw, protect=True)
    target = session_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # The temporary file contains ciphertext from its first write. Replacing
    # only after fsync preserves a previous working login on a failed write.
    descriptor, temporary_name = tempfile.mkstemp(prefix="session-", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(encrypted)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def save_auth_token(token: str) -> None:
    """Save a manually copied auth_token locally, without validating it on X.

    Accept bounded RFC 6265 cookie-octets rather than assuming a fixed token
    length or hexadecimal encoding. Invalid input and failed writes preserve any
    previous session. Neither values nor upstream exception text are logged.
    """
    if not isinstance(token, str):
        raise ValueError(_TOKEN_HELP)
    value = token.strip()
    if (not 1 <= len(value) <= _MAX_TOKEN_LENGTH or any(
            ord(char) < 33 or ord(char) > 126 or char in '\",;\\'
            for char in value)):
        raise ValueError(_TOKEN_HELP)
    cookies = [{"name": "auth_token", "value": value, "domain": ".x.com",
                "path": "/", "expires": -1, "secure": True, "httpOnly": True}]
    try:
        _store_session_cookies(cookies)
    except (OSError, ValueError, TypeError):
        raise ValueError(_STORE_HELP) from None


def load_session_cookies() -> list[dict]:
    """Decrypt a valid local session in memory or raise a secret-free error.

    Local validation detects expiry and damage; X can revoke a still-unexpired
    cookie, which is detected only when the downloader next connects to X.
    """
    try:
        with session_path().open("rb") as file:
            encrypted = file.read(_MAX_SESSION_BYTES + 1)
        if len(encrypted) > _MAX_SESSION_BYTES:
            raise ValueError
        if encrypted.startswith(_HEADER):
            decrypted = _dpapi(encrypted[len(_HEADER):], protect=False)
        elif encrypted.startswith(_LEGACY_HEADER):
            decrypted = _dpapi(encrypted[len(_LEGACY_HEADER):], protect=False, entropy=_LEGACY_ENTROPY)
        else:
            raise ValueError
        payload = json.loads(decrypted.decode("utf-8"))
        if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
            raise ValueError
        clean = _x_cookies(payload.get("cookies"))
        if not _has_auth(clean):
            raise ValueError
        return clean
    except (OSError, ValueError, UnicodeError, TypeError):
        raise ValueError(_SESSION_HELP) from None


def has_session() -> bool:
    """Check decryptability and cookie expiry without logging cookie values."""
    try:
        load_session_cookies()
        return True
    except (OSError, ValueError):
        return False


def clear_session() -> None:
    """Remove only this app's saved session; leave Chrome profiles untouched."""
    session_path().unlink(missing_ok=True)


