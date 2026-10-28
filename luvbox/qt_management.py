"""Off-thread management reads and guarded, explicitly confirmed mutations."""

from collections import deque
from contextlib import closing
from pathlib import Path
import sqlite3
from threading import Condition, Event, Thread
import uuid

from PySide6.QtCore import QObject, Property, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices

from .history import HistoryStore
from .management_store import ManagementSnapshot, PAGE_SIZE
from .storage import OutputGuard, publish_snapshot, resolve_storage


_SECTIONS = {"schedule", "missing", "deletions", "statistics", "runs"}
_STATUS = {"finished": "완료", "stopped": "중단", "error": "실패", "interrupted": "결과 확인 필요",
           "running": "실행 중", "partial": "일부 완료", "limited": "이전 버전 수량 제한",
           "recycled": "휴지통 이동", "failed": "실패", "cancelled": "취소", "pending": "처리 대기",
           "busy": "다른 작업으로 건너뜀"}


class ManagementController(QObject):
    changed = Signal()
    shutdownFinished = Signal()
    deleteConfirmationRequested = Signal()
    libraryChanged = Signal()
    retryRequested = Signal(object)
    _delivered = Signal(object)

    def __init__(self, library, parent=None):
        super().__init__(parent)
        self._library = library
        self._context = ("", "", False)
        self._generation = 0
        self._read_id = 0
        self._invalidated = True
        self._section = "schedule"
        self._page = 0
        self._pages = self._total = 0
        self._rows = []
        self._statistics = {}
        self._tokens = {}
        self._selected = set()
        self._mode = "all"
        self._all_accounts = False
        self._reading = self._mutating = self._locked = self._closing = False
        self._error = self._status = self._checked = ""
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

    @Property(str, notify=changed)
    def section(self): return self._section
    @Property("QVariantList", notify=changed)
    def rows(self): return self._rows
    @Property("QVariantMap", notify=changed)
    def statistics(self): return self._statistics
    @Property("QVariantList", notify=changed)
    def selectedKeys(self): return sorted(self._selected)
    @Property(int, notify=changed)
    def selectedCount(self): return len(self._selected)
    @Property(int, notify=changed)
    def total(self): return self._total
    @Property(int, notify=changed)
    def pageIndex(self): return self._page
    @Property(int, notify=changed)
    def pageCount(self): return self._pages
    @Property(bool, notify=changed)
    def busy(self): return self._reading
    @Property(bool, notify=changed)
    def mutating(self): return self._mutating
    @Property(bool, notify=changed)
    def locked(self): return self._locked or self._closing
    @Property(str, notify=changed)
    def error(self): return self._error
    @Property(str, notify=changed)
    def status(self): return self._status
    @Property(str, notify=changed)
    def checkedAt(self): return self._checked
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
        context = (account, output_dir, bool(separate_accounts))
        if context == self._context: return
        self._generation += 1
        self._invalidated = True
        self._context = context
        self._rows, self._statistics, self._tokens, self._selected = [], {}, {}, set()
        self._total = self._pages = self._page = 0
        self._error = self._status = self._checked = ""
        self._delete = None
        self._read(force=True) if self._section != "schedule" else self.changed.emit()

    @Slot(str)
    def setSection(self, section):
        if section not in _SECTIONS or self._closing: return
        self._section, self._page = section, 0
        self._selected.clear()
        if section != "schedule": self._read()
        else: self.changed.emit()

    @Slot(str)
    def setMissingMode(self, mode):
        if mode in {"all", "missing", "ignored", "unavailable"}:
            self._mode, self._page = mode, 0
            self._read()

    @Slot(bool)
    def setAllAccounts(self, value):
        self._all_accounts, self._page = bool(value), 0
        self._read()

    @Slot()
    def refresh(self):
        self._invalidated = True
        if self._section != "schedule": self._read(force=True)

    @Slot(int)
    def goPage(self, page):
        if not self._reading and 0 <= page < self._pages:
            self._page = page
            self._read()

    def _read(self, force=False, preserve_error=False):
        if self._closing: return
        self._reading = True
        self._read_id += 1
        self._selected.clear()
        self._tokens.clear()
        if not preserve_error:
            self._error = ""
        with self._condition:
            self._pending = deque(item for item in self._pending if item[0] != "read")
            self._pending.append(("read", self._generation, (self._context, self._section, self._page,
                                                           self._mode, self._all_accounts,
                                                           force or self._invalidated, self._read_id)))
            self._condition.notify()
        self.changed.emit()

    @Slot(str)
    def toggleRow(self, token):
        if self._section != "missing" or self._reading or self._mutating or token not in self._tokens: return
        if self._tokens[token].get("availability") == "unavailable": return
        self._selected.symmetric_difference_update({token})
        self.changed.emit()

    @Slot()
    def selectPage(self):
        if self._reading or self._mutating: return
        self._selected = {key for key, row in self._tokens.items() if row.get("availability") == "missing"}
        self.changed.emit()

    def _chosen(self):
        return [self._tokens[key] for key in self._selected if key in self._tokens
                and self._tokens[key].get("availability") == "missing"]

    @Slot(bool)
    def setIgnored(self, ignored):
        rows = self._chosen()
        if self.locked or self._mutating or not rows: return
        self._mutating = True
        self._stop.clear()
        self._enqueue("ignore", (self._context, [row["_identity"] for row in rows], bool(ignored)))

    @Slot()
    def retrySelected(self):
        rows = self._chosen()
        if self.locked or self._mutating or not rows: return
        self._mutating = True
        self._stop.clear()
        self._status = "다시 받을 파일의 상태를 확인합니다"
        self._enqueue("validate_retry", (self._context, [(row["_identity"], row["archive_key"]) for row in rows]))

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

    @Slot(str)
    def openLog(self, token):
        row = self._tokens.get(token)
        if not row or not row.get("log"): return
        from .scheduled import log_directory
        try:
            path = Path(row["log"])
            root = log_directory().resolve()
            if (path.is_symlink() or path.parent.resolve() != root or path.suffix != ".log"
                    or not path.name.startswith("scheduled-") or not path.is_file()): return
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except (OSError, ValueError):
            self._error = "로그 파일을 열지 못했습니다."
            self.changed.emit()

    def _enqueue(self, kind, payload):
        with self._condition:
            self._pending.append((kind, self._generation, payload))
            self._condition.notify()
        self.changed.emit()

    def _work(self):
        cache, cache_key = None, None
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._closing or self._pending)
                    if self._closing: return
                    kind, generation, payload = self._pending.popleft()
                try:
                    if generation != self._generation:
                        if kind != "read":
                            self._delivered.emit((kind, generation, {}, "계정이나 목록이 바뀌었습니다."))
                        continue
                    if kind == "read":
                        context, section, page, mode, all_accounts, force, read_id = payload
                        if context != cache_key or force:
                            if cache: cache.close()
                            cache, cache_key = None, context
                            try:
                                if not context[0] or not context[1]: raise FileNotFoundError("no account")
                                paths = resolve_storage(context[1], context[0], context[2])
                                cache = ManagementSnapshot(paths, lambda: self._closing or generation != self._generation)
                            except FileNotFoundError:
                                pass  # Reading an absent catalog never creates a new one.
                        if cache:
                            if section == "missing": result = cache.missing_page(page, mode)
                            elif section == "deletions": result = cache.deletion_page(page)
                            elif section == "statistics": result = cache.statistics(page)
                            else: result = cache.run_page(page, all_accounts)
                            result["checked_at"] = cache.checked_at
                        elif section == "runs" and (all_accounts or context[0]):
                            from .activity import outcome_page
                            result = outcome_page(page, PAGE_SIZE, account="" if all_accounts else context[0])
                        else: result = dict(rows=[], total=0, page=0, pages=0)
                        result.update(section=section, read_id=read_id)
                    elif kind == "prepare_delete":
                        result = self._library.deletionManifest(payload)
                        if self._stop.is_set(): result = {"rows": [], "cancelled": True}
                    elif kind == "delete":
                        from .deletion import run_deletion
                        manifest, keep = payload
                        result = run_deletion(manifest["location"], manifest["rows"], keep, self._stop,
                                              lambda event: self._delivered.emit(("progress", generation, event, "")))
                        cache_key = None
                    elif kind == "ignore":
                        context, identities, ignored = payload
                        result = self._ignore(context, identities, ignored)
                        cache_key = None
                    else:
                        context, selected = payload
                        result = self._validate_retry(context, selected)
                    self._delivered.emit((kind, generation, result, ""))
                except InterruptedError:
                    pass
                except Exception as exc:
                    detail = dict(section=payload[1], read_id=payload[6]) if kind == "read" else {}
                    error = ("파일 상태가 바뀌었습니다. 목록을 다시 확인해 주세요."
                             if isinstance(exc, ValueError) and kind in {"ignore", "validate_retry"}
                             else "작업을 마치지 못했습니다. 계정·폴더·파일 상태를 확인해 주세요.")
                    self._delivered.emit((kind, generation, detail, error))
        finally:
            if cache: cache.close()

    def _ignore(self, context, identities, ignored):
        from .protection import AccountGuard
        paths = resolve_storage(context[1], context[0], context[2])
        if not (paths.record_dir / ".luvbox-history.sqlite3").is_file():
            raise FileNotFoundError("catalog disappeared")
        account = AccountGuard(paths.account, sleep=lambda *_: None)
        account.acquire(respect_cooldown=False)
        try:
            output = OutputGuard(paths.output_dir)
            output.acquire()
            guards = [output]
            try:
                if self._stop.is_set(): return {"message": "변경을 취소했습니다"}
                with closing(HistoryStore.for_storage(paths, protected=True, create_media=False)) as store:
                    for root in store.media_roots:
                        if root == paths.output_dir:
                            continue
                        extra = OutputGuard(root, group=output)
                        extra.acquire()
                        guards.append(extra)
                    self._require_missing(store, identities)
                    if self._stop.is_set(): return {"message": "변경을 취소했습니다"}
                    store.set_missing_ignored(identities, ignored)
                warning = ""
                if paths.output_dir.is_dir():
                    try:
                        publish_snapshot(paths)
                    except (OSError, ValueError, sqlite3.Error):
                        warning = " 저장 폴더의 기록 사본은 다음 저장 때 갱신합니다."
            finally:
                for guard in reversed(guards):
                    guard.close()
        finally: account.close()
        return {"message": f"{len(identities):,}개 항목의 다시 받기 설정을 변경했습니다." + warning}

    @staticmethod
    def _require_missing(store, identities):
        for identity in identities:
            if store._db.execute("SELECT 1 FROM deletion_items WHERE tweet_id=? AND num=? AND media_type=? AND status='pending' LIMIT 1", identity).fetchone():
                raise ValueError("deletion outcome is pending")
            candidates = store._db.execute("SELECT path,last_path FROM media WHERE tweet_id=? AND num=? AND media_type=?", identity).fetchall()
            if not candidates or any(ManagementSnapshot._availability(store, row["path"] or row["last_path"])[0] != "missing"
                                     for row in candidates):
                raise ValueError("file availability changed")

    def _validate_retry(self, context, selected):
        from .qt_library import _ReadOnlyHistory
        paths = resolve_storage(context[1], context[0], context[2])
        with closing(_ReadOnlyHistory(paths)) as store:
            self._require_missing(store, [identity for identity, _ in selected])
        if self._stop.is_set():
            return {"message": "다시 받기를 취소했습니다", "keys": ()}
        return {"keys": tuple(key for _, key in selected)}

    @Slot(object)
    def _accept(self, event):
        kind, generation, value, error = event
        if self._closing: return
        if generation != self._generation:
            if kind not in {"read", "progress"}:
                self._delete = None
                self._mutating = False
                self._error = "계정이나 목록이 바뀌었습니다. 작업 결과를 다시 확인해 주세요."
                self.changed.emit()
            return
        if kind == "progress":
            self._status = str(value.get("message", ""))
        elif kind == "read":
            if value.get("section") != self._section or value.get("read_id") != self._read_id: return
            self._reading = False
            self._invalidated = bool(error)
            self._error = error or self._error
            self._rows, self._tokens, self._selected = [], {}, set()
            for row in value.get("rows", []):
                token = uuid.uuid4().hex
                self._tokens[token] = row
                status = ("휴지통 이동 결과 확인 필요" if row.get("deletion_pending") else
                          "확인 불가" if row.get("availability") == "unavailable" else
                          "다시 받지 않기" if row.get("ignored") else "파일 없음") if self._section == "missing" else _STATUS.get(row.get("status"), str(row.get("status", "")))
                origin = {"manual": "수동", "scheduled": "자동", "legacy": "기존 기록"}.get(row.get("origin"), "")
                detail = [row.get("account", ""), status,
                          row.get("created_at") or row.get("started_at") or row.get("finished_at") or row.get("posted_at") or "", origin]
                if self._section == "runs":
                    if not row.get("account"):
                        detail.insert(0, "계정 미확인")
                    detail.extend((f"저장 {row.get('saved', 0):,}개", f"건너뜀 {row.get('skipped', 0):,}개"))
                elif self._section == "deletions":
                    detail.extend(("다시 받지 않기" if row.get("keep_deleted") else "", row.get("path", "")))
                elif self._section == "missing":
                    detail.extend(("@" + row["author"] if row.get("author") else "", row.get("last_path", "")))
                self._rows.append(dict(token=token, title=row.get("filename") or ("@" + row["author"] if row.get("author") else origin or "실행 기록"),
                                       detail=" · ".join(str(part) for part in detail if part),
                                       message=str(row.get("message") or ""),
                                       selectable=row.get("availability") == "missing", hasLog=bool(row.get("log"))))
            self._statistics = value.get("statistics", {})
            self._total, self._page, self._pages = value.get("total", 0), value.get("page", 0), value.get("pages", 0)
            self._checked = value.get("checked_at", "")
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
                self.deleteConfirmationRequested.emit()
        elif kind == "validate_retry" and not error:
            self._mutating = False
            self._status = value.get("message", "")
            self.changed.emit()
            if value.get("keys"):
                self.retryRequested.emit(value["keys"])
            return
        else:
            self._mutating = False
            self._error = error
            self._status = str(value.get("message") or ("작업을 마쳤습니다" if not error else ""))
            self.libraryChanged.emit()
            self._invalidated = True
            if self._section != "schedule":
                self._read(force=True, preserve_error=bool(error))
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
