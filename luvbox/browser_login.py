"""Connect by signing in to X in a separate Microsoft Edge window.

The password is typed into Edge, never into this app. Edge runs with a new,
temporary profile, so the user's own browser profiles are never opened. Through
a pair of inherited anonymous pipes the app reads only the x.com
auth_token cookie, stores it with DPAPI exactly like a pasted token, closes the
window and deletes the temporary profile. No other site's data is kept.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .login import _x_cookies, save_auth_token
from .settings import app_directory


LOGIN_URL = "https://x.com/i/flow/login"
_PROFILE_PREFIX = "login-profile-"
_START_TIMEOUT = 20
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


class _PipeDevTools:
    """A CDP channel available only through the two inherited child handles.

    Both parent pipe ends are nonblocking (supported on Windows/Python 3.12),
    so a stopped browser cannot block reads or writes past the call deadline.
    No TCP listener, HTTP discovery or fallback endpoint exists.
    """

    _MAX_MESSAGE = 8 * 1024 * 1024
    _MAX_COMMAND = 64 * 1024

    def __init__(self):
        self._reader = self._writer = None
        self._child = []
        self._buffer = bytearray()
        self._next_id = 0
        try:
            child_read, self._writer = os.pipe()
            self._child.append(child_read)
            self._reader, child_write = os.pipe()
            self._child.append(child_write)
            os.set_blocking(self._reader, False)
            os.set_blocking(self._writer, False)
        except Exception:
            self.close()
            raise

    def launch(self, launch, arguments):
        if os.name != "nt":
            raise SignInError(NO_DEBUGGING)
        import msvcrt
        handles = [msvcrt.get_osfhandle(fd) for fd in self._child]
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {"handle_list": handles}
        flags = ["--remote-debugging-pipe",
                 "--remote-debugging-io-pipes=" + ",".join(str(handle & 0xFFFFFFFF) for handle in handles)]
        try:
            for fd in self._child:
                os.set_inheritable(fd, True)
            return launch([arguments[0], *flags, *arguments[1:]],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          close_fds=True, startupinfo=startup)
        finally:
            self._close_child_handles()

    def _close_child_handles(self):
        descriptors, self._child = self._child, []
        for fd in descriptors:
            try:
                os.close(fd)
            except OSError:
                pass

    def close(self):
        self._close_child_handles()
        for name in ("_reader", "_writer"):
            descriptor = getattr(self, name, None)
            setattr(self, name, None)
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        self._buffer.clear()

    @staticmethod
    def _wait(deadline, cancel):
        if cancel is not None and cancel.is_set():
            raise SignInError(CANCELLED)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("devtools timeout")
        if cancel is None:
            time.sleep(min(.02, remaining))
        elif cancel.wait(min(.02, remaining)):
            raise SignInError(CANCELLED)

    def _write(self, content, deadline, cancel):
        offset = 0
        while offset < len(content):
            self._wait_ready(deadline, cancel)
            try:
                written = os.write(self._writer, content[offset:])
            except BlockingIOError:
                self._wait(deadline, cancel)
                continue
            if not written:
                raise ConnectionError("devtools pipe closed")
            offset += written

    @staticmethod
    def _wait_ready(deadline, cancel):
        if cancel is not None and cancel.is_set():
            raise SignInError(CANCELLED)
        if time.monotonic() >= deadline:
            raise TimeoutError("devtools timeout")

    def _message(self, deadline, cancel):
        while True:
            self._wait_ready(deadline, cancel)
            end = self._buffer.find(0)
            if end >= 0:
                if end > self._MAX_MESSAGE:
                    raise ConnectionError("devtools message too large")
                content = bytes(self._buffer[:end])
                del self._buffer[:end + 1]
                return json.loads(content.decode("utf-8"))
            if len(self._buffer) > self._MAX_MESSAGE:
                raise ConnectionError("devtools message too large")
            try:
                chunk = os.read(self._reader, min(65536, self._MAX_MESSAGE + 1 - len(self._buffer)))
            except BlockingIOError:
                self._wait(deadline, cancel)
                continue
            if not chunk:
                raise ConnectionError("devtools pipe closed")
            self._buffer.extend(chunk)

    def call(self, method, *, timeout=5.0, params=None, session_id=None, cancel=None):
        self._next_id += 1
        ident = self._next_id
        command = {"id": ident, "method": method}
        if params is not None:
            command["params"] = params
        if session_id is not None:
            command["sessionId"] = session_id
        content = json.dumps(command, ensure_ascii=True).encode("utf-8") + b"\0"
        if len(content) > self._MAX_COMMAND:
            raise ValueError("devtools command too large")
        deadline = time.monotonic() + timeout
        self._write(content, deadline, cancel)
        while True:
            reply = self._message(deadline, cancel)
            if not isinstance(reply, dict) or reply.get("id") != ident or reply.get("sessionId") != session_id:
                continue
            if "error" in reply:
                raise ConnectionError("devtools command failed")
            result = reply.get("result", {})
            if not isinstance(result, dict):
                raise ValueError("invalid devtools result")
            return result


def devtools_call(connection, method, *, timeout=5.0, params=None, session_id=None, cancel=None):
    return connection.call(method, timeout=timeout, params=params, session_id=session_id, cancel=cancel)


def _x_token(cookies) -> str:
    for cookie in _x_cookies(cookies):
        if cookie["name"] == "auth_token" and cookie["path"] == "/":
            return cookie["value"]
    return ""


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


def _recover_blank_login(connection, pages, *, cancel=None) -> bool:
    """Retry a blank X login page through a flattened, pipe-only CDP session."""
    paths = {"/i/flow/login", "/login", "/i/jf/onboarding/web"}
    for page in pages:
        url, target = page.get("url", ""), page.get("targetId", "")
        if not isinstance(url, str) or not isinstance(target, str) or not target:
            continue
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "x.com" or parsed.username or
                parsed.port not in (None, 443) or parsed.path not in paths):
            continue
        attached = devtools_call(connection, "Target.attachToTarget",
                                 params={"targetId": target, "flatten": True}, cancel=cancel)
        session = attached.get("sessionId")
        if not isinstance(session, str) or not session or len(session) > 256:
            raise ValueError("invalid devtools session")
        try:
            result = devtools_call(connection, "Runtime.evaluate", session_id=session, cancel=cancel, params={
                "expression": "({ready: document.readyState !== 'loading', blank: !!document.body && "
                              "!document.body.innerText.trim() && !document.querySelector('input')})",
                "returnByValue": True})
            state = result.get("result", {}).get("value")
            if not isinstance(state, dict) or state.get("ready") is not True:
                continue
            if state.get("blank") is True:
                devtools_call(connection, "Page.reload", session_id=session,
                              params={"ignoreCache": True}, cancel=cancel)
            return True
        finally:
            try:
                devtools_call(connection, "Target.detachFromTarget", params={"sessionId": session}, timeout=3)
            except (OSError, ValueError):
                pass
    return False


def _stop_profile_processes(profile: Path) -> None:
    """Edge may hand off to another PID; stop only this unique profile's browser."""
    if os.name != "nt":
        return
    # Pass the path as environment data, never as interpolated PowerShell code.
    script = r'''
$needle = '--user-data-dir=' + $env:LUVBOX_LOGIN_PROFILE
$quoted = '--user-data-dir="' + $env:LUVBOX_LOGIN_PROFILE + '"'
$whole = '"--user-data-dir=' + $env:LUVBOX_LOGIN_PROFILE + '"'
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
                       env=dict(os.environ, LUVBOX_LOGIN_PROFILE=str(profile)),
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def sign_in(cancel: threading.Event, *, timeout: float = 600, edge: Path | None = None,
            launch=subprocess.Popen, poll_interval: float = 1.0) -> None:
    """Sign in through a dedicated Edge profile and inherited private pipes."""
    edge = edge or find_edge()
    if edge is None:
        raise SignInError(NO_EDGE)
    if cancel.is_set():
        raise SignInError(CANCELLED)
    _check_profile_policy()
    directory = app_directory().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix=_PROFILE_PREFIX, dir=directory))
    process = connection = None
    try:
        connection = _PipeDevTools()
        process = connection.launch(launch, [str(edge), f"--user-data-dir={profile}",
                                            "--disable-background-mode", "--no-first-run",
                                            "--no-default-browser-check", "--disable-sync", "--new-window", LOGIN_URL])
        started = time.monotonic()
        try:
            devtools_call(connection, "Browser.getVersion", timeout=_START_TIMEOUT, cancel=cancel)
        except (OSError, ValueError):
            raise SignInError(NO_DEBUGGING) from None
        deadline = time.monotonic() + timeout
        recovery_checked = False
        while True:
            if cancel.is_set():
                raise SignInError(CANCELLED)
            if time.monotonic() > deadline:
                raise SignInError(TIMED_OUT)
            try:
                token = _x_token(devtools_call(connection, "Storage.getCookies", cancel=cancel).get("cookies"))
                pages = [target for target in devtools_call(connection, "Target.getTargets", cancel=cancel).get("targetInfos", ())
                         if isinstance(target, dict) and target.get("type") == "page"]
            except OSError:
                raise SignInError(CLOSED) from None
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
            if not recovery_checked and time.monotonic() - started >= 10:
                try:
                    recovery_checked = _recover_blank_login(connection, pages, cancel=cancel)
                except (OSError, ValueError):
                    pass
                recovery_checked |= time.monotonic() - started >= 60
            cancel.wait(poll_interval)
    finally:
        closed = False
        if connection is not None:
            if process is not None:
                try:
                    devtools_call(connection, "Browser.close", timeout=3)
                    closed = True
                except (OSError, ValueError):
                    pass
            connection.close()
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
        for _ in range(25):
            if profile.is_symlink() or profile.is_junction() or profile.resolve().parent != directory:
                break
            shutil.rmtree(profile, ignore_errors=True)
            if not profile.exists():
                break
            time.sleep(0.2)
