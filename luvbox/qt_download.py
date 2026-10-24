"""Qt projection of the existing isolated downloader, without worker-side UI."""

import time

from PySide6.QtCore import QObject, Property, QTimer, Signal, Slot

from .core import JobOptions
from .download_service import DownloadService


def _count(value):
    try:
        return min(2_147_483_647, max(0, int(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _note(value, limit=1000):
    """A worker sentence safe to show. Anything else stays off the screen."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text[:limit]


class DownloadController(QObject):
    changed = Signal()
    reviewRequested = Signal(dict)
    reviewDismissed = Signal()
    eventReceived = Signal(dict)
    shutdownFinished = Signal()

    def __init__(self, parent=None, *, service=None):
        super().__init__(parent)
        self._service = service if service is not None else DownloadService()
        self._running = False
        self._stopping = False
        self._closing = False
        self._shutdown_emitted = False
        self._status = "저장할 준비가 됐습니다"
        self._saved = 0
        self._skipped = 0
        self._error = ""
        self._notice = ""
        self._progress = -1.0
        self._final = None
        self._repair_needed = False
        self._job_id = 0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._pump)

    @Property(bool, notify=changed)
    def running(self):
        return self._running

    @Property(int, notify=changed)
    def jobId(self):
        return self._job_id

    @Property(bool, notify=changed)
    def stopping(self):
        return self._stopping

    @Property(bool, notify=changed)
    def closing(self):
        return self._closing

    @Property(str, notify=changed)
    def status(self):
        return self._status

    @Property(int, notify=changed)
    def savedCount(self):
        return self._saved

    @Property(int, notify=changed)
    def skippedCount(self):
        return self._skipped

    @Property(str, notify=changed)
    def error(self):
        return self._error

    @Property(str, notify=changed)
    def notice(self):
        return self._notice

    @Property(float, notify=changed)
    def progress(self):
        return self._progress

    @Property(bool, notify=changed)
    def repairNeeded(self):
        """The last save stopped on a damaged records database."""
        return self._repair_needed

    def markRepaired(self):
        """A rebuilt records database replaces the damaged-records error."""
        if self._running or not self._repair_needed:
            return
        self._repair_needed = False
        self._error = ""
        self._status = "저장할 준비가 됐습니다"
        self.changed.emit()

    def start(self, options):
        if self._running or self._closing:
            return False
        try:
            if isinstance(options, dict):
                options = JobOptions(**options)
            if not self._service.start(options):
                return False
        except (TypeError, ValueError):
            self._error = "저장 설정을 확인해 주세요."
            self._status = self._error
            self.changed.emit()
            return False
        self._job_id = self._service.job_id
        self._running = True
        self._stopping = False
        self._saved = self._skipped = 0
        self._final = None
        self._repair_needed = False
        self._error = ""
        self._notice = ""
        self._progress = -1.0
        self._status = "저장을 시작합니다"
        self._timer.start()
        self.changed.emit()
        return True

    @Slot()
    def stop(self):
        if not self._running or self._stopping or self._final is not None:
            return
        self._stopping = True
        self._status = "중단하는 중입니다"
        self._service.stop()
        self.reviewDismissed.emit()
        self.changed.emit()

    @Slot(str, str)
    def resolveReview(self, ident, action):
        if self._service.resolve_review(ident, action):
            self.reviewDismissed.emit()
            if action == "stop":
                self.stop()
            else:
                self._status = "저장 중입니다"
                self.changed.emit()

    @Slot(bool)
    def setReviewEnabled(self, enabled):
        self._service.set_review_enabled(enabled)

    @Slot()
    def shutdown(self):
        if self._shutdown_emitted:
            return
        self._closing = True
        self._service.close()
        self.stop()
        self.changed.emit()
        if self._running:
            self._timer.start()
        else:
            self._finish_shutdown()

    def close(self):
        self.shutdown()

    def _finish_shutdown(self):
        if not self._shutdown_emitted:
            self._shutdown_emitted = True
            self._timer.stop()
            self.shutdownFinished.emit()

    @Slot()
    def _pump(self):
        deadline = time.perf_counter() + .005
        changed = False
        while time.perf_counter() < deadline:
            events = self._service.drain(1)
            if not events:
                break
            event = events[0]
            if event.get("job_id") != self._job_id:
                continue
            self._handle(event)
            self.eventReceived.emit(event)
            changed = True
        if changed:
            self.changed.emit()
        if not self._running:
            self._timer.stop()
            if self._closing:
                self._finish_shutdown()

    def _handle(self, event):
        kind = event.get("type")
        if kind == "progress":
            try:
                total = max(0, int(event.get("total") or 0))
                downloaded = max(0, int(event.get("downloaded") or 0))
            except (TypeError, ValueError, OverflowError):
                total = downloaded = 0
            self._progress = min(1.0, downloaded / total) if total else -1.0
        elif kind == "saved":
            self._saved += 1
            if not self._stopping:
                self._status = "저장 중입니다"
        elif kind == "skipped":
            self._skipped += 1
        elif kind == "converting" and not self._stopping:
            self._status = "GIF를 변환하고 있습니다"
        elif kind == "review-request":
            if self._stopping or self._closing:
                self._service.stop()
                return
            self._status = "저장할 사진을 확인해 주세요"
            self.reviewRequested.emit(event)
        elif kind == "finished":
            self._final = event.get("status", "error")
            self._saved = _count(event.get("saved", self._saved))
            self._skipped = _count(event.get("skipped", self._skipped))
            metadata_failed = _count(event.get("metadata_failed"))
            if metadata_failed:
                self._notice = f"파일 {metadata_failed:,}개의 작성자·출처 기록을 마치지 못했습니다. 받은 파일은 유지됩니다."
            summary = _note(event.get("message"))
            if summary and self._final != "error":
                self._notice = f"{summary} {self._notice}" if self._notice else summary
            self.reviewDismissed.emit()
            if self._final == "error":
                self._error = summary or "저장 작업이 중단됐습니다."
                self._status = self._error
                self._repair_needed = event.get("catalog_damaged") is True
            else:
                self._status = "저장 결과를 정리하고 있습니다"
        elif kind == "process_exit":
            self._running = False
            self.reviewDismissed.emit()
            if self._final == "finished" and event.get("code") == 0:
                self._status = "저장했습니다"
                self._progress = 1.0
            elif self._final == "stopped" or (self._final is None and self._stopping):
                self._status = "중단했습니다. 저장한 파일은 유지됩니다"
                self._progress = -1.0
            else:
                self._error = self._error or "저장 작업이 정상적으로 끝나지 않았습니다. 저장한 파일은 유지됩니다."
                self._status = self._error
                self._progress = -1.0
            self._stopping = False
