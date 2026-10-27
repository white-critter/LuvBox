"""Read-only Qt library browsing with off-thread catalog and file access."""

from collections import deque
from contextlib import closing
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from threading import Condition, Thread
import webbrowser

from PySide6.QtCore import QObject, Property, Qt, QTimer, QUrl, Signal, Slot

from .history import HistoryStore
from .preview import Thumbnail, thumbnail
from .qt_gallery import GalleryModel, SafeThumbnailProvider
from .recycling import file_signature
from .storage import records_root, resolve_storage
from .thumbcache import cloud_only


_KEY = re.compile(r"g([0-9]+)_(author|date|flat)_([0-9]{1,20})_([0-9]{1,5})_(photo|video|animated_gif)\Z")
_ORDERS = {"saved", "posted", "posted_oldest", "liked"}
_KINDS = {"all", "photo", "video", "animated_gif"}
_MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4"}
_DRIVE_MISSING = "파일을 열 수 없습니다. 저장 드라이브를 연결해 주세요."
_FILE_MISSING = "이 파일을 찾지 못했습니다. 저장 폴더 밖에서 지우거나 옮긴 파일일 수 있습니다."
_POSTED = re.compile(r"(\d{4}-\d\d-\d\d)T(\d\d:\d\d)")


def _posted(value, *, time=False):
    """'2026-10-01' (or with its Korean-time hour) from a stored post time."""
    match = _POSTED.match(value) if isinstance(value, str) else None
    if match is None:
        return ""
    return f"{match[1]} {match[2]}" if time else match[1]


class _ReadOnlyHistory(HistoryStore):
    def __init__(self, paths):
        self.account_dir = paths.record_dir
        self.media_dir = paths.media_dir
        self.output_dir = paths.output_dir
        self.legacy_dir = paths.legacy_dir
        self._isolated_records = True
        self._folder_cache = None
        records = records_root().resolve()
        self._app_data = (records, records.parent / "backups")
        self.path = self.account_dir / ".luvbox-history.sqlite3"
        if self._linked(self.path) or not self._real_folders(self.account_dir):
            raise ValueError("unsafe catalog")
        if not self.path.is_file():
            raise FileNotFoundError("catalog missing")
        self._db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=2)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA query_only=ON")
            self._db.execute("PRAGMA busy_timeout=2000")
            self._media_roots = self._read_media_roots()
        except Exception:
            self._db.close()
            raise


@dataclass(frozen=True)
class _Request:
    generation: int
    account: str
    output_dir: str
    separate_accounts: bool
    query: str = ""
    media_type: str = "all"
    order: str = "saved"
    author: str = ""

    def location(self):
        return resolve_storage(self.output_dir, self.account, self.separate_accounts)


