"""Off-thread lists of past runs and of media a save could not store.

Failed and size-limited media come from a small ledger in the app data folder.
Media the user chose not to save again is read from that account's catalog, which
keeps the choice when the ledger row is deleted, trimmed, or left behind a folder
move. Forgetting a failure row does not clear a catalog skip. Releasing a skip
clears only that choice and leaves saved files and catalog rows in place. A third
view lists media whose saved file is gone (the gallery's "파일 없음" tiles): they
can be downloaded again or excluded, which takes them out of the gallery. A retry
is handed to the application, which starts the same guarded save the downloader
already uses for chosen media.
"""

from collections import deque
from contextlib import closing
from pathlib import Path
import re
from threading import Condition, Thread
import uuid

from PySide6.QtCore import QObject, Property, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices

from . import thumbcache, unsaved
from .core import RETRY_LIMIT
from .history import HistoryError, HistoryStore, archive_key
from .protection import AccountGuard, ProtectionError
from .qt_library import _ReadOnlyHistory
from .run_history import run_page
from .storage import resolve_storage


_STATUS = {"finished": ("완료", "ok"), "stopped": ("중단", "muted"), "error": ("실패", "error"),
           "interrupted": ("결과 확인 필요", "warn"), "partial": ("일부 완료", "warn"),
           "limited": ("이전 버전 수량 제한", "muted"), "cancelled": ("취소", "muted"),
           "running": ("실행 중", "muted"), "busy": ("다른 작업 중이라 건너뜀", "muted")}
_ORIGIN = {"manual": "수동", "scheduled": "자동", "legacy": "이전 기록"}
_MEDIA = {"photo": "사진", "video": "영상", "animated_gif": "GIF"}
# A file that was saved and is no longer in the folder. It is not a ledger reason: the catalog knows it.
MISSING = "missing"
_REASON = {unsaved.FAILED: "저장 실패", unsaved.SIZE: "용량 제한", unsaved.EXCLUDED: "직접 제외",
           MISSING: "파일 없음"}
