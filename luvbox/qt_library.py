"""Read-only Qt library browsing with off-thread catalog and file access."""

from collections import deque
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from threading import Condition, Thread
import webbrowser

from PySide6.QtCore import QObject, Property, Qt, QTimer, QUrl, Signal, Slot

from .history import HistoryStore, page_cursor
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
_CLOUD_ONLY = "이 파일은 클라우드에만 있습니다. 탐색기에서 받은 뒤 다시 열 수 있습니다."
_POSTED = re.compile(r"(\d{4}-\d\d-\d\d)T(\d\d:\d\d)")
MAX_SELECTION = 2000


def _posted(value, *, time=False):
    """'2026-10-01' (or with its Korean-time hour) from a stored post time."""
    match = _POSTED.match(value) if isinstance(value, str) else None
    if match is None:
        return ""
    return f"{match[1]} {match[2]}" if time else match[1]


def _format_size(size_bytes: int | None) -> str:
    if size_bytes is None or size_bytes <= 0:
        return ""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


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
    date_field: str = "posted"
    date_from: str = ""
    date_to: str = ""
    hashtag: str = ""
    favorite_only: bool = False
    user_tag: str = ""
    duplicate_only: bool = False

    def location(self):
        return resolve_storage(self.output_dir, self.account, self.separate_accounts)


