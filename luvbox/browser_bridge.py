"""An explicit, one-shot bridge from the user's own browser extension.

The loopback listener is bound *before* a URL is opened. A 256-bit capability
appears only in its URL fragment, never a query, response, log, or token URL.
Only the extension receives X cookies; the bridge page never receives them.
HTTP carries AES-GCM ciphertext and a derived HMAC, never the capability or
plaintext cookie. Thus a process taking the port after cancellation gets no
credential. This module does not save credentials or make outbound requests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import hmac
import json
from pathlib import Path
import re
import secrets
import socket
import threading
import time
from urllib.parse import urlsplit
import webbrowser


_MAX_PAYLOAD = 8192
_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}\Z")
_USER_ID = re.compile(r"[1-9][0-9]{0,31}\Z")
_EXTENSION_HOST = re.compile(r"[a-zA-Z0-9-]{1,128}\Z")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+\Z")
_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LuvBox · 브라우저 연결</title></head><body>
<main><h1>LuvBox에 X 계정 연결</h1>
<p>현재 브라우저의 X 로그인을 사용합니다. 연결할 계정은 다음 화면에서 확인합니다.</p>
<p id="extension-status">LuvBox 연결 확장을 확인하고 있습니다. 확장이 없다면 앱에서
‘연결 확장 설치 안내’를 열어 안내에 따라 설치한 다음, 앱에서 연결을 다시 시작해 주세요.</p>
<p>브라우저의 비밀번호는 LuvBox에 전달되지 않습니다. 연결을 승인하면 X 로그인 쿠키와
계정 정보가 이 컴퓨터의 LuvBox에 전달됩니다. 창을 닫거나 앱에서 취소할 수 있습니다.</p>
</main></body></html>""".encode("utf-8")


class BrowserBridgeError(RuntimeError):
    """A fixed, credential-free error suitable for the connection UI."""


@dataclass(frozen=True)
class BrowserSession:
    # repr must never disclose a session cookie in debugger/error output.
    auth_token: str = field(repr=False)
    username: str
    user_id: str
    display_name: str = ""

    @property
    def identity(self) -> dict:
        return {"handle": self.username, "user_id": self.user_id,
                "display_name": self.display_name}


def extension_directory() -> Path:
    """Source and PyInstaller both keep extension files under assets/."""
    return Path(__file__).resolve().parent.parent / "assets" / "browser-extension"


def _extension_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (parsed.scheme in {"chrome-extension", "moz-extension"} and
                bool(_EXTENSION_HOST.fullmatch(parsed.netloc)) and
                not parsed.path and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def _session(payload: object) -> BrowserSession:
    if (not isinstance(payload, dict) or payload.get("version") != 1 or
            isinstance(payload.get("version"), bool) or
            set(payload) != {"version", "auth_token", "username", "user_id", "display_name"}):
        raise ValueError("invalid bridge payload")
    token, handle = payload["auth_token"], payload["username"]
    user_id, display_name = payload["user_id"], payload["display_name"]
    if (not isinstance(token, str) or not 1 <= len(token) <= 4096 or any(
            ord(char) < 33 or ord(char) > 126 or char in '\",;\\' for char in token) or
            not isinstance(handle, str) or not _HANDLE.fullmatch(handle) or
            not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id) or
            not isinstance(display_name, str) or len(display_name) > 200 or
            any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in display_name)):
        raise ValueError("invalid bridge payload")
    return BrowserSession(token, handle.lower(), user_id, display_name)


def _derive_key(secret: str, port: int, purpose: str) -> bytes:
    endpoint = f"http://127.0.0.1:{port}/session"
    return hashlib.sha256(f"LuvBox browser bridge {purpose} v1\0{endpoint}\0{secret}".encode("ascii")).digest()


def _decode(value: object, *, maximum: int) -> bytes:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not _BASE64URL.fullmatch(value):
        raise ValueError("invalid encrypted bridge payload")
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _decrypt(payload: object, secret: str, port: int) -> BrowserSession:
    if (not isinstance(payload, dict) or payload.get("version") != 1 or
            isinstance(payload.get("version"), bool) or
            set(payload) != {"version", "nonce", "ciphertext", "proof"}):
        raise ValueError("invalid encrypted bridge payload")
    nonce = _decode(payload["nonce"], maximum=16)
    cipher = _decode(payload["ciphertext"], maximum=8000)
    proof = _decode(payload["proof"], maximum=43)
    if len(nonce) != 12 or len(cipher) < 17 or len(proof) != 32:
        raise ValueError("invalid encrypted bridge payload")
    # An independent, constant-time HMAC check precedes the pinned library's
    # pure-Python GCM verification (which compares its tag as a Python list).
    # It also prevents unauthenticated requests from spending time in AES.
    expected = hmac.digest(_derive_key(secret, port, "proof"), nonce + cipher, "sha256")
    if not hmac.compare_digest(proof, expected):
        raise ValueError("invalid encrypted bridge payload")
    from gallery_dl.aes import aes_gcm_decrypt_and_verify_bytes
    raw = aes_gcm_decrypt_and_verify_bytes(cipher[:-16], _derive_key(secret, port, "encryption"), cipher[-16:], nonce)
    return _session(json.loads(raw))


