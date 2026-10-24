"""Qt controls for inspecting and maintaining an existing daily save task."""

from collections import deque
from threading import Condition, Thread

from PySide6.QtCore import QObject, Property, Qt, QTimer, Signal, Slot

from .scheduled import _SCHEDULE_HELP, normal_time, register_task, task_status, unregister_task


_MESSAGES = {
    "refresh": "예약 정보를 확인하는 중입니다.",
    "remove": "자동 저장 예약을 제거하는 중입니다.",
    "migrate": "기존 예약을 현재 버전에 연결하는 중입니다.",
    "removed": "예약을 제거했습니다. 진행 중인 저장은 계속됩니다.",
    "migrated": "기존 예약을 현재 버전에 연결했습니다. 실행 시각은 유지됩니다.",
    "changed": "예약 상태가 바뀌었습니다. 확인 후 다시 시도해 주세요.",
    "locked": "진행 중인 작업이 끝난 뒤 다시 시도해 주세요.",
    "closing": "예약 작업을 마무리하는 중입니다.",
}


class ScheduleController(QObject):
    changed = Signal()
    shutdownFinished = Signal()
    _delivered = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False
        self._mutating = False
        self._locked = False
        self._known = False
        self._state = self._empty_state()
        self._status = ""
        self._error = ""
        self._closing = False
        self._shutdown_complete = False
        self._condition = Condition()
        self._pending = deque()
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(50)
        self._shutdown_timer.timeout.connect(self._poll_shutdown)
        self._delivered.connect(self._accept, Qt.ConnectionType.QueuedConnection)
        self._worker = Thread(target=self._work, name="LuvBox schedule maintenance", daemon=True)
        try:
            self._worker.start()
        except RuntimeError:
            self._error = self._status = _SCHEDULE_HELP
        QTimer.singleShot(0, self.refresh)

    @staticmethod
    def _empty_state():
        return {"registered": False, "enabled": False, "current": False,
                "running": False, "time": ""}

    @Property(bool, notify=changed)
    def busy(self):
        return self._busy

    @Property(bool, notify=changed)
    def mutating(self):
        return self._mutating

    @Property(bool, notify=changed)
    def locked(self):
        return self._locked

    @Property(bool, notify=changed)
    def known(self):
        return self._known

    @Property(bool, notify=changed)
    def registered(self):
        return self._state["registered"]

    @Property(bool, notify=changed)
    def enabled(self):
        return self._state["enabled"]

    @Property(bool, notify=changed)
    def current(self):
        return self._state["current"]

    @Property(bool, notify=changed)
    def running(self):
        return self._state["running"]

    @Property(str, notify=changed)
    def time(self):
        return self._state["time"]

    @Property(str, notify=changed)
    def summary(self):
        if not self._known:
            return "예약 정보를 다시 확인해 주세요." if self._error else "기존 자동 저장 예약을 확인하는 중입니다."
        if not self.registered:
            return "자동 저장 예약이 없습니다."
        schedule_time = "매일 " + self.time if self.time else "실행 시각 확인 필요"
        state = "실행 중" if self.running else "사용 중" if self.enabled else "사용 안 함"
        version = "현재 버전" if self.current else "이전 버전"
        return " · ".join((schedule_time, state, version))

    @Property(str, notify=changed)
    def status(self):
        return self._status

    @Property(str, notify=changed)
    def error(self):
        return self._error

    @Slot(bool)
    def setLocked(self, locked):
        with self._condition:
            value = bool(locked) or self._closing
            if value == self._locked:
                return
            self._locked = value
        self.changed.emit()

    def _queue(self, operation):
        with self._condition:
            if self._closing or self._busy:
                return False
            mutating = operation in {"remove", "migrate"}
            if mutating and (self._locked or not self._known or not self.registered):
                return False
            if operation == "migrate" and (not self.enabled or self.current or self.running or not self.time):
                return False
            if not self._worker.is_alive():
                self._known = False
                self._state = self._empty_state()
                self._error = self._status = _SCHEDULE_HELP
            else:
                self._busy = True
                self._mutating = mutating
                self._error = ""
                self._status = _MESSAGES[operation]
                self._pending.append(operation)
                self._condition.notify()
        self.changed.emit()
        return self._busy

    @Slot(result=bool)
    def refresh(self):
        return self._queue("refresh")

    @Slot(result=bool)
    def remove(self):
        return self._queue("remove")

    @Slot(result=bool)
    def useCurrentVersion(self):
        return self._queue("migrate")

    @staticmethod
    def _read_state():
        data = task_status()
        if not isinstance(data, dict) or not isinstance(data.get("registered"), bool):
            raise ValueError("invalid task status")
        state = ScheduleController._empty_state()
        if not data["registered"]:
            return state
        state.update(registered=True, enabled=data.get("enabled") is True,
                     current=data.get("current") is True, running=data.get("running") is True)
        try:
            state["time"] = normal_time(data.get("time", ""))
        except ValueError:
            pass
        return state

    def _can_mutate(self):
        with self._condition:
            return not self._closing and not self._locked

    def _perform(self, operation):
        state = self._read_state()
        if operation == "refresh":
            return state, ""
        if not self._can_mutate():
            return state, _MESSAGES["locked"]
        if operation == "remove":
            if state["registered"]:
                unregister_task()
            state = self._read_state()
            if state["registered"]:
                raise ValueError("task removal not reflected")
            return state, _MESSAGES["removed"]
        if (not state["registered"] or not state["enabled"] or state["current"]
                or state["running"] or not state["time"]):
            return state, _MESSAGES["changed"]
        schedule_time = normal_time(state["time"])
        register_task(schedule_time)
        state = self._read_state()
        if (not state["registered"] or not state["enabled"] or not state["current"]
                or state["time"] != schedule_time):
            raise ValueError("task migration not reflected")
        return state, _MESSAGES["migrated"]

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closing or self._pending)
                if self._closing:
                    return
                operation = self._pending.popleft()
            try:
                state, message = self._perform(operation)
                result = (state, message, False)
            except Exception:
                result = (self._empty_state(), _SCHEDULE_HELP, True)
            with self._condition:
                if self._closing:
                    return
            self._delivered.emit(result)

    @Slot(object)
    def _accept(self, result):
        if self._closing:
            return
        state, message, failed = result
        self._state = state
        self._known = not failed
        self._error = _SCHEDULE_HELP if failed else ""
        self._status = message
        self._busy = False
        self._mutating = False
        self.changed.emit()

    @Slot(result=bool)
    def shutdown(self):
        with self._condition:
            if self._closing:
                return self._shutdown_complete
            self._closing = True
            self._locked = True
            self._pending.clear()
            self._condition.notify_all()
        if self._busy:
            self._status = _MESSAGES["closing"]
        self.changed.emit()
        self._shutdown_timer.start()
        self._poll_shutdown()
        return self._shutdown_complete

    @Slot()
    def _poll_shutdown(self):
        if not self._closing or self._shutdown_complete or self._worker.is_alive():
            return
        self._shutdown_timer.stop()
        if self._worker.ident is not None:
            self._worker.join(timeout=0)
        self._busy = False
        self._mutating = False
        self._shutdown_complete = True
        self.changed.emit()
        self.shutdownFinished.emit()
