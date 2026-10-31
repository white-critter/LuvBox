"""Off-thread, explicitly confirmed moves of gallery files to the Windows Recycle Bin."""

from collections import deque
from pathlib import Path
from threading import Condition, Event, Thread

from PySide6.QtCore import QObject, Property, Qt, QTimer, Signal, Slot


class ManagementController(QObject):
    changed = Signal()
    shutdownFinished = Signal()
    deleteConfirmationRequested = Signal()
    libraryChanged = Signal()
    _delivered = Signal(object)

    def __init__(self, library, parent=None):
        super().__init__(parent)
        self._library = library
        self._context = ("", "", False)
        self._generation = 0
        self._mutating = self._locked = self._closing = False
        self._error = self._status = ""
        self._delete = None
        self._stop = Event()
        self._condition = Condition()
        self._pending = deque()
        self._delivered.connect(self._accept, Qt.ConnectionType.QueuedConnection)
        self._worker = Thread(target=self._work, name="LuvBox management", daemon=True)
        self._worker.start()
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(50)
        self._shutdown_timer.timeout.connect(self._poll_shutdown)

    @Property(bool, notify=changed)
    def mutating(self): return self._mutating
    @Property(bool, notify=changed)
    def locked(self): return self._locked or self._closing
    @Property(str, notify=changed)
    def error(self): return self._error
    @Property(str, notify=changed)
    def status(self): return self._status
    @Property(bool, notify=changed)
    def deleteConfirmationPending(self): return self._delete is not None
    @Property(int, notify=changed)
    def deleteCount(self): return len(self._delete["rows"]) if self._delete else 0
    @Property(str, notify=changed)
    def deleteSummary(self):
        if not self._delete: return ""
        names = [row.get("filename") or Path(row["path"]).name for row in self._delete["rows"][:5]]
        return f"선택한 파일 {self.deleteCount:,}개를 Windows 휴지통으로 이동합니다.\n" + "\n".join(names)

    @Slot(bool)
    def setLocked(self, locked):
        if self._locked != bool(locked):
            self._locked = bool(locked)
            self.changed.emit()

    def setLibrary(self, account, output_dir, separate_accounts):
        """A different account or folder invalidates every pending selection."""
        context = (account, output_dir, bool(separate_accounts))
        if context == self._context:
            return
        self._context = context
        self._generation += 1
        self._delete = None
        self._error = self._status = ""
        self.changed.emit()

    @Slot("QVariantList")
    def prepareDelete(self, keys):
        if self.locked or self._mutating or self._closing or not keys: return
        self._mutating = True
        self._stop.clear()
        self._error = ""
        self._status = "휴지통으로 이동할 파일을 확인합니다"
        self._enqueue("prepare_delete", tuple(keys))

    @Slot()
    def cancelDelete(self):
        if self._delete is not None:
            self._delete = None
            self._mutating = False
            self._status = "파일을 유지했습니다"
            self.changed.emit()

    @Slot(bool)
    def confirmDelete(self, keep_deleted):
        manifest, self._delete = self._delete, None
        if manifest is None: return
        if self.locked or manifest["generation"] != self._library._generation:
            self._mutating = False
            self._error = "계정이나 목록이 바뀌었습니다. 파일을 다시 선택해 주세요."
            self.changed.emit()
            return
        self._stop.clear()
        self._status = "Windows 휴지통으로 이동합니다"
        self._enqueue("delete", (manifest, bool(keep_deleted)))

    @Slot()
    def stop(self):
        if self._delete is not None:
            self.cancelDelete()
            return
        self._stop.set()
        self._status = "관리 작업을 중단하는 중입니다"
        self.changed.emit()

    def _enqueue(self, kind, payload):
        with self._condition:
            self._pending.append((kind, self._generation, payload))
            self._condition.notify()
        self.changed.emit()

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closing or self._pending)
                if self._closing: return
                kind, generation, payload = self._pending.popleft()
            try:
                if generation != self._generation:
                    self._delivered.emit((kind, generation, {}, "계정이나 목록이 바뀌었습니다."))
                    continue
                if kind == "prepare_delete":
                    result = self._library.deletionManifest(payload)
                    if self._stop.is_set(): result = {"rows": [], "cancelled": True}
                else:
                    from .deletion import run_deletion
                    manifest, keep = payload
                    result = run_deletion(manifest["location"], manifest["rows"], keep, self._stop,
                                          lambda event: self._delivered.emit(("progress", generation, event, "")))
                self._delivered.emit((kind, generation, result, ""))
            except Exception:
                self._delivered.emit((kind, generation, {}, "작업을 마치지 못했습니다. 계정·폴더·파일 상태를 확인해 주세요."))

    @Slot(object)
    def _accept(self, event):
        kind, generation, value, error = event
        if self._closing: return
        if generation != self._generation:
            if kind != "progress":
                self._delete = None
                self._mutating = False
                self._error = "계정이나 목록이 바뀌었습니다. 작업 결과를 다시 확인해 주세요."
                self.changed.emit()
            return
        if kind == "progress":
            self._status = str(value.get("message", ""))
        elif kind == "prepare_delete":
            self._error = error
            if self._stop.is_set() or value.get("cancelled"):
                self._mutating = False
                self._status = "파일 이동을 취소했습니다"
            elif error or not value.get("rows"):
                self._mutating = False
                self._status = "이동할 파일이 없습니다"
            else:
                self._delete = value
                self._status = "휴지통으로 이동할지 확인해 주세요"
                self.deleteConfirmationRequested.emit()
        else:
            self._mutating = False
            self._error = error
            self._status = str(value.get("message") or ("작업을 마쳤습니다" if not error else ""))
            self.libraryChanged.emit()
        self.changed.emit()

    @Slot()
    def shutdown(self):
        self._closing = True
        self._stop.set()
        with self._condition:
            self._pending.clear()
            self._condition.notify_all()
        self._shutdown_timer.start()

    def _poll_shutdown(self):
        if not self._worker.is_alive():
            self._shutdown_timer.stop()
            self._worker.join(timeout=0)
            self.shutdownFinished.emit()
