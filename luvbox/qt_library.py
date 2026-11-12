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
from threading import Condition, Event, Thread
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
_EXCLUSIVE_ACTIONS = {"scan_duplicates", "scan_ai_tags", "clear_ai_tags", "teach"}
_EXCLUSIVE_KIND = {
    "scan_duplicates": "duplicates", "scan_ai_tags": "ai", "clear_ai_tags": "clear", "teach": "teach",
}
_EXCLUSIVE_BUSY = {
    "ai": "AI 자동분류가 끝난 뒤 다시 실행할 수 있습니다.",
    "duplicates": "중복 검사가 끝난 뒤 다시 실행할 수 있습니다.",
    "clear": "AI 태그 초기화가 끝난 뒤 다시 실행할 수 있습니다.",
    "teach": "분류를 반영한 뒤 다시 실행할 수 있습니다.",
}
_HOLD_REASON = {
    "ai": "AI 자동분류 중에는 태그 수정과 휴지통 이동을 잠시 멈춥니다. 갤러리 보기, 즐겨찾기, 저장은 계속할 수 있습니다.",
    "clear": "AI 태그 초기화 중에는 태그 수정과 휴지통 이동을 잠시 멈춥니다. 끝나면 다시 할 수 있습니다.",
    "duplicates": "중복 검사 중에는 휴지통 이동과 사본 선택을 잠시 멈춥니다. 갤러리 보기와 즐겨찾기는 계속할 수 있습니다.",
    "teach": "분류를 반영하는 중에는 태그 수정과 휴지통 이동을 잠시 멈춥니다.",
}
_CATALOG_MOVED = "저장 폴더가 바뀌어 작업을 멈췄습니다. 이미 처리한 내용은 유지됩니다."
_TAG_LOCKED = "AI 작업이 끝난 뒤 태그를 바꿀 수 있습니다. 분류나 초기화가 끝나면 다시 시도해 주세요."
_FAVORITE_ERROR = "즐겨찾기를 저장하지 못했습니다. 잠시 뒤 다시 시도해 주세요."
_STATISTICS_ERROR = "통계를 불러오지 못했습니다. 저장 폴더가 연결되어 있는지 확인한 뒤 다시 시도해 주세요."


