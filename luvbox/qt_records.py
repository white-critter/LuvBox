"""Off-thread lists of past runs and of media a save could not store.

Both lists are read-only views of small ledgers in the app data folder, plus the
person's choice to forget a row or to ask for another try. Nothing here touches
saved files or a catalog; a retry is handed to the application, which starts the
same guarded save the downloader already uses for chosen media.
"""

from collections import deque
from pathlib import Path
import re
from threading import Condition, Thread
import uuid

from PySide6.QtCore import QObject, Property, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices

from . import unsaved
from .core import RETRY_LIMIT
from .history import archive_key
from .run_history import run_page
from .storage import resolve_storage


_STATUS = {"finished": ("완료", "ok"), "stopped": ("중단", "muted"), "error": ("실패", "error"),
           "interrupted": ("결과 확인 필요", "warn"), "partial": ("일부 완료", "warn"),
           "limited": ("이전 버전 수량 제한", "muted"), "cancelled": ("취소", "muted"),
           "running": ("실행 중", "muted"), "busy": ("다른 작업 중이라 건너뜀", "muted")}
_ORIGIN = {"manual": "수동", "scheduled": "자동", "legacy": "이전 기록"}
_MEDIA = {"photo": "사진", "video": "영상", "animated_gif": "GIF"}
_REASON = {unsaved.FAILED: "저장 실패", unsaved.SIZE: "용량 제한", unsaved.EXCLUDED: "직접 제외"}
# The two views of the unsaved list: media the app could not store, and media the person excluded.
_KINDS = {"unsaved": (unsaved.FAILED, unsaved.SIZE), "excluded": (unsaved.EXCLUDED,)}
_UNSAVED_PAGE = 50
# A finished run's message only repeats the standard summary; show it when it says more.
_ROUTINE_MESSAGE = ("X가 보여 주는 좋아요 목록을 모두 확인했습니다", "새 Like 확인을 마쳤습니다")


def display_time(value):
    if not value:
        return ""
    text = str(value).strip().replace("T", " ")
    if len(text) >= 16 and text[4:5] == "-" and text[7:8] == "-" and text[10:11] == " ":
        return text[:16]
    return text


def size_text(size):
    if not size:
        return ""
    mebibytes = size / 1048576
    return f"{mebibytes / 1024:.1f}GB" if mebibytes >= 1024 else f"{mebibytes:.0f}MB" if mebibytes >= 10 else f"{mebibytes:.1f}MB"


def duration_text(seconds):
    if not seconds:
        return ""
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}분 {rest}초" if minutes else f"{rest}초"