class LibraryRepository:
    def __init__(self):
        self._condition = Condition()
        self._request = None
        self._served = {}
        self._cursor = None
        self._cursor_offset = 0
        self.provider = None

    def configure(self, request):
        with self._condition:
            self._request = request
            self._served.clear()
            self._cursor = None
            self._cursor_offset = 0

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
                period = ((request.date_field, request.date_from, request.date_to)
                          if request.date_from or request.date_to else None)
                # Continue from the last row this view already accepted. An
                # OFFSET would slide when a save inserts newer rows, and the
                # next page would repeat keys until the gallery stopped.
                after = self._cursor if offset and offset == self._cursor_offset else None
                page = store.record_page(0 if after is not None else offset // limit, limit,
                                         request.media_type, request.query,
                                         order=request.order, author=request.author, include_deleted=False,
                                         period=period, after=after, hashtag=request.hashtag,
                                         favorite_only=request.favorite_only, user_tag=request.user_tag,
                                         duplicate_only=request.duplicate_only)
                favs = store.favorite_identities()
                rows = []
                served = {}
                last = None
                for row in page["rows"]:
                    key = f"g{request.generation}_{row['folder_mode']}_{row['tweet_id']}_{row['num']}_{row['media_type']}"
                    if not _KEY.fullmatch(key):
                        raise ValueError("invalid media identity")
                    path, kind = self._locate(store, row)
                    stamp = None
                    if kind == "ready":
                        try:
                            info = path.stat()
                            stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                        except OSError:
                            path, kind = None, "missing"
                    served[key] = (str(path) if kind == "ready" else "", stamp)
                    with self._condition:
                        if self._request is not request:
                            return 0, []
                        if kind == "ready":
                            self.provider.register(key, path)
                    author = row.get("author") or ""
                    # New file names already start with the author; the post
                    # date tells tiles apart, the name stays searchable.
                    detail = _posted(row.get("posted_at")) or row.get("filename") or row["tweet_id"]
                    caption = ("@" + author + " · " if author else "") + detail
                    if kind == "cloud":
                        caption += " · 클라우드에만 있음"
                    elif kind != "ready":
                        caption += " · 파일 없음"
                    is_fav = (row["folder_mode"], row["tweet_id"], row["num"], row["media_type"]) in favs
                    rows.append({"key": key, "caption": caption, "missing": kind != "ready",
                                 "favorite": is_fav})
                    last = row
                if after is None and page["page"] * limit != offset:
                    return page["total"], []
                if after is not None and not rows:
                    # Nothing is left after the last shown row. Newer saves sit
                    # in front of this cursor; "새 사진 보기" loads them.
                    return offset, []
                with self._condition:
                    if self._request is not request:
                        return 0, []
                    for key, item in served.items():
                        self._served.setdefault(key, item)
                    if last is not None:
                        self._cursor = page_cursor(last, request.order)
                        self._cursor_offset = offset + len(rows)
                return page["total"], rows
        except FileNotFoundError:
            return 0, []

    def authors(self, generation=None):
        request = self.current(generation)
        if request is None:
            return []
        try:
            with closing(_ReadOnlyHistory(request.location())) as store:
                return store.authors(include_deleted=False)
        except Exception:
            return []

    def hashtags(self, generation=None):
        request = self.current(generation)
        if request is None:
            return []
        try:
            with closing(_ReadOnlyHistory(request.location())) as store:
                return store.hashtags(include_deleted=False)
        except Exception:
            return []

    def user_tags(self, generation=None):
        request = self.current(generation)
        if request is None:
            return []
        try:
            with closing(_ReadOnlyHistory(request.location())) as store:
                return store.user_tags()
        except Exception:
            return []

    def overview(self, generation=None):
        request = self.current(generation)
        if request is None:
            return {}
        try:
            with closing(_ReadOnlyHistory(request.location())) as store:
                ov = store.overview(verify_files=False, top=10)
                st = store.statistics(top=10)
                return {
                    "files": ov.get("files", 0),
                    "posts": ov.get("posts", 0),
                    "bytes": ov.get("bytes", 0),
                    "formatted_bytes": _format_size(ov.get("bytes", 0)),
                    "photo": ov.get("photo", 0),
                    "video": ov.get("video", 0),
                    "animated_gif": ov.get("animated_gif", 0),
                    "authors": st.get("authors", []),
                    "monthly": [{"month": m[0], "count": m[1]} for m in st.get("months", {}).get("posted", [])],
                }
        except Exception:
            return {}

    @staticmethod
    def _locate(store, row):
        """``(path, kind)`` where kind is ready, cloud, or missing.

        A cloud-only file is still in the folder. Reading it would download it,
        so the gallery refuses the bytes and says so instead of calling it deleted.
        """
        value = row.get("path")
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            return None, "missing"
        try:
            path = store._media_path(value)
            if path is None or not path.is_file():
                return None, "missing"
            if cloud_only(path):
                return path, "cloud"
        except (OSError, RuntimeError, ValueError):
            return None, "missing"
        return path, "ready"

    def _path(self, store, row):
        path, kind = self._locate(store, row)
        return path if kind == "ready" else None

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
            content_row = None
            if store._has_table("tweets"):
                has_hashtags = store._has_column("tweets", "hashtags")
                select_sql = "SELECT content, hashtags FROM tweets WHERE tweet_id=?" if has_hashtags else "SELECT content, '' FROM tweets WHERE tweet_id=?"
                content_row = store._db.execute(select_sql, (row["tweet_id"],)).fetchone()
            body_text = content_row[0] if content_row and isinstance(content_row[0], str) else ""
            raw_tags = content_row[1] if content_row and len(content_row) > 1 and isinstance(content_row[1], str) else ""
            tweet_hashtags = [t.strip() for t in raw_tags.split() if t.strip()]
            path, kind = self._locate(store, row)
            if kind != "ready":
                path = None
            width = height = raw_width = raw_height = 0
            if path is not None and (row["media_type"] == "photo" or (
                    row["media_type"] == "animated_gif" and path.suffix.lower() == ".gif")):
                from .preview import gif_screen_size, photo_info

                try:
                    signature = file_signature(path)
                    if row["media_type"] == "photo":
                        dimensions = photo_info(path)
                        width, height = dimensions.get("pixel_width", 0), dimensions.get("pixel_height", 0)
                        raw_width, raw_height = dimensions.get("source_width", 0), dimensions.get("source_height", 0)
                    else:
                        width, height = gif_screen_size(path)
                        raw_width, raw_height = width, height
                    if file_signature(path) != signature or self._path(store, row) != path:
                        path = None
                        width = height = raw_width = raw_height = 0
                except OSError:
                    path = None
                    width = height = raw_width = raw_height = 0
            source = f"https://x.com/i/web/status/{row['tweet_id']}"
            meta_parts = []
            if row.get("filename"):
                meta_parts.append(row["filename"])
            posted_str = _posted(row.get("posted_at"), time=True)
            if posted_str:
                meta_parts.append(posted_str)
            if width and height:
                dim_str = f"{width}×{height}"
                if raw_width and raw_height and (raw_width, raw_height) != (width, height):
                    dim_str += f" (원본 {raw_width}×{raw_height})"
                meta_parts.append(dim_str)
            sz = None
            if path is not None:
                try:
                    sz = path.stat().st_size
                except OSError:
                    sz = row.get("filesize")
            else:
                sz = row.get("filesize")
            if sz is not None and sz > 0:
                meta_parts.append(_format_size(sz))
            info = " · ".join(meta_parts)
            if kind == "cloud":
                note = _CLOUD_ONLY
            elif path is None:
                note = absence_note(store.output_dir)
            else:
                note = ""
            is_fav = store.is_favorite(row["folder_mode"], row["tweet_id"], row["num"], row["media_type"])
            tags = store.item_tags(row["folder_mode"], row["tweet_id"], row["num"], row["media_type"])
            return {"key": key, "author": row.get("author") or "",
                    "body": body_text,
                    "source": source, "path": str(path) if path is not None else "", "missing": path is None,
                    "missing_note": note,
                    "preview": QUrl.fromLocalFile(str(path)).toString() if path is not None else "",
                    "media_type": row["media_type"], "public_info": info,
                    "pixel_width": width, "pixel_height": height,
                    "source_width": raw_width, "source_height": raw_height,
                    "favorite": is_fav, "tags": tags, "hashtags": tweet_hashtags}

    def toggle_favorite(self, key, generation):
        request = self.current(generation)
        if request is None:
            return False
        match = _KEY.fullmatch(key) if isinstance(key, str) else None
        if match is None or int(match[1]) != request.generation:
            return False
        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                return store.toggle_favorite(match[2], match[3], int(match[4]), match[5])
        except Exception:
            return False

    def add_tag(self, key, generation, tag):
        request = self.current(generation)
        if request is None:
            return False
        match = _KEY.fullmatch(key) if isinstance(key, str) else None
        if match is None or int(match[1]) != request.generation:
            return False
        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                return store.add_tag(match[2], match[3], int(match[4]), match[5], tag)
        except Exception:
            return False

    def remove_tag(self, key, generation, tag):
        request = self.current(generation)
        if request is None:
            return False
        match = _KEY.fullmatch(key) if isinstance(key, str) else None
        if match is None or int(match[1]) != request.generation:
            return False
        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                return store.remove_tag(match[2], match[3], int(match[4]), match[5], tag)
        except Exception:
            return False

    def decode(self, path, size):
        # Only paths that read() checked against this request's catalog are
        # registered, and the provider rejects a link or a changed resolved
        # path before decoding. Reopening the catalog for every thumbnail
        # cost more than the decode itself.
        if self.current() is None:
            return Thumbnail(None, "")
        return thumbnail(path, size)

    def scan_duplicates(self, generation, progress_callback=None):
        request = self.current(generation)
        if request is None:
            return 0
        from .duplicates import compute_dhash, compute_file_hash, cluster_duplicates
        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                store._folder_cache = {}
                cached = store.get_image_hashes()
                records = (store.photo_records_for_scan()
                           if hasattr(store, "photo_records_for_scan")
                           else store.records(limit=10000, media_type="photo", include_deleted=False))
                total_records = len(records)
                new_hashes = []
                stale_hashes = []
                if progress_callback:
                    progress_callback(0, total_records)
                for idx, rec in enumerate(records):
                    if self.current(generation) is None:
                        return 0
                    if progress_callback and (idx % 25 == 0 or idx == total_records - 1):
                        progress_callback(idx + 1, total_records)
                    ident = (rec["folder_mode"], rec["tweet_id"], rec["num"], rec["media_type"])
                    path = self._path(store, rec)
                    if path is None or not path.is_file():
                        if ident in cached:
                            stale_hashes.append(ident)
                        continue
                    try:
                        sz = path.stat().st_size
                    except OSError:
                        continue
                    if ident in cached and cached[ident][0] == sz and (cached[ident][1] or cached[ident][2]):
                        continue
                    fhash = compute_file_hash(path)
                    dh = compute_dhash(path)
                    if fhash or dh:
                        new_hashes.append((*ident, sz, fhash, dh))
                        if len(new_hashes) >= 100:
                            store.save_image_hashes(new_hashes)
                            new_hashes.clear()
                if stale_hashes:
                    store.delete_image_hashes(stale_hashes)
                if new_hashes:
                    store.save_image_hashes(new_hashes)
                hashes = store.get_image_hashes()
                favs = store.favorite_identities()
                items = [
                    {"key": f"g{generation}_{k[0]}_{k[1]}_{k[2]}_{k[3]}",
                     "ident": k, "filesize": v[0], "file_hash": v[1], "dhash": v[2],
                     "favorite": k in favs}
                    for k, v in hashes.items()
                ]
                clusters = cluster_duplicates(items, max_distance=4)
                cluster_idents = [[x["ident"] for x in group] for group in clusters]
                store.save_clusters(cluster_idents)
                return store.duplicate_counts()
        except Exception:
            return 0

    def duplicate_copy_keys(self, generation):
        request = self.current(generation)
        if request is None:
            return []
        from .duplicates import cluster_duplicates
        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                hashes = store.get_image_hashes()
                favs = store.favorite_identities()
                items = [
                    {"key": f"g{generation}_{k[0]}_{k[1]}_{k[2]}_{k[3]}",
                     "ident": k, "filesize": v[0], "file_hash": v[1], "dhash": v[2],
                     "favorite": k in favs}
                    for k, v in hashes.items()
                ]
                clusters = cluster_duplicates(items, max_distance=4)
                copy_keys = []
                for group in clusters:
                    for duplicate in group[1:]:
                        copy_keys.append(duplicate["key"])
                return copy_keys
        except Exception:
            return []

    def deletion_manifest(self, keys):
        """Capture displayed catalog identities without accepting paths from QML."""
        if (not isinstance(keys, (list, tuple)) or not 1 <= len(keys) <= MAX_SELECTION
                or any(not isinstance(key, str) for key in keys)):
            raise ValueError("휴지통으로 보낼 사진을 1~2,000개 선택해 주세요.")
        keys = list(dict.fromkeys(keys))
        with self._condition:
            request = self._request
            if request is None or any(key not in self._served for key in keys):
                raise ValueError("목록이 바뀌었습니다. 사진을 다시 선택해 주세요.")
            displayed = {key: self._served[key] for key in keys}
        rows = []
        with closing(_ReadOnlyHistory(request.location())) as store:
            store._folder_cache = {}
            for key in keys:
                row = self._row(store, key, request)
                path = self._path(store, row) if row is not None else None
                previous_path, previous_signature = displayed[key]
                if path is None or str(path) != previous_path or previous_signature is None:
                    raise ValueError("선택한 파일을 확인하지 못했습니다. 목록을 새로 고침해 주세요.")
                signature = file_signature(path)
                if signature != previous_signature:
                    raise ValueError("선택한 뒤 파일이 바뀌었습니다. 목록을 새로 고침해 주세요.")
                rows.append(dict(row, _delete_signature=signature))
        with self._condition:
            if self._request is not request:
                raise ValueError("목록이 바뀌었습니다. 사진을 다시 선택해 주세요.")
        return {"location": request.location(), "rows": rows, "generation": request.generation}

    def selectable_keys(self, keys):
        """Use the displayed file snapshot to exclude missing items from selection.

        Deletion still rechecks the file and its original signature before
        confirmation. This lookup performs no disk access on the UI thread.
        """
        with self._condition:
            selectable = []
            unavailable = 0
            overflow = False
            for key in keys:
                item = self._served.get(key)
                if item is None or not item[0] or item[1] is None:
                    unavailable += 1
                elif len(selectable) < MAX_SELECTION:
                    selectable.append(key)
                else:
                    overflow = True
            return selectable, unavailable, overflow

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
            # The comma is explorer's own separator. A folder such as
            # "My, Photos" has to stay inside one quoted /select argument.
            from .qt_browser_setup import explorer_select_command
            command = explorer_select_command(selection["path"])
            if command is None:
                return "폴더를 열지 못했습니다."
            subprocess.Popen(command, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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
    selectionChanged = Signal()
    # Page loads also emit changed. The author list and the chosen author are
    # not part of a page, so the combo must not rebuild while the grid scrolls.
    authorsChanged = Signal()
    authorFilterChanged = Signal()
    hashtagsChanged = Signal()
    hashtagFilterChanged = Signal()
    userTagsChanged = Signal()
    userTagFilterChanged = Signal()
    favoriteFilterChanged = Signal()
    duplicateFilterChanged = Signal()
    duplicateCountChanged = Signal()
    duplicateScanProgress = Signal(int, int)
    statisticsLoaded = Signal(object)
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
        self._authors = []
        self._authors_loaded = False
        self._hashtag = ""
        self._hashtags = []
        self._hashtags_loaded = False
        self._user_tag = ""
        self._user_tags = []
        self._user_tags_loaded = False
        self._favorite_only = False
        self._duplicate_only = False
        self._duplicate_count = 0
        self._dup_current = 0
        self._dup_total = 0
        self._date_field = "posted"
        self._date_from = self._date_to = ""
        self._selection_mode = False
        self._selection_keys = set()
        self._selected_keys_order = []
        self._selection_revision = 0
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

    @Property(bool, notify=selectionChanged)
    def selectionMode(self):
        return self._selection_mode

    @Property("QVariantList", notify=selectionChanged)
    def selectedKeys(self):
        return self._selected_keys_order.copy()

    @Property(int, notify=selectionChanged)
    def selectionRevision(self):
        return self._selection_revision

    @Slot(str, result=bool)
    def isSelected(self, key):
        return isinstance(key, str) and key in self._selection_keys

    @Property(int, notify=selectionChanged)
    def selectedCount(self):
        return len(self._selection_keys)

    @Property(str, notify=authorFilterChanged)
    def filterAuthor(self):
        return self._author

    @Property("QVariantList", notify=authorsChanged)
    def authors(self):
        return self._authors.copy()

    @Property(str, notify=hashtagFilterChanged)
    def filterHashtag(self):
        return self._hashtag

    @Property("QVariantList", notify=hashtagsChanged)
    def hashtags(self):
        return self._hashtags.copy()

    @Property(str, notify=userTagFilterChanged)
    def filterUserTag(self):
        return self._user_tag

    @Property("QVariantList", notify=userTagsChanged)
    def userTags(self):
        return self._user_tags.copy()

    @Property(bool, notify=favoriteFilterChanged)
    def filterFavoriteOnly(self):
        return self._favorite_only

    @Property(bool, notify=duplicateFilterChanged)
    def filterDuplicateOnly(self):
        return self._duplicate_only

    @Property(int, notify=duplicateCountChanged)
    def duplicateCount(self):
        return self._duplicate_count

    @Property(int, notify=duplicateScanProgress)
    def duplicateProgressCurrent(self):
        return self._dup_current

    @Property(int, notify=duplicateScanProgress)
    def duplicateProgressTotal(self):
        return self._dup_total

    @Property(bool, notify=changed)
    def selectedIsFavorite(self):
        return bool(self._selected.get("favorite", False))

    @Property("QVariantList", notify=changed)
    def selectedTags(self):
        return self._selected.get("tags", []).copy()

    @Property("QVariantList", notify=changed)
    def selectedHashtags(self):
        return self._selected.get("hashtags", []).copy()

    @Property(str, notify=changed)
    def dateField(self):
        return self._date_field

    @Property(str, notify=changed)
    def dateFrom(self):
        return self._date_from

    @Property(str, notify=changed)
    def dateTo(self):
        return self._date_to

    @Property(str, notify=changed)
    def filterSummary(self):
        parts = []
        if self._query:
            parts.append("검색: " + self._query)
        if self._author:
            parts.append("작성자 @" + self._author)
        if self._hashtag:
            parts.append("태그 " + (self._hashtag if self._hashtag.startswith("#") else "#" + self._hashtag))
        if self._user_tag:
            parts.append("내 태그 #" + self._user_tag)
        if self._favorite_only:
            parts.append("★ 즐겨찾기")
        if self._duplicate_only:
            parts.append("중복 사진")
        if self._date_from or self._date_to:
            parts.append(("게시일 " if self._date_field == "posted" else "저장일 ")
                         + (self._date_from or "처음") + " ~ " + (self._date_to or "끝"))
        return " · ".join(parts)

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
        self._authors = []
        self._authors_loaded = False
        self.authorsChanged.emit()
        self._hashtags = []
        self._hashtags_loaded = False
        self.hashtagsChanged.emit()
        self._user_tags = []
        self._user_tags_loaded = False
        self.userTagsChanged.emit()
        self._duplicate_count = 0
        self.duplicateCountChanged.emit()
        self._refresh(reload_authors=True)

    @Slot()
    def refresh(self):
        self._refresh(reload_authors=True)

    def _refresh(self, *, reload_authors=True, status=""):
        if self._closed:
            return
        self._generation += 1
        self._selection_serial += 1
        self._selected = {}
        self._selection_key = ""
        self._navigation = None
        self.clearSelection()
        self._status = status
        self._busy = False
        with self._condition:
            self._pending.clear()
        request = None
        if self._account and self._output_dir:
            request = _Request(self._generation, self._account, self._output_dir, self._separate_accounts,
                               self._query, self._media_type, self._order, self._author,
                               self._date_field, self._date_from, self._date_to, self._hashtag,
                               self._favorite_only, self._user_tag, self._duplicate_only)
        else:
            self._authors = []
            self._authors_loaded = False
            self.authorsChanged.emit()
            self._hashtags = []
            self._hashtags_loaded = False
            self.hashtagsChanged.emit()
            self._user_tags = []
            self._user_tags_loaded = False
            self.userTagsChanged.emit()
        self._repository.configure(request)
        if request is not None and (reload_authors or not self._authors_loaded or not self._hashtags_loaded or not self._user_tags_loaded):
            with self._condition:
                self._pending.append(("authors", self._generation, self._selection_serial, ""))
                self._pending.append(("hashtags", self._generation, self._selection_serial, ""))
                self._pending.append(("user_tags", self._generation, self._selection_serial, ""))
                self._condition.notify()
        self.provider.clear_registry()
        self.model.search(str(self._generation))
        self.changed.emit()

    @Slot(str)
    def search(self, query):
        query = query[:500]
        if query != self._query:
            self._query = query
            self._refresh(reload_authors=False)

    @Slot(str, str, str)
    def setFilters(self, media_type, order, author):
        author = author.strip().lstrip("@").lower() if isinstance(author, str) else None
        if (media_type not in _KINDS or order not in _ORDERS or author is None
                or (author and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", author))):
            self._status = "필터를 확인해 주세요."
            self.changed.emit()
            return
        if (media_type, order, author) == (self._media_type, self._order, self._author):
            return
        author_changed = author != self._author
        self._media_type, self._order, self._author = media_type, order, author
        if author_changed:
            self.authorFilterChanged.emit()
        self._refresh(reload_authors=False)

    @Slot(str, str, str, str, str, result=bool)
    @Slot(str, str, str, str, result=bool)
    def setAdvancedFilters(self, author, date_field, first, last, hashtag=None):
        if self._closed:
            return False
        author = author.strip().lstrip("@").lower()
        first, last = first.strip(), last.strip()
        if hashtag is not None:
            hashtag = hashtag.strip() if isinstance(hashtag, str) else ""
            if hashtag and not hashtag.startswith("#"):
                hashtag = "#" + hashtag
        try:
            if date_field not in {"posted", "saved"} or (author and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", author)):
                raise ValueError
            for value in (first, last):
                if value and (not re.fullmatch(r"\d{4}-\d\d-\d\d", value) or date.fromisoformat(value).isoformat() != value):
                    raise ValueError
            if first and last and first > last:
                raise ValueError
            if last:
                date.fromisoformat(last) + timedelta(days=1)
        except (ValueError, OverflowError):
            self._status = "작성자 ID와 날짜(YYYY-MM-DD), 시작·종료 순서를 확인해 주세요."
            self.changed.emit()
            return False
        # The basis only matters while a date is set; picking it first must not reload the grid.
        results_change = (author != self._author or first != self._date_from or last != self._date_to
                          or (bool(first or last) and date_field != self._date_field)
                          or (hashtag is not None and hashtag != self._hashtag))
        author_changed = author != self._author
        hashtag_changed = hashtag is not None and hashtag != self._hashtag
        self._author, self._date_field, self._date_from, self._date_to = author, date_field, first, last
        if hashtag is not None:
            self._hashtag = hashtag
        if author_changed:
            self.authorFilterChanged.emit()
        if hashtag_changed:
            self.hashtagFilterChanged.emit()
        if results_change:
            self._refresh(reload_authors=False)
        else:
            self.changed.emit()
        return True

    @Slot()
    def resetFilters(self):
        author_changed = self._author != ""
        hashtag_changed = self._hashtag != ""
        fav_changed = self._favorite_only
        dup_changed = self._duplicate_only
        tag_changed = self._user_tag != ""
        self._query = self._author = self._hashtag = self._date_from = self._date_to = self._user_tag = ""
        self._favorite_only = False
        self._duplicate_only = False
        if author_changed:
            self.authorFilterChanged.emit()
        if hashtag_changed:
            self.hashtagFilterChanged.emit()
        if tag_changed:
            self.userTagFilterChanged.emit()
        if fav_changed:
            self.favoriteFilterChanged.emit()
        if dup_changed:
            self.duplicateFilterChanged.emit()
        self._date_field = "posted"
        self._media_type, self._order = "all", "saved"
        self._refresh(reload_authors=False)

    @Slot(str)
    def setUserTag(self, tag):
        tag = tag.strip().lstrip("#") if isinstance(tag, str) else ""
        if tag == self._user_tag:
            return
        self._user_tag = tag
        self.userTagFilterChanged.emit()
        self._refresh(reload_authors=False)

    @Slot(str, result=int)
    def addTagToSelection(self, tag):
        if self._closed or not tag:
            return 0
        clean = tag.strip().lstrip("#")
        if not clean:
            return 0
        count = 0
        for key in list(self._selection_keys):
            if self._repository.add_tag(key, self._generation, clean):
                count += 1
        if count:
            if clean not in self._user_tags:
                self._user_tags.append(clean)
                self._user_tags.sort()
                self.userTagsChanged.emit()
            self._user_tags_loaded = False
            self._refresh(reload_authors=False, status=f"선택한 {count:,}개 사진에 #{clean} 태그를 추가했습니다.")
        return count

    @Slot()
    def requestStatistics(self):
        if self._closed:
            return
        with self._condition:
            self._pending.append(("statistics", self._generation, self._selection_serial, ""))
            self._condition.notify()

    @Slot(bool)
    def setFavoriteOnly(self, enabled):
        if self._closed or self._favorite_only == bool(enabled):
            return
        self._favorite_only = bool(enabled)
        self.favoriteFilterChanged.emit()
        self._refresh(reload_authors=False)

    @Slot(bool)
    def setDuplicateOnly(self, enabled):
        if self._closed or self._duplicate_only == bool(enabled):
            return
        self._duplicate_only = bool(enabled)
        self.duplicateFilterChanged.emit()
        self._refresh(reload_authors=False)

    @Slot()
    def scanDuplicates(self):
        if self._closed or self._busy:
            return
        self._busy = True
        self._status = "중복 이미지를 검사하는 중…"
        with self._condition:
            self._pending.append(("scan_duplicates", self._generation, self._selection_serial, ""))
            self._condition.notify()
        self.changed.emit()

    @Slot(result=int)
    def selectDuplicateCopies(self):
        if self._closed:
            return 0
        copy_keys = self._repository.duplicate_copy_keys(self._generation)
        if not copy_keys:
            self._status = "선택할 중복 사본이 없습니다."
            self.changed.emit()
            return 0
        if self.model._rows:
            loaded_copies = [k for k in copy_keys if k in self.model._keys]
            selectable, _, _ = self._repository.selectable_keys(loaded_copies)
            if not selectable:
                self._status = "현재 목록에 불러온 중복 사본이 없습니다."
                self.changed.emit()
                return 0
            copy_keys = selectable
        self._selection_mode = True
        self._selection_keys.update(copy_keys)
        self._batch_selection_changed()
        self._status = f"중복 사본 {len(copy_keys):,}개를 선택했습니다."
        self.changed.emit()
        return len(copy_keys)

    @Slot(result=bool)
    def toggleSelectedFavorite(self):
        if self._closed or not self.selectedKey:
            return False
        key = self.selectedKey
        new_state = self._repository.toggle_favorite(key, self._generation)
        self._selected["favorite"] = new_state
        self.model.setFavorite(key, new_state)
        self.changed.emit()
        return new_state

    @Slot(str, result=bool)
    def toggleFavorite(self, key):
        if self._closed or not isinstance(key, str) or not key:
            return False
        new_state = self._repository.toggle_favorite(key, self._generation)
        if key == self.selectedKey:
            self._selected["favorite"] = new_state
            self.changed.emit()
        self.model.setFavorite(key, new_state)
        return new_state

    @Slot(str, result=bool)
    def addSelectedTag(self, tag):
        if self._closed or not self.selectedKey or not tag:
            return False
        clean = tag.strip().lstrip("#")
        if not clean:
            return False
        ok = self._repository.add_tag(self.selectedKey, self._generation, clean)
        if ok:
            tags = list(self._selected.get("tags", []))
            tags.append({"tag": clean, "source": "user"})
            self._selected["tags"] = tags
            if clean not in self._user_tags:
                self._user_tags.append(clean)
                self._user_tags.sort()
                self.userTagsChanged.emit()
            self.changed.emit()
        return ok

    @Slot(str, result=bool)
    def removeSelectedTag(self, tag):
        if self._closed or not self.selectedKey or not tag:
            return False
        clean = tag.strip().lstrip("#")
        if not clean:
            return False
        ok = self._repository.remove_tag(self.selectedKey, self._generation, clean)
        if ok:
            tags = [t for t in self._selected.get("tags", []) if t.get("tag") != clean]
            self._selected["tags"] = tags
            self._user_tags_loaded = False
            with self._condition:
                self._pending.append(("user_tags", self._generation, self._selection_serial, ""))
                self._condition.notify()
            self.changed.emit()
        return ok

    @Slot(bool)
    def setSelectionMode(self, enabled):
        if self._closed or self._selection_mode == enabled:
            return
        self._selection_mode = enabled
        if not enabled:
            self._selection_keys.clear()
            if self._status:
                self._status = ""
                self.changed.emit()
        self._batch_selection_changed()

    @Slot(str)
    def toggleSelection(self, key):
        if self._closed or key not in self.model._keys:
            return
        if key in self._selection_keys:
            self._selection_keys.remove(key)
        elif not self.canSelect(key):
            self._status = "파일이 없거나 확인할 수 없는 항목은 휴지통으로 이동할 수 없습니다."
            self.changed.emit()
            return
        elif len(self._selection_keys) < MAX_SELECTION:
            self._selection_keys.add(key)
        else:
            self._status = "한 번에 최대 2,000개를 선택할 수 있습니다."
            self.changed.emit()
            return
        if self._status:
            self._status = ""
            self.changed.emit()
        self._selection_mode = True
        self._batch_selection_changed()

    @Slot()
    def selectLoaded(self):
        if self._closed:
            return
        selectable, unavailable, overflow = self._repository.selectable_keys(row["key"] for row in self.model._rows)
        self._selection_keys = set(selectable)
        self._selection_mode = True
        self._batch_selection_changed()
        if overflow or unavailable:
            self._status = (f"확인된 파일 {len(self._selection_keys):,}개를 선택했습니다. " +
                            (f"파일 없음·확인 불가 {unavailable:,}개는 제외했습니다. " if unavailable else "") +
                            ("한 번에 최대 2,000개를 선택할 수 있습니다." if overflow else "")).strip()
        else:
            self._status = ""
        self.changed.emit()

    @Slot(str, result=bool)
    def canSelect(self, key):
        if self._closed or key not in self.model._keys:
            return False
        selectable, _, _ = self._repository.selectable_keys((key,))
        return bool(selectable)

    @Slot()
    def clearSelection(self):
        if self._selection_keys:
            self._selection_keys.clear()
            self._batch_selection_changed()
            if self._status:
                self._status = ""
                self.changed.emit()

    def _batch_selection_changed(self):
        self._selection_revision += 1
        self._selected_keys_order = sorted(self._selection_keys, key=lambda key: self.model._positions.get(key, -1))
        self.selectionChanged.emit()

    def deletionManifest(self, keys):
        # Called on the management worker; filesystem checks never run in QML.
        return self._repository.deletion_manifest(keys)

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
        # Drop the open file before the catalog read. Keeping it made the video
        # player stay in PlayingState after the next item was a photo or GIF.
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
                if action == "authors":
                    reader = getattr(self._repository, "authors", None)
                    selection = reader(generation) if callable(reader) else []
                elif action == "hashtags":
                    reader = getattr(self._repository, "hashtags", None)
                    selection = reader(generation) if callable(reader) else []
                elif action == "user_tags":
                    reader = getattr(self._repository, "user_tags", None)
                    selection = reader(generation) if callable(reader) else []
                elif action == "statistics":
                    reader = getattr(self._repository, "overview", None)
                    selection = reader(generation) if callable(reader) else {}
                elif action == "scan_duplicates":
                    def on_progress(cur, tot):
                        self._delivered.emit((generation, serial, "scan_progress", (cur, tot), f"중복 검사 중… ({cur:,} / {tot:,})"))
                    count = self._repository.scan_duplicates(generation, progress_callback=on_progress)
                    selection = count
                    message = f"중복·유사 사진 {count:,}개를 발견했습니다." if count else "중복·유사 사진이 없습니다."
                elif action == "select":
                    selection = self._repository.selected(key, generation)
                    if selection is None:
                        message = "사진을 다시 선택해 주세요."
                else:
                    message = self._repository.action(key, generation, action)
            except Exception:
                if action in {"authors", "hashtags", "user_tags"}:
                    selection = []
                elif action == "statistics":
                    selection = {}
                else:
                    message = "사진 정보를 읽지 못했습니다." if action == "select" else "열지 못했습니다. 파일과 기본 앱을 확인해 주세요."
            self._delivered.emit((generation, serial, action, selection, message))

    @Slot(object)
    def _accept(self, result):
        generation, serial, action, selection, message = result
        if self._closed or generation != self._generation:
            return
        if action == "scan_progress":
            curr, tot = selection
            self._dup_current = curr
            self._dup_total = tot
            self._status = message
            self.duplicateScanProgress.emit(curr, tot)
            self.changed.emit()
            return
        if action == "authors":
            self._authors = list(selection) if isinstance(selection, (list, tuple)) else []
            self._authors_loaded = True
            self.authorsChanged.emit()
            self.changed.emit()
            return
        if action == "hashtags":
            self._hashtags = list(selection) if isinstance(selection, (list, tuple)) else []
            self._hashtags_loaded = True
            self.hashtagsChanged.emit()
            self.changed.emit()
            return
        if action == "user_tags":
            self._user_tags = list(selection) if isinstance(selection, (list, tuple)) else []
            self._user_tags_loaded = True
            self.userTagsChanged.emit()
            self.changed.emit()
            return
        if action == "statistics":
            self.statisticsLoaded.emit(selection if isinstance(selection, dict) else {})
            return
        if action == "scan_duplicates":
            self._duplicate_count = int(selection) if isinstance(selection, int) else 0
            self._dup_current = 0
            self._dup_total = 0
            self.duplicateCountChanged.emit()
            self.duplicateScanProgress.emit(0, 0)
            self._busy = False
            self._refresh(reload_authors=False, status=message)
            return
        if serial != self._selection_serial:
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
