"""Local, per-run choices between the window and its download process."""

import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

from .recycling import file_signature


CONTROL_NAME = "review-control.json"
_HEARTBEAT_TIMEOUT = 15.0
# Antivirus scans or the parent's replace can hold the file for a moment, and
# after sleep/resume the parent needs up to a second to write a new heartbeat.
_READ_RETRY_SECONDS = 0.5
_STALE_GRACE_SECONDS = 1.2
_MAX_CONTROL_BYTES = 4096
_DETAIL_KEYS = ("tweet_id", "num", "author", "posted_at", "media_type", "filename")


class ReviewUnavailable(RuntimeError):
    """Confirmation cannot safely continue; never interpret it as approval."""


def _read(path):
    deadline = time.monotonic() + _READ_RETRY_SECONDS
    while True:
        try:
            with Path(path).open("rb") as source:
                content = source.read(_MAX_CONTROL_BYTES + 1)
            break
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise ReviewUnavailable("저장 확인 창과 연결하지 못했습니다.") from exc
            time.sleep(.05)
    try:
        if len(content) > _MAX_CONTROL_BYTES:
            raise ValueError("oversized control")
        value = json.loads(content)
        if (not isinstance(value, dict) or value.get("version") != 1 or
                type(value.get("enabled")) is not bool or
                not isinstance(value.get("session"), str) or len(value["session"]) != 32 or
                isinstance(value.get("heartbeat"), bool) or
                not isinstance(value.get("heartbeat"), (float, int)) or
                not math.isfinite(value["heartbeat"])):
            raise ValueError("invalid control")
        return value
    except (ValueError, TypeError) as exc:
        raise ReviewUnavailable("저장 확인 창과 연결하지 못했습니다.") from exc


class ReviewControl:
    """Thread-safe parent state. No Tk object is retained by this class."""

    def __init__(self, enabled=False):
        self._lock = threading.Lock()
        self._enabled = bool(enabled)
        self._path = None
        self._session = ""
        self._decision = None

    def _write(self):
        if self._path is None:
            return
        content = json.dumps({"version": 1, "session": self._session,
                              "enabled": self._enabled, "decision": self._decision,
                              "heartbeat": time.time()}, ensure_ascii=False).encode("utf8")
        staged = self._path.with_suffix(".tmp")
        try:
            with staged.open("wb") as output:
                output.write(content)
                output.flush()
            # A reader or an antivirus scan can briefly hold the old file.
            for attempt in range(25):
                try:
                    os.replace(staged, self._path)
                    break
                except PermissionError:
                    if attempt == 24:
                        raise
                    time.sleep(.02)
        finally:
            staged.unlink(missing_ok=True)

    def attach(self, path):
        with self._lock:
            self._path = Path(path)
            self._session = uuid.uuid4().hex
            self._decision = None
            self._write()

    def detach(self):
        with self._lock:
            self._path = None
            self._session = ""
            self._decision = None

    def set_enabled(self, enabled):
        with self._lock:
            self._enabled = bool(enabled)
            self._write()

    def respond(self, ident, action, *, enabled=None):
        if action not in {"save", "skip", "never", "stop"}:
            raise ValueError("invalid review decision")
        with self._lock:
            if enabled is not None:
                self._enabled = bool(enabled)
            self._decision = {"id": str(ident), "action": action}
            self._write()

    def heartbeat(self):
        with self._lock:
            self._write()


class ReviewChannel:
    """Child waits for this candidate's answer, checking stop and parent health."""

    def __init__(self, path, emit):
        self.path = Path(path)
        self.emit = emit
        self.session = _read(self.path)["session"]

    def _state(self):
        deadline = time.monotonic() + _STALE_GRACE_SECONDS
        while True:
            value = _read(self.path)
            if value["session"] != self.session:
                raise ReviewUnavailable("저장 확인 창의 연결이 종료되었습니다.")
            if -5 <= time.time() - value["heartbeat"] <= _HEARTBEAT_TIMEOUT:
                return value
            # Never approve on a stale state; allow one late heartbeat only.
            if time.monotonic() >= deadline:
                raise ReviewUnavailable("저장 확인 창의 연결이 종료되었습니다.")
            time.sleep(.1)

    def enabled(self):
        return self._state()["enabled"]

    def ask(self, path, media, check_stop):
        check_stop()
        if not self.enabled():
            return "save"
        path = Path(path)
        signature = file_signature(path)
        ident = uuid.uuid4().hex
        candidate = {key: media.get(key) for key in _DETAIL_KEYS}
        candidate.update(type="review-request", id=ident, path=str(path),
                         signature=list(signature), media_type=media.get("media_type") or media.get("type"))
        self.emit(candidate)
        while True:
            check_stop()
            state = self._state()
            decision = state.get("decision")
            action = None
            if isinstance(decision, dict) and decision.get("id") == ident:
                action = decision.get("action")
                if action not in {"save", "skip", "never", "stop"}:
                    raise ReviewUnavailable("저장 여부를 확인하지 못했습니다.")
            elif not state["enabled"]:
                action = "save"
            if action is not None:
                if action == "stop":
                    # The GUI also creates stop.flag, but allow its write to
                    # complete before reporting a missing cancellation signal.
                    for _ in range(5):
                        check_stop()
                        time.sleep(.05)
                    raise ReviewUnavailable("저장 확인을 중단했습니다.")
                if file_signature(path) != signature:
                    raise ReviewUnavailable("확인하던 임시 파일이 바뀌었습니다.")
                return action
            time.sleep(.05)
