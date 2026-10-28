"""Qt projection of the existing isolated downloader, without worker-side UI."""

import time
from pathlib import Path
import re

from PySide6.QtCore import QObject, Property, QTimer, Qt, Signal, Slot

from .core import JobOptions, _review_path
from .download_service import DownloadService
from .qt_saved_preview import SavedPreviewLoader


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


def _saved_text(value, limit, *, multiline=False):
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value[:limit]
                   if (ord(char) >= 32 and not 127 <= ord(char) <= 159)
                   or (multiline and char in "\n\t"))


def _saved_item(event):
    """Copy bounded post metadata, never the worker's cookie or raw payload."""
    value = event.get("path")
    if (not isinstance(value, str) or not 1 <= len(value) <= 32768
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        return {}
    path = str(_review_path(value))
    if not Path(path).is_absolute():
        return {}
    author = event.get("author")
    author = author if isinstance(author, str) and re.fullmatch(r"[A-Za-z0-9_]{1,15}", author) else ""
    source = event.get("source_url")
    source = source if isinstance(source, str) and re.fullmatch(r"https://x\.com/i/web/status/[0-9]{1,20}", source) else ""
    media_type = event.get("media_type")
    media_type = media_type if isinstance(media_type, str) and media_type in {"photo", "video", "animated_gif"} else "photo"
    return {"path": path, "filename": Path(path).name, "author": author,
            "display_name": _saved_text(event.get("display_name"), 200),
            "body": _saved_text(event.get("body"), 5000, multiline=True),
            "posted_at": _saved_text(event.get("posted_at"), 64),
            "source_url": source, "media_type": media_type,
            "preview_url": "", "preview_note": "", "preview_loading": False}


class DownloadController(QObject):
    changed = Signal()
    savedItemChanged = Signal()
    # Worker events stay Python objects: a review's file signature holds NTFS
    # file IDs above the signed 64-bit range that a QVariantMap cannot carry.
    reviewRequested = Signal(object)
    reviewDismissed = Signal()
    eventReceived = Signal(object)
    authenticationChanged = Signal(str)
    shutdownFinished = Signal()
    _previewResult = Signal(object)

    def __init__(self, parent=None, *, service=None):
        super().__init__(parent)
        self._service = service if service is not None else DownloadService()
        self._running = False
        self._stopping = False
        self._closing = False
        self._shutdown_emitted = False
        self._status = "저장 대기 중"
        self._saved = 0
        self._skipped = 0
        self._error = ""
        self._notice = ""
        self._progress = -1.0
        self._final = None
        self._repair_needed = False
        self._job_id = 0
        self._authentication_state = ""
        self._authentication_error = False
        self._details_enabled = False
        self._current_saved = {}
        self._preview_generation = 0
        self._previewResult.connect(self._accept_preview, Qt.ConnectionType.QueuedConnection)
        self._preview_loader = SavedPreviewLoader(
            lambda generation, result: self._previewResult.emit((generation, result)))
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

    @Property(bool, notify=changed)
    def detailsEnabled(self):
        return self._details_enabled

    @Property("QVariantMap", notify=savedItemChanged)
    def currentSavedItem(self):
        return dict(self._current_saved)

    @Slot(bool)
    def setDetailsEnabled(self, enabled):
        if self._closing or bool(enabled) == self._details_enabled:
            return
        self._details_enabled = bool(enabled)
        self._invalidate_preview()
        if self._details_enabled:
            self._request_preview()
        self.savedItemChanged.emit()
        self.changed.emit()

    def _invalidate_preview(self):
        self._preview_generation += 1
        self._preview_loader.cancel()
        if self._current_saved:
            self._current_saved.update(preview_url="", preview_note="", preview_loading=False)
            self.savedItemChanged.emit()

    def _request_preview(self):
        if not self._details_enabled or not self._current_saved or self._closing:
            return
        self._current_saved["preview_loading"] = True
        if not self._preview_loader.request(self._preview_generation, self._current_saved["path"]):
            self._current_saved.update(preview_loading=False, preview_note="미리보기를 준비하지 못했습니다.")

    @Slot()
    def retrySavedPreview(self):
        if (self._closing or not self._details_enabled or not self._current_saved
                or self._current_saved.get("preview_loading")):
            return
        self._invalidate_preview()
        self._request_preview()
        self.savedItemChanged.emit()

    @Slot(object)
    def _accept_preview(self, event):
        generation, result = event
        if (self._closing or not self._details_enabled or not self._current_saved
                or generation != self._preview_generation):
            return
        self._current_saved.update(result, preview_loading=False)
        self.savedItemChanged.emit()

    def markRepaired(self):
        """A rebuilt records database replaces the damaged-records error."""
        if self._running or not self._repair_needed:
            return
        self._repair_needed = False
        self._error = ""
        self._status = "저장 대기 중"
        self.changed.emit()

    @Slot()
    def clearAuthenticationError(self):
        """A new connection clears only a prior typed authentication failure."""
        if self._running or self._closing or not self._authentication_error:
            return
        self._authentication_error = False
        self._error = ""
        self._status = "저장 대기 중"
        self._final = None
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
        self._authentication_state = ""
        self._authentication_error = False
        self._repair_needed = False
        self._error = ""
        self._notice = ""
        self._progress = -1.0
        self._status = "저장을 시작합니다"
        self._invalidate_preview()
        self._current_saved = {}
        self.savedItemChanged.emit()
        self._timer.start()
        self.changed.emit()
        return True

    @Slot()
    def stop(self):
        if not self._running or self._stopping:
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
        self._invalidate_preview()
        self._preview_loader.close()
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
        if not self._shutdown_emitted and not self._preview_loader.alive():
            self._shutdown_emitted = True
            self._timer.stop()
            self.shutdownFinished.emit()
        elif not self._shutdown_emitted:
            self._timer.start()

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
        if (self._running and not self._closing and self._final is None and
                kind in {"authentication", "finished"}):
            state = event.get("auth_state")
            if (isinstance(state, str) and state in {"verified", "reconnect_required"} and
                    state != self._authentication_state and
                    self._authentication_state != "reconnect_required"):
                self._authentication_state = state
                self.authenticationChanged.emit(state)
        if kind == "progress":
            try:
                total = max(0, int(event.get("total") or 0))
                downloaded = max(0, int(event.get("downloaded") or 0))
            except (TypeError, ValueError, OverflowError):
                total = downloaded = 0
            self._progress = min(1.0, downloaded / total) if total else -1.0
        elif kind == "saved":
            self._saved += 1
            if self._running and not self._closing and self._final is None:
                item = _saved_item(event)
                if item:
                    self._invalidate_preview()
                    self._current_saved = item
                    self._request_preview()
                    self.savedItemChanged.emit()
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
                self._authentication_error = (event.get("error_kind") == "authentication" and
                                              event.get("auth_state") == "reconnect_required" and
                                              not self._repair_needed)
            else:
                self._status = "저장 결과를 정리하고 있습니다"
        elif kind == "process_exit":
            self._running = False
            self.reviewDismissed.emit()
            clean_exit = (type(event.get("code")) is int and event["code"] == 0
                          and event.get("forced") is not True)
            if self._final == "finished" and clean_exit:
                self._status = "저장했습니다"
                self._progress = 1.0
            elif self._final == "stopped" and clean_exit:
                self._status = "중단했습니다. 저장한 파일은 유지됩니다"
                self._progress = -1.0
            elif event.get("forced") is True:
                self._error = self._error or ("중단 요청에 응답하지 않아 작업을 강제로 종료했습니다. "
                                             "저장한 파일은 유지되며 다음 저장 때 기록을 다시 확인합니다.")
                self._status = self._error
                self._progress = -1.0
            else:
                self._error = self._error or "저장 작업이 정상적으로 끝나지 않았습니다. 저장한 파일은 유지됩니다."
                self._status = self._error
                self._progress = -1.0
            self._stopping = False
