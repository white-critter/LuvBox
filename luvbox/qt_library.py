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
import inspect
import time
import webbrowser

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, Property, Qt, QTimer, QUrl, Signal, Slot

# Names and the shared tag list only: the classifier itself (numpy, onnxruntime) and the
# learning (tag_learner) are loaded by the work that classifies.
from .ai_profile import AiProfileError, AiProfileStore, example_id_for_x, output_key, valid_tag
from .clip_model import ACTIVE_MODEL_ID, CATEGORY_NAMES
from .history import HistoryStore, _normalize_tag, page_cursor
from .preview import Thumbnail, thumbnail
from .qt_gallery import GalleryModel, SafeThumbnailProvider
from .recycling import file_signature
from .storage import records_root, resolve_storage
from .thumbcache import cloud_only
from .user_errors import UserFacingError, UserRuntimeError, safe_ui_error


def _safe_ui_error(exc, fallback="작업 중 문제가 생겼습니다. 잠시 후 다시 시도해 주세요."):
    return safe_ui_error(exc, fallback)


_KEY = re.compile(r"g([0-9]+)_(author|date|flat)_([0-9]{1,20})_([0-9]{1,5})_(photo|video|animated_gif)\Z")
_ORDERS = {"saved", "posted", "posted_oldest", "liked"}
_KINDS = {"all", "photo", "video", "animated_gif"}
_MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4"}
_DRIVE_MISSING = "파일을 열 수 없습니다. 저장 드라이브를 연결해 주세요."
_FILE_MISSING = "파일이 없습니다. 저장 위치를 확인해 주세요."
_CLOUD_ONLY = "이 파일은 클라우드에만 있습니다. 탐색기에서 받은 뒤 다시 열 수 있습니다."
_POSTED = re.compile(r"(\d{4}-\d\d-\d\d)T(\d\d:\d\d)")
MAX_SELECTION = 2000
_EXCLUSIVE_ACTIONS = {"scan_duplicates", "scan_ai_tags", "auto_ai_tags", "clear_ai_tags", "teach", "reclassify",
                      "sync_folder"}
_EXCLUSIVE_KIND = {
    "scan_duplicates": "duplicates", "scan_ai_tags": "ai", "auto_ai_tags": "ai", "clear_ai_tags": "clear",
    "teach": "teach", "reclassify": "ai", "sync_folder": "folder",
}
_EXCLUSIVE_BUSY = {
    "ai": "AI 자동분류가 끝난 뒤 다시 실행할 수 있습니다.",
    "duplicates": "중복 검사가 끝난 뒤 다시 실행할 수 있습니다.",
    "clear": "AI 태그 초기화가 끝난 뒤 다시 실행할 수 있습니다.",
    "teach": "분류를 반영한 뒤 다시 실행할 수 있습니다.",
    "folder": "폴더를 다 읽은 뒤 다시 실행할 수 있습니다.",
}
_HOLD_REASON = {
    "ai": "AI 자동분류가 끝난 뒤 태그 수정과 휴지통 이동을 할 수 있습니다.",
    "clear": "AI 태그 초기화가 끝난 뒤 태그 수정과 휴지통 이동을 할 수 있습니다.",
    "duplicates": "중복 검사가 끝난 뒤 사본 선택과 휴지통 이동을 할 수 있습니다.",
    "teach": "분류가 반영된 뒤 태그 수정과 휴지통 이동을 할 수 있습니다.",
    "folder": "폴더를 다 읽은 뒤 휴지통 이동을 할 수 있습니다.",
}
_CATALOG_MOVED = "저장 폴더가 바뀌어 작업을 멈췄습니다. 이미 처리한 내용은 유지됩니다."
_TAG_LOCKED = "AI 작업이 끝난 뒤 태그를 바꿀 수 있습니다."
_FAVORITE_ERROR = "즐겨찾기를 저장하지 못했습니다. 잠시 뒤 다시 시도해 주세요."
_DISTINCT_ERROR = "중복 아님 표시를 저장하지 못했습니다. 목록을 새로고침한 뒤 다시 시도해 주세요."
# How the status lines of a running classification begin (see _perform).
_AI_PROGRESS = ("AI 자동분류 중", "분류할 사진을 확인하는 중", "비슷한 사진에 분류를 맞추는 중")
_STATISTICS_ERROR = "통계를 불러오지 못했습니다. 저장 폴더를 확인해 주세요."


class ManifestError(UserFacingError, ValueError):
    """Safe user-facing rejection from building a deletion or alike manifest."""


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


def _open_profile():
    """The shared AI profile, or None when the app data folder cannot hold it."""
    try:
        return AiProfileStore()
    except (AiProfileError, OSError):
        return None


def _active_ai_names():
    """The switched-on AI tags, or None when the shared profile cannot be read (then nothing is hidden)."""
    profile = _open_profile()
    if profile is None:
        return None
    try:
        with closing(profile):
            return set(profile.active_tag_names())
    except AiProfileError:
        return None


def _shown_tags(rows, active):
    """A photo's tags as shown: the AI's guesses of a tag switched off or deleted (here or in the other
    tab) are left out until the next classification takes them away; the person's own tags and
    answers stay."""
    if active is None:
        return list(rows)
    return [row for row in rows if row.get("source") != "ai" or row.get("tag") in active]


def _profile_scope(request) -> tuple[str, str]:
    """``(output key, account)`` naming this library in the shared AI profile."""
    return output_key(request.output_dir), str(request.account or "").strip().lstrip("@").lower()


def _x_scope(request):
    """``(kind, scope, example id of a record)``: this library in the shared AI profile."""
    key, account = _profile_scope(request)
    return "x", f"{key}/{account}", (lambda ident: example_id_for_x(key, account, *ident))