class BrowserBridge:
    """A bounded pairing attempt; call start before open_browser or wait.

    Typical use, from a worker thread::

        with BrowserBridge() as bridge:
            bridge.open_browser()
            session = bridge.wait(cancel=cancel_event)
        save_authenticated_session(session.auth_token, session.identity)

    The caller must obtain the user's connection action before starting, and
    must handle extension installation separately. Nothing is auto-installed.
    """

    def __init__(self, *, timeout: float = 180):
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
            raise ValueError("browser pairing timeout must be 0–300 seconds")
        self._timeout = timeout
        self._secret = ""
        self._server = None
        self._thread = None
        self._deadline = 0.0
        self._result = None
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._closed = False

    def __repr__(self):
        return f"BrowserBridge(active={self._server is not None and not self._closed})"

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.close()

    @property
    def pairing_code(self) -> str:
        """Show this short code in the app and compare in the extension UI."""
        if not self._secret:
            raise BrowserBridgeError("브라우저 연결이 종료되었습니다. 다시 시작해 주세요.")
        return self._secret[:8].upper()

    @property
    def port(self) -> int:
        if self._server is None or self._closed:
            raise BrowserBridgeError("브라우저 연결이 종료되었습니다. 다시 시작해 주세요.")
        return self._server.server_port

    def start(self):
        if self._closed:
            raise BrowserBridgeError("브라우저 연결이 종료되었습니다. 다시 시작해 주세요.")
        if self._server is not None:
            return self
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            # HTTP/1.0 plus Connection: close gives each request a bounded life.
            timeout = 0.75

            def log_message(self, *_args):
                pass

            def _reply(self, code, content=b"", content_type="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
                self.send_header("Connection", "close")
                self.end_headers()
                if content:
                    self.wfile.write(content)

            def _valid_request(self, path):
                if (self.client_address[0] != "127.0.0.1" or
                        self.path != path or
                        self.headers.get_all("Host", []) != [f"127.0.0.1:{bridge._server.server_port}"] or
                        self.headers.get("Transfer-Encoding") is not None):
                    self._reply(400)
                    return False
                if bridge._closed or time.monotonic() >= bridge._deadline:
                    self._reply(410)
                    return False
                return True

            def do_GET(self):
                if self._valid_request("/connect"):
                    # No secret is ever returned to an unauthenticated requester.
                    self._reply(200, _PAGE, "text/html; charset=utf-8")

            def do_POST(self):
                if not self._valid_request("/session"):
                    return
                origins = self.headers.get_all("Origin", [])
                if len(origins) != 1 or not _extension_origin(origins[0]):
                    self._reply(403)
                    return
                lengths = self.headers.get_all("Content-Length", [])
                if (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,5}", lengths[0]) or
                        not 0 < int(lengths[0]) <= _MAX_PAYLOAD or
                        self.headers.get_all("Content-Type", []) != ["application/json"]):
                    self._reply(400)
                    return
                try:
                    raw = self.rfile.read(int(lengths[0]))
                    if len(raw) != int(lengths[0]):
                        raise ValueError("short payload")
                    result = _decrypt(json.loads(raw), bridge._secret, bridge._server.server_port)
                except (ValueError, UnicodeError, TimeoutError, OSError):
                    self._reply(400)
                    return
                with bridge._lock:
                    if (bridge._result is not None or bridge._closed or
                            time.monotonic() >= bridge._deadline):
                        self._reply(409)
                        return
                    bridge._result = result
                    bridge._secret = ""
                try:
                    self._reply(200, b'{"ok":true}')
                finally:
                    bridge._event.set()

        class Server(HTTPServer):
            allow_reuse_address = False

            def server_bind(self):
                # On Windows, another SO_REUSEADDR socket can otherwise steal
                # traffic even while this listener still owns its bound port.
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()

            def handle_error(self, *_args):
                # Never emit request bodies, headers or traceback locals.
                pass

        try:
            # Bind first: no probe/release/rebind gap and no externally supplied
            # address, fixed port, HTTP discovery or remote debugging fallback.
            self._server = Server(("127.0.0.1", 0), Handler)
            self._secret = secrets.token_urlsafe(32)
            self._deadline = time.monotonic() + self._timeout
            self._thread = threading.Thread(target=self._serve, name="LuvBox browser pairing", daemon=True)
            self._thread.start()
        except OSError:
            self.close()
            raise BrowserBridgeError("브라우저 연결을 시작하지 못했습니다. 다시 시도해 주세요.") from None
        return self

    def _serve(self):
        self._server.timeout = 0.1
        while not self._closed and not self._event.is_set() and time.monotonic() < self._deadline:
            self._server.handle_request()
        self._secret = ""
        self._server.server_close()
        self._event.set()

    def open_browser(self, *, opener=None):
        self.start()
        # The fragment is a one-time pairing capability, not an X credential.
        # The extension immediately removes it from the current history entry.
        url = f"http://127.0.0.1:{self.port}/connect#luvbox={self._secret}"
        try:
            opened = (webbrowser.open if opener is None else opener)(url)
        except Exception:
            self.close()
            raise BrowserBridgeError("기본 브라우저를 열지 못했습니다. 기본 브라우저 설정을 확인해 주세요.") from None
        if not opened:
            self.close()
            raise BrowserBridgeError("기본 브라우저를 열지 못했습니다. 기본 브라우저 설정을 확인해 주세요.")

    def wait(self, *, cancel=None) -> BrowserSession:
        self.start()
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    raise BrowserBridgeError("브라우저 연결을 취소했습니다.")
                if self._closed:
                    raise BrowserBridgeError("브라우저 연결을 취소했습니다.")
                if self._event.wait(0.05):
                    if cancel is not None and cancel.is_set():
                        raise BrowserBridgeError("브라우저 연결을 취소했습니다.")
                    if self._result is not None:
                        return self._result
                if time.monotonic() >= self._deadline:
                    raise BrowserBridgeError("브라우저 연결 시간이 지났습니다. 확장 설치를 확인한 뒤 다시 연결해 주세요.")
        finally:
            self.close()

    def close(self):
        self._closed = True
        self._event.set()
        self._secret = ""
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.1)
        if self._server is not None:
            self._server.server_close()