# The three views: media the app could not store, media the person excluded, and saved files that are gone.
_KINDS = {"unsaved": (unsaved.FAILED, unsaved.SIZE), "excluded": (unsaved.EXCLUDED,), "missing": (MISSING,)}
_UNSAVED_PAGE = 100
_MISSING_DETAIL = "저장한 파일이 폴더에 없습니다. 앱 밖에서 지웠거나 옮긴 것으로 보입니다."
_CATALOG_ERROR = "기록을 읽지 못했습니다. 저장 폴더와 기록 폴더를 확인해 주세요."
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
    # Count refreshes also emit changed. The open page, including its preview
    # images, is replaced only when that page itself changes.
    unsavedRowsChanged = Signal()
    # Which tiles of the open page are chosen; cleared when the page changes.
    unsavedSelectionChanged = Signal()
    # What the gallery lists changed (media excluded, or an exclusion released).
    catalogChanged = Signal()
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
        self._retryable_total = None
        self._retryable_limit = None
        self._unsaved_busy, self._unsaved_error, self._unsaved_status = False, "", ""
        self._counts = {reason: 0 for reason in unsaved.REASONS}
        self._missing_count = 0
        self._unsaved_selected = set()
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

    @Property("QVariantList", notify=unsavedRowsChanged)
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
    def missingCount(self):
        """Saved files that are gone: the gallery's "파일 없음" tiles."""
        return self._missing_count
    @Property(int, notify=unsavedSelectionChanged)
    def unsavedSelectedCount(self): return len(self._unsaved_selected)
    @Property("QVariantList", notify=unsavedSelectionChanged)
    def unsavedSelection(self): return sorted(self._unsaved_selected)
    @Property(int, notify=changed)
    def retryableCount(self):
        if self._unsaved_view == "excluded":
            return self._counts[unsaved.EXCLUDED]
        if self._unsaved_view == "missing":
            return self._missing_count
        visible = sum(1 for row in self._unsaved_tokens.values() if self._can_retry(row))
        if (self._unsaved_pages <= 1 or self._retryable_total is None
                or self._retryable_limit != self._limit_mb):
            return visible
        return self._retryable_total

    # -- context -------------------------------------------------------------------------

    def setLibrary(self, account, output_dir, separate_accounts):
        context = (account, output_dir, bool(separate_accounts))
        if context == self._context or self._closing:
            return
        self._generation += 1
        self._context = context
        self._counts = {reason: 0 for reason in unsaved.REASONS}
        self._missing_count = 0
        self._unsaved_rows, self._unsaved_tokens = [], {}
        self._clear_selection()
        self.unsavedRowsChanged.emit()
        self._unsaved_total = self._unsaved_pages = self._unsaved_page = 0
        self._run_rows, self._run_tokens = [], {}
        self._run_total = self._run_page = self._run_pages = 0
        self._unsaved_status = self._unsaved_error = self._run_error = self._run_note = ""
        self._retryable_total = None
        self._retryable_limit = None
        self.refresh()

    def setVideoLimit(self, megabytes):
        """The current size limit decides which size-limited media a retry could now store."""
        megabytes = megabytes if type(megabytes) is int and megabytes >= 0 else 0
        if megabytes != self._limit_mb:
            self._limit_mb = megabytes
            self._retryable_total = None
            self._rebuild_unsaved()
            if self._unsaved_pages > 1:
                self.loadUnsaved(self._unsaved_page)
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
        self._retryable_total = None
        self._enqueue("unsaved", (self._unsaved_view, max(0, int(page)), self._unsaved_serial,
                                  self._limit_mb), replace=True)
        self.changed.emit()

    @Slot(str)
    def setUnsavedView(self, view):
        if view in _KINDS and view != self._unsaved_view:
            self._unsaved_view, self._unsaved_status = view, ""
            self._unsaved_rows, self._unsaved_tokens, self._unsaved_total = [], {}, 0
            self._clear_selection()
            self.unsavedRowsChanged.emit()
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
        """Forget one failure or size-limit note. A catalog skip is not a note."""
        row = self._unsaved_tokens.get(token)
        if row and row.get("reason") in (unsaved.FAILED, unsaved.SIZE):
            self._unsaved_status = ""
            self._enqueue("dismiss", ([(row["tweet_id"], row["num"], row["media_type"])], None))

    @Slot()
    def dismissAll(self):
        """Clear the failure list only. Excluded media stays recoverable."""
        if self._unsaved_view != "unsaved":
            return
        self._unsaved_status = ""
        self._enqueue("dismiss", (None, _KINDS[self._unsaved_view]))

    @Slot(str)
    def release(self, token):
        """Let one excluded item be saved again. The catalog row and files stay."""
        row = self._unsaved_tokens.get(token)
        if row and row.get("reason") == unsaved.EXCLUDED:
            self._unsaved_status = ""
            self._enqueue("release", [(row["tweet_id"], row["num"], row["media_type"])])

    @Slot()
    def releaseAll(self):
        if self._unsaved_view != "excluded":
            return
        self._unsaved_status = ""
        self._enqueue("release", None)

    @Slot(str)
    def exclude(self, token):
        """Take one missing file out of the gallery and stop downloading it again. Its record stays."""
        row = self._unsaved_tokens.get(token)
        if row and row.get("reason") == MISSING:
            self._unsaved_status = ""
            self._enqueue("exclude", [(row["tweet_id"], row["num"], row["media_type"])])

    @Slot()
    def excludeAll(self):
        if self._unsaved_view != "missing":
            return
        self._unsaved_status = ""
        self._enqueue("exclude", None)

    # -- the chosen tiles of the open page ---------------------------------------------

    def _clear_selection(self):
        if self._unsaved_selected:
            self._unsaved_selected = set()
            self.unsavedSelectionChanged.emit()

    def _chosen(self):
        """Rows of the chosen tiles, in the order shown."""
        return [row for token, row in self._unsaved_tokens.items() if token in self._unsaved_selected]

    @Slot(str)
    def toggleUnsavedSelected(self, token):
        if token in self._unsaved_tokens:
            self._unsaved_selected ^= {token}
            self.unsavedSelectionChanged.emit()

    @Slot()
    def selectUnsavedPage(self):
        chosen = set(self._unsaved_tokens)
        if chosen != self._unsaved_selected:
            self._unsaved_selected = chosen
            self.unsavedSelectionChanged.emit()

    @Slot()
    def clearUnsavedSelection(self):
        self._clear_selection()

    @Slot()
    def retrySelected(self):
        keys = tuple(archive_key(row["tweet_id"], row["num"], row["media_type"])
                     for row in self._chosen() if self._can_retry(row))
        if keys:
            self.retryRequested.emit(keys)
        else:
            self._unsaved_status = "고른 것 가운데 다시 저장할 수 있는 미디어가 없습니다."
            self.changed.emit()

    @Slot()
    def settleSelected(self):
        """The view's own way to put the chosen away: forget the note, release the exclusion, or exclude."""
        items = [(row["tweet_id"], row["num"], row["media_type"]) for row in self._chosen()]
        if not items:
            return
        self._unsaved_status = ""
        if self._unsaved_view == "excluded":
            self._enqueue("release", items)
        elif self._unsaved_view == "missing":
            self._enqueue("exclude", items)
        else:
            self._enqueue("dismiss", (items, None))

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
        self.unsavedRowsChanged.emit()

    def _unsaved_row(self, token, row):
        media = _MEDIA.get(row["media_type"], "미디어")
        dimensions = f"{row['width']}×{row['height']}" if row["width"] and row["height"] else ""
        info = " · ".join(part for part in (media, dimensions, duration_text(row["duration"]),
                                            ("약 " + size_text(row["size_bytes"])) if row["size_bytes"] else "") if part)
        can_retry = self._can_retry(row)
        hint = ""
        if row["reason"] == MISSING:
            pass
        elif not can_retry:
            hint = "설정 › 고급에서 크기 제한을 올린 뒤 다시 저장할 수 있습니다."
        elif row["attempts"] > 1:
            hint = f"{row['attempts']}번째 시도까지 저장하지 못했습니다."
        preview = row.get("preview") if isinstance(row.get("preview"), str) else ""
        if not preview.startswith("data:image/png;base64,") or len(preview) > 200_000:
            preview = ""
        return dict(token=token, author=("@" + row["author"]) if row["author"] else "작성자 미확인",
                    reason=row["reason"], reasonLabel=_REASON.get(row["reason"], ""), mediaType=row["media_type"],
                    info=info, posted=(row["posted_at"] or "")[:10], detail=row["detail"],
                    seen=display_time(row["last_seen"]), canRetry=can_retry, hint=hint, preview=preview)

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
                    view, page, serial, limit_mb = payload
                    if view == "excluded":
                        result = dict(self._excluded_page(account, output_dir, separate, page),
                                      serial=serial, view=view)
                    elif view == "missing":
                        result = dict(self._missing_page(account, output_dir, separate, page),
                                      serial=serial, view=view)
                    else:
                        result = dict(unsaved.page(account, output_dir, _KINDS[view], page, _UNSAVED_PAGE),
                                      serial=serial, view=view)
                        if result.get("pages", 0) > 1:
                            result.update(retryable_total=unsaved.retryable_count(account, output_dir, limit_mb),
                                          retryable_limit=limit_mb)
                    self._attach_previews(account, result)
                elif kind == "counts":
                    result = unsaved.counts(account, output_dir) if account else {}
                    result[unsaved.EXCLUDED] = self._excluded_total(account, output_dir, separate)
                    result[MISSING] = self._missing_total(account, output_dir, separate)
                elif kind == "dismiss":
                    items, kinds = payload
                    unsaved.remove(account, output_dir, items, kinds)
                    result = {}
                elif kind == "release":
                    result = {"released": self._release_excluded(account, output_dir, separate, payload)}
                elif kind == "exclude":
                    result = {"excluded": self._exclude_missing(account, output_dir, separate, payload)}
                else:
                    keys = self._retry_keys(account, output_dir, separate, payload)
                    result = {"keys": keys}
                error = ""
            except (ProtectionError, HistoryError) as exc:
                result, error = {}, str(exc)
            except Exception:
                result, error = {}, ("목록을 읽지 못했습니다. 앱 데이터 폴더의 권한을 확인해 주세요."
                                     if kind in {"runs", "unsaved", "counts"} else
                                     "저장하지 못한 미디어 목록을 갱신하지 못했습니다.")
            # Errors belong to the request too. An older failed read must not
            # clear the busy state or replace an error for a newer page/view.
            if kind == "runs":
                result["serial"] = payload[1]
            elif kind == "unsaved":
                result["serial"] = payload[2]
            self._delivered.emit((kind, generation, result, error))

    def _storage(self, account, output_dir, separate):
        if not account or not output_dir:
            return None
        return resolve_storage(output_dir, account, separate)

    def _excluded_total(self, account, output_dir, separate):
        """How many catalog skips this account still has. A missing catalog is zero."""
        try:
            paths = self._storage(account, output_dir, separate)
        except (OSError, ValueError):
            raise HistoryError("제외 기록을 읽지 못했습니다. 저장 폴더와 기록 폴더를 확인해 주세요.") from None
        if paths is None or not (paths.record_dir / ".luvbox-history.sqlite3").is_file():
            return 0
        with closing(_ReadOnlyHistory(paths)) as store:
            return store.excluded_media(page=0, page_size=1)["total"]

    def _attach_previews(self, account, result):
        """Fill previews from this PC's cache. A miss stays blank; nothing is downloaded."""
        rows = result.get("rows") or []
        urls = {}
        for start in range(0, len(rows), 50):          # the cache answers fifty at a time
            urls.update(thumbcache.preview_data_urls(
                account, [(row.get("tweet_id"), row.get("num"), row.get("media_type"))
                          for row in rows[start:start + 50]]))
        for row in rows:
            row["preview"] = urls.get((row.get("tweet_id"), row.get("num"), row.get("media_type")), "")

    def _excluded_page(self, account, output_dir, separate, page):
        try:
            paths = self._storage(account, output_dir, separate)
        except (OSError, ValueError):
            raise HistoryError("제외 기록을 읽지 못했습니다. 저장 폴더와 기록 폴더를 확인해 주세요.") from None
        if paths is None or not (paths.record_dir / ".luvbox-history.sqlite3").is_file():
            return {"rows": [], "total": 0, "page": 0, "pages": 0, "page_size": _UNSAVED_PAGE}
        with closing(_ReadOnlyHistory(paths)) as store:
            found = store.excluded_media(page=page, page_size=_UNSAVED_PAGE)
        rows = [dict(tweet_id=row["tweet_id"], num=row["num"], media_type=row["media_type"],
                     reason=unsaved.EXCLUDED, author=row["author"], posted_at=row["posted_at"],
                     width=0, height=0, duration=0, size_bytes=0, limit_mb=0, detail=row["detail"],
                     attempts=1, first_seen=row["updated_at"], last_seen=row["updated_at"])
                for row in found["rows"]]
        found["rows"] = rows
        return found

    def _catalog(self, account, output_dir, separate):
        """Storage of this library when it has a catalog, else None."""
        try:
            paths = self._storage(account, output_dir, separate)
        except (OSError, ValueError):
            raise HistoryError(_CATALOG_ERROR) from None
        if paths is None or not (paths.record_dir / ".luvbox-history.sqlite3").is_file():
            return None
        return paths

    def _missing_total(self, account, output_dir, separate):
        paths = self._catalog(account, output_dir, separate)
        if paths is None:
            return 0
        with closing(_ReadOnlyHistory(paths)) as store:
            return store.lost_count() if hasattr(store, "lost_count") else 0

    def _missing_page(self, account, output_dir, separate, page):
        paths = self._catalog(account, output_dir, separate)
        if paths is None:
            return {"rows": [], "total": 0, "page": 0, "pages": 0, "page_size": _UNSAVED_PAGE}
        with closing(_ReadOnlyHistory(paths)) as store:
            found = store.lost_media(page=page, page_size=_UNSAVED_PAGE)
        found["rows"] = [dict(tweet_id=row["tweet_id"], num=row["num"], media_type=row["media_type"],
                              reason=MISSING, author=row["author"], posted_at=row["posted_at"],
                              width=0, height=0, duration=0, size_bytes=0, limit_mb=0, detail=_MISSING_DETAIL,
                              attempts=1, first_seen=row["saved_at"], last_seen=row["saved_at"])
                         for row in found["rows"]]
        return found

    def _exclude_missing(self, account, output_dir, separate, items):
        paths = self._catalog(account, output_dir, separate)
        if paths is None:
            return 0
        guard = AccountGuard(paths.account, sleep=lambda *_: None)
        guard.acquire(respect_cooldown=False)
        try:
            store = HistoryStore(paths.record_dir, media_dir=paths.media_dir, output_dir=paths.output_dir,
                                 legacy_dir=paths.legacy_dir)
            try:
                return store.exclude_lost(items)
            finally:
                store.close()
        finally:
            guard.close()

    def _release_excluded(self, account, output_dir, separate, items):
        paths = self._storage(account, output_dir, separate)
        if paths is None or not (paths.record_dir / ".luvbox-history.sqlite3").is_file():
            return 0
        guard = AccountGuard(paths.account, sleep=lambda *_: None)
        guard.acquire(respect_cooldown=False)
        try:
            store = HistoryStore(paths.record_dir, media_dir=paths.media_dir, output_dir=paths.output_dir,
                                 legacy_dir=paths.legacy_dir)
            try:
                released = store.release_excluded(items)
            finally:
                store.close()
        finally:
            guard.close()
        if released:
            # The ledger copy is not the choice. Dropping it cannot hide the
            # catalog row, and after a release that row is no longer a skip.
            unsaved.remove(account, output_dir, items=items, kinds=(unsaved.EXCLUDED,) if items is None else None)
        return released

    def _retry_keys(self, account, output_dir, separate, view):
        if view in ("excluded", "missing"):
            paths = self._storage(account, output_dir, separate)
            if paths is None or not (paths.record_dir / ".luvbox-history.sqlite3").is_file():
                return ()
            with closing(_ReadOnlyHistory(paths)) as store:
                return (store.excluded_archive_keys(RETRY_LIMIT) if view == "excluded"
                        else store.lost_archive_keys(RETRY_LIMIT))
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
                self._clear_selection()
                self._rebuild_unsaved()
                self._unsaved_total, self._unsaved_page, self._unsaved_pages = (
                    result.get("total", 0), result.get("page", 0), result.get("pages", 0))
                self._retryable_total = result.get("retryable_total")
                self._retryable_limit = result.get("retryable_limit")
        elif kind == "counts":
            if error:
                self._unsaved_error = error
            elif result:
                self._counts = {reason: result.get(reason, 0) for reason in unsaved.REASONS}
                self._missing_count = result.get(MISSING, 0)
        elif kind == "dismiss":
            self._unsaved_error = error
            self._unsaved_status = "" if error else "목록에서 지웠습니다. 저장된 파일에는 영향이 없습니다."
            self.refresh()
        elif kind == "release":
            self._unsaved_error = error
            if error:
                self._unsaved_status = ""
            elif result.get("released"):
                self._unsaved_status = "제외를 해제했습니다. 다음 저장에서 다시 받으며, 저장된 파일은 그대로입니다."
            else:
                self._unsaved_status = "해제할 제외 항목이 없습니다."
            if not error:
                self.refresh()
                if result.get("released"):
                    self.catalogChanged.emit()
        elif kind == "exclude":
            self._unsaved_error = error
            count = result.get("excluded", 0)
            if error:
                self._unsaved_status = ""
            elif count:
                self._unsaved_status = (f"{count:,}개를 갤러리에서 뺐습니다. 다시 받지 않으며, "
                                        "‘직접 제외’에서 되돌릴 수 있습니다.")
            else:
                self._unsaved_status = "뺄 항목이 없습니다. 파일이 다시 생겼을 수 있습니다."
            if not error:
                self.refresh()
                if count:
                    self.catalogChanged.emit()
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
