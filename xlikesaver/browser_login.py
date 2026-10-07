"""Connect by signing in to X in a separate Microsoft Edge window.

The password is typed into Edge, never into this app. Edge runs with a new,
temporary profile, so the user's own browser profiles are never opened. Through
that Edge's DevTools endpoint on 127.0.0.1 the app reads only the x.com
auth_token cookie, stores it with DPAPI exactly like a pasted token, closes the
window and deletes the temporary profile. No other site's data is kept.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from .login import _x_cookies, save_auth_token
from .settings import app_directory


LOGIN_URL = "https://x.com/i/flow/login"
_PROFILE_PREFIX = "login-profile-"
_START_TIMEOUT = 20
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
NO_EDGE = "Microsoft Edge를 찾지 못했습니다. ‘토큰 직접 입력’으로 연결해 주세요."
NO_DEBUGGING = "로그인 창에 연결하지 못했습니다(회사·학교 PC의 Edge 정책일 수 있음). ‘토큰 직접 입력’을 사용해 주세요."
CLOSED = "로그인 창이 닫혀 연결하지 않았습니다."
TIMED_OUT = "로그인을 기다리는 시간이 지나 창을 닫았습니다. 다시 시도해 주세요."
CANCELLED = "로그인 연결을 취소했습니다."


class SignInError(RuntimeError):
    """A fixed, secret-free message for the connect window."""


def find_edge() -> Path | None:
    """The installed msedge.exe (per-machine or per-user), if any."""
    candidates = []
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    if os.name == "nt":
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as key:
                    candidates.append(Path(winreg.QueryValue(key, None)))
            except OSError:
                pass
    return next((path for path in candidates if path.is_file()), None)


def _receive_exact(connection: socket.socket, size: int, deadline: float) -> bytes:
    data = bytearray()
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("devtools timeout")
        connection.settimeout(remaining)
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise ConnectionError("devtools connection closed")
        data.extend(chunk)
    return bytes(data)


def devtools_call(port: int, path: str, method: str, *, timeout: float = 5.0, params: dict | None = None) -> dict:
    """Send one DevTools command over a minimal WebSocket client.

    The endpoint is the browser's own one on 127.0.0.1. No Origin header is
    sent, which DevTools accepts from a local, non-browser client.
    """
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    deadline = time.monotonic() + timeout
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection:
        connection.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
                           .encode("ascii"))
        header = b""
        while b"\r\n\r\n" not in header:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("devtools timeout")
            connection.settimeout(remaining)
            chunk = connection.recv(1024)
            if not chunk or len(header) > 16384:
                raise ConnectionError("devtools handshake failed")
            header += chunk
        head, _, rest = header.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        accept = base64.b64encode(hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()).decode("ascii")
        fields = {name.strip().lower(): value.strip() for name, _, value in (line.partition(":") for line in lines[1:])}
        if " 101 " not in lines[0] + " " or fields.get("sec-websocket-accept") != accept:
            raise ConnectionError("devtools handshake failed")
        command = {"id": 1, "method": method}
        if params is not None:
            command["params"] = params
        payload = json.dumps(command).encode("utf-8")
        mask = os.urandom(4)
        if len(payload) < 126:
            frame = struct.pack("!BB", 0x81, 0x80 | len(payload))
        else:
            frame = struct.pack("!BBH", 0x81, 0x80 | 126, len(payload))
        connection.sendall(frame + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload)))
        buffered = rest
        message = b""

        def read(size):
            nonlocal buffered
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("devtools timeout")
            connection.settimeout(remaining)
            if len(buffered) >= size:
                data, buffered = buffered[:size], buffered[size:]
                return data
            data, buffered = buffered, b""
            return data + _receive_exact(connection, size - len(data), deadline)

        while True:
            first, second = read(2)
            opcode, length = first & 0x0F, second & 0x7F
            if length == 126:
                length = struct.unpack("!H", read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", read(8))[0]
            if length > 8 * 1024 * 1024 or len(message) + length > 8 * 1024 * 1024:
                raise ConnectionError("devtools message too large")
            data = read(length)
            if opcode == 0x8:
                raise ConnectionError("devtools connection closed")
            if opcode == 0x9:  # ping: answer and keep waiting
                pong_mask = os.urandom(4)
                connection.sendall(struct.pack("!BB", 0x8A, 0x80 | len(data)) + pong_mask +
                                   bytes(byte ^ pong_mask[index % 4] for index, byte in enumerate(data)))
                continue
            if opcode not in (0x0, 0x1):
                continue
            message += data
            if not first & 0x80:
                continue
            reply, message = json.loads(message.decode("utf-8")), b""
            if isinstance(reply, dict) and reply.get("id") == 1:
                if "error" in reply:
                    raise ConnectionError("devtools command failed")
                result = reply.get("result") or {}
                if not isinstance(result, dict):
                    raise ValueError("invalid devtools result")
                return result


def _x_token(cookies) -> str:
    for cookie in _x_cookies(cookies):
        if cookie["name"] == "auth_token" and cookie["path"] == "/":
            return cookie["value"]
    return ""


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _check_profile_policy() -> None:
    """A mandatory Edge directory policy overrides --user-data-dir."""
    if os.name != "nt":
        return
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Policies\Microsoft\Edge", 0, winreg.KEY_READ | view) as key:
                    value, _ = winreg.QueryValueEx(key, "UserDataDir")
            except OSError:
                continue
            if isinstance(value, str) and value.strip():
                raise SignInError(NO_DEBUGGING)


def _browser_endpoint(port: int) -> tuple[int, str]:
    # Loopback must not pass through the user's HTTP proxy.
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
        data = response.read(65537)
    if len(data) > 65536:
        raise ValueError("invalid endpoint")
    metadata = json.loads(data)
    if not isinstance(metadata, dict):
        raise ValueError("invalid endpoint")
    value = metadata.get("webSocketDebuggerUrl", "")
    if not isinstance(value, str):
        raise ValueError("invalid endpoint")
    url = urlsplit(value)
    if (url.scheme != "ws" or url.hostname not in {"127.0.0.1", "localhost"} or url.port != port or
            not url.path.startswith("/devtools/browser/") or url.query or url.fragment or url.username):
        raise ValueError("invalid endpoint")
    return port, url.path


def _recover_blank_login(port: int, pages: list) -> bool:
    """Retry an unrendered X login document once, without form changes."""
    for page in pages:
        url, target = page.get("url", ""), page.get("targetId", "")
        if not isinstance(url, str) or not url.startswith("https://x.com/") or not isinstance(target, str) or not target:
            continue
        if url.split("?", 1)[0] not in {LOGIN_URL, "https://x.com/login",
                                        "https://x.com/i/jf/onboarding/web"}:
            continue
        path = "/devtools/page/" + target
        result = devtools_call(port, path, "Runtime.evaluate", params={
            "expression": "({ready: document.readyState !== 'loading', blank: !!document.body && "
                          "!document.body.innerText.trim() && !document.querySelector('input')})",
            "returnByValue": True})
        state = result.get("result", {}).get("value")
        if not isinstance(state, dict) or state.get("ready") is not True:
            continue
        if state.get("blank") is True:
            devtools_call(port, path, "Page.reload", params={"ignoreCache": True})
        return True
    return False


def _stop_profile_processes(profile: Path) -> None:
    """Edge may hand off to another PID; stop only this unique profile's browser."""
    if os.name != "nt":
        return
    # Pass the path as environment data, never as interpolated PowerShell code.
    script = r'''
$needle = '--user-data-dir=' + $env:XLIKESAVER_LOGIN_PROFILE
$quoted = '--user-data-dir="' + $env:XLIKESAVER_LOGIN_PROFILE + '"'
$whole = '"--user-data-dir=' + $env:XLIKESAVER_LOGIN_PROFILE + '"'
Get-CimInstance Win32_Process -Filter "Name = 'msedge.exe'" | ForEach-Object {
    $tokens = $_.CommandLine
    if ($tokens -and ($tokens.Contains($quoted + ' ') -or $tokens.EndsWith($quoted) -or
        $tokens.Contains($whole + ' ') -or $tokens.EndsWith($whole) -or
        $tokens.Contains($needle + ' ') -or $tokens.EndsWith($needle))) {
        & taskkill.exe /PID $_.ProcessId /T /F 2>$null | Out-Null
    }
}
'''
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=dict(os.environ, XLIKESAVER_LOGIN_PROFILE=str(profile)),
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def sign_in(cancel: threading.Event, *, timeout: float = 600, edge: Path | None = None,
            launch=subprocess.Popen, poll_interval: float = 1.0) -> None:
    """Wait for the user to sign in to X in Edge and store its auth_token.

    Raises SignInError with a fixed message when no token was stored. The
    Cleanup closes only this attempt's Edge and removes its temporary profile.
    """
    edge = edge or find_edge()
    if edge is None:
        raise SignInError(NO_EDGE)
    if cancel.is_set():
        raise SignInError(CANCELLED)
    _check_profile_policy()
    directory = app_directory().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    # Another app instance may be signing in; clean only this unique profile.
    port = _unused_port()
    profile = Path(tempfile.mkdtemp(prefix=_PROFILE_PREFIX, dir=directory))
    process = None
    endpoint = None
    try:
        process = launch([str(edge), f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
                          "--remote-debugging-address=127.0.0.1", "--disable-background-mode",
                          "--no-first-run", "--no-default-browser-check", "--disable-sync", "--new-window", LOGIN_URL],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started = time.monotonic()
        while endpoint is None:
            if cancel.is_set():
                raise SignInError(CANCELLED)
            try:
                endpoint = _browser_endpoint(port)
            except (OSError, ValueError):
                # Edge's launcher can exit successfully while its browser
                # process continues. The endpoint is the lifecycle authority.
                if time.monotonic() - started > _START_TIMEOUT:
                    raise SignInError(NO_DEBUGGING) from None
                cancel.wait(0.2)
        deadline = time.monotonic() + timeout
        failed_since = None
        recovery_checked = False
        while True:
            if cancel.is_set():
                raise SignInError(CANCELLED)
            if time.monotonic() > deadline:
                raise SignInError(TIMED_OUT)
            try:
                token = _x_token(devtools_call(*endpoint, "Storage.getCookies").get("cookies"))
                pages = [target for target in devtools_call(*endpoint, "Target.getTargets").get("targetInfos", ())
                         if isinstance(target, dict) and target.get("type") == "page"]
                failed_since = None
            except OSError:
                failed_since = failed_since or time.monotonic()
                if time.monotonic() - failed_since > 5:
                    raise SignInError(CLOSED) from None
                token, pages = "", [None]
            except ValueError:
                raise SignInError(NO_DEBUGGING) from None
            if token:
                if cancel.is_set():
                    raise SignInError(CANCELLED)
                try:
                    save_auth_token(token)
                except ValueError as exc:
                    raise SignInError(str(exc)) from None
                return
            if not pages:
                raise SignInError(CLOSED)
            if not recovery_checked and failed_since is None and time.monotonic() - started >= 10:
                try:
                    recovery_checked = _recover_blank_login(endpoint[0], pages)
                except (OSError, ValueError):
                    pass  # Recovery is optional; cookie polling must keep working.
                recovery_checked |= time.monotonic() - started >= 60
            cancel.wait(poll_interval)
    finally:
        if endpoint is None and process is not None:
            # Cancellation can arrive before Edge writes its endpoint file.
            cleanup_deadline = time.monotonic() + 2
            while endpoint is None and time.monotonic() < cleanup_deadline:
                try:
                    endpoint = _browser_endpoint(port)
                except (OSError, ValueError):
                    time.sleep(0.1)
        closed = False
        if endpoint is not None:
            try:
                devtools_call(*endpoint, "Browser.close", timeout=3)
                closed = True
            except (OSError, ValueError):
                pass
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        if process is not None and not closed:
            _stop_profile_processes(profile)
        # Edge can release its profile files a moment after exiting.
        for _ in range(25):
            if profile.is_symlink() or profile.is_junction() or profile.resolve().parent != directory:
                break
            shutil.rmtree(profile, ignore_errors=True)
            if not profile.exists():
                break
            time.sleep(0.2)
