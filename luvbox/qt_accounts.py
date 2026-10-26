"""Two explicit login methods backed by the existing encrypted session store."""

import re
import threading
import time
import math

from PySide6.QtCore import QObject, Property, QTimer, Signal, Slot

from . import login
from .app_settings import SettingsRepository
from .browser_bridge import BrowserBridge, BrowserBridgeError
from .browser_discovery import open_browser_url


_PAIRING_CODE = re.compile(r"[A-Z0-9_-]{8}\Z", re.ASCII)
_CONNECT_ERROR = "연결하지 못했습니다. 확장 연결을 확인하고 다시 시도해 주세요."
_TOKEN_ERROR = "auth_token 값과 Windows 저장 권한을 확인해 주세요."
_ACCOUNT_ERROR = "계정을 변경하지 못했습니다. 다시 시도해 주세요."
_DISCONNECT_ERROR = "연결을 해제하지 못했습니다. 다시 시도해 주세요."
_CANCELLED = "연결을 취소했습니다."
_OPERATION_ERRORS = {"browser": _CONNECT_ERROR, "token": _TOKEN_ERROR,
                     "select": _ACCOUNT_ERROR, "disconnect": _DISCONNECT_ERROR}
_PROGRESS_TEXT = {
    "extension_ready": "확장이 준비됐습니다. 브라우저에서 연결 코드를 확인해 주세요.",
    "checking_account": "브라우저의 X 계정을 확인하는 중…",
    "login_required": "선택한 브라우저에서 X에 로그인한 뒤 ‘계정 다시 확인’을 눌러 주세요.",
    "account_changed": "X 계정이 바뀌었습니다. 브라우저에서 계정을 다시 확인해 주세요.",
    "account_error": "X 계정 확인을 마치지 못했습니다. 브라우저에서 계정을 다시 확인해 주세요.",
    "awaiting_approval": "브라우저에서 표시된 계정의 연결을 승인해 주세요.",
}
_HEALTH_TEXT = {
    "disconnected": "X 계정을 연결해 주세요",
    "saved": "연결 정보 저장됨 · 실제 사용은 아직 확인하지 않았습니다",
    "browser_confirmed": "브라우저 계정 확인됨 · 저장 시 연결을 확인합니다",
    "verified": "X 목록 접근 확인됨",
    "reconnect_required": "X 로그인이 만료됐습니다 · 다시 연결해 주세요",
}