class LibraryRepository:
    def __init__(self):
        self._condition = Condition()
        self._request = None
        self.provider = None

    def configure(self, request):
        with self._condition:
            self._request = request

    def current(self, generation=None):
        with self._condition:
            request = self._request
            return request if request is not None and (generation is None or request.generation == generation) else None

    def read(self, token, offset, limit):
        request = self.current()
        if request is None or token != str(request.generation):
            return 0, []
        try:
            with closing(_ReadOnlyHistory(request.location())) as store:
                # Check each folder's links once per page, as a library rescan
                # does. The thumbnail provider checks each file again (link
                # and resolved path) before and after decoding it.
                store._folder_cache = {}
                page = store.record_page(offset // limit, limit, request.media_type, request.query,
                                         order=request.order, author=request.author, include_deleted=False)
                rows = []
                for row in page["rows"]:
                    key = f"g{request.generation}_{row['folder_mode']}_{row['tweet_id']}_{row['num']}_{row['media_type']}"
                    if not _KEY.fullmatch(key):
                        raise ValueError("invalid media identity")
                    path = self._path(store, row)
                    with self._condition:
                        if self._request is not request:
                            return 0, []
                        if path is not None:
                            self.provider.register(key, path)
                    author = row.get("author") or ""
                    # New file names already start with the author; the post
                    # date tells tiles apart, the name stays searchable.
                    detail = _posted(row.get("posted_at")) or row.get("filename") or row["tweet_id"]
                    caption = ("@" + author + " · " if author else "") + detail
                    if path is None:
                        caption += " · 파일 없음"
                    rows.append({"key": key, "caption": caption})
                if page["page"] * limit != offset:
                    return page["total"], []
                return page["total"], rows
        except FileNotFoundError:
            return 0, []

    @staticmethod
    def _path(store, row):
        value = row.get("path")
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            return None
        try:
            path = store._media_path(value)
            if path is None or not path.is_file() or cloud_only(path):
                return None
        except (OSError, RuntimeError, ValueError):
            return None
        return path

    def _row(self, store, key, request):
        match = _KEY.fullmatch(key) if isinstance(key, str) else None
        if match is None or int(match[1]) != request.generation or not 1 <= int(match[4]) <= 10000:
            return None
        return store.media_record(match[2], {"tweet_id": match[3], "num": int(match[4]), "type": match[5]})

    def selected(self, key, generation):
        request = self.current(generation)
        if request is None:
            return None
        with closing(_ReadOnlyHistory(request.location())) as store:
            row = self._row(store, key, request)
            if row is None:
                return None
            content = store._db.execute("SELECT content FROM tweets WHERE tweet_id=?", (row["tweet_id"],)).fetchone()
            path = self._path(store, row)
            width = height = raw_width = raw_height = 0
            if path is not None and row["media_type"] == "photo":
                from .preview import photo_info

                try:
                    signature = file_signature(path)
                    dimensions = photo_info(path)
                    width, height = dimensions.get("pixel_width", 0), dimensions.get("pixel_height", 0)
                    raw_width, raw_height = dimensions.get("source_width", 0), dimensions.get("source_height", 0)
                    if file_signature(path) != signature or self._path(store, row) != path:
                        path = None
                        width = height = raw_width = raw_height = 0
                except OSError:
                    path = None
                    width = height = raw_width = raw_height = 0
            source = f"https://x.com/i/web/status/{row['tweet_id']}"
            info = " · ".join(value for value in (row.get("filename") or "", _posted(row.get("posted_at"), time=True))
                              if value)
            return {"key": key, "author": row.get("author") or "",
                    "body": content[0] if content and isinstance(content[0], str) else "",
                    "source": source, "path": str(path) if path is not None else "", "missing": path is None,
                    "missing_note": "" if path is not None else absence_note(store.output_dir),
                    "preview": QUrl.fromLocalFile(str(path)).toString() if path is not None else "",
                    "media_type": row["media_type"], "public_info": info,
                    "pixel_width": width, "pixel_height": height,
                    "source_width": raw_width, "source_height": raw_height}

    def decode(self, path, size):
        # Only paths that read() checked against this request's catalog are
        # registered, and the provider rejects a link or a changed resolved
        # path before decoding. Reopening the catalog for every thumbnail
        # cost more than the decode itself.
        if self.current() is None:
            return Thumbnail(None, "")
        return thumbnail(path, size)

    def action(self, key, generation, action):
        selection = self.selected(key, generation)
        if selection is None or self.current(generation) is None:
            return "사진을 다시 선택해 주세요."
        if action == "source":
            return "" if webbrowser.open(selection["source"]) else "브라우저를 열지 못했습니다."
        if not selection["path"]:
            return "파일을 찾지 못했습니다. 저장 위치를 확인해 주세요."
        if action == "open":
            if Path(selection["path"]).suffix.lower() not in _MEDIA_SUFFIXES:
                return "지원하는 사진·영상 파일만 열 수 있습니다. 폴더에서 파일을 확인해 주세요."
            os.startfile(selection["path"])
        elif action == "reveal":
            subprocess.Popen(["explorer.exe", "/select," + selection["path"]],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return ""


def absence_note(output_dir):
    """Say whether the save drive itself is gone, or only this file is."""
    try:
        root = Path(output_dir)
        if root.anchor and not Path(root.anchor).exists():
            return _DRIVE_MISSING
    except (OSError, RuntimeError, ValueError):
        pass
    return _FILE_MISSING


class _LibraryThumbnailProvider(SafeThumbnailProvider):
    def _record(self, name, elapsed):
        return None


class LibraryController(QObject):
    changed = Signal()
    shutdownFinished = Signal()
    _delivered = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._repository = LibraryRepository()
        self.provider = _LibraryThumbnailProvider(decoder=self._repository.decode)
        self._repository.provider = self.provider
        self.model = GalleryModel(self._repository.read, provider=self.provider, parent=self)
        self._generation = 0
        self._account = ""
        self._output_dir = ""
        self._separate_accounts = False
        self._query = ""
        self._media_type = "all"
        self._order = "saved"
        self._author = ""
        self._selected = {}
        self._selection_key = ""
        self._selection_serial = 0
        self._navigation = None
        self._busy = False
        self._status = ""
        self._condition = Condition()
        self._pending = deque()
        self._closed = False
        self._closing = False
        self._shutdown_complete = False
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(50)
        self._shutdown_timer.timeout.connect(self._poll_shutdown)
        self._delivered.connect(self._accept, Qt.ConnectionType.QueuedConnection)
        self._worker = Thread(target=self._work, name="qt-library-actions", daemon=True)
        self._worker.start()
        self.model.changed.connect(self._model_changed)

    @Property(str, notify=changed)
    def selectedKey(self):
        return self._selected.get("key", "")

    @Property(int, notify=changed)
    def selectedIndex(self):
        return self.model._positions.get(self._selection_key or self.selectedKey, -1)

    @Property(bool, notify=changed)
    def canPrevious(self):
        return not (self._closed or self._busy) and self.selectedIndex > 0

    @Property(bool, notify=changed)
    def canNext(self):
        index = self.selectedIndex
        return (not (self._closed or self._busy) and index >= 0
                and (index + 1 < self.model.count
                     or (not self.model._exhausted and self.model.count < self.model.total)))

    @Property(str, notify=changed)
    def selectedAuthor(self):
        return self._selected.get("author", "")

    @Property(str, notify=changed)
    def selectedBody(self):
        return self._selected.get("body", "")

    @Property(str, notify=changed)
    def selectedSource(self):
        return self._selected.get("source", "")

    @Property(str, notify=changed)
    def selectedPreviewUrl(self):
        return self._selected.get("preview", "")

    @Property(str, notify=changed)
    def selectedMediaType(self):
        return self._selected.get("media_type", "")

    @Property(str, notify=changed)
    def selectedPublicInfo(self):
        return self._selected.get("public_info", "")

    @Property(int, notify=changed)
    def selectedPixelWidth(self):
        return self._selected.get("pixel_width", 0)

    @Property(int, notify=changed)
    def selectedPixelHeight(self):
        return self._selected.get("pixel_height", 0)

    @Property(int, notify=changed)
    def selectedRawWidth(self):
        return self._selected.get("source_width", 0)

    @Property(int, notify=changed)
    def selectedRawHeight(self):
        return self._selected.get("source_height", 0)

    @Property(bool, notify=changed)
    def selectedMissing(self):
        return self._selected.get("missing", True)

    @Property(str, notify=changed)
    def selectedMissingNote(self):
        return self._selected.get("missing_note", "")

    @Property(bool, notify=changed)
    def busy(self):
        return self._busy

    @Property(str, notify=changed)
    def status(self):
        return self._status

    @Property(bool, notify=changed)
    def closing(self):
        return self._closing

    @Property(bool, notify=shutdownFinished)
    def shutdownComplete(self):
        return self._shutdown_complete

    @Slot(str, str, bool)
    def setLibrary(self, account, output_dir, separate_accounts):
        self._account = account.strip().lstrip("@").lower()
        self._output_dir = output_dir
        self._separate_accounts = separate_accounts
        self.refresh()

    @Slot()
    def refresh(self):
        if self._closed:
            return
        self._generation += 1
        self._selection_serial += 1
        self._selected = {}
        self._selection_key = ""
        self._navigation = None
        self._status = ""
        self._busy = False
        with self._condition:
            self._pending.clear()
        request = None
        if self._account and self._output_dir:
            request = _Request(self._generation, self._account, self._output_dir, self._separate_accounts,
                               self._query, self._media_type, self._order, self._author)
        self._repository.configure(request)
        self.provider.clear_registry()
        self.model.search(str(self._generation))
        self.changed.emit()

    @Slot(str)
    def search(self, query):
        query = query[:500]
        if query != self._query:
            self._query = query
            self.refresh()

    @Slot(str, str, str)
    def setFilters(self, media_type, order, author):
        if media_type not in _KINDS or order not in _ORDERS or (author and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", author)):
            self._status = "필터를 확인해 주세요."
            self.changed.emit()
            return
        self._media_type, self._order, self._author = media_type, order, author
        self.refresh()

    @Slot(str)
    def selectItem(self, key):
        if key not in self.model._keys or self._closed:
            return
        cancelled_navigation = self._navigation is not None
        if cancelled_navigation:
            self._navigation = None
            self._busy = False
        # Synchronizing the grid highlight with an open preview must not queue
        # the same selection again or clear its media while it is loading.
        if key == self._selection_key and (self._busy or self.selectedKey == key):
            if cancelled_navigation:
                self._status = ""
                self.changed.emit()
            return
        self._selection_serial += 1
        self._selection_key = key
        self._selected = {}
        self._busy = True
        self._status = ""
        with self._condition:
            self._pending = deque(item for item in self._pending if item[0] != "select")
            self._pending.append(("select", self._generation, self._selection_serial, key))
            self._condition.notify()
        self.changed.emit()

    @Slot(result=bool)
    def previousItem(self):
        return self._navigate(-1)

    @Slot(result=bool)
    def nextItem(self):
        return self._navigate(1)

    def _navigate(self, direction):
        if self._closed or self._busy:
            return False
        current = self.selectedIndex
        target = current + direction
        if current < 0 or target < 0:
            return False
        if target < self.model.count:
            self.selectItem(self.model._rows[target]["key"])
            return True
        if (direction < 0 or self.model._exhausted or target >= self.model.total):
            return False
        # The next page belongs to the existing filtered/sorted model. Keep
        # this photo visible while its asynchronous reader fetches the page.
        self._navigation = (self._generation, self._selection_serial, self.selectedKey, target)
        self._busy = True
        self._status = "다음 사진을 불러오는 중…"
        if not self.model.busy:
            self.model.loadMore()
        self.changed.emit()
        return True

    @Slot()
    def _model_changed(self):
        if self._closed:
            return
        pending = self._navigation
        if pending is not None and not self.model.busy:
            self._navigation = None
            generation, serial, key, target = pending
            if generation == self._generation and serial == self._selection_serial and key == self.selectedKey:
                self._busy = False
                if target < self.model.count:
                    self.selectItem(self.model._rows[target]["key"])
                    return
                self._status = self.model.error or "마지막 사진입니다."
        self.changed.emit()

    def _action(self, action):
        if self._closed or self._busy or not self.selectedKey:
            return
        self._busy = True
        with self._condition:
            self._pending.append((action, self._generation, self._selection_serial, self.selectedKey))
            self._condition.notify()
        self.changed.emit()

    @Slot()
    def openSelected(self):
        self._action("open")

    @Slot()
    def openSource(self):
        self._action("source")

    @Slot()
    def revealSelected(self):
        self._action("reveal")

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending)
                if self._closed:
                    return
                action, generation, serial, key = self._pending.popleft()
            if self._repository.current(generation) is None:
                continue
            selection, message = None, ""
            try:
                if action == "select":
                    selection = self._repository.selected(key, generation)
                    if selection is None:
                        message = "사진을 다시 선택해 주세요."
                else:
                    message = self._repository.action(key, generation, action)
            except Exception:
                message = "사진 정보를 읽지 못했습니다." if action == "select" else "열지 못했습니다. 파일과 기본 앱을 확인해 주세요."
            self._delivered.emit((generation, serial, action, selection, message))

    @Slot(object)
    def _accept(self, result):
        generation, serial, action, selection, message = result
        if self._closed or generation != self._generation or serial != self._selection_serial:
            return
        if action == "select":
            self._selected = selection or {}
        self._busy = False
        self._status = message
        self.changed.emit()

    @Slot(result=bool)
    def shutdown(self):
        if self._closing:
            return self._shutdown_complete
        self._closing = True
        with self._condition:
            self._closed = True
            self._pending.clear()
            self._navigation = None
            self._condition.notify_all()
        self._repository.configure(None)
        self.provider.close()
        self.model.shutdown(timeout=0)
        self.changed.emit()
        self._shutdown_timer.start()
        self._poll_shutdown()
        return self._shutdown_complete

    @Slot()
    def _poll_shutdown(self):
        if not self._closing or self._shutdown_complete:
            return
        if (self._worker.is_alive() or self.model._worker.is_alive() or
                not self.provider.wait_for_idle(timeout=0) or not self.provider.join_workers(timeout=0)):
            return
        self._shutdown_timer.stop()
        self._shutdown_complete = True
        self.shutdownFinished.emit()