class RecordsController(QObject):
    changed = Signal()
    retryRequested = Signal(object)
    shutdownFinished = Signal()
    _delivered = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._context = ("", "", False)
        self._generation = 0
        self._limit_mb = 0
        self._closing = False
        self._active = set()
        self._run_rows, self._run_tokens = [], {}
        self._run_total = self._run_page = self._run_pages = 0
        self._run_busy, self._run_error, self._run_note = False, "", ""
        self._run_serial = self._unsaved_serial = 0
        self._unsaved_view, self._unsaved_page = "unsaved", 0
        self._unsaved_rows, self._unsaved_tokens = [], {}
        self._unsaved_total = self._unsaved_pages = 0
        self._unsaved_busy, self._unsaved_error, self._unsaved_status = False, "", ""
        self._counts = {reason: 0 for reason in unsaved.REASONS}
        self._condition = Condition()
        self._pending = deque()
        self._delivered.connect(self._accept, Qt.ConnectionType.QueuedConnection)
        self._worker = Thread(target=self._work, name="LuvBox records", daemon=True)
        self._worker.start()
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(50)
        self._shutdown_timer.timeout.connect(self._poll_shutdown)

    @Property("QVariantList", notify=changed)
    def runRows(self): return self._run_rows
    @Property(int, notify=changed)
    def runTotal(self): return self._run_total
    @Property(int, notify=changed)
    def runPage(self): return self._run_page
    @Property(int, notify=changed)
    def runPages(self): return self._run_pages
    @Property(bool, notify=changed)
    def runBusy(self): return self._run_busy
    @Property(str, notify=changed)
    def runError(self): return self._run_error
    @Property(str, notify=changed)
    def runNote(self): return self._run_note

    @Property("QVariantList", notify=changed)
    def unsavedRows(self): return self._unsaved_rows
    @Property(int, notify=changed)
    def unsavedTotal(self): return self._unsaved_total
    @Property(int, notify=changed)
    def unsavedPage(self): return self._unsaved_page
    @Property(int, notify=changed)
    def unsavedPages(self): return self._unsaved_pages
    @Property(str, notify=changed)
    def unsavedView(self): return self._unsaved_view
    @Property(bool, notify=changed)
    def unsavedBusy(self): return self._unsaved_busy
    @Property(str, notify=changed)
    def unsavedError(self): return self._unsaved_error
    @Property(str, notify=changed)
    def unsavedStatus(self): return self._unsaved_status
    @Property(int, notify=changed)
    def unsavedCount(self):
        """Media the app could not store: the number worth a person's attention."""
        return self._counts[unsaved.FAILED] + self._counts[unsaved.SIZE]
    @Property(int, notify=changed)
    def excludedCount(self): return self._counts[unsaved.EXCLUDED]
    @Property(int, notify=changed)
    def retryableCount(self):
        return sum(1 for row in self._unsaved_tokens.values() if self._can_retry(row))

    # -- context -------------------------------------------------------------------------

    def setLibrary(self, account, output_dir, separate_accounts):
        context = (account, output_dir, bool(separate_accounts))
        if context == self._context or self._closing:
            return
        self._generation += 1
        self._context = context
        self._counts = {reason: 0 for reason in unsaved.REASONS}
        self._unsaved_rows, self._unsaved_tokens = [], {}
        self._unsaved_total = self._unsaved_pages = self._unsaved_page = 0
        self._run_rows, self._run_tokens = [], {}
        self._run_total = self._run_page = self._run_pages = 0
        self._unsaved_status = self._unsaved_error = self._run_error = self._run_note = ""
        self.refresh()

    def setVideoLimit(self, megabytes):
        """The current size limit decides which size-limited media a retry could now store."""
        megabytes = megabytes if type(megabytes) is int and megabytes >= 0 else 0
        if megabytes != self._limit_mb:
            self._limit_mb = megabytes
            self._rebuild_unsaved()
            self.changed.emit()

    @Slot(str, bool)
    def setActive(self, section, active):
        """A dialog is open: keep its list current after a save or a retry."""
        (self._active.add if active else self._active.discard)(section)
        if active:
            self._unsaved_status = ""
        if active and section == "runs":
            self.loadRuns(0)
        elif active and section == "unsaved":
            self.loadUnsaved(0)

    @Slot()
    def refresh(self):
        if self._closing:
            return
        self._enqueue("counts", None)
        if "runs" in self._active:
            self.loadRuns(self._run_page)
        if "unsaved" in self._active:
            self.loadUnsaved(self._unsaved_page)

    # -- run history ---------------------------------------------------------------------

    @Slot(int)
    def loadRuns(self, page):
        if self._closing:
            return
        self._run_serial += 1
        self._run_busy = True
        self._run_error = ""
        self._enqueue("runs", (max(0, int(page)), self._run_serial), replace=True)
        self.changed.emit()

    @Slot(str)
    def openLog(self, token):
        row = self._run_tokens.get(token)
        if not row or not row.get("log"):
            return
        from .scheduled import log_directory
        try:
            path = Path(row["log"])
            root = log_directory().resolve()
            if (path.is_symlink() or path.parent.resolve() != root or path.suffix != ".log"
                    or not path.name.startswith("scheduled-") or not path.is_file()):
                raise OSError
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except (OSError, ValueError):
            self._run_error = "로그 파일을 열지 못했습니다."
            self.changed.emit()

    # -- unsaved media -------------------------------------------------------------------

    @Slot(int)
    def loadUnsaved(self, page):
        if self._closing:
            return
        self._unsaved_serial += 1
        self._unsaved_busy = True
        self._unsaved_error = ""
        self._enqueue("unsaved", (self._unsaved_view, max(0, int(page)), self._unsaved_serial), replace=True)
        self.changed.emit()

    @Slot(str)
    def setUnsavedView(self, view):
        if view in _KINDS and view != self._unsaved_view:
            self._unsaved_view, self._unsaved_status = view, ""
            self._unsaved_rows, self._unsaved_tokens, self._unsaved_total = [], {}, 0
            self.loadUnsaved(0)

    @Slot(str)
    def openSource(self, token):
        row = self._unsaved_tokens.get(token)
        source = f"https://x.com/i/web/status/{row['tweet_id']}" if row else ""
        if re.fullmatch(r"https://x\.com/i/web/status/[0-9]{1,20}", source):
            if not QDesktopServices.openUrl(QUrl(source)):
                self._unsaved_error = "원문을 열지 못했습니다. 브라우저 설정을 확인해 주세요."
                self.changed.emit()

    @Slot(str)
    def dismiss(self, token):
        row = self._unsaved_tokens.get(token)
        if row:
            self._unsaved_status = ""
            self._enqueue("dismiss", ([(row["tweet_id"], row["num"], row["media_type"])], None))

    @Slot()
    def dismissAll(self):
        self._unsaved_status = ""
        self._enqueue("dismiss", (None, _KINDS[self._unsaved_view]))

    @Slot(str)
    def retry(self, token):
        row = self._unsaved_tokens.get(token)
        if row and self._can_retry(row):
            self.retryRequested.emit((archive_key(row["tweet_id"], row["num"], row["media_type"]),))

    @Slot()
    def retryAll(self):
        """Every row that could now succeed, including the ones on other pages."""
        self._enqueue("retry_keys", self._unsaved_view)

    def _can_retry(self, row):
        # A size-limited file only fits after the limit was raised since it was skipped.
        return (row["reason"] != unsaved.SIZE or self._limit_mb == 0
                or (row["limit_mb"] > 0 and self._limit_mb > row["limit_mb"]))

    def _rebuild_unsaved(self):
        self._unsaved_rows = [self._unsaved_row(token, row) for token, row in self._unsaved_tokens.items()]

    def _unsaved_row(self, token, row):
        media = _MEDIA.get(row["media_type"], "미디어")
        dimensions = f"{row['width']}×{row['height']}" if row["width"] and row["height"] else ""
        info = " · ".join(part for part in (media, dimensions, duration_text(row["duration"]),
                                            ("약 " + size_text(row["size_bytes"])) if row["size_bytes"] else "") if part)
        can_retry = self._can_retry(row)
        hint = ""
        if not can_retry:
            hint = "설정 › 고급에서 크기 제한을 올린 뒤 다시 저장할 수 있습니다."
        elif row["attempts"] > 1:
            hint = f"{row['attempts']}번째 시도까지 저장하지 못했습니다."
        return dict(token=token, author=("@" + row["author"]) if row["author"] else "작성자 미확인",
                    reason=row["reason"], reasonLabel=_REASON.get(row["reason"], ""), mediaType=row["media_type"],
                    info=info, posted=(row["posted_at"] or "")[:10], detail=row["detail"],
                    seen=display_time(row["last_seen"]), canRetry=can_retry, hint=hint)

    # -- worker --------------------------------------------------------------------------

    def _enqueue(self, kind, payload, *, replace=False):
        if self._closing:
            return
        with self._condition:
            if replace:
                self._pending = deque(item for item in self._pending if item[0] != kind)
            self._pending.append((kind, self._generation, payload))
            self._condition.notify()

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closing or self._pending)
                if self._closing:
                    return
                kind, generation, payload = self._pending.popleft()
                context = self._context
            if generation != self._generation:
                continue
            account, output_dir, separate = context
            try:
                if kind == "runs":
                    page, serial = payload
                    paths = None
                    if account and output_dir:
                        try:
                            paths = resolve_storage(output_dir, account, separate)
                        except (OSError, ValueError):
                            paths = None
                    result = dict(run_page(paths, page), serial=serial)
                elif kind == "unsaved":
                    view, page, serial = payload
                    result = dict(unsaved.page(account, output_dir, _KINDS[view], page, _UNSAVED_PAGE),
                                  serial=serial, view=view)
                elif kind == "counts":
                    result = unsaved.counts(account, output_dir) if account else {}
                elif kind == "dismiss":
                    items, kinds = payload
                    unsaved.remove(account, output_dir, items, kinds)
                    result = {}
                else:
                    keys = self._retry_keys(account, output_dir, payload)
                    result = {"keys": keys}
                error = ""
            except Exception:
                result, error = {}, ("목록을 읽지 못했습니다. 앱 데이터 폴더의 권한을 확인해 주세요."
                                     if kind in {"runs", "unsaved"} else
                                     "저장하지 못한 미디어 목록을 갱신하지 못했습니다.")
            self._delivered.emit((kind, generation, result, error))

    def _retry_keys(self, account, output_dir, view):
        keys = []
        page = 0
        while len(keys) < RETRY_LIMIT:
            found = unsaved.page(account, output_dir, _KINDS[view], page, 200)
            for row in found["rows"]:
                if self._can_retry(row):
                    keys.append(archive_key(row["tweet_id"], row["num"], row["media_type"]))
            page += 1
            if page >= found["pages"]:
                break
        return tuple(keys[:RETRY_LIMIT])

    @Slot(object)
    def _accept(self, event):
        kind, generation, result, error = event
        if self._closing or generation != self._generation:
            return
        if kind == "runs":
            if result.get("serial", self._run_serial) != self._run_serial:
                return
            self._run_busy = False
            self._run_error = error
            if not error:
                self._run_tokens = {}
                self._run_rows = [self._run_row(row) for row in result.get("rows", [])]
                self._run_total, self._run_page, self._run_pages = (
                    result.get("total", 0), result.get("page", 0), result.get("pages", 0))
                self._run_note = ("이전 버전에서 저장한 기록은 읽지 못해 표시하지 않았습니다."
                                  if result.get("legacy_unreadable") else "")
        elif kind == "unsaved":
            if result.get("serial", self._unsaved_serial) != self._unsaved_serial:
                return
            self._unsaved_busy = False
            self._unsaved_error = error
            if not error and result.get("view") == self._unsaved_view:
                self._unsaved_tokens = {uuid.uuid4().hex: row for row in result.get("rows", [])}
                self._rebuild_unsaved()
                self._unsaved_total, self._unsaved_page, self._unsaved_pages = (
                    result.get("total", 0), result.get("page", 0), result.get("pages", 0))
        elif kind == "counts":
            if not error and result:
                self._counts = {reason: result.get(reason, 0) for reason in unsaved.REASONS}
        elif kind == "dismiss":
            self._unsaved_error = error
            self._unsaved_status = "" if error else "목록에서 지웠습니다. 저장된 파일에는 영향이 없습니다."
            self.refresh()
        elif kind == "retry_keys":
            self._unsaved_error = error
            if not error:
                if result["keys"]:
                    self.retryRequested.emit(result["keys"])
                else:
                    self._unsaved_status = "다시 저장할 수 있는 미디어가 없습니다."
        self.changed.emit()

    def _run_row(self, row):
        token = uuid.uuid4().hex
        self._run_tokens[token] = row
        label, tone = _STATUS.get(row.get("status"), (str(row.get("status", "")), "muted"))
        counts = [f"저장 {row.get('saved', 0):,}개", f"이미 저장됨 {row.get('skipped', 0):,}개"]
        missed = row.get("size_skipped", 0) + row.get("failed_media", 0)
        if missed:
            counts.append(f"저장 못 함 {missed:,}개")
        message = str(row.get("message") or "")
        if row.get("status") == "finished" and message.startswith(_ROUTINE_MESSAGE):
            message = ""
        return dict(token=token, status=label, tone=tone, origin=_ORIGIN.get(row.get("origin"), ""),
                    when=display_time(row.get("started_at") or row.get("finished_at")),
                    account=("@" + row["account"]) if row.get("account") else "계정 미확인",
                    summary=" · ".join(counts), message=message, hasLog=bool(row.get("log")))

    # -- shutdown ------------------------------------------------------------------------

    @Slot()
    def shutdown(self):
        self._closing = True
        with self._condition:
            self._pending.clear()
            self._condition.notify_all()
        self._shutdown_timer.start()

    def _poll_shutdown(self):
        if not self._worker.is_alive():
            self._shutdown_timer.stop()
            self._worker.join(timeout=0)
            self.shutdownFinished.emit()