def _thumbnail_workers():
    """Thumbnail decoders for the gallery: two, or up to four on a PC with cores to spare.

    Decoding releases the interpreter lock, so a screen of tiles fills almost
    in proportion. Half the logical cores are left for the UI, playback and a
    running save.
    """
    return max(2, min(4, (os.cpu_count() or 2) // 2))


def _posted(value, *, time=False):
    """'2026-10-01' (or with its Korean-time hour) from a stored post time."""
    match = _POSTED.match(value) if isinstance(value, str) else None
    if match is None:
        return ""
    return f"{match[1]} {match[2]}" if time else match[1]


def _feedback_matrices(embeddings: dict, feedback: dict) -> dict:
    """Group normalized embeddings of photos the user has already taught."""
    import numpy as np

    buckets: dict[str, list] = {}
    for ident, category in feedback.items():
        stored = embeddings.get(ident)
        if not stored:
            continue
        vector = np.frombuffer(stored[0], dtype=np.float32)
        if vector.shape != (512,):
            continue
        buckets.setdefault(category or "", []).append(np.ascontiguousarray(vector))
    return {name: np.stack(rows) for name, rows in buckets.items() if rows}


def _choose_label(ident, tag, vector, feedback, estimated, groups):
    """Return the label to store, or None when the current label should stay.

    An empty string is a real result: the photo matches none of the five categories.
    """
    if ident in feedback:
        return None
    from .clip_classifier import neighbor_decision
    decision = neighbor_decision(vector, groups) if vector is not None else None
    chosen = decision[0] if decision is not None else (tag or "")
    if ident in estimated and decision is None:
        return None
    if estimated.get(ident) == chosen:
        return None
    return chosen


def _store_labels(store, label_rows) -> None:
    if not label_rows:
        return
    if hasattr(store, "replace_estimated_labels"):
        store.replace_estimated_labels(label_rows)
    else:
        store.save_ai_tags([(row[0], row[1], row[2], row[3], row[4]) for row in label_rows])


def _propagate_neighbors(store) -> int:
    """Relabel photos that are close to a taught example. Returns how many changed."""
    from .clip_classifier import neighbor_decisions
    import numpy as np

    if not hasattr(store, "learning_state"):
        return 0
    if hasattr(store, "feedback_count") and store.feedback_count() == 0:
        return 0
    embeddings, feedback, estimated = store.learning_state()
    groups = _feedback_matrices(embeddings, feedback)
    if not groups:
        return 0
    idents = []
    vectors = []
    for ident, (blob, _confidence) in embeddings.items():
        if ident in feedback:
            continue
        vector = np.frombuffer(blob, dtype=np.float32)
        if vector.shape != (512,):
            continue
        idents.append(ident)
        vectors.append(np.ascontiguousarray(vector))
    if not vectors or not hasattr(store, "replace_estimated_labels"):
        return 0
    updates = []
    for start in range(0, len(vectors), 512):
        decisions = neighbor_decisions(np.stack(vectors[start:start + 512]), groups)
        for ident, decision in zip(idents[start:start + 512], decisions):
            if decision is None or estimated.get(ident) == decision[0]:
                continue
            updates.append((*ident, decision[0]))
    if not updates:
        return 0
    return store.replace_estimated_labels(updates)


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
    ai_category: str = ""
    ai_origin: str = ""

    def location(self):
        return resolve_storage(self.output_dir, self.account, self.separate_accounts)


class LibraryRepository:
    def __init__(self):
        self._condition = Condition()
        self._request = None
        self._epoch = 0
        self._served = {}
        self._cursor = None
        self._cursor_offset = 0
        self._scan_note = ""
        self._duplicate_cancel = Event()
        self._ai_cancel = Event()
        self.provider = None

    def configure(self, request):
        with self._condition:
            if self._catalog_moved(self._request, request):
                self._epoch += 1
            self._request = request
            self._served.clear()
            self._cursor = None
            self._cursor_offset = 0

    @staticmethod
    def _catalog_moved(previous, request):
        if previous is None or request is None:
            return True
        return (previous.account != request.account or previous.output_dir != request.output_dir
                or previous.separate_accounts != request.separate_accounts)

    def catalog_epoch(self):
        with self._condition:
            return self._epoch

    def same_catalog(self, epoch):
        with self._condition:
            return self._request is not None and self._epoch == epoch

    def current(self, generation=None):
        with self._condition:
            request = self._request
            return request if request is not None and (generation is None or request.generation == generation) else None

    def _catalog_request(self, generation, epoch=None):
        """The open catalog. An epoch keeps a folder-wide job across filter reloads."""
        if epoch is None:
            return self.current(generation)
        if not self.same_catalog(epoch):
            return None
        return self.current()

    def cancel_ai_scan(self):
        self._ai_cancel.set()

    def cancel_duplicate_scan(self):
        self._duplicate_cancel.set()

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
                grouped = request.duplicate_only and hasattr(store, "duplicate_page")
                after = self._cursor if offset and offset == self._cursor_offset and not grouped else None
                if grouped:
                    # Each group together, the photo to keep first. The chosen sort order does not apply.
                    page = store.duplicate_page(offset // limit, limit, request.media_type, request.query,
                                                period=period, author=request.author, hashtag=request.hashtag,
                                                favorite_only=request.favorite_only, user_tag=request.user_tag,
                                                ai_category=getattr(request, "ai_category", ""),
                                                ai_origin=getattr(request, "ai_origin", ""))
                else:
                    page = store.record_page(0 if after is not None else offset // limit, limit,
                                             request.media_type, request.query,
                                             order=request.order, author=request.author, include_deleted=False,
                                             period=period, after=after, hashtag=request.hashtag,
                                             favorite_only=request.favorite_only, user_tag=request.user_tag,
                                             duplicate_only=request.duplicate_only,
                                             ai_category=getattr(request, "ai_category", ""),
                                             ai_origin=getattr(request, "ai_origin", ""))
                idents = [
                    (row["folder_mode"], row["tweet_id"], row["num"], row["media_type"])
                    for row in page["rows"]
                ]
                favs = store.favorite_identities_among(idents) if idents else set()
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
                    entry = {"key": key, "caption": caption, "missing": kind != "ready", "favorite": is_fav}
                    if grouped:
                        size = stamp[2] if stamp is not None else row.get("filesize")
                        entry.update(group=int(row.get("cluster_id") or 0), keeper=row.get("cluster_rank") == 1,
                                     group_size=int(row.get("cluster_size") or 0), detail=_format_size(size))
                    rows.append(entry)
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
                    if last is not None and not grouped:
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

    def duplicate_counts(self, generation=None):
        request = self.current(generation)
        if request is None:
            return 0
        try:
            with closing(_ReadOnlyHistory(request.location())) as store:
                stale = (hasattr(store, "duplicate_rule") and store.duplicate_rule() == ""
                         and store.has_duplicate_groups())
                if not stale:
                    return store.duplicate_counts() if hasattr(store, "duplicate_counts") else 0
            # 1.6.x grouped photos that only looked alike. Keep the identical files and drop the rest.
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                return store.regroup_exact_duplicates()
        except Exception:
            return 0

    def ai_counts(self, generation=None):
        """Photos per AI category, in the fixed category order, zero included."""
        from .clip_classifier import CATEGORY_NAMES
        request = self.current(generation)
        if request is None:
            return []
        try:
            with closing(_ReadOnlyHistory(request.location())) as store:
                found = store.ai_tag_stats() if hasattr(store, "ai_tag_stats") else {}
        except Exception:
            found = {}
        return [{"category": name, "count": int(found.get(name, 0))} for name in CATEGORY_NAMES]

    def overview(self, generation=None):
        """Numbers for the statistics window.

        A library with no records yet is empty, not an error. Any other
        failure is raised so the window can say so instead of waiting forever.
        """
        request = self.current(generation)
        if request is None:
            return None
        try:
            with closing(_ReadOnlyHistory(request.location())) as store:
                ov = store.overview(verify_files=False, top=10, include_missing=False)
                st = ov.get("stats", {})
                ai_stats = store.ai_tag_stats() if hasattr(store, "ai_tag_stats") else {}
                user_tag_list = store.user_tags(limit=15) if hasattr(store, "user_tags") else []
                dup_count = store.duplicate_counts() if hasattr(store, "duplicate_counts") else 0
                return {
                    "files": int(ov.get("files", 0)),
                    # Tweets that still have a file here, like the other cards.
                    "posts": int(ov.get("current_posts", ov.get("posts", 0))),
                    "bytes": int(ov.get("bytes", 0)),
                    "formatted_bytes": _format_size(ov.get("bytes", 0)) or "0 B",
                    "photo": int(ov.get("photo", 0)),
                    "video": int(ov.get("video", 0)),
                    "animated_gif": int(ov.get("animated_gif", 0)),
                    "duplicates": int(dup_count),
                    "authors": [{"author": str(row.get("author") or ""), "files": int(row.get("files") or 0)}
                                for row in st.get("authors", [])],
                    "monthly": [{"month": m[0], "count": m[1]} for m in st.get("months", {}).get("posted", [])],
                    "ai_categories": [{"category": k, "count": v} for k, v in ai_stats.items()],
                    "user_tags": user_tag_list,
                }
        except FileNotFoundError:
            return {"files": 0, "posts": 0, "bytes": 0, "formatted_bytes": "0 B", "photo": 0, "video": 0,
                    "animated_gif": 0, "duplicates": 0, "authors": [], "monthly": [], "ai_categories": [],
                    "user_tags": []}

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
            learned = (store.classification_of(row["folder_mode"], row["tweet_id"], row["num"], row["media_type"])
                       if hasattr(store, "classification_of") else {})
            confidence = learned.get("confidence")
            return {"key": key, "author": row.get("author") or "",
                    "body": body_text,
                    "source": source, "path": str(path) if path is not None else "", "missing": path is None,
                    "missing_note": note,
                    "preview": QUrl.fromLocalFile(str(path)).toString() if path is not None else "",
                    "media_type": row["media_type"], "public_info": info,
                    "pixel_width": width, "pixel_height": height,
                    "source_width": raw_width, "source_height": raw_height,
                    "favorite": is_fav, "tags": tags, "hashtags": tweet_hashtags,
                    "ai_category": learned.get("category") or "",
                    "ai_confirmed": bool(learned.get("confirmed")),
                    "ai_confidence": float(confidence) if isinstance(confidence, (int, float)) else -1.0}

    def toggle_favorite(self, key, generation):
        """Return the committed state, or None when no change could be saved."""
        request = self.current(generation)
        if request is None:
            return None
        match = _KEY.fullmatch(key) if isinstance(key, str) else None
        if match is None or int(match[1]) != request.generation:
            return None
        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                return store.toggle_favorite(match[2], match[3], int(match[4]), match[5])
        except Exception:
            return None

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

    def add_tag_batch(self, keys, generation, tag):
        request = self.current(generation)
        if request is None or not keys:
            return 0
        identities = []
        for key in keys:
            match = _KEY.fullmatch(key) if isinstance(key, str) else None
            if match is not None and int(match[1]) == request.generation:
                identities.append((match[2], match[3], int(match[4]), match[5]))
        if not identities:
            return 0
        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                if hasattr(store, "add_tags_batch"):
                    return store.add_tags_batch(identities, tag)
                count = 0
                for ident in identities:
                    if store.add_tag(ident[0], ident[1], ident[2], ident[3], tag):
                        count += 1
                return count
        except Exception:
            return 0

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

    def scan_duplicates(self, generation, progress_callback=None, epoch=None, near=False, compare_callback=None):
        """Find duplicate photos and store the groups.

        Identical files always count. With ``near`` a photo saved again at
        another size or quality counts too, after its pixels are compared.
        ``compare_callback(done, total)`` reports those comparisons.
        """
        request = self._catalog_request(generation, epoch)
        if request is None:
            return 0
        from .duplicates import compute_image_hashes, cluster_images, image_signature, ScanCancelled

        def check_cancel():
            if self._duplicate_cancel.is_set():
                raise ScanCancelled("중복 검사를 중단했습니다. 다시 검사하면 확인한 파일의 해시를 재사용합니다.")
            if self._catalog_request(generation, epoch) is None:
                raise ScanCancelled(_CATALOG_MOVED)

        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                store._folder_cache = {}
                cached = store.get_image_hashes(include_signatures=True)
                records = (store.photo_records_for_scan()
                           if hasattr(store, "photo_records_for_scan")
                           else store.records(limit=10000, media_type="photo", include_deleted=False))
                total_records = len(records)
                new_hashes = []
                stale_hashes = []
                active_hashes = {}
                active_paths = {}
                if progress_callback:
                    progress_callback(0, total_records)
                for idx, rec in enumerate(records):
                    check_cancel()
                    if progress_callback and idx % 25 == 0:
                        progress_callback(idx, total_records)
                    ident = (rec["folder_mode"], rec["tweet_id"], rec["num"], rec["media_type"])
                    path = self._path(store, rec)
                    if path is None or not path.is_file():
                        if ident in cached:
                            stale_hashes.append(ident)
                        continue
                    try:
                        signature = image_signature(path)
                        sz = int(signature.split(":")[2])
                    except OSError:
                        if ident in cached:
                            stale_hashes.append(ident)
                        continue
                    active_paths[ident] = path
                    if ident in cached and cached[ident][3] == signature and (cached[ident][1] or cached[ident][2]):
                        active_hashes[ident] = cached[ident]
                        continue
                    fhash, dh = compute_image_hashes(path)
                    check_cancel()
                    try:
                        unchanged = image_signature(path) == signature
                    except OSError:
                        unchanged = False
                    if not unchanged or not (fhash or dh):
                        stale_hashes.append(ident)
                        continue
                    active_hashes[ident] = (sz, fhash, dh)
                    new_hashes.append((*ident, sz, fhash, dh, signature))
                    if len(new_hashes) >= 100:
                        store.save_image_hashes(new_hashes)
                        new_hashes.clear()
                check_cancel()
                if stale_hashes:
                    store.delete_image_hashes(stale_hashes)
                if new_hashes:
                    store.save_image_hashes(new_hashes)
                favs = store.favorite_identities()
                items = [
                    {"key": f"g{generation}_{k[0]}_{k[1]}_{k[2]}_{k[3]}",
                     "ident": k, "filesize": v[0], "file_hash": v[1], "dhash": v[2],
                     "favorite": k in favs, "path": active_paths.get(k)}
                    for k, v in active_hashes.items()
                ]
                if progress_callback:
                    progress_callback(total_records, total_records)
                clusters = cluster_images(items, near=bool(near), check_cancel=check_cancel,
                                          progress=compare_callback)
                cluster_idents = [[x["ident"] for x in group] for group in clusters]
                check_cancel()
                store.save_clusters(cluster_idents)
                if hasattr(store, "set_duplicate_rule"):
                    store.set_duplicate_rule("near" if near else "exact")
                return store.duplicate_counts()
        except ScanCancelled:
            raise
        except Exception as exc:
            raise RuntimeError(
                "중복 검사를 끝내지 못했습니다. 저장 폴더를 확인한 뒤 다시 시도해 주세요."
            ) from exc

    def scan_ai_tags(self, generation, progress_callback=None, status_callback=None, epoch=None):
        self._scan_note = ""
        request = self._catalog_request(generation, epoch)
        if request is None:
            return 0
        from .duplicates import ScanCancelled
        from .clip_classifier import (
            categories_from_embeddings, classify_detailed, encode_skip_signature, ensure_model, get_session,
            is_available, release_session,
        )
        import numpy as np

        saved_count = 0

        def check_cancel():
            if self._ai_cancel.is_set():
                kept = f" 이미 분류된 {saved_count:,}장은 유지됩니다." if saved_count else ""
                raise ScanCancelled(f"AI 자동분류를 중단했습니다.{kept}")
            if self._catalog_request(generation, epoch) is None:
                raise ScanCancelled(_CATALOG_MOVED)

        def score_chunk(chunk):
            vectors = []
            usable = []
            for row in chunk:
                blob = row["embedding"]
                if isinstance(blob, memoryview):
                    blob = blob.tobytes()
                if not isinstance(blob, (bytes, bytearray)) or len(blob) != 2048:
                    continue
                vector = np.frombuffer(bytes(blob), dtype=np.float32)
                if vector.shape != (512,):
                    continue
                vectors.append(np.ascontiguousarray(vector))
                usable.append(row)
            label_rows = []
            applied = 0
            if vectors:
                scored = categories_from_embeddings(np.stack(vectors))
                for row, vector, (tag, _confidence) in zip(usable, vectors, scored):
                    ident = (row["folder_mode"], row["tweet_id"], row["num"], row["media_type"])
                    chosen = _choose_label(ident, tag, vector, feedback, estimated, groups)
                    if chosen is None:
                        continue
                    label_rows.append((*ident, chosen))
                    if chosen:
                        applied += 1
            _store_labels(store, label_rows)
            return applied

        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                store._folder_cache = {}
                records = (store.photos_without_embeddings()
                           if hasattr(store, "photos_without_embeddings") else store.unclassified_photo_records())
                relabel_count = (store.count_embeddings_without_labels()
                                 if hasattr(store, "count_embeddings_without_labels") else 0)
                if not records and not relabel_count:
                    return 0
                signatures = store.unreadable_signatures() if hasattr(store, "unreadable_signatures") else {}
                valid_items = []
                skipped_unreadable = 0
                record_count = len(records)
                for idx, rec in enumerate(records):
                    check_cancel()
                    if self._catalog_request(generation, epoch) is None:
                        return 0
                    if status_callback and record_count and (idx % 200 == 0 or idx == record_count - 1):
                        status_callback(f"분류할 사진을 확인하는 중… ({idx + 1:,} / {record_count:,})")
                    path = self._path(store, rec)
                    if path is None:
                        continue
                    ident = (rec["folder_mode"], rec["tweet_id"], rec["num"], rec["media_type"])
                    try:
                        info = path.stat()
                        signature = (int(info.st_size), int(info.st_mtime_ns))
                    except OSError:
                        continue
                    if signatures.get(ident) == signature:
                        skipped_unreadable += 1
                        continue
                    valid_items.append((rec, path))
                # Cleared tags keep their embedding and are scored without the vision model.
                # A file that cannot be decoded is skipped until its size or time changes.
                grand = len(valid_items) + relabel_count + skipped_unreadable
                if grand == 0:
                    return 0
                def remember_note(count):
                    if count:
                        self._scan_note = (
                            f" 읽을 수 없는 사진 {count:,}장은 파일이 바뀌기 전까지 다시 열지 않습니다."
                        )

                remember_note(skipped_unreadable)
                if progress_callback:
                    progress_callback(skipped_unreadable, grand)
                if not valid_items and not relabel_count:
                    return 0
                if hasattr(store, "learning_state"):
                    _embeddings, feedback, estimated = store.learning_state(include_embeddings=False)
                    if feedback and hasattr(store, "embeddings_for"):
                        _embeddings = store.embeddings_for(feedback)
                    groups = _feedback_matrices(_embeddings, feedback)
                else:
                    feedback, estimated, groups = {}, {}, {}
                saved_count = 0
                processed_relabel = 0
                if relabel_count:
                    for chunk in store.iter_embeddings_without_labels(64):
                        check_cancel()
                        if self._catalog_request(generation, epoch) is None:
                            return saved_count
                        saved_count += score_chunk(chunk)
                        processed_relabel += len(chunk)
                        if progress_callback:
                            progress_callback(skipped_unreadable + processed_relabel, grand)

                if valid_items:
                    if not is_available():
                        try:
                            def on_dl(dl_bytes, tot_bytes):
                                if status_callback and tot_bytes:
                                    status_callback(
                                        f"AI 분류 모델 내려받는 중… ({dl_bytes / (1024 * 1024):.1f} / {tot_bytes / (1024 * 1024):.1f} MB)"
                                    )
                            ensure_model(progress_callback=on_dl)
                        except Exception as exc:
                            raise RuntimeError("AI 분류 모델을 준비하지 못했습니다. 인터넷 연결과 저장 공간을 확인해 주세요.") from exc

                    sess = get_session()
                    if sess is None:
                        raise RuntimeError("AI 분류 엔진을 시작하지 못했습니다. 프로그램 파일을 확인해 주세요.")
                    batch_records = []
                    batch_paths = []
                    batch_size = 16
                    encode_total = len(valid_items)
                    newly_unreadable = 0
                    taught_encoded = False

                    for idx, (rec, path) in enumerate(valid_items):
                        check_cancel()
                        if self._catalog_request(generation, epoch) is None:
                            return saved_count
                        batch_records.append(rec)
                        batch_paths.append(path)

                        if len(batch_paths) >= batch_size or idx == encode_total - 1:
                            try:
                                predictions = classify_detailed(batch_paths, batch_size=batch_size, session=sess)
                            except Exception as exc:
                                kept = f"이미 분류된 {saved_count:,}장은 유지되며, " if saved_count else ""
                                raise RuntimeError(
                                    "AI 분류 중 일부 사진을 처리하지 못했습니다. "
                                    + kept
                                    + "다시 실행하면 남은 사진부터 이어서 분류합니다."
                                ) from exc
                            embed_rows = []
                            label_rows = []
                            forget = []
                            remember = []
                            for record, path_item, (tag, confidence, vector) in zip(batch_records, batch_paths, predictions):
                                ident = (record["folder_mode"], record["tweet_id"], record["num"], record["media_type"])
                                if vector is not None:
                                    embed_rows.append((*ident, vector.tobytes(), confidence))
                                    forget.append(ident)
                                    if ident in feedback:
                                        taught_encoded = True
                                else:
                                    signature = encode_skip_signature(path_item)
                                    if signature is not None:
                                        remember.append((*ident, signature[0], signature[1]))
                                        newly_unreadable += 1
                                chosen = _choose_label(ident, tag, vector, feedback, estimated, groups)
                                if chosen is None:
                                    continue
                                label_rows.append((*ident, chosen))
                                if chosen:
                                    saved_count += 1
                            if embed_rows and hasattr(store, "save_embeddings"):
                                store.save_embeddings(embed_rows)
                            _store_labels(store, label_rows)
                            if forget and hasattr(store, "forget_unreadable"):
                                store.forget_unreadable(forget)
                            if remember and hasattr(store, "remember_unreadable"):
                                store.remember_unreadable(remember)
                            batch_records.clear()
                            batch_paths.clear()
                            # Report every finished batch. Waiting for `idx % 8 == 0`
                            # never fired, because a batch ends on 15, 31, 47…
                            processed = skipped_unreadable + relabel_count + idx + 1
                            if progress_callback:
                                progress_callback(processed, grand)
                    remember_note(skipped_unreadable + newly_unreadable)
                else:
                    taught_encoded = False

                if taught_encoded:
                    if status_callback and feedback:
                        status_callback("비슷한 사진에 분류를 맞추는 중…")
                    saved_count += _propagate_neighbors(store)
                return saved_count
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                "AI 자동분류를 끝내지 못했습니다. 저장 폴더를 확인한 뒤 다시 시도해 주세요."
            ) from exc
        finally:
            release_session()

    def clear_ai_tags(self, generation, epoch=None):
        request = self._catalog_request(generation, epoch)
        if request is None:
            return 0
        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                return store.clear_ai_tags()
        except Exception as exc:
            raise RuntimeError(
                "AI 태그를 초기화하지 못했습니다. 저장 폴더를 확인한 뒤 다시 시도해 주세요."
            ) from exc

    def teach_labels(self, generation, identities, category, epoch=None, progress_callback=None):
        """Store a user category and relabel photos that are very close to it.

        Returns ``(taught, propagated)``. Photos that cannot be encoded are not taught.
        ``progress_callback(done, total)`` counts the selected photos, including ones
        that already have a stored embedding.
        """
        request = self._catalog_request(generation, epoch)
        if request is None:
            return 0, 0
        from .clip_classifier import CATEGORY_NAMES, classify_detailed, ensure_model, get_session, is_available, release_session
        if category not in CATEGORY_NAMES and category != "":
            return 0, 0
        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                store._folder_cache = {}
                if self._catalog_request(generation, epoch) is None:
                    return 0, 0
                wanted = []
                seen = set()
                for ident in identities:
                    key = tuple(ident)
                    if len(key) < 4 or key in seen:
                        continue
                    seen.add(key)
                    wanted.append(key)
                by_ident = store.photo_rows_for_identities(wanted)
                present = [ident for ident in wanted if ident in by_ident]
                known = store.embeddings_for(present) if present else {}
                missing = []
                for ident in present:
                    if ident in known:
                        continue
                    path = self._path(store, by_ident[ident])
                    if path is not None and path.is_file():
                        missing.append((ident, path))
                total = len(present)
                if progress_callback and total:
                    progress_callback(0, total)
                if missing:
                    if not is_available():
                        try:
                            ensure_model()
                        except Exception as exc:
                            raise RuntimeError("AI 분류 모델을 준비하지 못했습니다. 인터넷 연결과 저장 공간을 확인해 주세요.") from exc
                    session = get_session()
                    if session is None:
                        raise RuntimeError("AI 분류 엔진을 시작하지 못했습니다. 프로그램 파일을 확인해 주세요.")
                    done = total - len(missing)
                    for start in range(0, len(missing), 16):
                        if self._catalog_request(generation, epoch) is None:
                            return 0, 0
                        chunk = missing[start:start + 16]
                        detailed = classify_detailed([path for _ident, path in chunk], batch_size=16, session=session)
                        encoded = []
                        for (ident, _path), (_tag, confidence, vector) in zip(chunk, detailed):
                            if vector is not None:
                                encoded.append((*ident, vector.tobytes(), confidence))
                        if encoded:
                            store.save_embeddings(encoded)
                            if hasattr(store, "forget_unreadable"):
                                store.forget_unreadable([item[:4] for item in encoded])
                        done += len(chunk)
                        if progress_callback:
                            progress_callback(min(done, total), total)
                if progress_callback and total:
                    progress_callback(total, total)
                refreshed = store.embeddings_for(present) if present else {}
                teachable = [ident for ident in present if ident in refreshed]
                if not teachable:
                    return 0, 0
                taught = store.set_taught_categories(teachable, category)
                propagated = _propagate_neighbors(store)
                return taught, propagated
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                "분류를 저장하지 못했습니다. 저장 폴더를 확인한 뒤 다시 시도해 주세요."
            ) from exc
        finally:
            release_session()

    def duplicate_copy_keys(self, generation):
        request = self.current(generation)
        if request is None:
            return []
        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                stored = store.duplicate_copy_identities() if hasattr(store, "duplicate_copy_identities") else None
                if stored is not None:
                    return [f"g{generation}_{item[0]}_{item[1]}_{item[2]}_{item[3]}" for item in stored]
                from .duplicates import cluster_duplicates
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
                rows.append(dict(row, _delete_signature=signature, _key=key))
        with self._condition:
            if self._request is not request:
                raise ValueError("목록이 바뀌었습니다. 사진을 다시 선택해 주세요.")
        return {"location": request.location(), "rows": rows, "generation": request.generation}

    def forget(self, keys, generation):
        """Rows the gallery dropped after their files went to the Recycle Bin.

        The next page continues after the last shown row, which is still the
        same row; only the count of rows in front of it shrank.
        """
        with self._condition:
            request = self._request
            if request is None or request.generation != generation:
                return False
            dropped = sum(1 for key in keys if self._served.pop(key, None) is not None)
            self._cursor_offset = max(0, self._cursor_offset - dropped)
            return True

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
    duplicateGroupsChanged = Signal()
    duplicateScanProgress = Signal(int, int)
    aiScanProgress = Signal(int, int)
    aiScanningChanged = Signal()
    aiCountsChanged = Signal()
    # A map, not `object`: QML cannot read the fields of a Python object passed
    # as `object`, which left the statistics window on "…" for good.
    statisticsLoaded = Signal("QVariantMap")
    shutdownFinished = Signal()
    _delivered = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._repository = LibraryRepository()
        self.provider = _LibraryThumbnailProvider(decoder=self._repository.decode,
                                                  workers=_thumbnail_workers())
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
        self._ai_category = ""
        self._ai_origin = ""
        self._user_tags = []
        self._user_tags_loaded = False
        self._favorite_only = False
        self._duplicate_only = False
        self._duplicate_count = 0
        self._dup_current = 0
        self._dup_total = 0
        self._teach_request = None
        self._skipped_teach = 0
        self._ai_counts = []
        self._duplicate_near = False
        self._duplicate_groups = []
        self._statistics_wanted = False
        self._ai_scanning = False
        self._ai_current = 0
        self._ai_total = 0
        self._date_field = "posted"
        self._date_from = self._date_to = ""
        self._selection_mode = False
        self._selection_keys = set()
        self._selected_keys_order = []
        self._selection_revision = 0
        self._selection_anchor = ""
        self._band_base = None
        self._band_rect = None
        self._selected = {}
        self._selection_key = ""
        self._selection_serial = 0
        self._navigation = None
        self._busy = False
        self._exclusive = ""
        self._status = ""
        self._condition = Condition()
        self._pending = deque()
        self._interactive = deque()
        self._closed = False
        self._closing = False
        self._shutdown_complete = False
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(50)
        self._shutdown_timer.timeout.connect(self._poll_shutdown)
        self._delivered.connect(self._accept, Qt.ConnectionType.QueuedConnection)
        self._worker = Thread(target=self._work, name="qt-library-actions", daemon=True)
        self._interactive_worker = Thread(target=self._work_interactive, name="qt-library-browse", daemon=True)
        self._worker.start()
        self._interactive_worker.start()
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

    @Property(str, notify=changed)
    def filterAiCategory(self):
        return self._ai_category

    @Property(str, notify=changed)
    def filterAiOrigin(self):
        return self._ai_origin

    @Property("QVariantList", constant=True)
    def aiCategories(self):
        from .clip_classifier import CATEGORY_NAMES
        return list(CATEGORY_NAMES)

    @Property("QVariantList", notify=aiCountsChanged)
    def aiCategoryCounts(self):
        """[{category, count}] for the AI menu; empty until the first read finishes."""
        return [dict(item) for item in self._ai_counts]

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

    @Property("QVariantList", notify=duplicateGroupsChanged)
    def duplicateGroups(self):
        """Loaded duplicates as groups: [{group, size, items: [{key, caption, detail, thumbUrl, keeper, ...}]}]."""
        return self._duplicate_groups

    def _rebuild_duplicate_groups(self):
        groups = []
        if self._duplicate_only:
            current = None
            for row in self.model._rows:
                group = row.get("group", 0)
                if not group:
                    continue
                if current is None or current["group"] != group:
                    current = {"group": group, "size": row.get("group_size", 0), "items": []}
                    groups.append(current)
                current["items"].append({
                    "key": row["key"], "caption": row["caption"], "detail": row.get("detail", ""),
                    "thumbUrl": self.model.thumb_url(row["key"]), "keeper": bool(row.get("keeper", False)),
                    "favorite": bool(row.get("favorite", False)), "missing": bool(row.get("missing", False))})
            for index, group in enumerate(groups):
                group["number"] = index + 1
        if groups != self._duplicate_groups:
            self._duplicate_groups = groups
            self.duplicateGroupsChanged.emit()

    @Slot("QVariantList", result=int)
    def addToSelection(self, keys):
        """Select the given loaded tiles as well, for one duplicate group at a time."""
        if self._closed:
            return 0
        wanted = [key for key in keys if isinstance(key, str) and key in self.model._keys]
        added, _unavailable, _overflow = self._extend_selection(wanted, self._selection_keys)
        return added

    @Slot("QVariantList")
    def removeFromSelection(self, keys):
        if self._closed:
            return
        gone = {key for key in keys if isinstance(key, str)} & self._selection_keys
        if gone:
            self._selection_keys -= gone
            self._batch_selection_changed()

    @Property(int, notify=duplicateScanProgress)
    def duplicateProgressCurrent(self):
        return self._dup_current

    @Property(int, notify=duplicateScanProgress)
    def duplicateProgressTotal(self):
        return self._dup_total

    @Property(bool, notify=aiScanningChanged)
    def aiScanning(self):
        return self._ai_scanning

    @Property(int, notify=aiScanProgress)
    def aiProgressCurrent(self):
        return self._ai_current

    @Property(int, notify=aiScanProgress)
    def aiProgressTotal(self):
        return self._ai_total

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
    def selectedAiCategory(self):
        return self._selected.get("ai_category", "") or ""

    @Property(bool, notify=changed)
    def selectedAiConfirmed(self):
        return bool(self._selected.get("ai_confirmed", False))

    @Property(str, notify=changed)
    def selectedAiConfidence(self):
        value = self._selected.get("ai_confidence", -1)
        if not isinstance(value, (int, float)) or value < 0:
            return ""
        return f"{round(float(value) * 100)}%"

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
        if self._ai_category:
            parts.append(f"AI 분류 {self._ai_category}")
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
        return bool(self._exclusive or self._busy)

    @Property(bool, notify=changed)
    def selectionBusy(self):
        """Only the opened item is loading. A folder-wide scan does not count."""
        return self._busy

    @Property(bool, notify=changed)
    def tagsLocked(self):
        return self._exclusive in {"ai", "clear", "teach"}

    @Property(bool, notify=changed)
    def filesLocked(self):
        return bool(self._exclusive)

    @Property(bool, notify=changed)
    def copiesLocked(self):
        return self._exclusive == "duplicates"

    @Property(str, notify=changed)
    def holdReason(self):
        return _HOLD_REASON.get(self._exclusive, "")

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
        self._selection_anchor = ""
        self._band_base = None
        self.clearSelection()
        # A folder-wide scan keeps running while the grid reloads. Clearing the
        # flag here let a second scan start and left the first one's button stuck.
        if self._exclusive:
            self._busy = False
            if status:
                self._status = status
        else:
            self._status = status
            self._busy = False
        with self._condition:
            self._pending = deque(item for item in self._pending if item[0] in _EXCLUSIVE_ACTIONS)
            self._interactive.clear()
            self._condition.notify_all()
            request = None
        if self._account and self._output_dir:
            request = _Request(self._generation, self._account, self._output_dir, self._separate_accounts,
                               self._query, self._media_type, self._order, self._author,
                               self._date_field, self._date_from, self._date_to, self._hashtag,
                               self._favorite_only, self._user_tag, self._duplicate_only,
                               self._ai_category, self._ai_origin)
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
                if reload_authors or not self._authors_loaded:
                    self._interactive.append(("authors", self._generation, self._selection_serial, ""))
                if reload_authors or not self._hashtags_loaded:
                    self._interactive.append(("hashtags", self._generation, self._selection_serial, ""))
                if reload_authors or not self._user_tags_loaded:
                    self._interactive.append(("user_tags", self._generation, self._selection_serial, ""))
                if reload_authors:
                    self._interactive.append(("duplicate_counts", self._generation, self._selection_serial, ""))
                    self._interactive.append(("ai_counts", self._generation, self._selection_serial, ""))
                self._condition.notify_all()
        if request is not None and self._statistics_wanted:
            # The reload above emptied the queue; an open statistics window still waits.
            with self._condition:
                self._interactive.append(("statistics", self._generation, self._selection_serial, ""))
                self._condition.notify_all()
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
        ai_changed = bool(self._ai_category or self._ai_origin)
        self._query = self._author = self._hashtag = self._date_from = self._date_to = self._user_tag = ""
        self._ai_category = self._ai_origin = ""
        self._favorite_only = False
        self._duplicate_only = False
        if author_changed:
            self.authorFilterChanged.emit()
        if hashtag_changed:
            self.hashtagFilterChanged.emit()
        if tag_changed:
            self.userTagFilterChanged.emit()
        if ai_changed:
            self.changed.emit()
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

    @Slot(str, str)
    def setAiFilter(self, category, origin):
        from .clip_classifier import CATEGORY_NAMES
        category = category.strip() if isinstance(category, str) else ""
        # The screen shows one list per category. Estimated and taught photos
        # are no longer split; the second argument stays for older callers.
        origin = ""
        if category not in CATEGORY_NAMES:
            category = ""
        if category == self._ai_category and origin == self._ai_origin:
            return
        self._ai_category, self._ai_origin = category, origin
        self._refresh(reload_authors=False)

    def _queue_teach(self, category, keys):
        from .clip_classifier import CATEGORY_NAMES
        if self._closed:
            return False
        if category not in CATEGORY_NAMES and category != "":
            self._status = "가르칠 분류를 다시 선택해 주세요."
            self.changed.emit()
            return False
        if self._exclusive:
            self._status = _EXCLUSIVE_BUSY[self._exclusive]
            self.changed.emit()
            return False
        idents = []
        skipped = 0
        seen = set()
        for key in keys:
            match = _KEY.fullmatch(key) if isinstance(key, str) else None
            if match is None or int(match[1]) != self._generation:
                continue
            if match[5] != "photo":
                skipped += 1
                continue
            ident = (match[2], match[3], int(match[4]), match[5])
            if ident in seen:
                continue
            seen.add(ident)
            idents.append(ident)
        if not idents:
            self._status = "가르칠 사진이 없습니다. 영상과 GIF는 이 분류에 넣지 않습니다." if skipped else "가르칠 사진을 선택해 주세요."
            self.changed.emit()
            return False
        self._teach_request = (category, idents)
        self._skipped_teach = skipped
        return self._begin_exclusive("teach", "teach", "분류를 저장하는 중…")

    @Slot(str, str, result=bool)
    def teachOne(self, key, category):
        return self._queue_teach(category, [key])

    @Slot(str, result=bool)
    def teachSelected(self, category):
        if not self.selectedKey:
            self._status = "미리보기에서 사진을 연 뒤 분류를 가르쳐 주세요."
            self.changed.emit()
            return False
        return self._queue_teach(category, [self.selectedKey])

    @Slot(str, result=bool)
    def teachSelection(self, category):
        return self._queue_teach(category, list(self._selection_keys))

    def _upsert_cached_user_tag(self, clean: str, delta: int = 1) -> None:
        normalized: list[dict] = []
        found = False
        for item in self._user_tags:
            if isinstance(item, dict):
                t = str(item.get("tag", ""))
                c = int(item.get("count", 0) or 0)
            else:
                t = str(item)
                c = 1
            if not t:
                continue
            if t == clean:
                found = True
                new_c = c + delta
                if new_c <= 0:
                    continue
                c = new_c
            normalized.append({"tag": t, "count": c})
        if not found and delta > 0:
            normalized.append({"tag": clean, "count": delta})
        normalized.sort(key=lambda r: (-int(r.get("count", 0)), str(r.get("tag", ""))))
        self._user_tags = normalized
        if self._user_tag == clean and delta < 0 and not any(r.get("tag") == clean for r in normalized):
            self._user_tag = ""
            self.userTagFilterChanged.emit()
        self.userTagsChanged.emit()

    def _refuse_tags(self):
        if self._exclusive not in {"ai", "clear", "teach"}:
            return False
        self._status = _TAG_LOCKED
        self.changed.emit()
        return True

    def _begin_exclusive(self, kind, action, status):
        if self._closed:
            return False
        if self._exclusive:
            self._status = _EXCLUSIVE_BUSY[self._exclusive]
            self.changed.emit()
            return False
        self._exclusive = kind
        if kind == "ai":
            self._ai_scanning = True
            self._ai_current = 0
            self._ai_total = 0
            self.aiScanningChanged.emit()
        self._status = status
        epoch = self._repository.catalog_epoch() if hasattr(self._repository, "catalog_epoch") else None
        with self._condition:
            self._pending.append((action, self._generation, self._selection_serial, "", epoch))
            self._condition.notify_all()
        self.changed.emit()
        return True

    @Slot(str, result=int)
    def addTagToSelection(self, tag):
        if self._refuse_tags() or self._closed or not tag:
            return 0
        clean = tag.strip().lstrip("#")
        if not clean:
            return 0
        count = self._repository.add_tag_batch(list(self._selection_keys), self._generation, clean)
        if count:
            self._upsert_cached_user_tag(clean, delta=count)
            self._user_tags_loaded = False
            self._refresh(reload_authors=False, status=f"선택한 {count:,}개 사진에 #{clean} 태그를 추가했습니다.")
        return count

    @Slot()
    def requestStatistics(self):
        if self._closed:
            return
        if not (self._account and self._output_dir):
            self.statisticsLoaded.emit({"error": "저장할 계정과 폴더를 먼저 정해 주세요."})
            return
        self._statistics_wanted = True
        with self._condition:
            self._interactive.append(("statistics", self._generation, self._selection_serial, ""))
            self._condition.notify_all()

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

    @Slot(result=bool)
    @Slot(bool, result=bool)
    def scanDuplicates(self, near=False):
        """Look for identical files; with ``near`` also the same photo at another size or quality."""
        if not self._exclusive and hasattr(self._repository, "_duplicate_cancel"):
            self._repository._duplicate_cancel.clear()
        if not self._exclusive:
            self._duplicate_near = bool(near)
        return self._begin_exclusive("duplicates", "scan_duplicates", "같은 사진을 찾는 중…")

    @Slot()
    def cancelDuplicateScan(self):
        if self._exclusive == "duplicates" and hasattr(self._repository, "_duplicate_cancel"):
            self._repository._duplicate_cancel.set()
            self._status = "중복 검사를 중단하는 중… 현재 파일 확인이 끝나면 멈춥니다."
            self.changed.emit()

    @Slot(result=bool)
    def scanAiTags(self):
        if not self._exclusive and hasattr(self._repository, "_ai_cancel"):
            self._repository._ai_cancel.clear()
        return self._begin_exclusive("ai", "scan_ai_tags", "AI 자동분류 준비 중…")

    @Slot()
    def cancelAiScan(self):
        if self._exclusive == "ai" and hasattr(self._repository, "_ai_cancel"):
            self._repository._ai_cancel.set()
            self._status = "AI 자동분류를 중단하는 중… 현재 묶음 처리가 끝나면 멈춥니다."
            self.changed.emit()

    @Slot(result=bool)
    def clearAiTags(self):
        return self._begin_exclusive("clear", "clear_ai_tags", "AI 자동분류 태그를 초기화하는 중…")

    @Slot(result=int)
    def selectDuplicateCopies(self):
        if self._closed:
            return 0
        if self._exclusive == "duplicates":
            self._status = _HOLD_REASON["duplicates"]
            self.changed.emit()
            return 0
        copy_keys = self._repository.duplicate_copy_keys(self._generation)
        if not copy_keys:
            self._status = "선택할 중복 사본이 없습니다."
            self.changed.emit()
            return 0
        loaded_copies = [k for k in copy_keys if k in self.model._keys and k not in self._selection_keys]
        selectable, _, _ = self._repository.selectable_keys(loaded_copies)
        capacity = max(0, MAX_SELECTION - len(self._selection_keys))
        copy_keys = selectable[:capacity]
        if not copy_keys:
            self._status = ("한 번에 최대 2,000개를 선택할 수 있습니다." if not capacity else
                            "현재 목록에 새로 선택할 중복 사본이 없습니다.")
            self.changed.emit()
            return 0
        self._selection_mode = True
        self._selection_keys.update(copy_keys)
        self._batch_selection_changed()
        self._status = f"중복 사본 {len(copy_keys):,}개를 선택했습니다."
        if len(selectable) > capacity:
            self._status += " 한 번에 최대 2,000개를 선택할 수 있습니다."
        self.changed.emit()
        return len(copy_keys)

    @Slot(result=bool)
    def toggleSelectedFavorite(self):
        if self._closed or not self.selectedKey:
            return False
        return self.toggleFavorite(self.selectedKey)

    @Slot(str, result=bool)
    def toggleFavorite(self, key):
        if self._closed or not isinstance(key, str) or not key:
            return False
        new_state = self._repository.toggle_favorite(key, self._generation)
        if type(new_state) is not bool:
            self._status = _FAVORITE_ERROR
            self.changed.emit()
            return False
        if self._status == _FAVORITE_ERROR:
            self._status = ""
            self.changed.emit()
        if key == self.selectedKey:
            self._selected["favorite"] = new_state
            self.changed.emit()
        self.model.setFavorite(key, new_state)
        if self._duplicate_only:
            self._refresh(reload_authors=False)
        return new_state

    @Slot(str, result=str)
    def favoriteNotice(self, key):
        """Toggle a favorite and say what happened; a failed save is not a removal."""
        if self._closed or not isinstance(key, str) or not key:
            return ""
        added = self.toggleFavorite(key)
        # A successful toggle clears the error, so it is still set only after a failure.
        if not added and self._status == _FAVORITE_ERROR:
            return _FAVORITE_ERROR
        return "즐겨찾기에 추가했습니다." if added else "즐겨찾기에서 제외했습니다."

    @Slot(str, result=bool)
    def addSelectedTag(self, tag):
        if self._refuse_tags() or self._closed or not self.selectedKey or not tag:
            return False
        clean = tag.strip().lstrip("#")
        if not clean:
            return False
        had_tag = any(
            (t.get("tag") if isinstance(t, dict) else str(t)) == clean
            for t in self._selected.get("tags", [])
        )
        ok = self._repository.add_tag(self.selectedKey, self._generation, clean)
        if ok:
            tags = [t for t in self._selected.get("tags", []) if not (isinstance(t, dict) and t.get("tag") == clean)]
            tags.append({"tag": clean, "source": "user"})
            tags.sort(key=lambda t: str(t.get("tag", "")) if isinstance(t, dict) else str(t))
            self._selected["tags"] = tags
            if not had_tag:
                self._upsert_cached_user_tag(clean, delta=1)
            self._user_tags_loaded = False
            with self._condition:
                self._interactive.append(("user_tags", self._generation, self._selection_serial, ""))
                self._condition.notify_all()
            self.changed.emit()
        return ok

    @Slot(str, result=bool)
    def removeSelectedTag(self, tag):
        if self._refuse_tags() or self._closed or not self.selectedKey or not tag:
            return False
        clean = tag.strip().lstrip("#")
        if not clean:
            return False
        ok = self._repository.remove_tag(self.selectedKey, self._generation, clean)
        if ok:
            tags = [t for t in self._selected.get("tags", []) if t.get("tag") != clean]
            self._selected["tags"] = tags
            self._upsert_cached_user_tag(clean, delta=-1)
            self._user_tags_loaded = False
            with self._condition:
                self._interactive.append(("user_tags", self._generation, self._selection_serial, ""))
                self._condition.notify_all()
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
        self._selection_anchor = key
        self._selection_mode = True
        self._batch_selection_changed()

    @Slot(str)
    def setSelectionAnchor(self, key):
        """The tile a Shift+click measures its range from."""
        if not self._closed and key in self.model._keys:
            self._selection_anchor = key

    def _extend_selection(self, keys, base):
        """Selection = base plus the present files among keys, within the limit."""
        chosen = set(base)
        selectable, unavailable, _overflow = self._repository.selectable_keys(
            [key for key in keys if key not in chosen])
        room = max(0, MAX_SELECTION - len(chosen))
        chosen.update(selectable[:room])
        changed = chosen != self._selection_keys
        self._selection_keys = chosen
        if changed or not self._selection_mode:
            self._selection_mode = True
            self._batch_selection_changed()
        return len(selectable[:room]), unavailable, len(selectable) > room

    @Slot(str, result=int)
    def selectRangeTo(self, key):
        """Shift+click: add every loaded tile from the last clicked one to this one."""
        if self._closed or key not in self.model._keys:
            return 0
        positions = self.model._positions
        anchor = self._selection_anchor if self._selection_anchor in positions else (
            self.selectedKey if self.selectedKey in positions else key)
        first, last = sorted((positions[anchor], positions[key]))
        keys = [self.model._rows[index]["key"] for index in range(first, last + 1)]
        added, unavailable, overflow = self._extend_selection(keys, self._selection_keys)
        notes = []
        if unavailable:
            notes.append(f"파일 없음·확인 불가 {unavailable:,}개는 제외했습니다.")
        if overflow:
            notes.append("한 번에 최대 2,000개를 선택할 수 있습니다.")
        if notes or self._status:
            self._status = " ".join(notes)
            self.changed.emit()
        return added

    @Slot(bool)
    def beginBandSelection(self, additive):
        """Start a drag selection; with Ctrl held the earlier selection stays."""
        if self._closed:
            return
        self._band_base = set(self._selection_keys) if additive else set()
        self._band_rect = None

    @Slot(int, int, int, int, int)
    def bandSelect(self, row_first, row_last, column_first, column_last, columns):
        """Select the tiles a dragged rectangle touches, given as grid rows and columns."""
        if self._closed or self._band_base is None or columns < 1:
            return
        rect = (row_first, row_last, column_first, column_last, columns)
        if rect == self._band_rect:
            return
        self._band_rect = rect
        count = len(self.model._rows)
        keys = []
        for row in range(max(0, row_first), max(0, row_last) + 1):
            start = row * columns
            if start >= count:
                break
            for column in range(max(0, column_first), min(columns - 1, column_last) + 1):
                index = start + column
                if index < count:
                    keys.append(self.model._rows[index]["key"])
        self._extend_selection(keys, self._band_base)

    @Slot()
    def endBandSelection(self):
        if self._band_base is None:
            return
        self._band_base = None
        self._band_rect = None
        if self._selection_keys:
            self._selection_anchor = self._selected_keys_order[-1] if self._selected_keys_order else ""

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

    def removeDeleted(self, keys):
        """Take recycled files out of the shown list without reloading it.

        Returns False when the list has to be reloaded instead: the view
        changed since the files were chosen, a page is still loading, or the
        filter itself depends on which copies remain.
        """
        forget = getattr(self._repository, "forget", None)
        if (self._closed or not callable(forget) or self._duplicate_only or self._navigation is not None
                or not isinstance(keys, (list, tuple)) or not keys):
            return False
        prefix = f"g{self._generation}_"
        if any(not isinstance(key, str) or not key.startswith(prefix) for key in keys):
            return False
        present = [key for key in dict.fromkeys(keys) if key in self.model._keys]
        gone = set(present)
        # Where the opened item sits now, counted in rows that will remain.
        opened = self._selection_key or self.selectedKey
        successor = None
        if opened in gone:
            position = self.model._positions.get(opened, 0)
            successor = position - sum(1 for key in gone if self.model._positions[key] < position)
        if not present or not self.model.removeKeys(present):
            return False
        forget(present, self._generation)
        if self._selection_keys & gone:
            self._selection_keys -= gone
            self._batch_selection_changed()
        if successor is not None:
            # Open the photo that took the place at once. Waiting for the grid to
            # move its highlight left a moment with nothing selected, and a count
            # arriving in that moment closed a preview that had just come back.
            self._selection_serial += 1
            self._selected = {}
            self._selection_key = ""
            self._busy = False
            if self.model._rows:
                self.selectItem(self.model._rows[min(successor, len(self.model._rows) - 1)]["key"])
        # Counts beside the filters include the files that just left.
        with self._condition:
            for action in ("authors", "hashtags", "user_tags", "duplicate_counts", "ai_counts"):
                self._interactive.append((action, self._generation, self._selection_serial, ""))
            self._condition.notify_all()
        self.changed.emit()
        return True

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
        if not self._exclusive:
            self._status = ""
        with self._condition:
            self._interactive = deque(item for item in self._interactive if item[0] != "select")
            self._interactive.append(("select", self._generation, self._selection_serial, key))
            self._condition.notify_all()
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
        self._rebuild_duplicate_groups()
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
            self._interactive.append((action, self._generation, self._selection_serial, self.selectedKey))
            self._condition.notify_all()
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

    def _same_catalog(self, epoch):
        check = getattr(self._repository, "same_catalog", None)
        if epoch is None or not callable(check):
            return self._repository.current() is not None
        return bool(check(epoch))

    def _emit(self, generation, serial, action, selection, message, epoch):
        self._delivered.emit((generation, serial, action, selection, message, epoch))

    def _perform(self, action, generation, serial, key, epoch):
        if action in _EXCLUSIVE_ACTIONS and not self._same_catalog(epoch):
            self._emit(generation, serial, action, None, _CATALOG_MOVED, epoch)
            return
        if action not in _EXCLUSIVE_ACTIONS and self._repository.current(generation) is None:
            return
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
            elif action == "duplicate_counts":
                reader = getattr(self._repository, "duplicate_counts", None)
                selection = reader(generation) if callable(reader) else 0
            elif action == "statistics":
                reader = getattr(self._repository, "overview", None)
                selection = reader(generation) if callable(reader) else None
            elif action == "ai_counts":
                reader = getattr(self._repository, "ai_counts", None)
                selection = reader(generation) if callable(reader) else []
            elif action == "scan_duplicates":
                def on_progress(cur, tot):
                    message = ("사진 비교·중복 묶음 정리 중…" if cur == tot else
                               f"사진 확인 중… ({cur:,} / {tot:,})")
                    self._emit(generation, serial, "scan_progress", (cur, tot), message, epoch)
                def on_compare(cur, tot):
                    self._emit(generation, serial, "scan_progress", (cur, tot + 1),
                               f"화질만 다른 같은 사진인지 확인 중… ({cur:,} / {tot:,})", epoch)
                near = bool(getattr(self, "_duplicate_near", False))
                try:
                    if near:
                        count = self._repository.scan_duplicates(generation, progress_callback=on_progress,
                                                                 epoch=epoch, near=True, compare_callback=on_compare)
                    else:
                        count = self._repository.scan_duplicates(generation, progress_callback=on_progress, epoch=epoch)
                except RuntimeError as exc:
                    selection, message = None, str(exc)
                else:
                    if not self._same_catalog(epoch):
                        selection, message = None, _CATALOG_MOVED
                    else:
                        selection = count
                        kind = "같은 사진(크기·화질만 다른 것 포함)" if near else "완전히 같은 사진"
                        message = f"{kind} {count:,}장을 찾았습니다." if count else f"{kind}이 없습니다."
            elif action == "scan_ai_tags":
                progress_state = {"cur": 0, "tot": 0}
                def on_ai_progress(cur, tot):
                    progress_state["cur"] = cur
                    progress_state["tot"] = tot
                    self._emit(generation, serial, "ai_scan_progress", (cur, tot), f"AI 자동분류 중… ({cur:,} / {tot:,})", epoch)
                def on_ai_status(msg):
                    # Keep the last real count. A status line used to reset it to 0/0.
                    self._emit(
                        generation, serial, "ai_scan_progress",
                        (progress_state["cur"], progress_state["tot"]), msg, epoch,
                    )
                try:
                    count = self._repository.scan_ai_tags(
                        generation,
                        progress_callback=on_ai_progress,
                        status_callback=on_ai_status,
                        epoch=epoch,
                    )
                    if not self._same_catalog(epoch):
                        selection, message = None, _CATALOG_MOVED
                    else:
                        selection = count
                        seen = progress_state["tot"]
                        note = getattr(self._repository, "_scan_note", "")
                        if count:
                            message = f"AI 자동분류 완료: {seen:,}장을 확인했고, 사진 {count:,}장에 태그를 적용했습니다."
                        elif seen:
                            message = f"AI 자동분류 완료: {seen:,}장을 확인했습니다. 새로 붙인 태그는 없습니다."
                        else:
                            message = "새로 분류할 사진이 없거나 분류가 완료되었습니다."
                        if note:
                            message += note
                except RuntimeError as exc:
                    selection = None
                    message = str(exc)
            elif action == "teach":
                request_teach = self._teach_request
                try:
                    if not request_teach:
                        selection, message = None, "가르칠 분류를 다시 선택해 주세요."
                    else:
                        category, idents = request_teach

                        def on_teach_progress(cur, tot):
                            self._emit(
                                generation, serial, "ai_scan_progress", (cur, tot),
                                f"분류를 저장하는 중… ({cur:,} / {tot:,})", epoch,
                            )

                        taught, propagated = self._repository.teach_labels(
                            generation, idents, category, epoch=epoch, progress_callback=on_teach_progress,
                        )
                        if not self._same_catalog(epoch):
                            selection, message = None, _CATALOG_MOVED
                        else:
                            selection = (taught, propagated)
                            skipped = getattr(self, "_skipped_teach", 0)
                            missed = max(0, len(idents) - taught)
                            if taught == 0:
                                message = "사진의 특징을 만들지 못해 분류를 저장하지 못했습니다."
                            elif propagated:
                                message = f"{taught:,}장의 분류를 저장했습니다. 비슷한 사진 {propagated:,}장도 함께 바꿨습니다."
                            else:
                                message = f"{taught:,}장의 분류를 저장했습니다."
                            if missed and taught:
                                message += f" 특징을 읽지 못한 {missed:,}장은 빠졌습니다."
                            if skipped:
                                message += f" 영상·GIF {skipped:,}개는 제외했습니다."
                except RuntimeError as exc:
                    selection, message = None, str(exc)
            elif action == "clear_ai_tags":
                try:
                    count = self._repository.clear_ai_tags(generation, epoch=epoch)
                except RuntimeError as exc:
                    selection, message = None, str(exc)
                else:
                    if not self._same_catalog(epoch):
                        selection, message = None, _CATALOG_MOVED
                    else:
                        # The gallery filter may have changed while this ran. Read the
                        # catalog that is open now, not the generation captured at the start.
                        remaining = self._repository.user_tags(None)
                        selection = (count, remaining)
                        message = f"AI 자동분류 태그 {count:,}개를 초기화했습니다." if count else "초기화할 AI 자동분류 태그가 없습니다."
            elif action == "select":
                selection = self._repository.selected(key, generation)
                if selection is None:
                    message = "사진을 다시 선택해 주세요."
            elif action == "reselect":
                selection = self._repository.selected(key, generation)
            else:
                message = self._repository.action(key, generation, action)
        except Exception:
            if action in {"authors", "hashtags", "user_tags"}:
                selection = []
            elif action == "duplicate_counts":
                selection = 0
            elif action == "statistics":
                selection = {"error": _STATISTICS_ERROR}
            elif action == "ai_counts":
                selection = []
            elif action == "reselect":
                selection = None
            else:
                message = "사진 정보를 읽지 못했습니다." if action == "select" else "열지 못했습니다. 파일과 기본 앱을 확인해 주세요."
        self._emit(generation, serial, action, selection, message, epoch)

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending)
                if self._closed:
                    return
                action, generation, serial, key, epoch = self._unpack(self._pending.popleft())
            self._perform(action, generation, serial, key, epoch)

    def _work_interactive(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._interactive)
                if self._closed:
                    return
                action, generation, serial, key, epoch = self._unpack(self._interactive.popleft())
            self._perform(action, generation, serial, key, epoch)

    @staticmethod
    def _unpack(item):
        action, generation, serial, key = item[:4]
        epoch = item[4] if len(item) > 4 else None
        return action, generation, serial, key, epoch

    def _reselect(self):
        """Read the opened item again; a finished job may have changed its tags."""
        key = self.selectedKey
        if self._closed or not key:
            return
        with self._condition:
            self._interactive.append(("reselect", self._generation, self._selection_serial, key))
            self._condition.notify_all()

    def _settle(self, message, *, reload):
        """Show a finished job's result.

        The list is reloaded only when its filter depends on what the job
        changed. Otherwise the scroll position and an open preview stay.
        """
        if reload:
            self._refresh(reload_authors=False, status=message)
            with self._condition:
                self._interactive.append(("ai_counts", self._generation, self._selection_serial, ""))
                self._condition.notify_all()
            return
        self._status = message
        with self._condition:
            if not self._user_tags_loaded:
                self._interactive.append(("user_tags", self._generation, self._selection_serial, ""))
            self._interactive.append(("ai_counts", self._generation, self._selection_serial, ""))
            self._condition.notify_all()
        self._reselect()
        self.changed.emit()

    @Slot(object)
    def _release_exclusive(self):
        self._exclusive = ""
        self._ai_scanning = False
        self._ai_current = 0
        self._ai_total = 0
        self._dup_current = 0
        self._dup_total = 0
        self.aiScanningChanged.emit()
        self.aiScanProgress.emit(0, 0)
        self.duplicateScanProgress.emit(0, 0)

    @Slot(object)
    def _accept(self, result):
        generation, serial, action, selection, message = result[:5]
        epoch = result[5] if len(result) > 5 else None
        if self._closed:
            return
        catalog_job = action in _EXCLUSIVE_ACTIONS or action in {"scan_progress", "ai_scan_progress"}
        if catalog_job:
            if not self._same_catalog(epoch):
                if action in _EXCLUSIVE_ACTIONS:
                    self._release_exclusive()
                    self._status = message or _CATALOG_MOVED
                    self.changed.emit()
                return
        elif generation != self._generation:
            return
        if action == "scan_progress":
            curr, tot = selection
            self._dup_current = curr
            self._dup_total = tot
            self._status = message
            self.duplicateScanProgress.emit(curr, tot)
            self.changed.emit()
            return
        if action == "ai_scan_progress":
            curr, tot = selection
            self._ai_current = curr
            self._ai_total = tot
            self._status = message
            self.aiScanProgress.emit(curr, tot)
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
        if action == "duplicate_counts":
            count = int(selection) if isinstance(selection, int) else 0
            if count != self._duplicate_count:
                self._duplicate_count = count
                self.duplicateCountChanged.emit()
                self.changed.emit()
            return
        if action == "statistics":
            self._statistics_wanted = False
            self.statisticsLoaded.emit(selection if isinstance(selection, dict) else {"error": _STATISTICS_ERROR})
            return
        if action == "ai_counts":
            counts = [dict(item) for item in selection if isinstance(item, dict)] if isinstance(selection, list) else []
            if counts != self._ai_counts:
                self._ai_counts = counts
                self.aiCountsChanged.emit()
            return
        if action == "teach":
            self._teach_request = None
            self._release_exclusive()
            if selection is None:
                self._status = message
                self.changed.emit()
                return
            self._settle(message, reload=bool(self._ai_category))
            return
        if action == "scan_duplicates":
            self._release_exclusive()
            if not isinstance(selection, int):
                self._status = message
                self.changed.emit()
                return
            self._duplicate_count = selection
            self.duplicateCountChanged.emit()
            self._settle(message, reload=self._duplicate_only)
            return
        if action == "scan_ai_tags":
            self._release_exclusive()
            if selection is None:
                self._status = message
                self.changed.emit()
                return
            self._user_tags_loaded = False
            self._settle(message, reload=bool(self._ai_category))
            return
        if action == "clear_ai_tags":
            self._release_exclusive()
            if selection is None:
                self._status = message
                self.changed.emit()
                return
            if self._selected and "tags" in self._selected:
                self._selected["tags"] = [
                    t for t in self._selected.get("tags", [])
                    if not (isinstance(t, dict) and t.get("source") == "ai")
                ]
            filtered = bool(self._ai_category)
            if self._ai_category and self._ai_origin == "ai":
                self._ai_category = self._ai_origin = ""
            if isinstance(selection, tuple) and len(selection) == 2:
                _count, remaining = selection
                self._user_tags = list(remaining) if isinstance(remaining, (list, tuple)) else []
                self._user_tags_loaded = True
                self.userTagsChanged.emit()
                remaining_names = {
                    (str(item.get("tag", "")) if isinstance(item, dict) else str(item))
                    for item in self._user_tags
                }
                if self._user_tag and self._user_tag not in remaining_names:
                    self._user_tag = ""
                    self.userTagFilterChanged.emit()
            else:
                self._user_tags_loaded = False
            self._settle(message, reload=filtered)
            return
        if action == "reselect":
            if (serial == self._selection_serial and isinstance(selection, dict)
                    and selection.get("key") == self.selectedKey):
                self._selected = selection
                self.changed.emit()
            return
        if serial != self._selection_serial:
            return
        if action == "select":
            self._selected = selection or {}
        self._busy = False
        # Browsing while a scan runs must not replace the scan's progress line.
        if not (self._exclusive and action in {"select", "open", "reveal", "source"} and not message):
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
            self._interactive.clear()
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
        if (self._worker.is_alive() or self._interactive_worker.is_alive() or self.model._worker.is_alive() or
                not self.provider.wait_for_idle(timeout=0) or not self.provider.join_workers(timeout=0)):
            return
        self._shutdown_timer.stop()
        self._shutdown_complete = True
        self.shutdownFinished.emit()