def _sync_examples(store, profile, request, tags=None) -> bool:
    """Make this library's examples in the shared profile match what its photos say now (ai_pipeline)."""
    from . import ai_pipeline

    kind, scope, example_id = _x_scope(request)
    return ai_pipeline.sync_examples(store, profile, kind, scope, example_id, tags)


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
        self._known_total = 0
        self._scan_note = ""
        self._ai_wrote = False
        self._ai_changed = 0
        self._ai_had_embeddings = False
        self._dup_synced_changes = False
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
            self._known_total = 0

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
        with self._condition:
            return self._request if self._epoch == epoch else None

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
                media_type = "all" if request.duplicate_only else request.media_type
                after = self._cursor if offset and offset == self._cursor_offset else None
                if grouped:
                    # Each group together, the photo to keep first. The chosen sort order and media type do not apply.
                    dup_kwargs = ({"count_total": False}
                                  if (after is not None and type(store).duplicate_page is HistoryStore.duplicate_page)
                                  else {})
                    page = store.duplicate_page(0 if after is not None else offset // limit, limit,
                                                media_type, request.query,
                                                period=period, author=request.author, hashtag=request.hashtag,
                                                favorite_only=request.favorite_only, user_tag=request.user_tag,
                                                ai_category=getattr(request, "ai_category", ""),
                                                ai_origin=getattr(request, "ai_origin", ""), after=after,
                                                **dup_kwargs)
                else:
                    page_kwargs = {"count_total": False} if (after is not None and type(store).record_page is HistoryStore.record_page) else {}
                    page = store.record_page(0 if after is not None else offset // limit, limit,
                                             media_type, request.query,
                                             order=request.order, author=request.author, include_deleted=False,
                                             period=period, after=after, hashtag=request.hashtag,
                                             favorite_only=request.favorite_only, user_tag=request.user_tag,
                                             duplicate_only=request.duplicate_only,
                                             ai_category=getattr(request, "ai_category", ""),
                                             ai_origin=getattr(request, "ai_origin", ""),
                                             **page_kwargs)
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
                        # copy: a photo before it in the group has its bytes. Others that are not
                        # kept are only alike (another size or quality) and are not offered as copies.
                        entry.update(group=int(row.get("cluster_id") or 0), keeper=row.get("cluster_rank") == 1,
                                     copy=bool(row.get("cluster_copy")),
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
                    if last is not None:
                        self._cursor = ((int(last["cluster_id"]), int(last["cluster_rank"]))
                                        if grouped else page_cursor(last, request.order))
                        self._cursor_offset = offset + len(rows)
                    if after is None:
                        self._known_total = int(page.get("total") or 0)
                    known_total = self._known_total
                if page.get("total") is not None:
                    total = int(page["total"])
                elif not page.get("has_more"):
                    total = offset + len(rows)
                else:
                    total = max(known_total, offset + len(rows) + 1)
                return total, rows
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
        """Files under each switched-on AI tag, in the profile's order, zero included."""
        request = self.current(generation)
        if request is None:
            return []
        profile = _open_profile()
        if profile is None:
            return []
        with closing(profile):
            names = profile.active_tag_names()
        try:
            with closing(_ReadOnlyHistory(request.location())) as store:
                found = store.ai_tag_counts(names) if hasattr(store, "ai_tag_counts") else {}
        except Exception:
            found = {}
        return [{"category": name, "count": int(found.get(name, 0))} for name in names]

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
                names = list(CATEGORY_NAMES)
                profile = _open_profile()
                if profile is not None:
                    with closing(profile):
                        names = profile.active_tag_names()
                ai_counts = store.ai_tag_counts(names) if hasattr(store, "ai_tag_counts") else {}
                ai_list = [{"category": name, "count": int(ai_counts[name])}
                           for name in names if ai_counts.get(name, 0) > 0]
                ai_list.sort(key=lambda item: (-item["count"], item["category"]))
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
                    "ai_categories": ai_list,
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
            active = _active_ai_names()
            tags = _shown_tags(store.item_tags(row["folder_mode"], row["tweet_id"], row["num"], row["media_type"]),
                               active)
            learned = (store.classification_of(row["folder_mode"], row["tweet_id"], row["num"], row["media_type"])
                       if hasattr(store, "classification_of") else {})
            ai_tags = _shown_tags(store.ai_tags_of(row["folder_mode"], row["tweet_id"], row["num"], row["media_type"])
                                  if hasattr(store, "ai_tags_of") else [], active)
            if learned.get("category") and not any(item["tag"] == learned["category"] for item in ai_tags):
                learned = {**learned, "category": ai_tags[0]["tag"] if ai_tags else "",
                           "confirmed": bool(ai_tags and ai_tags[0].get("confirmed"))}
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
                    "ai_tags": ai_tags,
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
                added = store.add_tag(match[2], match[3], int(match[4]), match[5], tag)
                if added:
                    self._teach_profile(store, request, tag)
                return added
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
                    count = store.add_tags_batch(identities, tag)
                else:
                    count = sum(1 for ident in identities if store.add_tag(ident[0], ident[1], ident[2], ident[3], tag))
                if count:
                    self._teach_profile(store, request, tag)
                return count
        except Exception:
            return 0

    def remove_tag_detailed(self, key, generation, tag):
        request = self.current(generation)
        if request is None:
            return False, False
        match = _KEY.fullmatch(key) if isinstance(key, str) else None
        if match is None or int(match[1]) != request.generation:
            return False, False
        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                remover = getattr(store, "remove_tag_detailed", None)
                if callable(remover):
                    removed, confirmed_restored = remover(match[2], match[3], int(match[4]), match[5], tag)
                else:
                    removed = bool(store.remove_tag(match[2], match[3], int(match[4]), match[5], tag))
                    confirmed_restored = False
                if removed:
                    self._teach_profile(store, request, tag)
                return removed, confirmed_restored
        except Exception:
            return False, False

    def remove_tag(self, key, generation, tag):
        return self.remove_tag_detailed(key, generation, tag)[0]

    @staticmethod
    def _teach_profile(store, request, tag):
        """A tag typed or taken away on a photo: if it is an AI tag, its examples follow at once.

        A tag the person typed is a "yes"; an AI tag taken away is a "not this".
        Learning from them and showing the result on other photos waits for the
        next classification (or "지금 다시 분류"), so the edit itself stays quick.
        """
        clean = valid_tag(tag)
        profile = _open_profile() if clean else None
        if profile is None:
            return
        try:
            with closing(profile):
                if clean in profile.known_tag_names():
                    _sync_examples(store, profile, request, [clean])
        except Exception:
            pass      # the next classification syncs every example again

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
                raise ScanCancelled("중복 검사를 중단했습니다.")
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
                seen_files = set()
                # Versions up to 1.9.3 hashed every record, so a file with a
                # record per folder layout was stored twice and paired with itself.
                if hasattr(store, "forget_unlisted_hashes"):
                    store.forget_unlisted_hashes()
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
                    # The same file reached by another name (another spelling of
                    # its path, a hard link) is not a copy of itself. A file
                    # system that reports no file number cannot say.
                    volume, number = signature.split(":")[:2]
                    if number != "0":
                        if (volume, number) in seen_files:
                            if ident in cached:
                                stale_hashes.append(ident)
                            continue
                        seen_files.add((volume, number))
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
            raise UserRuntimeError(
                "중복 검사를 끝내지 못했습니다. 저장 폴더를 확인한 뒤 다시 시도해 주세요."
            ) from exc

    def auto_ai_pending(self, generation):
        """Whether an automatic classification has anything to do right now.

        Photos to encode count only when the model is on this PC: a run that
        starts on its own never downloads it. Cloud-only and missing files do
        not count, as the scan does not read them, and neither do photos it
        could not decode. Stored features count when no classification has
        looked at them yet, or when the AI tags or what they learned changed
        since this library was last scored (in any library, as the profile is
        shared); scoring them needs no model file.
        """
        request = self.current(generation)
        if request is None:
            return False
        from .clip_model import model_file_present
        try:
            with closing(_ReadOnlyHistory(request.location())) as store:
                store._folder_cache = {}
                if not hasattr(store, "count_embeddings"):
                    return False
                if store.count_embeddings(ACTIVE_MODEL_ID, unlabeled=True):
                    return True
                if store.count_embeddings(ACTIVE_MODEL_ID):
                    profile = _open_profile()
                    if profile is not None:
                        with closing(profile):
                            version = profile.rules_version()
                        if store.ai_scored_state() != (str(version), ACTIVE_MODEL_ID):
                            return True
                if not model_file_present():
                    return False
                signatures = store.unreadable_signatures() if hasattr(store, "unreadable_signatures") else {}
                for rec in store.photos_without_embeddings(ACTIVE_MODEL_ID):
                    if self.current(generation) is None:
                        return False
                    path = self._path(store, rec)
                    if path is None:
                        continue
                    try:
                        info = path.stat()
                    except OSError:
                        continue
                    if cloud_only(path, status=info):
                        continue
                    ident = (rec["folder_mode"], rec["tweet_id"], rec["num"], rec["media_type"])
                    if signatures.get(ident) != (int(info.st_size), int(info.st_mtime_ns)):
                        return True
        except Exception:
            return False
        return False

    def scan_ai_tags(self, generation, progress_callback=None, status_callback=None, epoch=None, download=True,
                     encode=True):
        """Encode photos that have no SigLIP 2 feature yet, then give the library its AI tags.

        ``download=False`` is the run that starts on its own after a save: it
        uses the model only when it is already on this PC. ``encode=False``
        only scores the stored features again (after the AI tags or what they
        learned changed); it never loads the vision model.

        The order matters for what a person taught: the photos they answered
        about or tagged are encoded first, the shared profile learns from them,
        and only then are the other photos scored, batch by batch, so tags
        appear while the rest is still being encoded. Returns how many photos
        got new AI tags.
        """
        self._scan_note = ""
        self._ai_wrote = False
        self._ai_changed = 0
        request = self._catalog_request(generation, epoch)
        if request is None:
            return 0
        from contextlib import ExitStack
        from . import ai_pipeline
        from .duplicates import ScanCancelled

        run = ai_pipeline.Run()
        release_session = None
        session_use = None
        if encode:
            from .clip_classifier import is_available, release_session, session_use

        def check_cancel():
            if self._ai_cancel.is_set():
                kept = f" 이미 분류된 {run.tagged:,}장은 유지됩니다." if run.tagged else ""
                raise ScanCancelled(f"AI 자동분류를 중단했습니다.{kept}")
            if self._catalog_request(generation, epoch) is None:
                raise ScanCancelled(_CATALOG_MOVED)

        def remember_note(count):
            if count:
                self._scan_note = f" 읽을 수 없는 사진 {count:,}장은 제외했습니다."

        profile = _open_profile()
        if profile is None:
            raise UserRuntimeError("AI 태그 설정을 열지 못했습니다. LuvBox 기록 폴더의 권한을 확인해 주세요.")
        try:
            with closing(profile), closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                store._folder_cache = {}
                records = store.photos_without_embeddings(ACTIVE_MODEL_ID) if encode else []
                signatures = store.unreadable_signatures() if records else {}
                valid_items = []
                skipped_unreadable = 0
                record_count = len(records)
                for idx, rec in enumerate(records):
                    check_cancel()
                    if status_callback and record_count and (idx % 200 == 0 or idx == record_count - 1):
                        status_callback(f"분류할 사진을 확인하는 중… ({idx + 1:,} / {record_count:,})")
                    path = self._path(store, rec)
                    if path is None:
                        continue
                    ident = (rec["folder_mode"], rec["tweet_id"], rec["num"], rec["media_type"])
                    try:
                        info = path.stat()
                        if cloud_only(path, status=info):
                            continue
                        signature = (int(info.st_size), int(info.st_mtime_ns))
                    except OSError:
                        continue
                    if signatures.get(ident) == signature:
                        skipped_unreadable += 1
                        continue
                    valid_items.append((ident, path))
                if valid_items and not download and not is_available():
                    valid_items = []          # no model here, and this run does not fetch one
                remember_note(skipped_unreadable)
                kind, scope, example_id = _x_scope(request)
                try:
                    with ExitStack() as scope_guard:
                        if session_use is not None and valid_items:
                            scope_guard.enter_context(session_use())
                        ai_pipeline.classify(store, profile, kind=kind, scope=scope, example_id=example_id,
                                             items=valid_items, skipped=skipped_unreadable, check_cancel=check_cancel,
                                             progress=progress_callback, status=status_callback, download=download,
                                             run=run)
                finally:
                    self._ai_wrote = run.wrote
                    self._ai_changed = run.changed
                remember_note(skipped_unreadable + run.unreadable)
                return run.tagged if encode else run.changed
        except (RuntimeError, ScanCancelled):
            raise
        except Exception as exc:
            raise UserRuntimeError(
                "AI 자동분류를 끝내지 못했습니다. 저장 폴더를 확인한 뒤 다시 시도해 주세요."
            ) from exc
        finally:
            if release_session is not None:
                release_session()

    def reclassify(self, generation, progress_callback=None, status_callback=None, epoch=None):
        """Score every stored feature again with the AI tags as they are now. No photo is decoded."""
        request = self._catalog_request(generation, epoch)
        if request is None:
            return 0
        self._ai_had_embeddings = False
        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                self._ai_had_embeddings = store.count_embeddings(ACTIVE_MODEL_ID) > 0
                store.set_ai_scored_state(-1, ACTIVE_MODEL_ID)      # everything, not only what is new
        except Exception as exc:
            raise UserRuntimeError("AI 태그를 다시 매기지 못했습니다. 저장 폴더를 확인한 뒤 다시 시도해 주세요.") from exc
        return self.scan_ai_tags(generation, progress_callback=progress_callback, status_callback=status_callback,
                                 epoch=epoch, download=False, encode=False)

    def clear_ai_tags(self, generation, epoch=None):
        request = self._catalog_request(generation, epoch)
        if request is None:
            return 0
        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                return store.clear_ai_tags()
        except Exception as exc:
            raise UserRuntimeError(
                "AI 태그를 초기화하지 못했습니다. 저장 폴더를 확인한 뒤 다시 시도해 주세요."
            ) from exc

    def teach_labels(self, generation, identities, category, epoch=None, progress_callback=None, status_callback=None):
        """The single-label teaching of earlier versions: "yes" for one tag, or "" for none of the shown ones."""
        return self.teach_tag(generation, identities, category, bool(category), epoch=epoch,
                              progress_callback=progress_callback, status_callback=status_callback)

    def teach_tag(self, generation, identities, tag, positive, epoch=None, progress_callback=None,
                  status_callback=None):
        """Remember "yes, this" or "not this" about one AI tag on these photos, learn from it, and spread it.

        ``tag=""`` with ``positive=False`` turns down every AI tag the photos
        show. Photos without a feature are encoded first (the model is fetched
        if needed); a photo that cannot be encoded is not taught. Returns
        ``(taught, propagated)``: photos taught, and other photos whose AI tags
        changed because of it. ``progress_callback(done, total)`` counts the
        selected photos, including ones that already have a stored feature.
        """
        request = self._catalog_request(generation, epoch)
        if request is None:
            return 0, 0
        from . import ai_pipeline
        from .clip_classifier import release_session
        from .duplicates import ScanCancelled
        clean = valid_tag(tag)
        if not clean and (tag or positive):
            return 0, 0

        def check_cancel():
            if self._ai_cancel.is_set() or self._catalog_request(generation, epoch) is None:
                raise ScanCancelled("분류 가르치기를 중단했습니다.")

        profile = _open_profile()
        if profile is None:
            raise UserRuntimeError("AI 태그 설정을 열지 못했습니다. LuvBox 기록 폴더의 권한을 확인해 주세요.")
        try:
            with closing(profile), closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
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
                known = store.embeddings_for(present, ACTIVE_MODEL_ID) if present else {}
                missing = []
                for ident in present:
                    if ident in known:
                        continue
                    path = self._path(store, by_ident[ident])
                    if path is not None and path.is_file():
                        missing.append((ident, path))
                ai_pipeline.encode_missing_for_teach(
                    store, missing, total=len(present), check_cancel=check_cancel,
                    progress_callback=progress_callback, status_callback=status_callback,
                )
                check_cancel()
                refreshed = store.embeddings_for(present, ACTIVE_MODEL_ID) if present else {}
                teachable = [ident for ident in present if ident in refreshed]
                if not teachable:
                    return 0, 0
                kind, scope, example_id = _x_scope(request)
                return ai_pipeline.teach(
                    store, profile, kind=kind, scope=scope, example_id=example_id,
                    idents=teachable, tag=clean, positive=positive,
                    check_stop=lambda: self._ai_cancel.is_set() or self._catalog_request(generation, epoch) is None,
                )
        except (RuntimeError, ScanCancelled):
            raise
        except Exception as exc:
            raise UserRuntimeError(
                "분류를 저장하지 못했습니다. 저장 폴더를 확인한 뒤 다시 시도해 주세요."
            ) from exc
        finally:
            release_session()

    def sync_tag_examples(self, generation, tags, epoch=None) -> bool:
        """After a tag edit: bring this library's examples of these AI tags up to date in the profile."""
        request = self._catalog_request(generation, epoch) if epoch is not None else self.current(generation)
        names = [valid_tag(tag) for tag in tags if valid_tag(tag)]
        if request is None or not names:
            return False
        profile = _open_profile()
        if profile is None:
            return False
        try:
            with closing(profile), closing(_ReadOnlyHistory(request.location())) as store:
                known = set(profile.known_tag_names())
                names = [tag for tag in names if tag in known]
                return _sync_examples(store, profile, request, names) if names else False
        except Exception:
            return False

    def duplicate_copy_keys(self, generation):
        request = self.current(generation)
        if request is None:
            return []
        period = ((request.date_field, request.date_from, request.date_to)
                  if request.date_from or request.date_to else None)
        media_type = "all" if request.duplicate_only else request.media_type
        filtered = (media_type != "all" or bool(request.query) or period is not None
                    or bool(request.author) or bool(request.hashtag) or bool(request.favorite_only)
                    or bool(request.user_tag) or bool(getattr(request, "ai_category", ""))
                    or bool(getattr(request, "ai_origin", "")))
        try:
            with closing(_ReadOnlyHistory(request.location())) as store:
                if hasattr(store, "duplicate_copy_identities"):
                    stored = (store.duplicate_copy_identities(
                                  media_type, request.query, period=period, author=request.author,
                                  hashtag=request.hashtag, favorite_only=request.favorite_only,
                                  user_tag=request.user_tag, ai_category=getattr(request, "ai_category", ""),
                                  ai_origin=getattr(request, "ai_origin", ""))
                              if filtered else store.duplicate_copy_identities())
                else:
                    stored = None
                if stored is not None:
                    return [f"g{generation}_{item[0]}_{item[1]}_{item[2]}_{item[3]}" for item in stored]
                from .duplicates import cluster_duplicates
                hashes = store.get_image_hashes()
                favs = store.favorite_identities()
                allowed = None
                if filtered:
                    allowed = {
                        (row["folder_mode"], row["tweet_id"], row["num"], row["media_type"])
                        for row in store.records(
                            limit=10000, media_type=media_type, query=request.query,
                            order="saved", period=period, author=request.author, include_deleted=False,
                            hashtag=request.hashtag, favorite_only=request.favorite_only,
                            user_tag=request.user_tag, ai_category=getattr(request, "ai_category", ""),
                            ai_origin=getattr(request, "ai_origin", ""))
                    }
                items = [
                    {"key": f"g{generation}_{k[0]}_{k[1]}_{k[2]}_{k[3]}",
                     "ident": k, "filesize": v[0], "file_hash": v[1], "dhash": v[2],
                     "favorite": k in favs}
                    for k, v in hashes.items()
                ]
                clusters = cluster_duplicates(items, max_distance=4)
                copy_keys = []
                for group in clusters:
                    # As the stored groups offer them: only a file whose bytes a photo before it has.
                    seen = set()
                    for member in group:
                        digest = member["file_hash"]
                        if digest and digest in seen and (allowed is None or member["ident"] in allowed):
                            copy_keys.append(member["key"])
                        seen.add(digest)
                return copy_keys
        except FileNotFoundError:
            return []
        except Exception:
            return []

    def mark_group_distinct(self, key, generation):
        """The owner says the photos grouped with this one are not duplicates.

        Returns the files of that group (what ``undo_group_distinct`` takes), or
        None when nothing was changed.
        """
        request = self.current(generation)
        if request is None:
            return None
        match = _KEY.fullmatch(key) if isinstance(key, str) else None
        if match is None or int(match[1]) != request.generation:
            return None
        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                return store.set_duplicate_group_distinct(match[2], match[3], int(match[4]), match[5])
        except Exception:
            return None

    def mark_photos_distinct(self, keys, generation):
        """The owner says these photos are no duplicates of the rest of their group.

        Returns the mark (what ``undo_photos_distinct`` takes), or None when
        nothing was changed: the photos are not of one group, or are all of it.
        """
        request = self.current(generation)
        if request is None or not keys:
            return None
        identities = []
        for key in keys:
            match = _KEY.fullmatch(key) if isinstance(key, str) else None
            if match is None or int(match[1]) != request.generation:
                return None
            identities.append((match[2], match[3], int(match[4]), match[5]))
        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                return store.set_duplicate_photos_distinct(identities)
        except Exception:
            return None

    def undo_photos_distinct(self, mark, generation):
        request = self.current(generation)
        if request is None or not mark:
            return False
        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                return bool(store.undo_duplicate_photos_distinct(mark))
        except Exception:
            return False

    def undo_group_distinct(self, members, generation):
        request = self.current(generation)
        if request is None or not members:
            return False
        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                return bool(store.undo_duplicates_distinct(members))
        except Exception:
            return False

    def clear_distinct(self, generation):
        """Forget every "not duplicates" mark; how many there were, or None when it could not be saved."""
        request = self.current(generation)
        if request is None:
            return None
        try:
            with closing(HistoryStore.for_storage(request.location(), create_media=False)) as store:
                return int(store.clear_duplicates_distinct())
        except Exception:
            return None

    def distinct_counts(self, generation=None):
        request = self.current(generation)
        if request is None:
            return 0
        try:
            with closing(_ReadOnlyHistory(request.location())) as store:
                return int(store.distinct_duplicate_count()) if hasattr(store, "distinct_duplicate_count") else 0
        except Exception:
            return 0

    def deletion_manifest(self, keys):
        """Capture displayed catalog identities without accepting paths from QML."""
        if (not isinstance(keys, (list, tuple)) or not 1 <= len(keys) <= MAX_SELECTION
                or any(not isinstance(key, str) for key in keys)):
            raise ManifestError("휴지통으로 보낼 사진을 1~2,000개 선택해 주세요.")
        keys = list(dict.fromkeys(keys))
        with self._condition:
            request = self._request
            if request is None or any(key not in self._served for key in keys):
                raise ManifestError("목록이 바뀌었습니다. 사진을 다시 선택해 주세요.")
            displayed = {key: self._served[key] for key in keys}
        rows = []
        kept = 0
        with closing(_ReadOnlyHistory(request.location())) as store:
            store._folder_cache = {}
            for key in keys:
                row = self._row(store, key, request)
                path = self._path(store, row) if row is not None else None
                previous_path, previous_signature = displayed[key]
                if path is None or str(path) != previous_path or previous_signature is None:
                    raise ManifestError("선택한 파일을 확인하지 못했습니다. 목록을 새로고침해 주세요.")
                signature = file_signature(path)
                if signature != previous_signature:
                    raise ManifestError("선택한 뒤 파일이 바뀌었습니다. 목록을 새로고침해 주세요.")
                rows.append(dict(row, _delete_signature=signature, _key=key))
            if request.duplicate_only:
                rows, kept = self._copies_only(store, rows)
        with self._condition:
            if self._request is not request:
                raise ManifestError("목록이 바뀌었습니다. 사진을 다시 선택해 주세요.")
        return {"location": request.location(), "rows": rows, "generation": request.generation, "kept": kept}

    @staticmethod
    def _copies_only(store, rows):
        """Mark what the duplicate view sends to the Recycle Bin as copies.

        Each row gets the other photos of its group that stay (``_copy_keepers``):
        the deletion worker moves a copy only while one of those is, at that
        moment, a different file with the same content. A group chosen whole
        keeps its first photo, so the view can never remove the last one.
        A photo the view shows as only alike ('비슷함': neither kept nor a copy)
        is marked ``_copy_near``; it stays unless a file with its bytes does.
        Returns the rows to move and how many chosen photos were kept back.
        """
        rule = store.duplicate_rule() if hasattr(store, "duplicate_rule") else "exact"
        groups = store.duplicate_members() if hasattr(store, "duplicate_members") else {}
        member_of = {member["identity"]: number for number, members in groups.items() for member in members}
        first = {members[0]["identity"] for members in groups.values() if members}
        copies = set(store.duplicate_copy_identities() or ()) if groups else set()
        chosen = {(row["folder_mode"], row["tweet_id"], row["num"], row["media_type"]) for row in rows}
        spared = set()
        for members in groups.values():
            if members and all(member["identity"] in chosen for member in members):
                spared.add(members[0]["identity"])      # the photo the view calls "남김"
        marked = []
        for row in rows:
            identity = (row["folder_mode"], row["tweet_id"], row["num"], row["media_type"])
            if identity in spared:
                continue
            keepers = [member["path"] for member in groups.get(member_of.get(identity), ())
                       if member["identity"] != identity and (
                           member["identity"] not in chosen
                           or member["identity"] in spared
                           or (identity in copies and member["identity"] not in copies))]
            near = identity in member_of and identity not in first and identity not in copies
            marked.append(dict(row, _copy_keepers=keepers, _copy_rule=rule or "exact", _copy_near=near))
        return marked, len(spared)

    def alike_manifest(self, key):
        """One photo the duplicate view shows only as alike, beside the photo its group keeps.

        For a person to compare the two and send this one alone to the Recycle
        Bin: '비슷함' photos have bytes of their own, so no copy check lets
        them go (``_copies_only``). The row carries the photo kept
        (``_alike_keeper``) and its file signature as shown, and the deletion
        worker checks both again right before the move
        (``deletion.kept_alike``). Refused, with the reason, for a photo kept
        or offered as a copy, one outside a group, a file changed or missing,
        a photo kept that is gone or is this very file, or a pair the near
        rule no longer finds to be one picture.
        """
        from .duplicates import nearly_identical, pixel_vector, same_in_detail

        if not isinstance(key, str) or not key:
            raise ManifestError("비교할 사진을 다시 골라 주세요.")
        with self._condition:
            request = self._request
            if request is None or not request.duplicate_only or key not in self._served:
                raise ManifestError("목록이 바뀌었습니다. 사진을 다시 골라 주세요.")
            previous_path, previous_signature = self._served[key]
        with closing(_ReadOnlyHistory(request.location())) as store:
            store._folder_cache = {}
            row = self._row(store, key, request)
            path = self._path(store, row) if row is not None else None
            if path is None or str(path) != previous_path or previous_signature is None:
                raise ManifestError("선택한 파일을 확인하지 못했습니다. 목록을 새로고침해 주세요.")
            signature = file_signature(path)
            if signature != previous_signature:
                raise ManifestError("선택한 뒤 파일이 바뀌었습니다. 목록을 새로고침해 주세요.")
            identity = (row["folder_mode"], row["tweet_id"], row["num"], row["media_type"])
            groups = store.duplicate_members() if hasattr(store, "duplicate_members") else {}
            members = next((members for members in groups.values()
                            if any(member["identity"] == identity for member in members)), None)
            copies = set(store.duplicate_copy_identities() or ()) if members else set()
            if not members or members[0]["identity"] == identity or identity in copies:
                raise ManifestError("‘비슷함’으로 표시된 사진만 남길 사진과 비교해 휴지통으로 보낼 수 있습니다.")
            mode, tweet, number, kind = members[0]["identity"]
            keeper_row = store.media_record(mode, {"tweet_id": tweet, "num": number, "type": kind})
            keeper = self._path(store, keeper_row) if keeper_row is not None else None
            if keeper is None:
                raise ManifestError("남길 사진 파일이 없어 이 사진을 보낼 수 없습니다.")
            keeper_signature = file_signature(keeper)
            if (os.path.normcase(os.path.realpath(keeper)) == os.path.normcase(os.path.realpath(path))
                    or (keeper_signature[1] and keeper_signature[:2] == signature[:2])):
                raise ManifestError("남길 사진이 이 사진과 같은 파일이라 보낼 수 없습니다.")
            if not (nearly_identical(pixel_vector(path), pixel_vector(keeper)) and same_in_detail(path, keeper)):
                # In a group joined through a third photo, this one can be the same picture as that
                # photo and still not pass beside the photo kept: say what can be done either way.
                raise ManifestError("남길 사진과 같은 사진으로 다시 확인되지 않아 보낼 수 없습니다. "
                                 "‘중복 아님’으로 표시하거나 갤러리에서 휴지통으로 보내세요.")
            if file_signature(path) != signature or file_signature(keeper) != keeper_signature:
                raise ManifestError("비교하는 동안 파일이 바뀌었습니다. 목록을 새로고침해 주세요.")
            favorites = store.favorite_identities_among([identity, members[0]["identity"]])
            shown = {"alike": self._compared(path, row, signature, identity in favorites),
                     "keeper": self._compared(keeper, keeper_row, keeper_signature,
                                              members[0]["identity"] in favorites)}
        with self._condition:
            if self._request is not request:
                raise ManifestError("목록이 바뀌었습니다. 사진을 다시 골라 주세요.")
        marked = dict(row, _delete_signature=signature, _key=key,
                      _alike_keeper=str(keeper), _alike_keeper_signature=keeper_signature)
        return {"location": request.location(), "rows": [marked], "generation": request.generation,
                "kept": 0, "alike": shown}

    @staticmethod
    def _compared(path, row, signature, favorite):
        """What the comparison shows of one photo: the picture, its size in pixels and bytes, who and when."""
        from .preview import photo_info

        try:
            size = photo_info(path) if row.get("media_type") == "photo" else {}
        except OSError:
            size = {}
        width, height = size.get("pixel_width", 0), size.get("pixel_height", 0)
        author = row.get("author") or ""
        posted = _posted(row.get("posted_at"), time=True)
        return {"url": QUrl.fromLocalFile(str(path)).toString(), "name": path.name,
                "pixels": f"{width}×{height}" if width and height else "",
                "bytes": _format_size(signature[2]),
                "caption": " · ".join(part for part in (("@" + author) if author else "", posted) if part),
                "favorite": bool(favorite)}

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
            self._known_total = max(0, self._known_total - dropped)
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


class DuplicateGroupModel(QAbstractListModel):
    """The duplicate groups for the card list.

    A plain list given to QML rebuilds every card, and the list returns to its
    top, whenever one group changes. Here a group that left is removed, a group
    that changed is redrawn, and the rest stays where the person was looking.
    """

    FIELDS = ("group", "number", "size", "items")
    _FIRST = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._groups = []

    def roleNames(self):
        return {self._FIRST + index: name.encode() for index, name in enumerate(self.FIELDS)}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._groups)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._groups):
            return None
        field = role - self._FIRST
        return self._groups[index.row()][self.FIELDS[field]] if 0 <= field < len(self.FIELDS) else None

    def adopt(self, groups):
        """Show these groups, touching only what differs from what is shown.

        Groups are known by their number in the catalog. When the ones that stay
        keep their order (something was put away, a mark was taken back, more
        was read), rows are removed, added and redrawn in place. Any other
        change is another list and starts from its top.
        """
        groups = list(groups)
        old = [group["group"] for group in self._groups]
        new = [group["group"] for group in groups]
        before, after = set(old), set(new)
        kept = [number for number in old if number in after]
        if (not kept or len(after) != len(new) or len(before) != len(old)
                or [number for number in new if number in before] != kept):
            if groups or self._groups:
                self.beginResetModel()
                self._groups = groups
                self.endResetModel()
            return
        for row in range(len(self._groups) - 1, -1, -1):
            if self._groups[row]["group"] not in after:
                self.beginRemoveRows(QModelIndex(), row, row)
                del self._groups[row]
                self.endRemoveRows()
        row = 0
        while row < len(groups):
            if row < len(self._groups) and self._groups[row]["group"] == new[row]:
                row += 1
                continue
            end = row                       # a run of groups that were not shown: they go in here
            while end < len(groups) and new[end] not in before:
                end += 1
            self.beginInsertRows(QModelIndex(), row, end - 1)
            self._groups[row:row] = groups[row:end]
            self.endInsertRows()
            row = end
        for row, group in enumerate(groups):
            shown = self._groups[row]
            roles = [self._FIRST + index for index, name in enumerate(self.FIELDS) if shown[name] != group[name]]
            if roles:
                self._groups[row] = group
                index = self.index(row, 0)
                self.dataChanged.emit(index, index, roles)


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
    duplicateDistinctChanged = Signal()
    # The duplicate cards are being read again after something was put away.
    duplicateSettlingChanged = Signal()
    duplicateScanProgress = Signal(int, int)
    aiScanProgress = Signal(int, int)
    aiScanningChanged = Signal()
    aiCountsChanged = Signal()
    # The AI tags themselves (added, switched, deleted) and what each has learned.
    aiCategoriesChanged = Signal()
    aiTagRulesChanged = Signal()
    # A map, not `object`: QML cannot read the fields of a Python object passed
    # as `object`, which left the statistics window on "…" for good.
    statisticsLoaded = Signal("QVariantMap")
    shutdownFinished = Signal()
    # This controller changed the shared AI profile (a tag, a switch, what a tag learned): the
    # other tab reads it again (ApplicationController connects the gallery and 폴더 정리).
    aiProfileChanged = Signal()
    _delivered = Signal(object)
    # The QML image provider this list's thumbnails come from.
    image_provider = "thumbs"
    # Start the worker threads with the first library rather than when made (a tab that may
    # never be shown costs no thread until then).
    lazy_workers = False

    # -- what a subclass over another kind of library replaces (qt_local_folder) -------------
    def _make_repository(self):
        return LibraryRepository()

    def _make_request(self):
        """The request of the list as filtered now, or None when no library is open."""
        if not (self._account and self._output_dir):
            return None
        return _Request(self._generation, self._account, self._output_dir, self._separate_accounts,
                        self._query, self._media_type, self._order, self._author,
                        self._date_field, self._date_from, self._date_to, self._hashtag,
                        self._favorite_only, self._user_tag, self._duplicate_only,
                        self._ai_category, self._ai_origin)

    def _teach_identity(self, key):
        """``(what the repository's teach_tag names this tile by, is it a photo)``; None for another list's key."""
        match = _KEY.fullmatch(key) if isinstance(key, str) else None
        if match is None or int(match[1]) != self._generation:
            return None, False
        return (match[2], match[3], int(match[4]), match[5]), match[5] == "photo"

    def _job_finished(self, action, selection, message):
        """A subclass's own job ended (see _accept); True when it was handled."""
        return False

    def __init__(self, parent=None):
        super().__init__(parent)
        self._repository = self._make_repository()
        self.provider = _LibraryThumbnailProvider(decoder=self._repository.decode,
                                                  workers=_thumbnail_workers())
        self._repository.provider = self.provider
        self.model = GalleryModel(self._repository.read, provider=self.provider, image_provider=self.image_provider,
                                  lazy=self.lazy_workers, parent=self)
        self._generation = 0
        self._library_set = False
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
        # Raised when a duplicate scan sets the count: a count read queued before that answers
        # later with what it read then, and is dropped (see _accept).
        self._duplicate_count_stamp = 0
        self._dup_current = 0
        self._dup_total = 0
        self._teach_request = None
        self._skipped_teach = 0
        self._preserve_status_serial = 0
        self._ai_counts = []
        self._ai_tag_names = []          # switched-on AI tags, in list order (the shared profile)
        self._ai_known_names = set()     # every AI tag that is not deleted, on or off
        self._ai_rules = []              # [{tag, enabled, built_in, pos_count, neg_count, regime, status_text}]
        self._ai_deleted_builtin = 0
        self._reload_ai_rules(emit=False)
        # The counts beside the filters (duplicates, "not duplicates", AI categories) of this
        # library have arrived. Track each separately so an interrupted reload only re-asks
        # what did not finish.
        self._duplicate_counts_loaded = False
        self._distinct_counts_loaded = False
        self._ai_counts_loaded = False
        self._duplicate_near = False
        self._duplicate_groups = []
        self._duplicate_model = DuplicateGroupModel(self)
        self._duplicate_hold = None      # rows to read again before the cards are redrawn in place
        self._duplicate_return = None    # (opened, picked) to find again once the cards are read; keys without "g<n>_"
        self._distinct_count = 0         # "not duplicates" marks: whole groups and photos taken out of one
        self._distinct_undo = None       # the last such mark as (kind, what the catalog takes back)
        self._auto_classify = True       # classify newly saved photos without being asked
        self._auto_ai_wanted = False     # a save ended or a library opened: look for photos to classify
        self._auto_ai_seen = None        # the library that look was already made for at opening
        self._statistics_wanted = False
        self._ai_scanning = False
        self._ai_current = 0
        self._ai_total = 0
        self._date_field = "posted"
        self._date_from = self._date_to = ""
        self._selection_mode = False
        self._selection_explicit = False     # the person switched selecting on, rather than just picking a tile
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
        # The look for new photos waits a moment after a save or a reload, so it does not
        # compete with the list, and several reasons in a row make one look.
        self._auto_ai_timer = QTimer(self)
        self._auto_ai_timer.setSingleShot(True)
        self._auto_ai_timer.setInterval(1500)
        self._auto_ai_timer.timeout.connect(self._look_for_new_photos)
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(50)
        self._shutdown_timer.timeout.connect(self._poll_shutdown)
        self._delivered.connect(self._accept, Qt.ConnectionType.QueuedConnection)
        self._worker = Thread(target=self._work, name="qt-library-actions", daemon=True)
        self._interactive_worker = Thread(target=self._work_interactive, name="qt-library-browse", daemon=True)
        self._workers_started = False
        if not self.lazy_workers:
            self._start_workers()
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

    @Property("QVariantList", notify=aiCategoriesChanged)
    def aiCategories(self):
        """The switched-on AI tags, in list order (the five defaults unless the person changed them)."""
        return list(self._ai_tag_names)

    @Property("QVariantList", notify=aiTagRulesChanged)
    def aiTagRules(self):
        """Every AI tag that is not deleted, for the AI tag manager, with how far its learning has come."""
        return [dict(item) for item in self._ai_rules]

    @Property(int, notify=aiTagRulesChanged)
    def aiDeletedBuiltinCount(self):
        """Default AI tags the person deleted; "기본 태그 복원" brings them back."""
        return self._ai_deleted_builtin

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
        """Loaded duplicates as groups: [{group, size, items: [{key, caption, detail, thumbUrl, keeper, copy, ...}]}]."""
        return self._duplicate_groups

    @Property(QObject, constant=True)
    def duplicateGroupModel(self):
        """The same groups for the card list, changed in place so the list keeps where it is."""
        return self._duplicate_model

    @Property(bool, notify=duplicateSettlingChanged)
    def duplicatesSettling(self):
        """The cards on screen are from before the last change; the list is being read again."""
        return self._duplicate_hold is not None

    @Property(int, notify=duplicateDistinctChanged)
    def distinctDuplicateCount(self):
        """Marks "not duplicates" (groups, and photos taken out of a group); a scan honours them until cleared."""
        return self._distinct_count

    @Property(bool, notify=duplicateDistinctChanged)
    def canUndoDistinct(self):
        return bool(self._distinct_undo)

    def _hold_duplicates(self, rows):
        held = self._duplicate_hold is not None
        self._duplicate_hold = rows
        if held != (rows is not None):
            self.duplicateSettlingChanged.emit()

    def _rebuild_duplicate_groups(self):
        if self._duplicate_hold is not None:
            # Something was put away and the list is read again from its start. The cards stay
            # as they are until it has been read as far as before; then only what differs is
            # redrawn, and the view stays where the person was looking.
            if self._duplicate_only and not self.model.error:
                if self.model.busy:
                    return
                if self.model.count < self._duplicate_hold and self.model.canFetchMore():
                    self.model.loadMore()
                    return
            self._hold_duplicates(None)
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
                    "copy": bool(row.get("copy", False)),
                    "favorite": bool(row.get("favorite", False)), "missing": bool(row.get("missing", False))})
            for index, group in enumerate(groups):
                group["number"] = index + 1
        if groups != self._duplicate_groups:
            self._duplicate_groups = groups
            self._duplicate_model.adopt(groups)
            self.duplicateGroupsChanged.emit()
        if self._duplicate_return is not None:
            self._return_to_duplicates()

    def _return_to_duplicates(self):
        """The cards were read again with the same photos: open and pick them again under their new keys."""
        (opened, picked), self._duplicate_return = self._duplicate_return, None
        prefix = f"g{self._generation}_"
        found = [prefix + key for key in picked if prefix + key in self.model._keys]
        if found:
            self._extend_selection(found, self._selection_keys)
        if opened and prefix + opened in self.model._keys:
            self.selectItem(prefix + opened)
        elif opened:
            # The photo left the list (a favorites filter): its preview closes instead of waiting.
            self._selection_serial += 1
            self._selected = {}
            self._selection_key = ""
            self._busy = False
            self.changed.emit()

    @Slot()
    def forgetOpened(self):
        """Esc took the highlight off the grid: no photo is the opened one any more.

        The grid's highlight is the grid's own. Left alone, the library went on
        naming the photo, so the next page that loaded put the highlight and the
        scroll position back on it, and F still starred a photo nothing marked.
        Photos picked for a batch are another matter and stay picked.
        """
        if self._closed or not (self._selection_key or self._selected or self._navigation is not None):
            return
        self._navigation = None
        self._selection_serial += 1
        self._selection_key = ""
        self._selected = {}
        self._busy = False
        with self._condition:
            self._interactive = deque(item for item in self._interactive if item[0] != "select")
        self.changed.emit()

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

    @Property(bool, notify=changed)
    def aiCancellable(self):
        return self._exclusive in ("ai", "teach")

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

    @Property("QVariantList", notify=changed)
    def selectedAiTags(self):
        """The AI tags shown on the opened photo: [{tag, source, confirmed}], confirmed ones first."""
        return [dict(item) for item in self._selected.get("ai_tags", []) if isinstance(item, dict)]

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
            parts.append("해시태그 " + (self._hashtag if self._hashtag.startswith("#") else "#" + self._hashtag))
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

    def _start_workers(self):
        if not self._workers_started and not self._closed:
            self._workers_started = True
            self._worker.start()
            self._interactive_worker.start()

    @Slot(str, str, bool)
    def setLibrary(self, account, output_dir, separate_accounts):
        self._start_workers()
        normalized = account.strip().lstrip("@").lower()
        library = (normalized, output_dir, bool(separate_accounts))
        if self._library_set and library == (self._account, self._output_dir, bool(self._separate_accounts)):
            return
        self._library_set = True
        self._account, self._output_dir, self._separate_accounts = library
        self._counts_loaded = False
        self._ai_counts = []
        self.aiCountsChanged.emit()
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
        self._distinct_count = 0
        self._distinct_undo = None
        self.duplicateDistinctChanged.emit()
        self._refresh(reload_authors=True)
        if self._account and self._output_dir and library != self._auto_ai_seen:
            # Scheduled saves run while the app is closed; their photos are classified now.
            self._auto_ai_seen = library
            self.classifyNewPhotos()

    @property
    def _counts_loaded(self):
        return self._duplicate_counts_loaded and self._distinct_counts_loaded and self._ai_counts_loaded

    @_counts_loaded.setter
    def _counts_loaded(self, value):
        flag = bool(value)
        self._duplicate_counts_loaded = flag
        self._distinct_counts_loaded = flag
        self._ai_counts_loaded = flag

    @Slot()
    def refresh(self):
        # The same listing read again (files were recycled, a save ended): duplicate cards keep their place.
        self._authors_loaded = False
        self._hashtags_loaded = False
        self._user_tags_loaded = False
        self._counts_loaded = False
        self._refresh(reload_authors=True, keep_place=True)

    def _refresh(self, *, reload_authors=True, status="", keep_place=False, keep_opened=False):
        if self._closed:
            return
        # keep_place: the listing is the same one, minus what was just put away (or, after a favorite,
        # with another photo first in its group). In the duplicate view the cards then wait for the
        # rows that were loaded before (see _rebuild_duplicate_groups).
        holding = bool(keep_place and self._duplicate_only and self._duplicate_groups)
        self._hold_duplicates(max(self.model.count, self._duplicate_hold or 0) if holding else None)
        # keep_opened: the same photos come back (a favorite changed only which one a group keeps).
        # Once the cards have been read again, the opened photo opens again and the picked photos
        # are picked again (see _return_to_duplicates); until then the preview waits for its photo.
        # A return still waiting carries over to the next reading of the same cards.
        return_state = self._duplicate_return if holding else None
        if holding and keep_opened and return_state is None:
            prefix = f"g{self._generation}_"
            opened = self._selection_key or self.selectedKey
            picked = [key[len(prefix):] for key in self._selected_keys_order if key.startswith(prefix)]
            return_state = (opened[len(prefix):] if opened.startswith(prefix) else "", picked)
        self._generation += 1
        self._selection_serial += 1
        self._selected = {}
        self._selection_key = ""
        self._navigation = None
        self._selection_anchor = ""
        self._band_base = None
        self._preserve_status_serial = 0
        self.clearSelection()
        self._duplicate_return = return_state
        self._busy = bool(self._duplicate_return and self._duplicate_return[0])
        # A folder-wide scan keeps running while the grid reloads. Clearing the
        # flag here let a second scan start and left the first one's button stuck.
        if self._exclusive:
            if status:
                self._status = status
        else:
            self._status = status
        with self._condition:
            self._pending = deque(item for item in self._pending if item[0] in _EXCLUSIVE_ACTIONS)
            self._interactive = deque(item for item in self._interactive if item[0] == "sync_tag_examples")
            self._condition.notify_all()
        request = self._make_request()
        if request is None:
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
        if request is not None and (reload_authors or not self._authors_loaded or not self._hashtags_loaded
                                    or not self._user_tags_loaded or not self._counts_loaded):
            with self._condition:
                if reload_authors or not self._authors_loaded:
                    self._authors_loaded = False
                    self._interactive.append(("authors", self._generation, self._selection_serial, ""))
                if reload_authors or not self._hashtags_loaded:
                    self._hashtags_loaded = False
                    self._interactive.append(("hashtags", self._generation, self._selection_serial, ""))
                if reload_authors or not self._user_tags_loaded:
                    self._user_tags_loaded = False
                    self._interactive.append(("user_tags", self._generation, self._selection_serial, ""))
                if reload_authors or not self._duplicate_counts_loaded:
                    self._duplicate_counts_loaded = False
                    self._interactive.append(("duplicate_counts", self._generation, self._selection_serial,
                                              str(self._duplicate_count_stamp)))
                if reload_authors or not self._distinct_counts_loaded:
                    self._distinct_counts_loaded = False
                    self._interactive.append(("distinct_counts", self._generation, self._selection_serial, ""))
                if reload_authors or not self._ai_counts_loaded:
                    self._ai_counts_loaded = False
                    self._interactive.append(("ai_counts", self._generation, self._selection_serial, ""))
                self._condition.notify_all()
        if request is not None and self._statistics_wanted:
            # The reload above emptied the queue; an open statistics window still waits.
            with self._condition:
                self._interactive.append(("statistics", self._generation, self._selection_serial, ""))
                self._condition.notify_all()
        if self._auto_ai_wanted:
            self._auto_ai_timer.start()
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
            self._status = "작성자와 날짜(예: 2026-01-01), 시작·종료 순서를 확인해 주세요."
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
        tag = _normalize_tag(tag)
        if tag == self._user_tag:
            return
        self._user_tag = tag
        self.userTagFilterChanged.emit()
        self._refresh(reload_authors=False)

    @Slot(str, str)
    def setAiFilter(self, category, origin):
        category = valid_tag(category) if isinstance(category, str) else ""
        # The screen shows one list per AI tag. Estimated and taught photos
        # are no longer split; the second argument stays for older callers.
        origin = ""
        if category not in self._ai_tag_names:
            category = ""
        if category == self._ai_category and origin == self._ai_origin:
            return
        self._ai_category, self._ai_origin = category, origin
        self._refresh(reload_authors=False)

    # -- the AI tags --------------------------------------------------------------------------
    @staticmethod
    def _rule_status(rule):
        """One line for the AI tag manager on how a tag is decided now."""
        pos, neg = int(rule.get("pos_count") or 0), int(rule.get("neg_count") or 0)
        regime = rule.get("regime") or ""
        no = f" · 아님 {neg:,}장" if neg else ""
        if pos == 0:
            if rule.get("built_in"):
                return "기본 분류" + no
            return "예시 1장 이상 필요" + no
        if regime == "linear" or pos >= 5:
            return ("기본 + " if rule.get("built_in") else "") + f"예시 {pos:,}장 · 학습됨" + no
        return ("기본 + " if rule.get("built_in") else "") + f"예시 {pos:,}장 · 닮은 사진 찾기 (5장부터 학습)" + no

    @Slot()
    def reloadAiProfile(self):
        """The other tab changed the shared AI profile: show its tags and what they learned."""
        if not self._closed:
            self._reload_ai_rules(announce=False)

    def _reload_ai_rules(self, emit=True, announce=True):
        """Read the AI tags again from the shared profile (a small local database, no AI libraries).

        ``announce`` tells the other tab when something changed here (aiProfileChanged).
        """
        rules, deleted = [], 0
        profile = _open_profile()
        if profile is not None:
            try:
                with closing(profile):
                    rules = profile.active_tags(include_disabled=True)
                    deleted = profile.deleted_builtin_count()
            except AiProfileError:
                rules = []
        else:
            # The app data folder cannot hold the profile: offer the defaults, untaught.
            rules = [{"tag": name, "enabled": True, "built_in": True, "pos_count": 0, "neg_count": 0,
                      "regime": "zeroshot"} for name in CATEGORY_NAMES]
        shown = [{"tag": rule["tag"], "enabled": bool(rule["enabled"]), "built_in": bool(rule["built_in"]),
                  "pos_count": int(rule.get("pos_count") or 0), "neg_count": int(rule.get("neg_count") or 0),
                  "regime": rule.get("regime") or "", "status_text": self._rule_status(rule)} for rule in rules]
        names = [rule["tag"] for rule in shown if rule["enabled"]]
        names_changed = names != self._ai_tag_names
        rules_changed = shown != self._ai_rules or deleted != self._ai_deleted_builtin
        self._ai_rules = shown
        self._ai_tag_names = names
        self._ai_known_names = {rule["tag"] for rule in shown}
        self._ai_deleted_builtin = deleted
        if not emit:
            return
        if names_changed:
            self.aiCategoriesChanged.emit()
        if rules_changed:
            self.aiTagRulesChanged.emit()
        if names_changed and self._ai_category and self._ai_category not in names:
            # The tag this list was filtered on is switched off or deleted (here or in the other
            # tab): the list shows every photo again instead of the guesses of a tag that is gone.
            self.setAiFilter("", "")
        if announce and (names_changed or rules_changed):
            self.aiProfileChanged.emit()
        if names_changed and self._selected:
            self._reselect()
        if not announce and (names_changed or rules_changed):
            self.classifyNewPhotos()
        if names_changed:
            # The menu's counts follow the tag list.
            self._ai_counts_loaded = False
            with self._condition:
                self._interactive.append(("ai_counts", self._generation, self._selection_serial, ""))
                self._condition.notify_all()

    def _profile_edit(self, edit, message):
        """Run one change of the shared AI profile; say so when it could not be saved."""
        if self._closed:
            return None
        profile = _open_profile()
        if profile is None:
            self._status = "AI 태그 설정을 저장하지 못했습니다. LuvBox 기록 폴더의 권한을 확인해 주세요."
            self.changed.emit()
            return None
        try:
            with closing(profile):
                result = edit(profile)
        except AiProfileError as exc:
            self._status = str(exc)
            self.changed.emit()
            return None
        self._reload_ai_rules()
        if message:
            self._status = message
            self.changed.emit()
        return result

    @Slot(str, result=bool)
    def addAiTag(self, tag):
        """Make a tag an AI tag (a new one, a deleted one back, or one of the person's own tags)."""
        clean = valid_tag(tag)
        if not clean:
            self._status = "AI 태그 이름은 1~50자로 적어 주세요."
            self.changed.emit()
            return False
        added = self._profile_edit(lambda profile: profile.add_tag(clean),
                                   f"AI 태그에 #{clean} 추가 완료 · ‘지금 다시 분류’로 사진에 반영합니다.")
        if added is None:
            return False
        # Photos the person already tagged this way are its first examples (synced off the UI thread).
        if self._account and self._output_dir:
            epoch = self._repository.catalog_epoch() if hasattr(self._repository, "catalog_epoch") else None
            with self._condition:
                self._interactive.append(("sync_tag_examples", self._generation, self._selection_serial, clean, epoch))
                self._condition.notify_all()
        return True

    @Slot(str, result=bool)
    def enableUserTagAsAi(self, tag):
        return self.addAiTag(tag)

    @Slot(str, bool, result=bool)
    def setAiTagEnabled(self, tag, enabled):
        clean = valid_tag(tag)
        if not clean:
            return False
        done = self._profile_edit(lambda profile: profile.set_tag_enabled(clean, bool(enabled)),
                                  f"#{clean} 자동분류를 " + ("켰습니다." if enabled else "껐습니다."))
        return bool(done)

    @Slot(str, result=bool)
    @Slot(str, bool, result=bool)
    def deleteAiTag(self, tag, purge_examples=False):
        """The tag stops being an AI tag. Its examples stay unless ``purge_examples``; typed tags stay."""
        clean = valid_tag(tag)
        if not clean:
            return False
        done = self._profile_edit(lambda profile: profile.delete_tag(clean, purge_examples=bool(purge_examples)),
                                  f"AI 태그에서 #{clean} 삭제 완료 · ‘지금 다시 분류’로 사진에서도 뺍니다.")
        return bool(done)

    @Slot(result=int)
    def restoreBuiltinAiTags(self):
        restored = self._profile_edit(lambda profile: profile.restore_builtin_tags(), "")
        if restored:
            self._status = f"기본 AI 태그 {restored}개를 되살렸습니다."
            self.changed.emit()
        return int(restored or 0)

    @Slot(result=bool)
    def reclassifyNow(self):
        """Give every photo its AI tags again from the stored features, without reading the photos."""
        if not self._exclusive and hasattr(self._repository, "_ai_cancel"):
            self._repository._ai_cancel.clear()
        return self._begin_exclusive("ai", "reclassify", "AI 태그를 다시 매기는 중…")

    def _queue_teach(self, category, keys, positive=True):
        if self._closed:
            return False
        tag = valid_tag(category) if category else ""
        if (category and not tag) or (not tag and positive):
            self._status = "가르칠 분류를 다시 선택해 주세요."
            self.changed.emit()
            return False
        if self._exclusive:
            self._status = _EXCLUSIVE_BUSY[self._exclusive]
            self.changed.emit()
            return False
        idents = []
        taught_keys = []
        skipped = 0
        seen = set()
        for key in keys:
            ident, photo = self._teach_identity(key)
            if ident is None:
                continue
            if not photo:
                skipped += 1
                continue
            if ident in seen:
                continue
            seen.add(ident)
            idents.append(ident)
            taught_keys.append(key)
        if not idents:
            self._status = "가르칠 사진이 없습니다. 영상과 GIF는 이 분류에 넣지 않습니다." if skipped else "가르칠 사진을 선택해 주세요."
            self.changed.emit()
            return False
        self._teach_request = (tag, bool(positive), idents, taught_keys)
        self._skipped_teach = skipped
        return self._begin_exclusive("teach", "teach", "분류를 저장하는 중…")

    @Slot(str, str, result=bool)
    def teachOne(self, key, category):
        return self._queue_teach(category, [key], bool(category))

    @Slot(str, result=bool)
    def teachSelected(self, category):
        if not self.selectedKey:
            self._status = "미리보기에서 사진을 연 뒤 분류를 가르쳐 주세요."
            self.changed.emit()
            return False
        return self._queue_teach(category, [self.selectedKey], bool(category))

    @Slot(str, result=bool)
    def teachSelection(self, category):
        return self._queue_teach(category, list(self._selection_keys), bool(category))

    @Slot(str, str, bool, result=bool)
    def teachTag(self, key, tag, positive):
        """"Yes, this" (✓) or "not this" about one AI tag on one photo; the AI learns from it."""
        return self._queue_teach(tag, [key], bool(positive))

    @Slot(str, bool, result=bool)
    def teachSelectionTag(self, tag, positive):
        return self._queue_teach(tag, list(self._selection_keys), bool(positive))

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
        # The tag being filtered on stays the filter even when its last photo
        # loses it: the list on screen is still that filter's list, and the
        # filter chip names a tag that is no longer in the list on its own.
        # (Turning the filter off here left one photo listed under "no filter".)
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
        if kind in ("ai", "teach"):
            # A stop asked of an earlier scan must not end this one at once. Only the button's
            # own slot used to clear it, so after one "분류 중단" every classification that
            # started by itself (after a save, on opening) ended as "stopped" straight away.
            cancel = getattr(self._repository, "_ai_cancel", None)
            if cancel is not None:
                cancel.clear()
        if kind == "ai":
            self._ai_scanning = True
            self._ai_current = 0
            self._ai_total = 0
            self.aiScanningChanged.emit()
        if status is not None:
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
        clean = _normalize_tag(tag)
        if not clean:
            return 0
        count = self._repository.add_tag_batch(list(self._selection_keys), self._generation, clean)
        if count:
            self._upsert_cached_user_tag(clean, delta=count)
            self._user_tags_loaded = False
            if clean in self._ai_known_names:
                self._reload_ai_rules()
            # A tag does not change which photos are listed, so the place, the selection and an
            # open preview stay. A tag typed with an AI tag's name keeps the photos in that AI
            # tag's list too: the list shows typed tags of that name as well.
            self._settle(f"선택한 {count:,}개 사진에 #{clean} 태그를 추가했습니다.", reload=False)
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
            self._status = "중복 검사 중단 중…"
            self.changed.emit()

    def setAutoClassify(self, enabled):
        """Preference: classify newly saved photos on their own, with the model already on this PC."""
        enabled = bool(enabled)
        turned_on = enabled and not self._auto_classify
        self._auto_classify = enabled
        if not enabled:
            self._auto_ai_wanted = False
            self._auto_ai_timer.stop()
        elif turned_on:
            self.classifyNewPhotos()

    @Slot()
    def classifyNewPhotos(self):
        """A save ended or a library opened: classify photos that have no category yet.

        Quiet when there is nothing to do, and it waits for a running job.
        """
        if self._closed or not self._auto_classify:
            return
        self._auto_ai_wanted = True
        self._auto_ai_timer.start()

    def _look_for_new_photos(self):
        if self._closed or not self._auto_classify or not self._auto_ai_wanted:
            return
        if self._exclusive or getattr(self, "_deletion_busy", False) or not (self._account and self._output_dir):
            return                      # asked again when the running job ends
        with self._condition:
            if not any(item[0] == "auto_ai_check" for item in self._pending):
                self._pending.append(("auto_ai_check", self._generation, self._selection_serial, "", None))
                self._condition.notify_all()

    @Slot(result=bool)
    def scanAiTags(self):
        if not self._exclusive and hasattr(self._repository, "_ai_cancel"):
            self._repository._ai_cancel.clear()
        return self._begin_exclusive("ai", "scan_ai_tags", "AI 자동분류 준비 중…")

    @Slot()
    def cancelAiScan(self):
        if self._exclusive in ("ai", "teach") and hasattr(self._repository, "_ai_cancel"):
            self._repository._ai_cancel.set()
            self._status = "분류 가르치기 중단 중…" if self._exclusive == "teach" else "AI 자동분류 중단 중…"
            self.changed.emit()

    @Slot(result=bool)
    def clearAiTags(self):
        return self._begin_exclusive("clear", "clear_ai_tags", "AI 자동분류 태그를 초기화하는 중…")

    def _duplicate_marks_changed(self, status):
        """Reload the duplicate list and its counts after a "not duplicates" mark changed."""
        self.duplicateDistinctChanged.emit()
        self._duplicate_counts_loaded = False
        self._distinct_counts_loaded = False
        # keep_opened: photos picked in other groups stay picked, as they do after a favorite.
        self._refresh(reload_authors=False, status=status, keep_place=True, keep_opened=True)

    @Slot(str, result=bool)
    def markGroupDistinct(self, key):
        """The photos grouped with this one are not duplicates: stop listing and grouping them."""
        if self._closed or not isinstance(key, str) or not key:
            return False
        if self._exclusive == "duplicates":
            self._status = _HOLD_REASON["duplicates"]
            self.changed.emit()
            return False
        members = self._repository.mark_group_distinct(key, self._generation)
        if not members:
            self._status = _DISTINCT_ERROR
            self.changed.emit()
            return False
        self._distinct_undo = ("group", members)
        self._distinct_count += 1
        self._duplicate_marks_changed(
            f"사진 {len(members):,}장을 중복 아님으로 표시했습니다.")
        return True

    @Slot("QVariantList", result=bool)
    def markPhotosDistinct(self, keys):
        """These photos are no duplicates of the rest of their group: take only them out of it."""
        if self._closed or not isinstance(keys, (list, tuple)) or not keys:
            return False
        if self._exclusive == "duplicates":
            self._status = _HOLD_REASON["duplicates"]
            self.changed.emit()
            return False
        mark = self._repository.mark_photos_distinct(list(keys), self._generation)
        if not mark:
            self._status = _DISTINCT_ERROR
            self.changed.emit()
            return False
        self._distinct_undo = ("photos", mark)
        self._distinct_count += 1
        self._duplicate_marks_changed(
            f"사진 {len(mark['photos']):,}장을 묶음에서 뺐습니다.")
        return True

    @Slot(result=bool)
    def undoDistinct(self):
        """Take back the last "not duplicates" mark."""
        if self._closed or not self._distinct_undo:
            return False
        if self._exclusive == "duplicates":
            self._status = _HOLD_REASON["duplicates"]
            self.changed.emit()
            return False
        (kind, mark), self._distinct_undo = self._distinct_undo, None
        undo = self._repository.undo_photos_distinct if kind == "photos" else self._repository.undo_group_distinct
        if not undo(mark, self._generation):
            self._status = _DISTINCT_ERROR
            self.duplicateDistinctChanged.emit()
            self.changed.emit()
            return False
        self._distinct_count = max(0, self._distinct_count - 1)
        self._duplicate_marks_changed("중복 아님 표시를 되돌렸습니다. 묶음이 다시 보입니다.")
        return True

    @Slot(result=int)
    def clearDistinct(self):
        """Forget every "not duplicates" mark; the groups are listed again."""
        if self._closed:
            return 0
        if self._exclusive == "duplicates":
            self._status = _HOLD_REASON["duplicates"]
            self.changed.emit()
            return 0
        count = self._repository.clear_distinct(self._generation)
        if count is None:
            self._status = _DISTINCT_ERROR
            self.changed.emit()
            return 0
        self._distinct_undo = None
        self._distinct_count = 0
        self._duplicate_marks_changed(
            f"중복 아님 표시 {count:,}개를 초기화했습니다. 묶음이 다시 보입니다." if count
            else "초기화할 중복 아님 표시가 없습니다.")
        return count

    @Slot(result=int)
    def selectDuplicateCopies(self):
        # Only in the duplicate view: there a file is checked again before it moves. The button
        # is hidden elsewhere, but a key press could still reach it while it kept the focus.
        if self._closed or not self._duplicate_only:
            return 0
        if self._exclusive == "duplicates":
            self._status = _HOLD_REASON["duplicates"]
            self.changed.emit()
            return 0
        copy_keys = self._repository.duplicate_copy_keys(self._generation)
        # Photos of a group that are only alike (another size or quality) are never copies.
        similar = sum(1 for row in self.model._rows
                      if row.get("group") and not row.get("keeper") and not row.get("copy"))
        left = f" ‘비슷함’ {similar:,}장은 고르지 않았습니다." if similar else ""
        if not copy_keys:
            self._status = "선택할 중복 사본이 없습니다." + left
            self.changed.emit()
            return 0
        # The list is read 200 rows at a time; copies further down are not on screen yet.
        waiting = sum(1 for k in copy_keys if k not in self.model._keys)
        if waiting:
            left += f" 아직 불러오지 않은 사본 {waiting:,}장은 아래로 내린 뒤 한 번 더 누르면 고릅니다."
        loaded_copies = [k for k in copy_keys if k in self.model._keys and k not in self._selection_keys]
        selectable, _, _ = self._repository.selectable_keys(loaded_copies)
        capacity = max(0, MAX_SELECTION - len(self._selection_keys))
        copy_keys = selectable[:capacity]
        if not copy_keys:
            self._status = ("한 번에 최대 2,000개를 선택할 수 있습니다." if not capacity else
                            "현재 목록에 새로 선택할 중복 사본이 없습니다." + left)
            self.changed.emit()
            return 0
        self._selection_mode = True
        self._selection_keys.update(copy_keys)
        self._batch_selection_changed()
        self._status = f"중복 사본 {len(copy_keys):,}개를 선택했습니다."
        if len(selectable) > capacity:
            self._status += " 한 번에 최대 2,000개를 선택할 수 있습니다."
        self._status += left
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
            # A favorite is the photo its group keeps. The groups stay: their cards are redrawn in
            # place, and the photo opened and the photos picked are found again under the new keys.
            self._refresh(reload_authors=False, keep_place=True, keep_opened=True)
        elif self._favorite_only and not new_state:
            # The saved query no longer contains this photo. Leaving it in the
            # model also makes its loaded-row count stop paging one photo early.
            # Its tile is taken out where it is: reading the list again (1.10.5) sent a
            # person tidying deep in the list back to its top and closed the preview.
            if not self.removeDeleted([key]):
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
        clean = _normalize_tag(tag)
        if not clean:
            return False
        had_user_tag = any(
            (t.get("tag") == clean and t.get("source") == "user") if isinstance(t, dict) else str(t) == clean
            for t in self._selected.get("tags", [])
        )
        ok = self._repository.add_tag(self.selectedKey, self._generation, clean)
        if ok:
            tags = [t for t in self._selected.get("tags", []) if (t.get("tag") if isinstance(t, dict) else str(t)) != clean]
            tags.append({"tag": clean, "source": "user"})
            tags.sort(key=lambda t: str(t.get("tag", "")) if isinstance(t, dict) else str(t))
            self._selected["tags"] = tags
            # A typed tag of an AI label's name takes that label's place on the photo.
            self._selected["ai_tags"] = [item for item in self._selected.get("ai_tags", [])
                                         if isinstance(item, dict) and item.get("tag") != clean]
            if not had_user_tag:
                self._upsert_cached_user_tag(clean, delta=1)
            self._user_tags_loaded = False
            if clean in self._ai_known_names:
                self._reload_ai_rules()
            # The photo stays in every list it was in: an AI tag's list shows typed tags of its name too.
            self._settle(self._status, reload=False)
        return ok

    @Slot(str, result=bool)
    def removeSelectedTag(self, tag):
        if self._refuse_tags() or self._closed or not self.selectedKey or not tag:
            return False
        clean = _normalize_tag(tag)
        if not clean:
            return False
        had_user_tag = any(
            (t.get("tag") == clean and t.get("source") == "user") if isinstance(t, dict) else str(t) == clean
            for t in self._selected.get("tags", [])
        )
        was_ai = any(
            isinstance(t, dict) and t.get("tag") == clean and t.get("source") in ("ai", "confirmed")
            for t in self._selected.get("tags", [])
        )
        remover = getattr(self._repository, "remove_tag_detailed", None)
        if callable(remover):
            ok, confirmed_restored = remover(self.selectedKey, self._generation, clean)
        else:
            ok = bool(self._repository.remove_tag(self.selectedKey, self._generation, clean))
            confirmed_restored = False
        if ok:
            tags = [t for t in self._selected.get("tags", []) if (t.get("tag") if isinstance(t, dict) else str(t)) != clean]
            if confirmed_restored:
                tags.append({"tag": clean, "source": "confirmed"})
                ai_tags = [item for item in self._selected.get("ai_tags", [])
                           if isinstance(item, dict) and item.get("tag") != clean]
                ai_tags.append({"tag": clean, "confirmed": True, "source": "confirmed"})
                self._selected["ai_tags"] = ai_tags
            else:
                self._selected["ai_tags"] = [item for item in self._selected.get("ai_tags", [])
                                             if isinstance(item, dict) and item.get("tag") != clean]
            self._selected["tags"] = tags
            if had_user_tag:
                self._upsert_cached_user_tag(clean, delta=-1)
            self._user_tags_loaded = False
            if clean in self._ai_known_names:
                self._reload_ai_rules()
            leaves = (clean == self._user_tag and had_user_tag) or (clean == self._ai_category and not confirmed_restored)
            if not was_ai:
                status_text = self._status
            elif getattr(self, "_share_learning", True) is False:
                status_text = f"#{clean} 태그를 빼고 이 폴더에만 반영했습니다."
            else:
                status_text = f"#{clean} 태그를 빼고 AI 학습에 반영했습니다."
            if not (leaves and self.removeDeleted([self.selectedKey])):
                self._settle(status_text, reload=leaves)
            elif was_ai:
                if self._busy:
                    self._preserve_status_serial = self._selection_serial
                self._settle(status_text, reload=False)
        return ok

    @Slot(bool)
    def setSelectionMode(self, enabled):
        if self._closed or self._selection_mode == enabled:
            return
        self._selection_mode = self._selection_explicit = enabled
        if not enabled:
            if self._duplicate_return is not None and self._duplicate_return[1]:
                self._duplicate_return = (self._duplicate_return[0], [])
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
        # A highlighted tile (arrow keys) has no click anchor yet. The highlight
        # is the start of the range; the click that follows must not have moved
        # it already.
        if self._selection_anchor not in positions:
            seed = self._selection_key if self._selection_key in positions else (
                self.selectedKey if self.selectedKey in positions else key)
            self._selection_anchor = seed
        anchor = self._selection_anchor
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
        """Start a drag selection; with Ctrl held the earlier selection stays.

        It also stays while the person has switched "여러 개 선택" on: tiles cover the
        whole grid there, so a click that moved a few pixels counted as a drag and
        threw away everything picked so far.
        """
        if self._closed:
            return
        additive = bool(additive) or self._selection_explicit
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
        if self._duplicate_return is not None and self._duplicate_return[1]:
            self._duplicate_return = (self._duplicate_return[0], [])
        if self._selection_keys:
            self._selection_keys.clear()
            self._batch_selection_changed()
            if self._status:
                self._status = ""
                self.changed.emit()

    def _batch_selection_changed(self):
        # In the duplicate view a photo is picked by clicking it, without switching selecting on
        # first. Once nothing is picked (it was put away, or let go) the bar that came with the
        # pick goes too; it stays only for someone who switched selecting on with its button.
        if self._duplicate_only and self._selection_mode and not self._selection_explicit and not self._selection_keys:
            self._selection_mode = False
        self._selection_revision += 1
        self._selected_keys_order = sorted(self._selection_keys, key=lambda key: self.model._positions.get(key, -1))
        self.selectionChanged.emit()

    def deletionManifest(self, keys):
        # Called on the management worker; filesystem checks never run in QML.
        return self._repository.deletion_manifest(keys)

    def alikeManifest(self, key):
        # Called on the management worker: it reads both photos to compare them.
        return self._repository.alike_manifest(key)

    def _alike(self, key):
        """The duplicate view shows this photo only as alike ('비슷함'): in a group, neither kept nor a copy."""
        if not self._duplicate_only or not isinstance(key, str) or not key:
            return False
        position = self.model._positions.get(key)
        if position is None or not 0 <= position < len(self.model._rows):
            return False
        row = self.model._rows[position]
        return (row.get("key") == key and bool(row.get("group")) and not row.get("keeper")
                and not row.get("copy") and not row.get("missing"))

    @Property(bool, notify=changed)
    def selectedAlike(self):
        """The photo open in the preview is one the duplicate view shows as '비슷함'."""
        return self._alike(self.selectedKey)

    def alikeKeys(self, keys):
        """Which of these keys the duplicate view shows as '비슷함' (ManagementController routes them)."""
        return [key for key in keys if self._alike(key)]

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
        self._authors_loaded = False
        self._hashtags_loaded = False
        self._user_tags_loaded = False
        self._duplicate_counts_loaded = False
        self._ai_counts_loaded = False
        with self._condition:
            for action in ("authors", "hashtags", "user_tags", "duplicate_counts", "ai_counts"):
                stamp = str(self._duplicate_count_stamp) if action == "duplicate_counts" else ""
                self._interactive.append((action, self._generation, self._selection_serial, stamp))
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
        self._preserve_status_serial = 0
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
                selection = (key, reader(generation) if callable(reader) else 0)
            elif action == "auto_ai_check":
                reader = getattr(self._repository, "auto_ai_pending", None)
                selection = bool(reader(generation)) if callable(reader) else False
            elif action == "distinct_counts":
                reader = getattr(self._repository, "distinct_counts", None)
                selection = reader(generation) if callable(reader) else 0
            elif action == "statistics":
                reader = getattr(self._repository, "overview", None)
                selection = reader(generation) if callable(reader) else None
            elif action == "ai_counts":
                reader = getattr(self._repository, "ai_counts", None)
                selection = reader(generation) if callable(reader) else []
            elif action == "sync_tag_examples":
                reader = getattr(self._repository, "sync_tag_examples", None)
                if callable(reader):
                    kwargs = {"epoch": epoch} if "epoch" in inspect.signature(reader).parameters else {}
                    selection = bool(reader(generation, [key], **kwargs))
                else:
                    selection = False
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
                    selection, message = None, _safe_ui_error(exc, "중복 검사를 끝내지 못했습니다. 폴더를 확인한 뒤 다시 시도해 주세요.")
                else:
                    if not self._same_catalog(epoch):
                        selection, message = None, _CATALOG_MOVED
                    else:
                        selection = count
                        kind = "같은 사진(크기·화질만 다른 것 포함)" if near else "완전히 같은 사진"
                        message = f"{kind} {count:,}장을 찾았습니다." if count else f"{kind}이 없습니다."
            elif action in ("scan_ai_tags", "auto_ai_tags", "reclassify"):
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
                    # The run that starts on its own never downloads the model.
                    if hasattr(self._repository, "_ai_wrote"):
                        self._repository._ai_wrote = False
                    if action == "reclassify":
                        count = self._repository.reclassify(
                            generation, progress_callback=on_ai_progress, status_callback=on_ai_status, epoch=epoch)
                    else:
                        extra = {"download": False} if action == "auto_ai_tags" else {}
                        count = self._repository.scan_ai_tags(
                            generation,
                            progress_callback=on_ai_progress,
                            status_callback=on_ai_status,
                            epoch=epoch,
                            **extra,
                        )
                    if not self._same_catalog(epoch):
                        selection, message = None, _CATALOG_MOVED
                    else:
                        selection = count
                        seen = progress_state["tot"]
                        note = getattr(self._repository, "_scan_note", "")
                        if action == "reclassify":
                            if count:
                                message = f"AI 태그를 다시 매겼습니다: 사진 {count:,}장의 AI 태그가 바뀌었습니다."
                            elif not getattr(self._repository, "_ai_had_embeddings", True):
                                message = ("AI 태그를 다시 매겼습니다. 아직 분석된 사진이 없다면 "
                                           "먼저 ‘AI 자동분류 실행’을 눌러 주세요.")
                            else:
                                message = "AI 태그를 다시 매겼습니다. 바뀐 사진은 없습니다."
                            note = ""
                        elif action == "auto_ai_tags":
                            # Nothing new is the usual case; it is not worth a line.
                            noun = "새 사진을" if getattr(self, "_root", "") else "새로 저장한 사진을"
                            message = (f"{noun} AI로 분류했습니다: {seen:,}장을 확인했고, "
                                       f"{count:,}장에 태그를 적용했습니다." if count else "")
                            note = note if count else ""
                        elif count:
                            message = f"AI 자동분류 완료: {seen:,}장을 확인했고, 사진 {count:,}장에 태그를 적용했습니다."
                        elif seen:
                            message = f"AI 자동분류 완료: {seen:,}장을 확인했습니다. 새로 붙인 태그는 없습니다."
                        else:
                            message = "새로 분류할 사진이 없습니다."
                        if note:
                            message += note
                except RuntimeError as exc:
                    selection = None
                    message = _safe_ui_error(exc, "AI 자동분류를 끝내지 못했습니다. 폴더를 확인한 뒤 다시 시도해 주세요.")
            elif action == "teach":
                request_teach = self._teach_request
                try:
                    if not request_teach:
                        selection, message = None, "가르칠 분류를 다시 선택해 주세요."
                    else:
                        category, positive, idents = request_teach[:3]

                        progress_state = {"cur": 0, "tot": 0}

                        def on_teach_progress(cur, tot):
                            progress_state["cur"] = cur
                            progress_state["tot"] = tot
                            self._emit(
                                generation, serial, "ai_scan_progress", (cur, tot),
                                f"분류를 저장하는 중… ({cur:,} / {tot:,})", epoch,
                            )

                        def on_teach_status(msg):
                            self._emit(
                                generation, serial, "ai_scan_progress",
                                (progress_state["cur"], progress_state["tot"]), msg, epoch,
                            )

                        teach = getattr(self._repository, "teach_tag", None)
                        if hasattr(self._repository, "_teach_skipped_media"):
                            self._repository._teach_skipped_media = 0
                        if callable(teach):
                            extra = ({"status_callback": on_teach_status}
                                     if "status_callback" in inspect.signature(teach).parameters else {})
                            taught, propagated = teach(generation, idents, category, positive, epoch=epoch,
                                                       progress_callback=on_teach_progress, **extra)
                        else:
                            taught, propagated = self._repository.teach_labels(
                                generation, idents, category if positive else "", epoch=epoch,
                                progress_callback=on_teach_progress,
                            )
                        if not self._same_catalog(epoch):
                            selection, message = None, _CATALOG_MOVED
                        else:
                            selection = (taught, propagated)
                            skipped_media = int(getattr(self._repository, "_teach_skipped_media", 0) or 0)
                            skipped = getattr(self, "_skipped_teach", 0) + skipped_media
                            missed = max(0, len(idents) - taught - skipped_media)
                            if taught == 0:
                                message = ("가르칠 사진이 없습니다. 영상과 GIF는 이 분류에 넣지 않습니다."
                                           if skipped_media and skipped_media >= len(idents)
                                           else "사진을 읽지 못해 분류를 저장하지 못했습니다.")
                            elif propagated:
                                message = f"{taught:,}장의 분류를 저장했습니다. 비슷한 사진 {propagated:,}장도 함께 바꿨습니다."
                            else:
                                message = f"{taught:,}장의 분류를 저장했습니다."
                            if missed and taught:
                                message += f" 읽지 못한 사진 {missed:,}장은 빠졌습니다."
                            if skipped and taught:
                                message += f" 영상·GIF {skipped:,}개는 제외했습니다."
                except (RuntimeError, Exception) as exc:
                    if exc.__class__.__name__ == "ScanCancelled" or isinstance(exc, RuntimeError):
                        selection, message = None, _safe_ui_error(exc, "분류를 저장하지 못했습니다. 저장 폴더를 확인한 뒤 다시 시도해 주세요.")
                    else:
                        selection, message = None, "분류를 저장하지 못했습니다. 저장 폴더를 확인한 뒤 다시 시도해 주세요."
            elif action == "clear_ai_tags":
                try:
                    count = self._repository.clear_ai_tags(generation, epoch=epoch)
                except RuntimeError as exc:
                    selection, message = None, _safe_ui_error(exc, "AI 태그를 초기화하지 못했습니다. 저장 폴더를 확인한 뒤 다시 시도해 주세요.")
                else:
                    if not self._same_catalog(epoch):
                        selection, message = None, _CATALOG_MOVED
                    else:
                        # The gallery filter may have changed while this ran. Read the
                        # catalog that is open now, not the generation captured at the start.
                        remaining = self._repository.user_tags(None)
                        selection = (count, remaining)
                        message = f"AI 자동분류 태그 {count:,}개를 초기화했습니다." if count else "초기화할 AI 자동분류 태그가 없습니다."
            elif action == "sync_folder":
                # The 폴더 정리 tab reads its folder again (qt_local_folder).
                try:
                    selection = self._repository.sync_folder(generation, epoch=epoch)
                except RuntimeError as exc:      # fixed texts: a stop, or a folder that cannot be read
                    selection, message = None, _safe_ui_error(exc, "폴더를 읽지 못했습니다.")
                except Exception:
                    selection, message = None, "폴더를 읽지 못했습니다. 폴더를 확인한 뒤 다시 시도해 주세요."
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
                selection = (key, 0)
            elif action == "distinct_counts":
                selection = 0
            elif action == "auto_ai_check":
                selection = False
            elif action == "statistics":
                selection = {"error": _STATISTICS_ERROR}
            elif action == "ai_counts":
                selection = []
            elif action == "sync_tag_examples":
                selection = False
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
        self._ai_counts_loaded = False
        if reload:
            self._refresh(reload_authors=False, status=message, keep_place=self._duplicate_only)
            return
        self._status = message
        with self._condition:
            if not self._user_tags_loaded:
                self._interactive.append(("user_tags", self._generation, self._selection_serial, ""))
            self._interactive.append(("ai_counts", self._generation, self._selection_serial, ""))
            self._condition.notify_all()
        self._reselect()
        self.changed.emit()

    @Slot()
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
        if self._auto_ai_wanted:
            self._auto_ai_timer.start()

    @Slot(object)
    def _accept(self, result):
        generation, serial, action, selection, message = result[:5]
        epoch = result[5] if len(result) > 5 else None
        if self._closed:
            return
        catalog_job = action in _EXCLUSIVE_ACTIONS or action in {"scan_progress", "ai_scan_progress"} or (
            action == "sync_tag_examples" and epoch is not None
        )
        if catalog_job:
            if not self._same_catalog(epoch):
                if action in _EXCLUSIVE_ACTIONS:
                    self._release_exclusive()
                    self._status = message or _CATALOG_MOVED
                    self.changed.emit()
                return
        elif action != "forget_folder" and generation != self._generation:
            return
        if self._job_finished(action, selection, message):
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
            stamp, count = selection if isinstance(selection, tuple) and len(selection) == 2 else ("", selection)
            if stamp != str(self._duplicate_count_stamp):
                return                  # read before a duplicate scan that ended since: its count stands
            self._duplicate_counts_loaded = True
            count = int(count) if isinstance(count, int) else 0
            if count != self._duplicate_count:
                self._duplicate_count = count
                self.duplicateCountChanged.emit()
                self.changed.emit()
            return
        if action == "auto_ai_check":
            if not selection or not self._auto_classify:
                self._auto_ai_wanted = False
            elif not self._exclusive and not getattr(self, "_deletion_busy", False):
                self._auto_ai_wanted = False
                self._begin_exclusive("ai", "auto_ai_tags", None)
            return                      # busy: asked again when the running job ends
        if action == "auto_ai_tags":
            self._release_exclusive()
            self._reload_ai_rules()
            wrote = bool(getattr(self._repository, "_ai_wrote", False))
            synced = bool(getattr(self._repository, "_ai_synced_changes", False))
            if hasattr(self._repository, "_ai_wrote"):
                self._repository._ai_wrote = False
            if hasattr(self._repository, "_ai_synced_changes"):
                self._repository._ai_synced_changes = False
            if selection is None:
                if wrote or synced:
                    self._user_tags_loaded = False
                    if synced:
                        self._counts_loaded = False
                    self._settle(message, reload=synced or bool(self._ai_category))
                else:
                    self._status = message
                    self.changed.emit()
                return
            if message:
                self._user_tags_loaded = False
                if synced:
                    self._counts_loaded = False
                self._settle(message, reload=synced or bool(self._ai_category))
            elif (wrote or synced) and selection is not None:
                if self._status.startswith(_AI_PROGRESS):
                    self._status = ""
                self._user_tags_loaded = False
                if synced:
                    self._counts_loaded = False
                self._settle(self._status, reload=synced or bool(self._ai_category))
            else:
                # Nothing to report; do not leave the run's own progress line standing.
                if self._status.startswith(_AI_PROGRESS):
                    self._status = ""
                self.changed.emit()
            return
        if action == "distinct_counts":
            self._distinct_counts_loaded = True
            count = int(selection) if isinstance(selection, int) else 0
            if count != self._distinct_count:
                self._distinct_count = count
                self.duplicateDistinctChanged.emit()
            return
        if action == "statistics":
            self._statistics_wanted = False
            self.statisticsLoaded.emit(selection if isinstance(selection, dict) else {"error": _STATISTICS_ERROR})
            return
        if action == "ai_counts":
            self._ai_counts_loaded = True
            counts = [dict(item) for item in selection if isinstance(item, dict)] if isinstance(selection, list) else []
            if counts != self._ai_counts:
                self._ai_counts = counts
                self.aiCountsChanged.emit()
            return
        if action == "sync_tag_examples":
            if selection:
                self._reload_ai_rules()
            return
        if action == "teach":
            request_teach = self._teach_request
            self._teach_request = None
            self._release_exclusive()
            self._reload_ai_rules()
            if selection is None:
                self._status = message
                self.changed.emit()
                return
            if request_teach and len(request_teach) >= 4:
                taught_tag, taught_positive, taught_idents, taught_keys = request_teach[:4]
            else:
                taught_tag, taught_positive, taught_idents = request_teach or ("", True, [])
                taught_keys = [self.selectedKey] if len(taught_idents) == 1 and self.selectedKey else []
            taught_count, propagated = (
                selection if isinstance(selection, tuple) and len(selection) == 2 else (0, 0)
            )
            single_key = taught_keys[0] if len(taught_keys) == 1 else ""
            kept_by_user = bool(
                single_key
                and self._ai_category
                and self._selected.get("key") == single_key
                and any(
                    isinstance(t, dict) and t.get("tag") == self._ai_category and t.get("source") == "user"
                    for t in self._selected.get("tags", [])
                )
            )
            if not self._ai_category or taught_count <= 0:
                reload = False
            elif taught_positive and propagated == 0:
                reload = False
            elif not taught_positive and propagated == 0 and taught_tag and taught_tag != self._ai_category:
                reload = False
            elif (not taught_positive and propagated == 0
                  and (not taught_tag or taught_tag == self._ai_category)
                  and len(taught_idents) == 1 and single_key
                  and self._selected.get("key") == single_key and not kept_by_user
                  and self.removeDeleted([single_key])):
                reload = False
            else:
                reload = True
            self._settle(message, reload=reload)
            return
        if action == "scan_duplicates":
            dup_synced = bool(getattr(self._repository, "_dup_synced_changes", False))
            if hasattr(self._repository, "_dup_synced_changes"):
                self._repository._dup_synced_changes = False
            self._release_exclusive()
            if dup_synced:
                self._counts_loaded = False
                self._user_tags_loaded = False
            if not isinstance(selection, int):
                if dup_synced:
                    self._settle(message, reload=True)
                else:
                    self._status = message
                    self.changed.emit()
                return
            self._duplicate_count = selection
            self._duplicate_count_stamp += 1
            self._duplicate_counts_loaded = True
            self.duplicateCountChanged.emit()
            self._settle(message, reload=self._duplicate_only or dup_synced)
            return
        if action in ("scan_ai_tags", "reclassify"):
            self._release_exclusive()
            self._reload_ai_rules()     # what the tags learned, for the AI tag manager
            wrote = bool(getattr(self._repository, "_ai_wrote", False))
            synced = bool(getattr(self._repository, "_ai_synced_changes", False))
            if hasattr(self._repository, "_ai_wrote"):
                self._repository._ai_wrote = False
            if hasattr(self._repository, "_ai_synced_changes"):
                self._repository._ai_synced_changes = False
            if selection is None:
                if wrote or synced:
                    self._user_tags_loaded = False
                    if synced:
                        self._counts_loaded = False
                    self._settle(message, reload=synced or bool(self._ai_category))
                else:
                    self._status = message
                    self.changed.emit()
                return
            self._user_tags_loaded = False
            if synced:
                self._counts_loaded = False
            self._settle(message, reload=synced or bool(self._ai_category))
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
        if action == "select":
            preserve = (serial == self._preserve_status_serial)
            self._preserve_status_serial = 0
        else:
            preserve = False
        if not (action in {"select", "open", "reveal", "source"} and not message and (self._exclusive or preserve)):
            self._status = message
        self.changed.emit()

    @Slot(result=bool)
    def shutdown(self):
        if self._closing:
            return self._shutdown_complete
        self._closing = True
        self._auto_ai_timer.stop()
        with self._condition:
            self._closed = True
            self._pending.clear()
            self._interactive.clear()
            self._navigation = None
            self._condition.notify_all()
        if hasattr(self._repository, "cancel_ai_scan"):
            self._repository.cancel_ai_scan()
        if hasattr(self._repository, "cancel_duplicate_scan"):
            self._repository.cancel_duplicate_scan()
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
