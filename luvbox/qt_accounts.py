"""Two explicit login methods backed by the existing encrypted session store."""

import re
import threading
import time

from PySide6.QtCore import QObject, Property, QTimer, Signal, Slot

from . import login
from .app_settings import SettingsRepository
from .browser_bridge import BrowserBridge, BrowserBridgeError


_PAIRING_CODE = re.compile(r"[A-Z0-9_-]{8}\Z", re.ASCII)
_CONNECT_ERROR = "연결하지 못했습니다. 확장 연결을 확인하고 다시 시도해 주세요."
_TOKEN_ERROR = "auth_token 값과 Windows 저장 권한을 확인해 주세요."
_ACCOUNT_ERROR = "계정을 변경하지 못했습니다. 다시 시도해 주세요."
_DISCONNECT_ERROR = "연결을 해제하지 못했습니다. 다시 시도해 주세요."
_CANCELLED = "연결을 취소했습니다."
_OPERATION_ERRORS = {"browser": _CONNECT_ERROR, "token": _TOKEN_ERROR,
                     "select": _ACCOUNT_ERROR, "disconnect": _DISCONNECT_ERROR}


class AccountController(QObject):
    changed = Signal()
    accountChanged = Signal()
    connectionCompleted = Signal()
    settled = Signal()
    shutdownFinished = Signal()
    _workerEvent = Signal(object)

    def __init__(self, parent=None, *, settings=None):
        super().__init__(parent)
        self._settings = settings if settings is not None else SettingsRepository()
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

    @staticmethod
    def _read_state():
        accounts = []
        for account in login.list_accounts():
            identity = login.normalize_identity(account)
            accounts.append({**identity, "active": account.get("active") is True})
        active = login.active_account()
        return {"accounts": accounts,
                "identity": login.normalize_identity(active) if active is not None else {},
                "connected": login.has_session()}

    def _apply_state(self, state):
        self._accounts = state["accounts"]
        self._identity = state["identity"]
        self._connected = state["connected"]

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
        self._thread = threading.Thread(target=self._work, args=(operation, value, self._cancel, self._approval),
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
    def connectBrowser(self):
        return self._start("browser")

    @Slot()
    def approveBrowser(self):
        if (self._operation != "browser" or not self._busy or not self._approval_pending
                or self._closing or self._locked or self._cancel.is_set()):
            return
        self._approval_pending = False
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

    def _browser_connection(self, cancel, approval):
        with BrowserBridge(timeout=180) as bridge:
            if cancel.is_set():
                raise login.SessionCancelled()
            code = bridge.pairing_code
            if not _PAIRING_CODE.fullmatch(code):
                raise ValueError("invalid pairing code")
            self._workerEvent.emit({"kind": "pairing", "code": code,
                                    "url": f"http://127.0.0.1:{bridge.port}/connect"})
            bridge.open_browser()
            session = bridge.wait(cancel=cancel)
        if cancel.is_set():
            raise login.SessionCancelled()
        identity = login.normalize_identity(session.identity)
        self._workerEvent.emit({"kind": "approval", "identity": identity})
        deadline = time.monotonic() + 180
        while not approval.wait(0.05):
            if cancel.is_set():
                raise login.SessionCancelled()
            if time.monotonic() >= deadline:
                raise TimeoutError("approval expired")
        if cancel.is_set():
            raise login.SessionCancelled()
        return login.save_authenticated_session(session.auth_token, identity, cancel=cancel)

    def _work(self, operation, value, cancel, approval):
        result = {"kind": "finished", "operation": operation, "success": False, "cancelled": False}
        try:
            if cancel.is_set():
                raise login.SessionCancelled()
            if operation == "browser":
                self._browser_connection(cancel, approval)
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
            if result["success"] and operation != "disconnect" and not result.get("warning"):
                preferences = {"auth_mode": "managed"}
                if result["state"]["identity"]:
                    preferences["account"] = result["state"]["identity"]["handle"]
                try:
                    self._settings.update(preferences)
                except Exception:
                    result["warning"] = "연결은 저장했지만 설정을 저장하지 못했습니다."
            self._workerEvent.emit(result)

    @Slot(object)
    def _receive(self, event):
        kind = event.get("kind")
        if kind == "pairing":
            if self.browserConnecting and not self._closing and not self._cancel.is_set():
                self._pairing_code = event["code"]
                self._pairing_url = event["url"]
                self._status = "확장의 연결 코드를 확인해 주세요."
        elif kind == "approval":
            if self.browserConnecting and not self._closing and not self._cancel.is_set():
                self._pending_identity = dict(event["identity"])
                self._approval_pending = True
                self._status = "계정과 연결 코드를 확인하고 승인해 주세요."
        elif kind == "finished":
            if self._busy and event.get("operation") == self._operation:
                self._finished_event = event
                self._retire_worker()
            return
        self.changed.emit()

    def _complete_operation(self, event):
        self._pending_identity = {}
        self._approval_pending = False
        self._pairing_code = ""
        self._pairing_url = ""
        self._apply_state(event["state"])
        if event["success"]:
            if event["operation"] == "disconnect":
                self._status = ("현재 계정 연결을 해제하고 저장된 다른 계정을 선택했습니다."
                                if self._connected else "연결을 해제했습니다.")
            elif event["operation"] == "select":
                self._status = "계정을 변경했습니다."
            elif event["operation"] == "token":
                self._status = "토큰 연결을 저장했습니다."
            else:
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
            if event["operation"] in {"browser", "token"} and not self._error and not self._closing:
                self.connectionCompleted.emit()

    def _retire_worker(self):
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