class AccountController(QObject):
    changed = Signal()
    accountChanged = Signal()
    connectionCompleted = Signal()
    settled = Signal()
    shutdownFinished = Signal()
    _workerEvent = Signal(object)

    def __init__(self, parent=None, *, settings=None, browser_setup=None, persist_preferences=True):
        super().__init__(parent)
        self._settings = settings if settings is not None else SettingsRepository()
        # The product shell commits account + output folder together. Standalone
        # consumers retain the previous preference-writing behavior by default.
        self._persist_preferences = bool(persist_preferences)
        self._browser_setup = browser_setup
        self._accounts = []
        self._identity = {}
        self._pending_identity = {}
        self._connected = False
        self._busy = False
        self._locked = False
        self._status = ""
        self._error = ""
        self._pairing_code = ""
        self._pairing_url = ""
        self._approval_pending = False
        self._thread = None
        self._finished_event = None
        self._cancel = threading.Event()
        self._approval = threading.Event()
        self._operation = ""
        self._closing = False
        self._shutdown_emitted = False
        self._shutdown_callbacks = []
        self._attempt = 0
        self._connection_stage = "idle"
        self._extension_ready = False
        self._setup_needed = False
        self._deadline = 0.0
        self._extension_wait_started = 0.0
        self._remaining = 0
        self._authentication_state = "disconnected"
        self._workerEvent.connect(self._receive)
        self._retire_timer = QTimer(self)
        self._retire_timer.setInterval(25)
        self._retire_timer.timeout.connect(self._retire_worker)
        self._refresh()

    @Property("QVariantList", notify=changed)
    def accounts(self):
        return [dict(account) for account in self._accounts]

    @Property(str, notify=changed)
    def selectedAccount(self):
        return self._identity.get("user_id", "")

    @Property("QVariantMap", notify=changed)
    def identity(self):
        return dict(self._identity)

    @Property("QVariantMap", notify=changed)
    def pendingIdentity(self):
        return dict(self._pending_identity)

    @Property(bool, notify=changed)
    def connected(self):
        return self._connected

    @Property(bool, notify=changed)
    def busy(self):
        return self._busy

    @Property(bool, notify=changed)
    def locked(self):
        return self._locked

    @Property(str, notify=changed)
    def status(self):
        return self._status

    @Property(str, notify=changed)
    def error(self):
        return self._error

    @Property(str, notify=changed)
    def pairingCode(self):
        return self._pairing_code

    @Property(str, notify=changed)
    def pairingUrl(self):
        return self._pairing_url

    @Property(bool, notify=changed)
    def approvalPending(self):
        return self._approval_pending

    @Property(bool, notify=changed)
    def browserConnecting(self):
        return self._busy and self._operation == "browser"

    @Property(bool, notify=changed)
    def cancelling(self):
        return self.browserConnecting and self._cancel.is_set()

    @Property(str, notify=changed)
    def connectionStage(self):
        return self._connection_stage

    @Property(bool, notify=changed)
    def extensionReady(self):
        return self._extension_ready

    @Property(bool, notify=changed)
    def setupNeeded(self):
        return self._setup_needed

    @Property(int, notify=changed)
    def secondsRemaining(self):
        return self._remaining

    @Property(str, notify=changed)
    def authenticationState(self):
        return self._authentication_state

    @Property(str, notify=changed)
    def authenticationLabel(self):
        return _HEALTH_TEXT[self._authentication_state]

    @Slot(str)
    def updateAuthentication(self, state):
        # Only the downloader's typed evidence changes health. Generic network,
        # permission and rate-limit failures must not invalidate a saved login.
        if self._closing or not self._connected or state not in {"verified", "reconnect_required"}:
            return
        if state != self._authentication_state:
            self._authentication_state = state
            self.changed.emit()

    @staticmethod
    def _read_state():
        return login.account_state()

    def _apply_state(self, state):
        self._accounts = state["accounts"]
        self._identity = state["identity"]
        self._connected = state["connected"]
        self._authentication_state = "saved" if self._connected else "disconnected"

    def _refresh(self):
        try:
            self._apply_state(self._read_state())
        except Exception:
            self._accounts = []
            self._identity = {}
            self._connected = False
            self._error = "저장된 연결을 읽지 못했습니다."
        self.changed.emit()

    @Slot(bool)
    def setLocked(self, locked):
        self._locked = bool(locked)
        self.changed.emit()

    def _start(self, operation, value=None):
        if self._closing or self._busy or self._locked or self._thread is not None:
            return False
        self._operation = operation
        self._attempt += 1
        self._finished_event = None
        self._cancel = threading.Event()
        self._approval = threading.Event()
        self._busy = True
        self._error = ""
        self._status = {"browser": "브라우저에서 연결해 주세요.",
                        "token": "연결 정보를 저장하는 중…", "select": "계정을 변경하는 중…",
                        "disconnect": "현재 연결을 해제하는 중…"}[operation]
        self._pending_identity = {}
        self._approval_pending = False
        self._pairing_code = ""
        self._pairing_url = ""
        self._connection_stage = "opening_browser" if operation == "browser" else "idle"
        self._extension_ready = self._setup_needed = False
        self._remaining = 0
        self._deadline = self._extension_wait_started = 0.0
        self._thread = threading.Thread(target=self._work, args=(operation, value, self._cancel, self._approval, self._attempt),
                                        name="LuvBox account connection", daemon=True)
        try:
            self._thread.start()
        except RuntimeError:
            self._thread = None
            self._busy = False
            self._operation = ""
            self._error = _OPERATION_ERRORS.get(operation, _CONNECT_ERROR)
            self._status = self._error
            self.changed.emit()
            self.settled.emit()
            return False
        self._retire_timer.start()
        self.changed.emit()
        return True

    @Slot(result=bool)
    @Slot(str, result=bool)
    def connectBrowser(self, browser_id=""):
        if not browser_id and self._browser_setup is not None:
            browser_id = self._browser_setup.selectedBrowser
        if browser_id not in {"chrome", "edge", "firefox"}:
            if not self._busy and not self._closing:
                self._error = "연결할 브라우저를 먼저 선택해 주세요."
                self.changed.emit()
            return False
        return self._start("browser", browser_id)

    @Slot()
    def approveBrowser(self):
        if (self._operation != "browser" or not self._busy or not self._approval_pending
                or self._closing or self._locked or self._cancel.is_set()):
            return
        self._approval_pending = False
        self._connection_stage = "saving"
        self._status = "연결 정보를 저장하는 중…"
        self._approval.set()
        self.changed.emit()

    @Slot()
    def cancelBrowser(self):
        if self._operation != "browser" or not self._busy or self._cancel.is_set():
            return
        self._cancel.set()
        self._approval_pending = False
        self._pending_identity = {}
        self._pairing_code = ""
        self._pairing_url = ""
        self._connection_stage = "cancelling"
        self._remaining = 0
        self._status = "연결을 취소하는 중…"
        self.changed.emit()

    @Slot(str, result=bool)
    def connectToken(self, token):
        return self._start("token", token)

    @Slot(str, result=bool)
    def selectAccount(self, user_id):
        if any(account["user_id"] == user_id for account in self._accounts):
            return self._start("select", user_id)
        if not self._busy and not self._closing and not self._locked:
            self._error = self._status = _ACCOUNT_ERROR
            self.changed.emit()
        return False

    @Slot(result=bool)
    def disconnectAccount(self):
        return self._start("disconnect")

    def _browser_connection(self, cancel, approval, browser_id, attempt):
        with BrowserBridge(timeout=180) as bridge:
            if cancel.is_set():
                raise login.SessionCancelled()
            code = bridge.pairing_code
            if not _PAIRING_CODE.fullmatch(code):
                raise ValueError("invalid pairing code")
            self._workerEvent.emit({"kind": "pairing", "code": code,
                                    "url": f"http://127.0.0.1:{bridge.port}/connect", "attempt": attempt})
            bridge.open_browser(opener=lambda url: open_browser_url(browser_id, url))
            session = bridge.wait(cancel=cancel, on_progress=lambda stage: self._workerEvent.emit({
                "kind": "progress", "stage": stage, "attempt": attempt}))
        if cancel.is_set():
            raise login.SessionCancelled()
        identity = login.normalize_identity(session.identity)
        self._workerEvent.emit({"kind": "approval", "identity": identity, "attempt": attempt})
        deadline = time.monotonic() + 180
        while not approval.wait(0.05):
            if cancel.is_set():
                raise login.SessionCancelled()
            if time.monotonic() >= deadline:
                raise TimeoutError("approval expired")
        if cancel.is_set():
            raise login.SessionCancelled()
        return login.save_authenticated_session(session.auth_token, identity, cancel=cancel)

    def _work(self, operation, value, cancel, approval, attempt):
        browser_id = value if operation == "browser" else ""
        result = {"kind": "finished", "operation": operation, "success": False, "cancelled": False,
                  "attempt": attempt}
        try:
            if cancel.is_set():
                raise login.SessionCancelled()
            if operation == "browser":
                self._browser_connection(cancel, approval, value, attempt)
            elif operation == "token":
                login.save_auth_token(value)
            elif operation == "select":
                login.select_account(value)
            elif operation == "disconnect":
                login.clear_active_session()
            else:
                raise ValueError("invalid account operation")
            result["success"] = True
        except login.SessionCancelled:
            result["cancelled"] = True
        except BrowserBridgeError as error:
            result["cancelled"] = cancel.is_set()
            if not result["cancelled"]:
                result["error"] = str(error)
        except TimeoutError:
            result["cancelled"] = cancel.is_set()
            if operation == "browser" and not result["cancelled"]:
                result["error"] = "계정 연결 승인 시간이 지났습니다. 다시 연결해 주세요."
        except Exception:
            result["cancelled"] = cancel.is_set()
        finally:
            value = None
            try:
                result["state"] = self._read_state()
            except Exception:
                result["state"] = {"accounts": [], "identity": {}, "connected": False}
                result["warning"] = "저장된 연결을 읽지 못했습니다. 다시 연결해 주세요."
            if (self._persist_preferences and result["success"] and operation != "disconnect"
                    and not result.get("warning")):
                preferences = {"auth_mode": "managed"}
                if result["state"]["identity"]:
                    preferences["account"] = result["state"]["identity"]["handle"]
                if operation == "browser":
                    preferences["connection_browser"] = browser_id
                try:
                    self._settings.update(preferences)
                except Exception:
                    result["warning"] = "연결은 저장했지만 설정을 저장하지 못했습니다."
            self._workerEvent.emit(result)

    @Slot(object)
    def _receive(self, event):
        if event.get("attempt") != self._attempt:
            return
        kind = event.get("kind")
        if kind == "pairing":
            if self.browserConnecting and not self._closing and not self._cancel.is_set():
                self._pairing_code = event["code"]
                self._pairing_url = event["url"]
                self._connection_stage = "extension_wait"
                self._status = "선택한 브라우저에서 연결 확장의 응답을 기다리는 중…"
                self._extension_wait_started = time.monotonic()
                self._deadline = self._extension_wait_started + 180
                self._remaining = 180
        elif kind == "progress":
            stage = event.get("stage")
            if self.browserConnecting and not self._closing and not self._cancel.is_set() and stage in _PROGRESS_TEXT:
                self._extension_ready = True
                self._setup_needed = False
                self._connection_stage = stage
                self._status = _PROGRESS_TEXT[stage]
        elif kind == "approval":
            if self.browserConnecting and not self._closing and not self._cancel.is_set():
                self._pending_identity = dict(event["identity"])
                self._approval_pending = True
                self._connection_stage = "app_approval"
                self._extension_ready = True
                self._setup_needed = False
                self._deadline = time.monotonic() + 180
                self._remaining = 180
                self._status = "브라우저에서 확인한 계정입니다. 아래에서 연결을 승인해 주세요."
        elif kind == "finished":
            if self._busy and event.get("operation") == self._operation:
                self._finished_event = event
                self._retire_worker()
            return
        self.changed.emit()

    def _complete_operation(self, event):
        previous_identity = self._identity
        previous_connected = self._connected
        previous_health = self._authentication_state
        self._pending_identity = {}
        self._approval_pending = False
        self._pairing_code = ""
        self._pairing_url = ""
        self._connection_stage = "idle"
        self._remaining = 0
        self._deadline = 0.0
        self._apply_state(event["state"])
        if (self._connected and previous_connected and self._identity == previous_identity
                and (not event["success"] or event["operation"] == "select")):
            self._authentication_state = previous_health
        if event["success"]:
            if event["operation"] == "disconnect":
                self._status = ("현재 계정 연결을 해제하고 저장된 다른 계정을 선택했습니다."
                                if self._connected else "연결을 해제했습니다.")
            elif event["operation"] == "select":
                self._status = "계정을 변경했습니다."
            elif event["operation"] == "token":
                self._status = "연결 정보를 저장했습니다. 실제 X 인증은 저장을 시작할 때 확인합니다."
            else:
                if self._connected:
                    self._authentication_state = "browser_confirmed"
                self._status = "로그인을 연결했습니다."
            self._error = event.get("warning", "")
        elif event["cancelled"]:
            self._status = _CANCELLED
            self._error = event.get("warning", "")
        else:
            self._error = event.get("error") or _OPERATION_ERRORS.get(event["operation"], _CONNECT_ERROR)
            self._status = self._error
        self.changed.emit()
        if event["success"]:
            self.accountChanged.emit()
            if event["operation"] == "browser" and not self._error and not self._closing:
                self.connectionCompleted.emit()

    def _retire_worker(self):
        if self.browserConnecting and not self._cancel.is_set() and self._deadline:
            remaining = max(0, math.ceil(self._deadline - time.monotonic()))
            changed = remaining != self._remaining
            self._remaining = remaining
            if (self._connection_stage == "extension_wait" and not self._setup_needed
                    and time.monotonic() - self._extension_wait_started >= 8):
                self._setup_needed = True
                self._status = "확장의 응답이 없습니다. 선택한 브라우저·프로필과 확장 설치를 확인해 주세요."
                changed = True
            if changed:
                self.changed.emit()
        if self._thread is not None and not self._thread.is_alive() and self._finished_event is not None:
            self._thread.join(timeout=0)
            event, self._finished_event = self._finished_event, None
            self._thread = None
            self._operation = ""
            self._busy = False
            self._retire_timer.stop()
            self._complete_operation(event)
            self.settled.emit()
        if self._closing and self._thread is None:
            self._finish_shutdown()

    @Slot()
    def shutdown(self, callback=None):
        if self._shutdown_emitted:
            if callback is not None:
                callback()
            return
        if callback is not None:
            self._shutdown_callbacks.append(callback)
        self._closing = True
        self._cancel.set()
        self._approval_pending = False
        self._pending_identity = {}
        self._pairing_code = ""
        self._pairing_url = ""
        self.changed.emit()
        if self._thread is None:
            self._finish_shutdown()
        else:
            self._retire_timer.start()

    def _finish_shutdown(self):
        if self._shutdown_emitted:
            return
        self._shutdown_emitted = True
        self.shutdownFinished.emit()
        callbacks, self._shutdown_callbacks = self._shutdown_callbacks, []
        for callback in callbacks:
            callback()
