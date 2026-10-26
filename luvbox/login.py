"""Locally validate and encrypt manually supplied X authentication cookies.

This module does not launch a browser or connect to X. New sessions use the
LuvBox DPAPI format; prior headers and entropy remain readable for migration.
"""

from __future__ import annotations

import ctypes
from contextlib import contextmanager
import json
import math
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from .file_mutex import file_mutex, replace_file


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
_ACCOUNT_HELP = "연결된 계정을 확인하지 못했습니다. 계정을 다시 선택해 주세요."
_USER_ID = re.compile(r"[1-9][0-9]{0,31}\Z")
_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}\Z")
_SESSION_LOCK = threading.RLock()
_MAX_ACCOUNTS = 100


@contextmanager
def _session_mutation():
    # Atomic replacement prevents partial ciphertext, while this process lock
    # also prevents another app window from overwriting a newer account list.
    with _SESSION_LOCK:
        with file_mutex(session_path()):
            yield


class SessionCancelled(ValueError):
    """A cancelled connection must not replace the last saved session."""


def _check_cancelled(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise SessionCancelled("로그인 연결을 취소했습니다.")


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


def _write_payload(payload: dict, *, cancel=None) -> None:
    _check_cancelled(cancel)
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > _MAX_SESSION_BYTES // 2:
        raise ValueError(_STORE_HELP)
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
        _check_cancelled(cancel)
        replace_file(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _read_payload() -> dict:
    with session_path().open("rb") as file:
        encrypted = file.read(_MAX_SESSION_BYTES + 1)
    if len(encrypted) > _MAX_SESSION_BYTES:
        raise ValueError(_SESSION_HELP)
    if encrypted.startswith(_HEADER):
        decrypted = _dpapi(encrypted[len(_HEADER):], protect=False)
    elif encrypted.startswith(_LEGACY_HEADER):
        decrypted = _dpapi(encrypted[len(_LEGACY_HEADER):], protect=False, entropy=_LEGACY_ENTROPY)
    else:
        raise ValueError(_SESSION_HELP)
    payload = json.loads(decrypted.decode("utf-8"))
    if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
        raise ValueError(_SESSION_HELP)
    return payload


def normalize_identity(identity: object) -> dict:
    """Validate identity obtained from the authenticated browser, never a token.

    The caller must establish that this is the signed-in user's profile. These
    checks validate shape and bounded text; they do not authenticate a username.
    """
    if not isinstance(identity, dict):
        raise ValueError(_ACCOUNT_HELP)
    user_id, handle = identity.get("user_id"), identity.get("handle")
    display_name = identity.get("display_name", handle)
    if (not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id) or
            not isinstance(handle, str) or not _HANDLE.fullmatch(handle) or
            not isinstance(display_name, str) or len(display_name) > 200 or
            any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in display_name)):
        raise ValueError(_ACCOUNT_HELP)
    return {"user_id": user_id, "handle": handle.lower(),
            "display_name": display_name.strip() or handle}


def _account_records(payload: dict) -> list[dict]:
    rows = payload.get("accounts", [])
    if not isinstance(rows, list) or len(rows) > _MAX_ACCOUNTS:
        raise ValueError(_SESSION_HELP)
    records = {}
    # Include early identity-aware sessions which did not yet have an account
    # collection, and prefer the active copy if a previous writer left it newer.
    for row in [*rows, payload]:
        if not isinstance(row, dict) or "identity" not in row:
            continue
        try:
            identity = normalize_identity(row["identity"])
        except ValueError:
            continue
        clean = _x_cookies(row.get("cookies"))
        if _has_auth(clean):
            records[identity["user_id"]] = {"identity": identity, "cookies": clean}
    return list(records.values())


def _existing_accounts() -> list[dict]:
    try:
        return _account_records(_read_payload())
    except FileNotFoundError:
        # A new login may create an absent store, but must never replace one
        # whose existing accounts could not be read or decrypted.
        return []


def _store_session_cookies(cookies: list[dict]) -> None:
    clean = _x_cookies(cookies)
    if not _has_auth(clean):
        raise ValueError(_SESSION_HELP)
    with _session_mutation():
        payload = {"version": 1, "cookies": clean}
        accounts = _existing_accounts()
        if accounts:
            payload["accounts"] = accounts
        _write_payload(payload)


def save_authenticated_session(token: str, identity: dict, *, cookies: list[dict] | None = None,
                               cancel=None) -> dict:
    """Store a browser-verified identity and session together, then select it.

    All account cookies and names stay inside the existing current-user DPAPI
    file. Its v1 active-cookie fields remain readable by older app versions.
    """
    _check_cancelled(cancel)
    normalized = normalize_identity(identity)
    if not isinstance(token, str) or not 1 <= len(token) <= _MAX_TOKEN_LENGTH or any(
            ord(char) < 33 or ord(char) > 126 or char in '\",;\\' for char in token):
        raise ValueError(_TOKEN_HELP)
    clean = _x_cookies(cookies) if cookies is not None else [{
        "name": "auth_token", "value": token, "domain": ".x.com", "path": "/",
        "expires": -1, "secure": True, "httpOnly": True}]
    tokens = [item["value"] for item in clean if item["name"] == "auth_token" and item["path"] == "/"]
    if not tokens or any(value != token for value in tokens):
        raise ValueError(_TOKEN_HELP)
    with _session_mutation():
        try:
            accounts = [row for row in _existing_accounts()
                        if row["identity"]["user_id"] != normalized["user_id"]]
            if len(accounts) >= _MAX_ACCOUNTS:
                raise ValueError(_STORE_HELP)
            accounts.append({"identity": normalized, "cookies": clean})
            _write_payload({"version": 1, "cookies": clean, "identity": normalized,
                            "accounts": accounts}, cancel=cancel)
        except SessionCancelled:
            raise
        except (OSError, ValueError, TypeError):
            raise ValueError(_STORE_HELP) from None
    return dict(normalized)


def _active_identity(payload: dict) -> dict | None:
    if not _has_auth(_x_cookies(payload.get("cookies"))):
        return None
    try:
        return normalize_identity(payload.get("identity"))
    except ValueError:
        return None


def account_state() -> dict:
    """Read public account and connection state from one encrypted snapshot.

    Another app window can atomically replace the store between reads. Keeping
    the list, active identity and connection flag together also decrypts the
    session only once, and never returns cookie values to the UI.
    """
    with _SESSION_LOCK:
        try:
            payload = _read_payload()
            active = _active_identity(payload)
            active_id = active["user_id"] if active else None
            accounts = [dict(row["identity"], active=row["identity"]["user_id"] == active_id)
                        for row in _account_records(payload)]
            return {"accounts": accounts, "identity": active or {},
                    "connected": _has_auth(_x_cookies(payload.get("cookies")))}
        except FileNotFoundError:
            return {"accounts": [], "identity": {}, "connected": False}
        except (OSError, ValueError, UnicodeError, TypeError):
            raise ValueError(_SESSION_HELP) from None


def active_account() -> dict | None:
    """Return verified active identity, or None for legacy/manual sessions."""
    with _SESSION_LOCK:
        try:
            payload = _read_payload()
            return _active_identity(payload)
        except (OSError, ValueError, UnicodeError, TypeError):
            return None


def list_accounts() -> list[dict]:
    """List connected identities without exposing cookies or token values."""
    with _SESSION_LOCK:
        try:
            payload = _read_payload()
            active = _active_identity(payload)
            active_id = active["user_id"] if active else None
            return [dict(row["identity"], active=row["identity"]["user_id"] == active_id)
                    for row in _account_records(payload)]
        except (OSError, ValueError, UnicodeError, TypeError):
            return []


def select_account(user_id: str) -> dict:
    """Atomically make an already connected account the downloader session."""
    if not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id):
        raise ValueError(_ACCOUNT_HELP)
    with _session_mutation():
        try:
            accounts = _account_records(_read_payload())
            selected = next(row for row in accounts if row["identity"]["user_id"] == user_id)
            _write_payload({"version": 1, **selected, "accounts": accounts})
            return dict(selected["identity"])
        except (OSError, ValueError, UnicodeError, TypeError, StopIteration):
            raise ValueError(_ACCOUNT_HELP) from None


def remove_account(user_id: str) -> None:
    """Remove one account; select another if the removed account was active."""
    if not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id):
        raise ValueError(_ACCOUNT_HELP)
    with _session_mutation():
        try:
            payload = _read_payload()
            accounts = [row for row in _account_records(payload)
                        if row["identity"]["user_id"] != user_id]
            active = payload.get("identity")
            if isinstance(active, dict) and active.get("user_id") == user_id:
                if not accounts:
                    session_path().unlink(missing_ok=True)
                    return
                payload = {"version": 1, **accounts[0]}
            payload["accounts"] = accounts
            _write_payload(payload)
        except FileNotFoundError:
            return
        except (OSError, ValueError, UnicodeError, TypeError):
            raise ValueError(_ACCOUNT_HELP) from None


def clear_active_session() -> None:
    """Disconnect the current login while retaining other connected accounts.

    A manual token has no verified identity and is not part of ``accounts``.
    Clearing it must therefore leave the encrypted account collection intact,
    without silently connecting one of those accounts in its place.
    """
    with _session_mutation():
        try:
            payload = _read_payload()
            active = _active_identity(payload)
            if active is not None:
                remove_account(active["user_id"])
                return
            accounts = _account_records(payload)
            if accounts:
                _write_payload({"version": 1, "cookies": [], "accounts": accounts})
            else:
                clear_session()
        except FileNotFoundError:
            return
        except (OSError, ValueError, UnicodeError, TypeError):
            raise ValueError(_ACCOUNT_HELP) from None


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


def load_session_cookies(*, account: str | None = None) -> list[dict]:
    """Decrypt a valid local session in memory or raise a secret-free error.

    A named account reads its own saved session without changing the global
    selection, so scheduled runs cannot accidentally use another account's
    login. Cookie-only sessions remain usable without claiming an identity.
    X can revoke an unexpired cookie; the downloader detects that on connection.
    """
    try:
        with _SESSION_LOCK:
            payload = _read_payload()
            if account is not None:
                if not isinstance(account, str) or not _HANDLE.fullmatch(account):
                    raise ValueError(_ACCOUNT_HELP)
                if "identity" in payload:
                    matches = [row for row in _account_records(payload)
                               if row["identity"]["handle"] == account.lower()]
                    if len(matches) == 1:
                        return matches[0]["cookies"]
                    raise ValueError(_ACCOUNT_HELP)
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
    with _session_mutation():
        session_path().unlink(missing_ok=True)


