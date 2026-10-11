"""Local media catalog and run history, independent of download archives.

No cookies, media request URLs, or arbitrary tweet metadata are stored. Old archive rows are
read-only inputs. Legacy file modification times are never called save times.
"""

from __future__ import annotations

import bisect
import csv
import functools
import json
import os
import re
import sqlite3
import stat
import uuid
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .storage import StoragePaths, prepare_storage, records_root


_KOREA = timezone(timedelta(hours=9))
_MODES = {"author", "date", "flat"}
_TYPES = {"photo", "video", "animated_gif"}
_HISTORY_HELP = "저장 내역을 읽거나 기록하지 못했습니다. 저장 폴더의 권한과 다른 실행 중인 작업을 확인해 주세요."
_INTERRUPTED_MESSAGE = "이전 작업이 비정상 종료되어 중단으로 정리했습니다. 받은 파일과 진행 수치는 그대로입니다."
_ENTRY = re.compile(r"(?:twitter)?([0-9]{1,20})_(?:(video|animated_gif|gif)_)?([0-9]{1,5})\Z")
_FILENAME = re.compile(r"([0-9]{1,20})_(?:(video|gif|animated_gif)_)?([0-9]{1,5})\.(jpg|jpeg|png|webp|gif|mp4)\Z", re.I)
_ARCHIVES = {"author": ".xlikesaver-archive.sqlite3",
             "date": ".xlikesaver-archive.date.sqlite3",
             "flat": ".xlikesaver-archive.flat.sqlite3"}
# Rows per write transaction while importing; small enough that a concurrent
# download's single-row write never waits near SQLite's busy timeout.
_WRITE_BATCH = 200
_RUN_FIELDS = {"saved", "skipped", "size_skipped", "visited", "last_tweet_id",
               "last_posted_at", "message"}
_COUNTERS = {"saved", "skipped", "size_skipped", "visited"}
_CSV_FIELDS = ("folder_mode", "tweet_id", "num", "media_type", "author", "author_id", "filename",
               "posted_at", "posted_source", "saved_at", "path", "filesize", "status", "record_source",
               "source_url", "metadata_status", "author_metadata_status")
_CSV_LIKE_FIELDS = ("liked_after", "liked_before")
_CSV_TWEET_FIELDS = ("content", "hashtags")
_TWEET_TEXT_LIMIT = 5000
# Catalog rows joined with what is known per tweet (Like order and text).
_JOINED = ("media LEFT JOIN likes ON likes.tweet_id=media.tweet_id "
           "LEFT JOIN tweets ON tweets.tweet_id=media.tweet_id")
# Sort orders for the records list. Like order exists only for tweets a run
# has walked since 1.8.0; the rest follow in tweet order.
_ORDERS = {
    "saved": "COALESCE(media.saved_at,media.posted_at,'') DESC,media.tweet_id DESC",
    "liked": "likes.like_key IS NULL,likes.like_key DESC,media.tweet_id DESC",
    "posted": "COALESCE(media.posted_at,'') DESC,media.tweet_id DESC",
    "posted_oldest": "media.posted_at IS NULL,media.posted_at,media.tweet_id",
}
_PERIOD_FIELDS = {"posted": "media.posted_at", "saved": "media.saved_at"}
# 'retry' runs download chosen missing files by tweet, not the Like list.
_SCAN_MODES = {"full", "quick", "retry"}
_DELETION_STATUSES = {"pending", "recycled", "failed", "cancelled", "interrupted"}


class HistoryError(RuntimeError):
    """A fixed application error; source exceptions may include user paths."""


class CatalogDamaged(HistoryError):
    """The records database file itself is damaged; it can be rebuilt."""


_DAMAGED_HELP = ("기록 DB가 손상됐습니다. ‘기록 · 통계’에서 ‘기록 복구’를 눌러 주세요. "
                 "사진·영상과 중복 방지 기록은 그대로 있습니다.")
# SQLITE_CORRUPT and SQLITE_NOTADB, including their extended codes.
_DAMAGED_CODES = {11, 26}


def _safe(method):
    @functools.wraps(method)
    def call(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except HistoryError:
            raise
        except sqlite3.DatabaseError as exc:
            if (getattr(exc, "sqlite_errorcode", 0) or 0) & 0xFF in _DAMAGED_CODES:
                raise CatalogDamaged(_DAMAGED_HELP) from None
            raise HistoryError(_HISTORY_HELP) from None
        except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
            raise HistoryError(_HISTORY_HELP) from None
    return call


def _within(path: Path, folder: Path) -> bool:
    """``path.is_relative_to(folder)`` for absolute paths, by string prefix.

    Python 3.12 builds and compares every parent folder; with several checks
    per file that dominated refreshing a large library. The comparison stays
    lexical and case-insensitive on Windows, exactly like pathlib's.
    """
    child, parent = str(path), str(folder)
    if not (os.path.isabs(child) and os.path.isabs(parent)):
        return path.is_relative_to(folder)
    if os.name == "nt":
        child, parent = child.lower(), parent.lower()
    return child == parent or child.startswith(parent if parent.endswith(os.sep) else parent + os.sep)


def _now() -> str:
    return datetime.now(_KOREA).isoformat(timespec="microseconds")


def _posted(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValueError("invalid date")
    if value.tzinfo is None:
        # gallery-dl's legacy tweet dates are naive UTC datetimes.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_KOREA).isoformat(timespec="seconds")


def _estimated_posted(tweet_id: str) -> str | None:
    value = int(tweet_id)
    if not 2**22 <= value <= 2**63 - 1:
        return None
    milliseconds = (value >> 22) + 1_288_834_974_657
    posted = datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
    if posted > datetime.now(timezone.utc) + timedelta(days=1):
        return None
    return _posted(posted)


def _clean_text(value, limit=2000) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value[:limit] if ord(char) >= 32 and ord(char) != 127)


def _author_fields(value) -> tuple[str, str]:
    """Only gallery-dl's tweet author is authoritative, never its `user`."""
    if not isinstance(value, dict):
        return "", ""
    name = value.get("name", "")
    name = name.lstrip("@") if isinstance(name, str) else ""
    ident = str(value.get("id") or "")
    return (name if re.fullmatch(r"[A-Za-z0-9_]{1,15}", name) else "",
            ident if re.fullmatch(r"[0-9]{1,20}", ident) else "")


def _date_folder(name: str) -> bool:
    try:
        return datetime.strptime(name, "%Y-%m-%d").strftime("%Y-%m-%d") == name
    except ValueError:
        return False


def _identity(row: dict) -> tuple[str, int, str]:
    return row["tweet_id"], row["num"], row["media_type"]


def archive_key(tweet_id: str, number: int, media_type: str) -> str:
    """The worker's download-archive key for one media (without the prefix)."""
    return f"{tweet_id}_{number}" if media_type == "photo" else f"{tweet_id}_{media_type}_{number}"


def _csv_cell(value):
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


class HistoryStore:
    @_safe
    def __init__(self, account_dir: Path, *, media_dir: Path | None = None,
                 output_dir: Path | None = None, legacy_dir: Path | None = None):
        self.account_dir = Path(account_dir).expanduser().resolve()
        self.media_dir = Path(media_dir if media_dir is not None else account_dir).expanduser().resolve()
        self.output_dir = Path(output_dir if output_dir is not None else self.media_dir).expanduser().resolve()
        self.legacy_dir = Path(legacy_dir).expanduser() if legacy_dir is not None else None
        self._isolated_records = media_dir is not None
        self._folder_cache = None
        # This PC's live records and their backups are never media, even when
        # a chosen media folder happens to contain the app data folder.
        records = records_root().resolve()
        self._app_data = (records, records.parent / "backups")
        self.account_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.account_dir / ".xlikesaver-history.sqlite3"
        self._db = sqlite3.connect(self.path, timeout=5)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA journal_mode=WAL")
            # Flush WAL commits so progress is more durable after an OS crash.
            # Hardware/filesystem failures still cannot be fully guaranteed.
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS media (
                    folder_mode TEXT NOT NULL, tweet_id TEXT NOT NULL, num INTEGER NOT NULL,
                    media_type TEXT NOT NULL, author TEXT NOT NULL DEFAULT '',
                    filename TEXT NOT NULL DEFAULT '', posted_at TEXT, posted_source TEXT NOT NULL DEFAULT '',
                    saved_at TEXT, path TEXT NOT NULL DEFAULT '', filesize INTEGER,
                    status TEXT NOT NULL, record_source TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(folder_mode, tweet_id, num, media_type)
                );
                CREATE INDEX IF NOT EXISTS media_posted ON media(posted_at);
                CREATE INDEX IF NOT EXISTS media_type ON media(media_type);
                CREATE INDEX IF NOT EXISTS media_path ON media(path);
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, folder_mode TEXT NOT NULL,
                    started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
                    saved INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
                    size_skipped INTEGER NOT NULL DEFAULT 0, visited INTEGER NOT NULL DEFAULT 0,
                    last_tweet_id TEXT NOT NULL DEFAULT '', last_posted_at TEXT,
                    message TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS catalog_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS likes (
                    tweet_id TEXT PRIMARY KEY, like_key REAL NOT NULL,
                    liked_after TEXT, liked_before TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS likes_order ON likes(like_key);
                CREATE TABLE IF NOT EXISTS tweets (
                    tweet_id TEXT PRIMARY KEY, content TEXT NOT NULL DEFAULT '',
                    hashtags TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS missing_choices (
                    tweet_id TEXT NOT NULL, num INTEGER NOT NULL, media_type TEXT NOT NULL,
                    ignored INTEGER NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(tweet_id, num, media_type)
                );
                CREATE TABLE IF NOT EXISTS deletion_items (
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
                    tweet_id TEXT NOT NULL, num INTEGER NOT NULL, media_type TEXT NOT NULL,
                    folder_mode TEXT NOT NULL, path TEXT NOT NULL, filename TEXT NOT NULL,
                    filesize INTEGER, signature TEXT NOT NULL,
                    posted_at TEXT, author TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '', recycle_path TEXT NOT NULL DEFAULT '',
                    keep_deleted INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS deletion_created ON deletion_items(created_at);
                -- The library filter looks up each record's deletion receipt.
                CREATE INDEX IF NOT EXISTS deletion_identity ON deletion_items(tweet_id,num,media_type);
            """)
            primary = [row["name"] for row in sorted(self._db.execute("PRAGMA table_info(media)").fetchall(),
                                                     key=lambda row: row["pk"]) if row["pk"]]
            if primary == ["folder_mode", "tweet_id", "num"]:
                # Accommodate an early catalog created during development. This
                # never changes the independent gallery-dl archive databases.
                with self._db:
                    self._db.execute("BEGIN IMMEDIATE")
                    self._db.execute("ALTER TABLE media RENAME TO media_old")
                    self._db.execute("CREATE TABLE media AS SELECT * FROM media_old WHERE 0")
                    self._db.execute("CREATE UNIQUE INDEX media_identity ON media(folder_mode,tweet_id,num,media_type)")
                    self._db.execute("INSERT INTO media SELECT * FROM media_old")
                    self._db.execute("DROP TABLE media_old")
                    self._db.execute("CREATE INDEX IF NOT EXISTS media_posted ON media(posted_at)")
                    self._db.execute("CREATE INDEX IF NOT EXISTS media_type ON media(media_type)")
                    self._db.execute("CREATE INDEX IF NOT EXISTS media_path ON media(path)")
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(media)")}
            with self._db:
                if "source_url" not in columns:
                    self._db.execute("ALTER TABLE media ADD COLUMN source_url TEXT NOT NULL DEFAULT ''")
                if "metadata_status" not in columns:
                    self._db.execute("ALTER TABLE media ADD COLUMN metadata_status TEXT NOT NULL DEFAULT 'pending'")
                if "author_id" not in columns:
                    self._db.execute("ALTER TABLE media ADD COLUMN author_id TEXT NOT NULL DEFAULT ''")
                if "author_metadata_status" not in columns:
                    self._db.execute("ALTER TABLE media ADD COLUMN author_metadata_status TEXT NOT NULL DEFAULT 'pending'")
                if "last_path" not in columns:
                    self._db.execute("ALTER TABLE media ADD COLUMN last_path TEXT NOT NULL DEFAULT ''")
                # Write only when an old row needs it. An UPDATE always waits
                # for the write lock, so every open (even a file opened from
                # the library while a save writes) used to wait up to 5 s.
                if self._db.execute("SELECT 1 FROM media WHERE source_url='' LIMIT 1").fetchone():
                    self._db.execute("UPDATE media SET source_url='https://x.com/i/web/status/' || tweet_id WHERE source_url=''")
            run_columns = {row["name"] for row in self._db.execute("PRAGMA table_info(runs)")}
            with self._db:
                # Runs from earlier versions keep 'full' with no signature, so
                # the first run after an upgrade is always a complete scan.
                if "scan_mode" not in run_columns:
                    self._db.execute("ALTER TABLE runs ADD COLUMN scan_mode TEXT NOT NULL DEFAULT 'full'")
                if "media_signature" not in run_columns:
                    self._db.execute("ALTER TABLE runs ADD COLUMN media_signature TEXT NOT NULL DEFAULT ''")
            self._media_roots = self._read_media_roots()
        except sqlite3.IntegrityError:
            # A damaged row can reach a NOT NULL check before SQLite reports
            # SQLITE_CORRUPT during the source-URL migration. Verify only this
            # error path; normal catalog opens need no full integrity scan.
            try:
                try:
                    checked = self._db.execute("PRAGMA quick_check").fetchall()
                except sqlite3.DatabaseError as exc:
                    damaged = (getattr(exc, "sqlite_errorcode", 0) or 0) & 0xFF in _DAMAGED_CODES
                else:
                    damaged = len(checked) != 1 or checked[0][0] != "ok"
                if damaged:
                    raise CatalogDamaged(_DAMAGED_HELP) from None
            finally:
                self._db.close()
            raise
        except Exception:
            self._db.close()
            raise

    @classmethod
    @_safe
    def for_storage(cls, paths: StoragePaths, *, protected: bool = False) -> "HistoryStore":
        prepare_storage(paths, protected=protected)
        return cls(paths.record_dir, media_dir=paths.media_dir, output_dir=paths.output_dir,
                   legacy_dir=paths.legacy_dir)

    def _read_media_roots(self) -> tuple[Path, ...]:
        roots = [self.output_dir]
        row = self._db.execute("SELECT value FROM catalog_meta WHERE key='allowed_media_roots'").fetchone()
        if row is None:
            return tuple(roots)
        try:
            values = json.loads(row[0])
        except (ValueError, TypeError):
            return tuple(roots)
        if not isinstance(values, list):
            return tuple(roots)
        for value in values:
            if not isinstance(value, str) or not Path(value).is_absolute():
                continue
            root = Path(os.path.abspath(value))
            if root not in roots:
                roots.append(root)
        return tuple(roots)

    @property
    def media_roots(self) -> tuple[Path, ...]:
        """Selected output roots whose known files may still be opened or tagged."""
        return self._media_roots

    @staticmethod
    def _linked(path: Path) -> bool:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())

    def _real_folders(self, folder: Path) -> bool:
        """Whether a folder and every ancestor are ordinary folders.

        Include the root and its ancestors: a formerly ordinary folder can
        later be replaced with a junction to another location, so outside a
        whole-folder rescan every check looks again.
        """
        current = folder
        while True:
            if self._linked(current):
                return False
            if current == current.parent:
                return True
            current = current.parent

    def _path_in_root(self, path: Path | str, root: Path) -> Path | None:
        try:
            supplied = Path(os.path.abspath(path))
            if not _within(supplied, root) or self._linked(supplied):
                return None
            cache = self._folder_cache
            if cache is None:
                # A single check (e.g. a download target) resolves the file itself.
                if not self._real_folders(supplied.parent):
                    return None
                candidate = Path(path).resolve()
                if not _within(candidate, root):
                    return None
            else:
                # A whole-folder rescan only records paths; it checks each
                # folder once. Writes and opens later check the file again.
                key = (supplied.parent, root)
                if key not in cache:
                    folder = supplied.parent.resolve() if self._real_folders(supplied.parent) else None
                    cache[key] = folder if folder is not None and _within(folder, root) else None
                if cache[key] is None:
                    return None
                candidate = cache[key] / supplied.name
            record_roots = getattr(self, "_media_roots", (self.output_dir,))
            if self._isolated_records and any(_within(candidate, parent / ".xlikesaver") for parent in record_roots):
                return None
            if any(_within(candidate, folder) for folder in self._app_data):
                return None
            return candidate
        except (OSError, RuntimeError, ValueError):
            return None

    def is_download_path(self, path: Path | str) -> bool:
        """New downloads may only write within the currently selected output."""
        return self._path_in_root(path, self.output_dir) is not None

    def _media_path(self, path: Path | str) -> Path | None:
        """The resolved path when it may be read (see is_media_path)."""
        candidate = self._path_in_root(path, self.output_dir)
        if candidate is not None:
            return candidate
        for root in self.media_roots[1:]:
            candidate = self._path_in_root(path, root)
            if candidate is not None and self._db.execute(
                    "SELECT 1 FROM media WHERE path=? LIMIT 1", (str(candidate),)).fetchone():
                return candidate
        return None

    def is_media_path(self, path: Path | str) -> bool:
        """Allow former roots only for exact paths already present in this catalog."""
        return self._media_path(path) is not None

    def _preserve_missing_location(self, path: str, actual: str) -> str:
        # A disconnected drive should remain visible and become usable again
        # after reconnecting, rather than losing its exact original location.
        if not actual and path and self.is_media_path(path) and not self.is_download_path(path):
            return path
        return actual

    def _mode(self, value: str) -> str:
        if value not in _MODES:
            raise HistoryError(_HISTORY_HELP)
        return value

    def _key(self, metadata: dict) -> tuple[str, int]:
        tweet_id = str(metadata.get("tweet_id", ""))
        number = metadata.get("num")
        if (not re.fullmatch(r"[0-9]{1,20}", tweet_id) or isinstance(number, bool) or
                not isinstance(number, int) or not 1 <= number <= 10000):
            raise HistoryError(_HISTORY_HELP)
        return tweet_id, number

    def _file(self, path: Path | str) -> tuple[str, int | None, str]:
        candidate = Path(path)
        # Check both the supplied path and resolved target to avoid importing
        # symlink/junction targets outside the explicitly selected account.
        if self._linked(candidate):
            return "", None, candidate.name
        resolved = self._media_path(candidate)
        if resolved is None:
            raise HistoryError(_HISTORY_HELP)
        candidate = resolved
        try:
            status = candidate.stat()
        except FileNotFoundError:
            return "", None, candidate.name
        if not stat.S_ISREG(status.st_mode):
            return "", None, candidate.name
        return str(candidate), status.st_size, candidate.name

    def _upsert(self, row: dict, *, replace_path=True) -> None:
        row["source_url"] = "https://x.com/i/web/status/" + row["tweet_id"]
        row.setdefault("metadata_status", "pending")
        row.setdefault("author_id", "")
        row.setdefault("author_metadata_status", "pending")
        previous = self._db.execute("SELECT * FROM media WHERE folder_mode=? AND tweet_id=? AND num=? AND media_type=?",
                                    (row["folder_mode"], row["tweet_id"], row["num"], row["media_type"])).fetchone()
        if previous is not None:
            old = dict(previous)
            row.setdefault("last_path", old["last_path"])
            if not row["author"] or (row["record_source"] in {"archive", "file_scan"} and old["author"]):
                row["author"] = old["author"]
            if not row["author_id"]:
                row["author_id"] = old["author_id"]
            if (not row["posted_at"] or (row["posted_source"] == "id_estimate" and
                                         old["posted_source"] == "metadata")):
                row["posted_at"], row["posted_source"] = old["posted_at"], old["posted_source"]
            if row["status"] != "saved":
                row["saved_at"] = old["saved_at"]
                if old["status"] == "saved":
                    row["status"] = "saved"
            if old["record_source"] == "saved" and row["record_source"] != "saved":
                row["record_source"] = "saved"
            if not replace_path:
                row["path"], row["filesize"] = old["path"], old["filesize"]
                row["filename"] = old["filename"] or row["filename"]
            if not row["filename"]:
                row["filename"] = old["filename"]
            if row["metadata_status"] == "pending":
                row["metadata_status"] = old["metadata_status"]
            if (row["author_metadata_status"] == "pending" and
                    row["author"] == old["author"] and
                    row["author_id"] == old["author_id"]):
                row["author_metadata_status"] = old["author_metadata_status"]
            # An archive-only row describes an old photo, not the exact type of
            # a file that has since been imported with richer metadata.
            if row["record_source"] == "archive":
                row["media_type"] = old["media_type"]
        if previous is not None and all(row[key] == previous[key] for key in row if key != "updated_at"):
            # Nothing changed: skip the write (and the disk flush of its commit).
            return
        row["updated_at"] = _now()
        columns = tuple(row)
        self._db.execute("INSERT OR REPLACE INTO media (" + ",".join(columns) + ") VALUES (" +
                         ",".join("?" for _ in columns) + ")", tuple(row[key] for key in columns))

    @_safe
    def recover_interrupted_runs(self) -> int:
        """Recover abandoned runs only after the worker holds AccountGuard.

        Read-only record windows never call this method. No actual end time can
        be reconstructed after a crash, so keep finished_at unknown.
        """
        with self._db:
            result = self._db.execute(
                "UPDATE runs SET status='interrupted',message=? WHERE status='running' AND finished_at IS NULL",
                (_INTERRUPTED_MESSAGE,))
        return result.rowcount

    @_safe
    def start_run(self, folder_mode: str, *, scan_mode: str = "full", media_signature: str = "") -> int:
        if scan_mode not in _SCAN_MODES or not isinstance(media_signature, str):
            raise HistoryError(_HISTORY_HELP)
        with self._db:
            row = self._db.execute(
                "INSERT INTO runs(folder_mode,started_at,status,scan_mode,media_signature) VALUES (?,?,?,?,?)",
                (self._mode(folder_mode), _now(), "running", scan_mode, _clean_text(media_signature, 200)))
        return row.lastrowid

    @_safe
    def quick_scan_readiness(self, folder_mode: str, media_signature: str) -> str:
        """Whether a quick scan may stop at the first long run of saved files.

        That is only safe when the latest run of this layout went through to a
        normal end with the same media settings: everything older than the
        point where saved files resume was then covered by that run (or, by
        induction, by the complete runs before it). Returns 'ready',
        'settings-changed' or 'incomplete'.
        """
        # Re-downloading chosen files does not walk the Like list, so it never
        # serves as a complete scan.
        row = self._db.execute(
            "SELECT status,media_signature FROM runs WHERE folder_mode=? AND status!='running' "
            "AND scan_mode!='retry' ORDER BY started_at DESC,id DESC LIMIT 1", (self._mode(folder_mode),)).fetchone()
        if row is None or row["status"] != "finished":
            return "incomplete"
        return "ready" if row["media_signature"] == media_signature else "settings-changed"

    def _run_values(self, fields: dict) -> dict:
        if not set(fields).issubset(_RUN_FIELDS):
            raise HistoryError(_HISTORY_HELP)
        values = {}
        for key, value in fields.items():
            if key in _COUNTERS:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise HistoryError(_HISTORY_HELP)
            elif key == "last_tweet_id":
                value = str(value or "")
                if value and not re.fullmatch(r"[0-9]{1,20}", value):
                    raise HistoryError(_HISTORY_HELP)
            elif key == "last_posted_at":
                value = _posted(value)
            elif key == "message":
                value = _clean_text(value)
            values[key] = value
        return values

    def _update_run(self, run_id: int, values: dict) -> None:
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
            raise HistoryError(_HISTORY_HELP)
        if not values:
            return
        changed = self._db.execute("UPDATE runs SET " + ",".join(key + "=?" for key in values) + " WHERE id=?",
                                   (*values.values(), run_id))
        if not changed.rowcount:
            raise HistoryError(_HISTORY_HELP)

    @_safe
    def update_run(self, run_id: int, **fields) -> None:
        with self._db:
            self._update_run(run_id, self._run_values(fields))

    @_safe
    def finish_run(self, run_id: int, status: str, **fields) -> None:
        if status not in {"finished", "stopped", "error", "limited", "partial", "cancelled"}:
            raise HistoryError(_HISTORY_HELP)
        values = self._run_values(fields)
        values.update(status=status, finished_at=_now())
        with self._db:
            self._update_run(run_id, values)

    @_safe
    def record_media(self, folder_mode: str, metadata: dict, path: Path | str, *, status="saved") -> None:
        if not isinstance(metadata, dict) or status not in {"saved", "existing"}:
            raise HistoryError(_HISTORY_HELP)
        mode = self._mode(folder_mode)
        tweet_id, number = self._key(metadata)
        media_type = metadata.get("type", "photo")
        if media_type not in _TYPES:
            raise HistoryError(_HISTORY_HELP)
        actual_path, filesize, filename = self._file(path)
        if status == "saved" and not actual_path:
            raise HistoryError(_HISTORY_HELP)
        author_data = metadata.get("author")
        # Preserve imported display/search text just as earlier catalogs did;
        # only the metadata writer accepts this as a validated X handle.
        author = _clean_text(author_data.get("name", ""), 100) if isinstance(author_data, dict) else ""
        _, author_id = _author_fields(author_data)
        posted = _posted(metadata.get("date"))
        row = {"folder_mode": mode, "tweet_id": tweet_id, "num": number, "media_type": media_type,
               "author": author, "author_id": author_id, "filename": filename, "posted_at": posted,
               "posted_source": "metadata" if posted else "", "saved_at": _now() if status == "saved" else None,
               "path": actual_path, "filesize": filesize, "status": status, "record_source": status}
        metadata_status = metadata.get("_xlikesaver_metadata_status", "pending")
        if metadata_status not in {"embedded", "failed", "unsupported", "pending"}:
            raise HistoryError(_HISTORY_HELP)
        row["metadata_status"] = metadata_status
        author_status = metadata.get("_xlikesaver_author_metadata_status", "pending")
        if author_status not in {"embedded", "unknown", "error", "pending"}:
            raise HistoryError(_HISTORY_HELP)
        row["author_metadata_status"] = author_status
        with self._db:
            self._upsert(row)

    @_safe
    def media_record(self, folder_mode: str, metadata: dict) -> dict | None:
        """Look up a single layout/media identity without changing its record."""
        tweet_id, number = self._key(metadata)
        media_type = metadata.get("type", "photo")
        if media_type not in _TYPES:
            raise HistoryError(_HISTORY_HELP)
        row = self._db.execute("SELECT * FROM media WHERE folder_mode=? AND tweet_id=? AND num=? AND media_type=?",
                               (self._mode(folder_mode), tweet_id, number, media_type)).fetchone()
        return dict(row) if row else None

    @_safe
    def get_metadata_status(self, folder_mode: str, metadata: dict) -> str:
        row = self.media_record(folder_mode, metadata)
        return row["metadata_status"] if row else "pending"

    @_safe
    def mark_metadata(self, row: dict, status: str, *, author_status: str | None = None) -> None:
        """Update a local tag operation without changing the original save time."""
        if status not in {"embedded", "failed", "unsupported", "pending"}:
            raise HistoryError(_HISTORY_HELP)
        if author_status is not None and author_status not in {"embedded", "unknown", "error", "pending"}:
            raise HistoryError(_HISTORY_HELP)
        actual, size, _ = self._file(row.get("path") or "")
        actual = self._preserve_missing_location(row.get("path") or "", actual)
        with self._db:
            self._db.execute("UPDATE media SET metadata_status=?,source_url=?,path=?,filesize=?,updated_at=? WHERE folder_mode=? AND tweet_id=? AND num=? AND media_type=?",
                             (status, "https://x.com/i/web/status/" + row["tweet_id"], actual, size, _now(), row["folder_mode"], row["tweet_id"], row["num"], row["media_type"]))
            if author_status is not None:
                self._db.execute("UPDATE media SET author_metadata_status=? WHERE folder_mode=? AND tweet_id=? AND num=? AND media_type=?",
                                 (author_status, row["folder_mode"], row["tweet_id"], row["num"], row["media_type"]))

    @_safe
    def mp4_gif_records(self) -> list[dict]:
        """Animated GIF tweets that are still stored as MP4 files."""
        return [dict(row) for row in self._db.execute(
            "SELECT * FROM media WHERE media_type='animated_gif' AND lower(path) LIKE '%.mp4' "
            "ORDER BY COALESCE(saved_at,posted_at,'') DESC,tweet_id DESC,num")]

    @_safe
    def replace_media_file(self, row: dict, path: Path | str, status: str, *, author_status: str) -> bool:
        """Point one record at its converted file, keeping its save time.

        Only a record still at its old path changes, so a concurrent refresh
        or download that moved it meanwhile is never overwritten.
        """
        if status not in {"embedded", "failed", "unsupported", "pending"}:
            raise HistoryError(_HISTORY_HELP)
        if author_status not in {"embedded", "unknown", "error", "pending"}:
            raise HistoryError(_HISTORY_HELP)
        candidate = Path(path)
        # A former output root only authorizes paths already in the catalog,
        # so also accept the converted file beside such a recorded original.
        beside_original = (row.get("path") and self.is_media_path(row["path"]) and
                           Path(os.path.abspath(candidate)).parent == Path(os.path.abspath(row["path"])).parent and
                           any(self._path_in_root(candidate, root) is not None for root in self.media_roots))
        if not (beside_original or self.is_media_path(candidate)):
            raise HistoryError(_HISTORY_HELP)
        try:
            candidate = candidate.resolve(strict=True)
            size = candidate.stat().st_size
        except OSError:
            raise HistoryError(_HISTORY_HELP) from None
        if not candidate.is_file():
            raise HistoryError(_HISTORY_HELP)
        actual, filename = str(candidate), candidate.name
        with self._db:
            changed = self._db.execute(
                "UPDATE media SET path=?,filename=?,filesize=?,metadata_status=?,author_metadata_status=?,updated_at=? "
                "WHERE folder_mode=? AND tweet_id=? AND num=? AND media_type=? AND path=?",
                (actual, filename, size, status, author_status, _now(),
                 row["folder_mode"], row["tweet_id"], row["num"], row["media_type"], row["path"]))
        return changed.rowcount == 1

    def _legacy_row(self, mode: str, tweet_id: str, number: int, *, path="", filesize=None,
                    filename="", media_type="photo", author="", source="archive") -> dict:
        posted = _estimated_posted(tweet_id)
        return {"folder_mode": mode, "tweet_id": tweet_id, "num": number, "media_type": media_type,
                "author": _clean_text(author, 100), "filename": filename or f"{tweet_id}_{number}",
                "posted_at": posted, "posted_source": "id_estimate" if posted else "",
                "saved_at": None, "path": path, "filesize": filesize,
                "status": "existing", "record_source": source}

    def _files(self):
        roots = [(self.media_dir, self._isolated_records and self.media_dir == self.output_dir)]
        if self.legacy_dir is not None and self.legacy_dir != self.media_dir:
            roots.append((self.legacy_dir, False))
        for root, restricted in roots:
            if not root.is_dir() or root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
                continue
            yield from self._scan_files(root, restricted)

    def _scan_files(self, root, restricted):
        pending = [root]
        account_names = set()
        if restricted and (self.output_dir / ".xlikesaver").is_dir():
            account_names = {path.name for path in (self.output_dir / ".xlikesaver").iterdir() if path.is_dir()}
        if self.legacy_dir is not None:
            account_names.add(self.legacy_dir.name)
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if entry.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if (entry.name == ".xlikesaver" or (restricted and directory == root and entry.name in account_names)
                                or path in self._app_data):
                            continue
                        pending.append(path)
                    elif entry.is_file(follow_symlinks=False) and _FILENAME.fullmatch(entry.name):
                        yield path, root, restricted

    def _write_batches(self, items: list, apply) -> None:
        """Apply writes in short transactions so a download can record in between."""
        for start in range(0, len(items), _WRITE_BATCH):
            with self._db:
                for item in items[start:start + _WRITE_BATCH]:
                    apply(item)

    @_safe
    def import_legacy(self, *, refresh=False) -> dict:
        """Import once by default; an explicit refresh checks local files again.

        Files and archives are read outside any write transaction, then merged
        in short batches. A download worker sharing this catalog must never wait
        for a whole-library scan (it would exceed SQLite's busy timeout). Every
        batch is an idempotent merge and the completion marker is written last.
        """
        imported = self._db.execute("SELECT value FROM catalog_meta WHERE key='legacy_imported'").fetchone()
        if imported is not None and not refresh:
            return {"imported": False, "records": 0, "warnings": []}
        self._folder_cache = {}
        try:
            return self._import_legacy(refresh)
        finally:
            self._folder_cache = None

    def _restore_known_paths(self) -> None:
        """Reconnect restored files, including exact locations in former roots.

        Former roots are never scanned wholesale. Only a catalog row's saved
        last location may be probed, after the same folder/link checks used
        for current media. The conditional update preserves concurrent saves.
        """
        restored = []
        for row in self._db.execute(
                "SELECT folder_mode,tweet_id,num,media_type,last_path,updated_at FROM media "
                "WHERE path='' AND last_path!=''").fetchall():
            if not Path(row["last_path"]).is_absolute():
                continue
            for root in self.media_roots:
                candidate = self._path_in_root(row["last_path"], root)
                if candidate is not None:
                    break
            else:
                continue
            try:
                info = candidate.stat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                restored.append((str(candidate), info.st_size, _now(), row["folder_mode"],
                                 row["tweet_id"], row["num"], row["media_type"],
                                 row["last_path"], row["updated_at"]))
        self._write_batches(restored, lambda values: self._db.execute(
            "UPDATE media SET path=?,filesize=?,updated_at=? "
            "WHERE folder_mode=? AND tweet_id=? AND num=? AND media_type=? "
            "AND path='' AND last_path=? AND updated_at=?", values))

    def _import_legacy(self, refresh: bool) -> dict:
        before = self._db.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        warnings = []
        if refresh:
            changed = []
            for row in self._db.execute("SELECT folder_mode,tweet_id,num,media_type,path,filesize FROM media WHERE path!=''").fetchall():
                try:
                    actual, size, _ = self._file(row["path"])
                    actual = self._preserve_missing_location(row["path"], actual)
                except HistoryError:
                    # A copied/moved catalog may contain its old absolute
                    # paths. Do not inspect those files outside this folder;
                    # match files at the new location in the scan below.
                    actual, size = "", None
                if (actual, size) != (row["path"], row["filesize"]):
                    changed.append((actual, size, row["folder_mode"], row["tweet_id"], row["num"],
                                    row["media_type"], row["path"]))
            # The final path condition leaves rows a download changed meanwhile.
            self._write_batches(changed, lambda values: self._db.execute(
                "UPDATE media SET last_path=CASE WHEN ?='' THEN path ELSE last_path END,path=?,filesize=? "
                "WHERE folder_mode=? AND tweet_id=? AND num=? AND media_type=? AND path=?",
                (values[0], *values)))
            self._restore_known_paths()
        archived = []
        for mode, name in _ARCHIVES.items():
            archive = self.account_dir / name
            if not archive.is_file() or archive.is_symlink():
                continue
            try:
                with closing(sqlite3.connect(archive.as_uri() + "?mode=ro", uri=True, timeout=5)) as legacy:
                    for (entry,) in legacy.execute("SELECT entry FROM archive"):
                        match = _ENTRY.fullmatch(entry) if isinstance(entry, str) else None
                        if not match or not 1 <= int(match[3]) <= 10000:
                            continue
                        media_type = "animated_gif" if match[2] in {"gif", "animated_gif"} else match[2] or "photo"
                        archived.append(self._legacy_row(mode, match[1], int(match[3]), media_type=media_type))
            except sqlite3.Error:
                warnings = ["일부 기존 다운로드 기록을 읽지 못했습니다. 실제 파일에서 확인한 내역은 표시합니다."]
        # A known record with a date gains nothing from its archive entry.
        dated = {(row[0], row[1], row[2], row[3]) for row in self._db.execute(
            "SELECT folder_mode,tweet_id,num,media_type FROM media WHERE posted_at IS NOT NULL")}
        archived = [row for row in archived
                    if (row["folder_mode"], row["tweet_id"], row["num"], row["media_type"]) not in dated]
        self._write_batches(archived, lambda row: self._upsert(row, replace_path=False))
        known = {(row["folder_mode"], row["tweet_id"], row["num"], row["media_type"]): dict(row)
                 for row in self._db.execute("SELECT * FROM media")}
        known_media = {}
        for mode, tweet, number, kind in known:
            known_media.setdefault((tweet, number, kind), []).append(mode)
        known_paths = {row["path"]: row for row in known.values() if row["path"]}
        scanned = []
        for path, scan_root, restricted in self._files():
            match = _FILENAME.fullmatch(path.name)
            number = int(match[3])
            if not 1 <= number <= 10000:
                continue
            # The walk joined these names onto scan_root (relative_to would
            # rebuild every parent folder of every file).
            relative = path.parts[len(scan_root.parts):]
            parent = relative[0] if len(relative) > 1 else ""
            mode = "flat" if not parent else "date" if _date_folder(parent) else "author"
            actual, size, filename = self._file(path)
            extension = match[4].lower()
            tag = (match[2] or "").lower()
            media_type = "animated_gif" if tag in {"gif", "animated_gif"} or extension == "gif" else "video" if tag == "video" or extension == "mp4" else "photo"
            previous = known_paths.get(actual)
            if (previous is not None and previous["filesize"] == size and previous["filename"] == filename and
                    previous["posted_at"] and (previous["folder_mode"] != "author" or previous["author"])):
                # Already recorded exactly here; rewriting it would change nothing.
                continue
            if previous is not None:
                # An author's handle can equal the account-folder name.
                # The recorded identity resolves that directory ambiguity.
                mode, media_type = previous["folder_mode"], previous["media_type"]
                parent = previous["author"] if mode == "author" else parent
            elif self._isolated_records and (scan_root == self.legacy_dir or restricted):
                alternatives = known_media.get((match[1], number, media_type), [])
                if mode not in alternatives and len(alternatives) == 1:
                    old = known[(alternatives[0], match[1], number, media_type)]
                    # A manual move may leave only the previous layout's row.
                    # Reconnect it without losing author/save metadata. Keep
                    # present copies and disconnected former-root paths intact.
                    if scan_root == self.legacy_dir or not old["path"]:
                        mode = alternatives[0]
            if restricted and (match[1], number, media_type) not in known_media:
                # Shared output folders contain no reliable account marker.
                # Only this account's archive/catalog identities are valid.
                continue
            row = self._legacy_row(mode, match[1], number, path=actual, filesize=size,
                                   filename=filename, media_type=media_type,
                                   author=parent if mode == "author" else "", source="file_scan")
            previous_identity = self.media_record(mode, {"tweet_id": match[1], "num": number, "type": media_type})
            old_path = previous_identity.get("path", "") if previous_identity else ""
            keep_old = (old_path and not self.is_download_path(old_path) and
                        self.is_media_path(old_path) and Path(old_path).is_file())
            scanned.append((row, not keep_old))
        self._write_batches(scanned, lambda item: self._upsert(item[0], replace_path=item[1]))
        self._release_restored_choices()
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('legacy_imported',?)", (_now(),))
        after = self._db.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        return {"imported": True, "records": after - before, "warnings": warnings}

    def _release_restored_choices(self) -> None:
        """Allow downloads again once a recycled file is back at its original path.

        Restoring from the Recycle Bin means the user wants the file. Only the
        choice that deletion itself recorded (same time stamp) is released, so
        a later deletion of another copy or a later explicit choice is kept.
        """
        candidates = self._db.execute(
            "SELECT deletion_items.tweet_id,deletion_items.num,deletion_items.media_type,"
            "deletion_items.path,deletion_items.finished_at FROM deletion_items JOIN missing_choices "
            "ON missing_choices.tweet_id=deletion_items.tweet_id AND missing_choices.num=deletion_items.num "
            "AND missing_choices.media_type=deletion_items.media_type "
            "WHERE deletion_items.keep_deleted=1 AND deletion_items.status IN ('recycled','interrupted') "
            "AND missing_choices.ignored=1 AND missing_choices.updated_at=deletion_items.finished_at").fetchall()
        modes = sorted(_MODES)
        released = []
        for item in candidates:
            key = self._file_key(item["path"])
            present = [row for row in self._db.execute(
                "SELECT tweet_id,num,media_type,path FROM media WHERE folder_mode IN ("
                + ",".join("?" for _ in modes) + ") AND tweet_id=? AND path!=''", (*modes, item["tweet_id"]))
                       if self._file_key(row["path"]) == key and os.path.isfile(row["path"])]
            if present:
                identities = {(item["tweet_id"], item["num"], item["media_type"])}
                identities.update((row["tweet_id"], row["num"], row["media_type"]) for row in present)
                released.extend((*identity, item["finished_at"]) for identity in identities)
        if released:
            stamp = _now()
            with self._db:
                self._db.executemany(
                    "UPDATE missing_choices SET ignored=0,updated_at=? "
                    "WHERE tweet_id=? AND num=? AND media_type=? AND ignored=1 AND updated_at=?",
                    [(stamp, *values) for values in released])

    @staticmethod
    def _file_key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    def _unique_files(self, rows: list[dict], *, verify_files: bool) -> tuple[list[dict], int, set]:
        """A file view, leaving all layout-specific download records intact.

        The UI uses cached sizes while a background rescan checks the folder.
        Other callers verify present regular files, once per distinct path.
        Never deduplicate by tweet: two real copies are two files.
        """
        def priority(row):
            return (row["record_source"] in {"saved", "existing"}, row["updated_at"],
                    row["folder_mode"], row["tweet_id"], row["num"])

        groups = {}
        for row in rows:
            if row["path"]:
                groups.setdefault(self._file_key(row["path"]), []).append(row)
        present = {}
        previous_cache = self._folder_cache
        if verify_files:
            self._folder_cache = {}
        try:
            for key, group in groups.items():
                # Prefer download metadata to guesses made from an archive or
                # filename; newer metadata wins when an author changes names.
                if len(group) > 1:
                    group.sort(key=priority, reverse=True)
                if verify_files:
                    try:
                        path, size, _ = self._file(group[0]["path"])
                    except (HistoryError, OSError):
                        continue  # missing, inaccessible or outside authorized roots
                    if not path or size is None:
                        continue
                    key = self._file_key(path)
                else:
                    size = next((row["filesize"] for row in group if row["filesize"] is not None), None)
                    if size is None:
                        continue
                if key in present:
                    present[key][1].extend(group)
                else:
                    present[key] = (size, group)
        finally:
            self._folder_cache = previous_cache
        files = []
        matched = 0
        found = set()
        for size, group in present.values():
            matched += len(group)
            if len(group) == 1:
                # Most files have one record, which already is its own merge
                # below; skipping the general path halves a large library's load.
                item = group[0]
                found.add(_identity(item))
                row = dict(item)
                row["filesize"] = size
                row["saved_at"] = item["saved_at"] or None
                files.append(row)
                continue
            found.update(_identity(item) for item in group)
            group.sort(key=priority, reverse=True)
            row = dict(group[0])
            row["filesize"] = size
            author = next((item for item in group if item["author"]), None)
            if author:
                row["author"], row["author_id"] = author["author"], author["author_id"]
            posted = next((item for item in group if item["posted_source"] == "metadata" and item["posted_at"]),
                          next((item for item in group if item["posted_at"]), None))
            if posted:
                row["posted_at"], row["posted_source"] = posted["posted_at"], posted["posted_source"]
            saved = [item["saved_at"] for item in group if item["saved_at"]]
            row["saved_at"] = min(saved) if saved else None
            for field in ("metadata_status", "author_metadata_status"):
                if any(item[field] == "embedded" for item in group):
                    row[field] = "embedded"
            files.append(row)
        return files, matched, found

    def _file_snapshot(self, *, verify_files: bool) -> tuple[list[dict], list[dict], int, set]:
        # Plain tuples zipped with the column names convert a whole library
        # noticeably faster than sqlite3.Row objects.
        cursor = self._db.cursor()
        cursor.row_factory = None
        cursor.execute("SELECT * FROM media")
        names = [column[0] for column in cursor.description]
        rows = [dict(zip(names, values)) for values in cursor]
        files, matched, found = self._unique_files(rows, verify_files=verify_files)
        return rows, files, matched, found

    def _ignored(self) -> set:
        return {(row[0], row[1], row[2]) for row in self._db.execute(
            "SELECT tweet_id,num,media_type FROM missing_choices WHERE ignored=1 "
            "UNION SELECT tweet_id,num,media_type FROM deletion_items WHERE status='pending' AND keep_deleted=1")}

    def _summary(self, rows: list[dict], files: list[dict], matched: int, found: set) -> dict:
        # A media is missing when no layout's record points at a present file;
        # older layouts' records of a file that still exists elsewhere are not.
        missing = {_identity(row) for row in rows} - found
        ignored = self._ignored()
        dates = [row["posted_at"] for row in rows if row["posted_at"]]
        counts = {kind: sum(row["media_type"] == kind for row in files) for kind in _TYPES}
        result = {
            "files": len(files), "posts": len({row["tweet_id"] for row in rows}),
            "current_posts": len({row["tweet_id"] for row in files}), "record_count": len(rows),
            "duplicate_records": matched - len(files), "unmatched_records": len(rows) - matched,
            "missing_files": len(missing - ignored), "ignored_missing": len(missing & ignored),
            "known_files": len(files), "bytes": sum(row["filesize"] for row in files),
            "unknown_bytes": sum(row["filesize"] is None for row in rows),
            "oldest_posted_at": min(dates) if dates else None,
            "newest_posted_at": max(dates) if dates else None,
            "estimated_dates": sum(row["posted_source"] == "id_estimate" for row in rows),
            "counts": counts, **counts,
            "embedded_sources": sum(row["metadata_status"] == "embedded" for row in files),
            "embedded_authors": sum(row["author_metadata_status"] == "embedded" for row in files),
            "mp4_gifs": sum(row["media_type"] == "animated_gif" and row["path"].lower().endswith(".mp4")
                            for row in files),
        }
        result["last_run"] = next(iter(self.runs(limit=1)), None)
        result["archivepaths"] = [str(self.account_dir / name) for name in _ARCHIVES.values()
                                  if (self.account_dir / name).is_file() and not (self.account_dir / name).is_symlink()]
        return result

    @_safe
    def summary(self, *, verify_files: bool = True) -> dict:
        return self._summary(*self._file_snapshot(verify_files=verify_files))

    @_safe
    def overview(self, *, verify_files: bool = True, top: int | None = 20) -> dict:
        """Summary, charts and missing files share one snapshot, even during downloads."""
        rows, files, matched, found = self._file_snapshot(verify_files=verify_files)
        result = self._summary(rows, files, matched, found)
        result["stats"] = self._statistics(files, top)
        result["missing"] = self._missing(rows, found)
        return result

    @_safe
    def missing_media(self, *, verify_files: bool = True) -> list[dict]:
        rows, _, _, found = self._file_snapshot(verify_files=verify_files)
        return self._missing(rows, found)

    def _missing(self, rows: list[dict], found: set) -> list[dict]:
        """One entry per media without any present file, newest save first.

        Each carries what is known across its layouts, whether the user chose
        not to download it again, and the key that names it in an archive.
        Return the complete snapshot so filtering and paging cannot hide older
        media behind the user's choices to keep newer files deleted.
        """
        groups = {}
        for row in rows:
            identity = _identity(row)
            if identity not in found:
                groups.setdefault(identity, []).append(row)
        ignored = self._ignored()
        items = []
        for identity, group in groups.items():
            group.sort(key=lambda row: (row["record_source"] == "saved", row["updated_at"]), reverse=True)
            item = {key: group[0][key] for key in ("tweet_id", "num", "media_type", "filename", "source_url")}
            item["author"] = next((row["author"] for row in group if row["author"]), "")
            item["author_id"] = next((row["author_id"] for row in group if row["author_id"]), "")
            posted = next((row for row in group if row["posted_source"] == "metadata" and row["posted_at"]),
                          next((row for row in group if row["posted_at"]), None))
            item["posted_at"] = posted["posted_at"] if posted else None
            item["posted_source"] = posted["posted_source"] if posted else ""
            saved = [row["saved_at"] for row in group if row["saved_at"]]
            item["saved_at"] = min(saved) if saved else None
            item["folder_modes"] = sorted({row["folder_mode"] for row in group})
            item["ignored"] = identity in ignored
            item["archive_key"] = archive_key(*identity)
            items.append(item)
        items.sort(key=lambda item: (item["saved_at"] or item["posted_at"] or "", item["tweet_id"], item["num"]),
                   reverse=True)
        tweets = sorted({item["tweet_id"] for item in items})
        details = {}
        for start in range(0, len(tweets), 500):
            chunk = tweets[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            for row in self._db.execute("SELECT tweet_id,content,hashtags FROM tweets WHERE tweet_id IN (" + marks + ")",
                                        chunk):
                details[row[0]] = (row[1], row[2])
        for item in items:
            item["content"], item["hashtags"] = details.get(item["tweet_id"], ("", ""))
        return items

    @_safe
    def set_missing_ignored(self, items, ignored: bool) -> int:
        """Remember whether chosen missing media should stay deleted.

        ``items`` holds (tweet_id, num, media_type). The newest choice wins
        when records from another PC are merged.
        """
        if type(ignored) is not bool:
            raise HistoryError(_HISTORY_HELP)
        rows = []
        for tweet_id, number, media_type in items:
            tweet_id = str(tweet_id)
            if (not re.fullmatch(r"[0-9]{1,20}", tweet_id) or isinstance(number, bool) or not isinstance(number, int)
                    or not 1 <= number <= 10000 or media_type not in _TYPES):
                raise HistoryError(_HISTORY_HELP)
            rows.append((tweet_id, number, media_type, int(ignored), _now()))
        with self._db:
            self._db.executemany(
                "INSERT INTO missing_choices(tweet_id,num,media_type,ignored,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(tweet_id,num,media_type) DO UPDATE SET ignored=excluded.ignored,updated_at=excluded.updated_at",
                rows)
        return len(rows)

    @_safe
    def ignored_media(self) -> set:
        """Media the user chose not to download again, in any layout."""
        return self._ignored()

    @_safe
    def begin_deletion(self, batch_id: str, row: dict, keep_deleted: bool, signature) -> str:
        """Commit intent before the worker moves one approved file to the Recycle Bin.

        The journal preserves the original record even after a successful move
        clears its present-file path. File operations belong to the protected
        worker; recording intent never removes a file or its media history.
        """
        if (not isinstance(batch_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", batch_id)
                or type(keep_deleted) is not bool or not isinstance(row, dict)
                or not isinstance(signature, (list, tuple)) or len(signature) != 4
                or any(type(value) is not int or value < 0 for value in signature)):
            raise HistoryError(_HISTORY_HELP)
        tweet_id, number, media_type = row.get("tweet_id"), row.get("num"), row.get("media_type")
        mode, path = row.get("folder_mode"), row.get("path")
        if (not isinstance(tweet_id, str) or not re.fullmatch(r"[0-9]{1,20}", tweet_id)
                or type(number) is not int or not 1 <= number <= 10000
                or media_type not in _TYPES or mode not in _MODES
                or not isinstance(path, str) or not path or not Path(path).is_absolute()
                or not self.is_media_path(path)):
            raise HistoryError(_HISTORY_HELP)
        stored = self._db.execute(
            "SELECT * FROM media WHERE folder_mode=? AND tweet_id=? AND num=? AND media_type=?",
            (mode, tweet_id, number, media_type)).fetchone()
        if stored is None or not stored["path"] or self._file_key(stored["path"]) != self._file_key(path):
            raise HistoryError(_HISTORY_HELP)
        ident = uuid.uuid4().hex
        with self._db:
            self._db.execute(
                "INSERT INTO deletion_items(id,batch_id,tweet_id,num,media_type,folder_mode,path,filename,"
                "filesize,signature,posted_at,author,created_at,status,keep_deleted) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
                (ident, batch_id, tweet_id, number, media_type, mode, str(Path(os.path.abspath(path))),
                 stored["filename"] or Path(path).name, signature[2], json.dumps(list(signature)),
                 stored["posted_at"], stored["author"], _now(), int(keep_deleted)))
        return ident

    @staticmethod
    def _deletion_path_absent(path: str) -> bool:
        try:
            Path(path).lstat()
        except FileNotFoundError:
            return True
        except OSError:
            pass  # An inaccessible file is not evidence that it was removed.
        return False

    def _apply_deleted_path(self, item: dict, stamp: str) -> None:
        """Update only records of this physical path, within the journal commit."""
        path = item["path"]
        identities = {(item["tweet_id"], item["num"], item["media_type"])}
        # A file recreated at the original path is a present file. Never clear
        # its catalog path merely because an earlier copy was recycled.
        if self._deletion_path_absent(path) and self.is_media_path(path):
            # File names begin with their tweet ID, so only that tweet's
            # records can share this path. The mode list lets SQLite use the
            # primary key instead of reading the whole library for each file.
            modes = sorted(_MODES)
            matching = [row for row in self._db.execute(
                "SELECT folder_mode,tweet_id,num,media_type,path,last_path FROM media "
                "WHERE folder_mode IN (" + ",".join("?" for _ in modes) + ") AND tweet_id=? "
                "AND (path!='' OR last_path!='')", (*modes, item["tweet_id"]))
                        if self._file_key(row["path"] or row["last_path"]) == self._file_key(path)]
            for row in matching:
                identities.add((row["tweet_id"], row["num"], row["media_type"]))
                self._db.execute(
                    "UPDATE media SET last_path=CASE WHEN path='' THEN last_path ELSE path END,"
                    "path='',filesize=NULL,updated_at=? "
                    "WHERE folder_mode=? AND tweet_id=? AND num=? AND media_type=? AND path=?",
                    (stamp, row["folder_mode"], row["tweet_id"], row["num"], row["media_type"], row["path"]))
        for identity in identities:
            self._db.execute(
                "INSERT INTO missing_choices(tweet_id,num,media_type,ignored,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(tweet_id,num,media_type) DO UPDATE SET ignored=excluded.ignored,updated_at=excluded.updated_at",
                (*identity, item["keep_deleted"], stamp))

    @_safe
    def finish_deletion(self, ident: str, status: str, message: str = "", recycle_path: str = "") -> None:
        """Persist a verified outcome; repeated completion is harmless.

        ``recycled`` is supplied only after the native worker verifies a
        Recycle Bin receipt. Missing paths alone are never called successful.
        """
        if (not isinstance(ident, str) or status not in _DELETION_STATUSES - {"pending"}
                or not isinstance(message, str) or not isinstance(recycle_path, str)):
            raise HistoryError(_HISTORY_HELP)
        stored = self._db.execute("SELECT * FROM deletion_items WHERE id=?", (ident,)).fetchone()
        if stored is None:
            raise HistoryError(_HISTORY_HELP)
        item = dict(stored)
        if item["status"] != "pending":
            return
        stamp = _now()
        with self._db:
            if status == "recycled" or (status == "interrupted" and self._deletion_path_absent(item["path"])):
                self._apply_deleted_path(item, stamp)
            self._db.execute(
                "UPDATE deletion_items SET status=?,message=?,recycle_path=?,finished_at=? WHERE id=? AND status='pending'",
                (status, _clean_text(message, 2000), recycle_path, stamp, ident))

    @_safe
    def pending_deletions(self) -> list[dict]:
        return [dict(row) for row in self._db.execute(
            "SELECT * FROM deletion_items WHERE status='pending' ORDER BY created_at,id")]

    @_safe
    def reconcile_deletions(self) -> dict:
        """Settle interrupted intents while the caller holds the output locks.

        A surviving file remains untouched. A vanished file is marked as an
        uncertain interruption, retaining the chosen download policy and its
        original path in the journal. This never retries a deletion.
        """
        result = {"interrupted": 0, "missing": 0}
        for item in self.pending_deletions():
            absent = self._deletion_path_absent(item["path"])
            self.finish_deletion(item["id"], "interrupted",
                                 "작업이 중단되어 휴지통 이동 결과를 확인하지 못했습니다. 파일과 Windows 휴지통을 확인해 주세요."
                                 if absent else "이전 삭제 작업이 중단되었습니다. 파일은 그대로 두었습니다.")
            result["interrupted"] += 1
            result["missing"] += int(absent)
        return result

    @_safe
    def deletion_page(self, page=0, page_size=100) -> dict:
        def reader(limit, offset):
            return [dict(row) for row in self._db.execute(
                "SELECT * FROM deletion_items ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (limit, offset))]
        return self._page("deletion_items", (), page, page_size, reader)

    @staticmethod
    def _record_filter(media_type, query, period=None, author="", include_deleted=True):
        """WHERE clause on media; ``period`` is (field, first day, last day).

        Days are inclusive YYYY-MM-DD dates in Korean time, either may be empty.
        Stored times are Korean-time ISO strings, so they compare as text.
        """
        if media_type not in _TYPES | {"all"} or not isinstance(query, str) or type(include_deleted) is not bool:
            raise HistoryError(_HISTORY_HELP)
        clauses, params = [], []
        if not include_deleted:
            clauses.append("NOT (media.path='' AND media.last_path!='' AND EXISTS ("
                           "SELECT 1 FROM deletion_items WHERE deletion_items.tweet_id=media.tweet_id "
                           "AND deletion_items.num=media.num AND deletion_items.media_type=media.media_type "
                           "AND deletion_items.path=media.last_path "
                           + ("COLLATE NOCASE " if os.name == "nt" else "") +
                           "AND deletion_items.status IN ('recycled','interrupted')))" )
        if media_type != "all":
            clauses.append("media.media_type=?")
            params.append(media_type)
        if query:
            clauses.append("(media.author LIKE ? ESCAPE '\\' OR media.tweet_id LIKE ? ESCAPE '\\' OR media.filename LIKE ? ESCAPE '\\'"
                           " OR tweets.content LIKE ? ESCAPE '\\' OR tweets.hashtags LIKE ? ESCAPE '\\')")
            query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend(["%" + query + "%"] * 5)
        if author:
            if not isinstance(author, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", author):
                raise HistoryError(_HISTORY_HELP)
            clauses.append("media.author=? COLLATE NOCASE")
            params.append(author)
        if period is not None:
            field, first, last = period
            if field not in _PERIOD_FIELDS:
                raise HistoryError(_HISTORY_HELP)
            column = _PERIOD_FIELDS[field]
            if first:
                clauses.append(column + ">=?")
                params.append(datetime.strptime(first, "%Y-%m-%d").date().isoformat())
            if last:
                following = datetime.strptime(last, "%Y-%m-%d").date() + timedelta(days=1)
                clauses.append(column + "<?")
                params.append(following.isoformat())
        return " WHERE " + " AND ".join(clauses) if clauses else "", params

    @_safe
    def records(self, limit=100, offset=0, media_type="all", query="", *, order="saved", period=None,
                author="", include_deleted=True) -> list[dict]:
        if (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000 or
                isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or
                media_type not in _TYPES | {"all"} or not isinstance(query, str) or order not in _ORDERS):
            raise HistoryError(_HISTORY_HELP)
        where, params = self._record_filter(media_type, query, period, author, include_deleted)
        # The keys include the whole primary key, so both sorts agree exactly.
        order_by = " ORDER BY " + _ORDERS[order] + ",media.num,media.folder_mode,media.media_type"
        # Choose the page by row id first and attach Like order and tweet text
        # to those rows only. Sorting every joined row with its text took most
        # of each page's time in a large library. Only Like order and a text
        # search need the other tables while choosing.
        source = _JOINED if query or order == "liked" else "media"
        rows = self._db.execute("SELECT media.*,likes.like_key,likes.liked_after,likes.liked_before,"
                                "tweets.content,tweets.hashtags FROM (SELECT media.rowid AS chosen FROM " +
                                source + where + order_by + " LIMIT ? OFFSET ?) AS page "
                                "JOIN media ON media.rowid=page.chosen "
                                "LEFT JOIN likes ON likes.tweet_id=media.tweet_id "
                                "LEFT JOIN tweets ON tweets.tweet_id=media.tweet_id" + order_by,
                                (*params, limit, offset))
        return [dict(row) for row in rows]

    @_safe
    def runs(self, limit=20, offset=0) -> list[dict]:
        if (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000 or
                isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
            raise HistoryError(_HISTORY_HELP)
        return [dict(row) for row in self._db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))]

    @_safe
    def record_page(self, page=0, page_size=100, media_type="all", query="", *, order="saved", period=None,
                    author="", include_deleted=True) -> dict:
        where, params = self._record_filter(media_type, query, period, author, include_deleted)
        # Only a text search reads tweets; the joins never change the count.
        return self._page((_JOINED if query else "media") + where, params, page, page_size,
                          lambda limit, offset: self.records(limit, offset, media_type, query,
                                                             order=order, period=period, author=author,
                                                             include_deleted=include_deleted))

    @_safe
    def run_page(self, page=0, page_size=100) -> dict:
        return self._page("runs", (), page, page_size, self.runs)

    def _page(self, table, params, page, page_size, reader):
        if (isinstance(page, bool) or not isinstance(page, int) or page < 0 or
                isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 1000):
            raise HistoryError(_HISTORY_HELP)
        # A short read snapshot keeps totals and rows consistent while the
        # downloader writes new records through a separate WAL connection.
        with self._db:
            self._db.execute("BEGIN")
            total = self._db.execute("SELECT COUNT(*) FROM " + table, params).fetchone()[0]
            pages = (total + page_size - 1) // page_size
            page = min(page, max(0, pages - 1))
            rows = reader(page_size, page * page_size)
        return {"rows": rows, "total": total, "page": page, "pages": pages, "page_size": page_size}

    @_safe
    def export_records(self, destination: Path | str) -> int:
        exported = 0
        with Path(destination).open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            writer.writerow(_CSV_FIELDS + _CSV_LIKE_FIELDS + _CSV_TWEET_FIELDS)
            columns = ",".join(["media." + name for name in _CSV_FIELDS] + ["likes." + name for name in _CSV_LIKE_FIELDS] +
                               ["tweets." + name for name in _CSV_TWEET_FIELDS])
            for row in self._db.execute("SELECT " + columns + " FROM " + _JOINED + " ORDER BY media.tweet_id,media.num"):
                writer.writerow([_csv_cell(value) for value in row])
                exported += 1
        return exported

    @_safe
    def like_order(self, run_id: int) -> "LikeOrder":
        return LikeOrder(self, run_id)

    @_safe
    def record_tweets(self, tweets) -> None:
        """Keep each tweet's text and hashtags; ``tweets`` holds (id, text, tags)."""
        rows = []
        for tweet_id, content, hashtags in tweets:
            tweet_id = str(tweet_id)
            if not re.fullmatch(r"[0-9]{1,20}", tweet_id):
                continue
            tags = " ".join("#" + _clean_text(tag, 140) for tag in hashtags or () if isinstance(tag, str) and tag)
            # Line breaks become spaces; other control characters are dropped.
            text = " ".join(_clean_text(" ".join(content.split()) if isinstance(content, str) else "",
                                        _TWEET_TEXT_LIMIT).split())
            rows.append((tweet_id, text, tags[:2000], _now()))
        if not rows:
            return
        with self._db:
            self._db.executemany(
                "INSERT INTO tweets(tweet_id,content,hashtags,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(tweet_id) DO UPDATE SET content=excluded.content,hashtags=excluded.hashtags,"
                "updated_at=excluded.updated_at", rows)

    @_safe
    def statistics(self, top: int | None = 20, *, verify_files: bool = True) -> dict:
        """Current files by author/month, plus independent historical Likes.

        Months are Korean-time YYYY-MM. 'liked' counts only Likes whose period
        is known (first seen after an earlier complete run), by month seen.
        """
        _, files, *_ = self._file_snapshot(verify_files=verify_files)
        return self._statistics(files, top)

    def _statistics(self, files: list[dict], top: int | None) -> dict:
        """``top`` limits the author ranking; None lists every author."""
        if top is not None and (isinstance(top, bool) or not isinstance(top, int) or top < 0):
            raise HistoryError(_HISTORY_HELP)
        grouped = {}
        for row in files:
            if row["author"]:
                grouped.setdefault(row["author"].lower(), []).append(row)
        authors = [{"author": max(row["author"] for row in group), "files": len(group),
                    "posts": len({row["tweet_id"] for row in group}),
                    "latest": max((row["posted_at"] for row in group if row["posted_at"]), default=None)}
                   for group in grouped.values()]
        authors.sort(key=lambda row: (-row["files"], -row["posts"], row["author"].lower()))
        months = {}
        for name, field in (("posted", "posted_at"), ("saved", "saved_at")):
            counts = Counter(row[field][:7] for row in files if isinstance(row[field], str))
            # Check each distinct month once instead of every file.
            months[name] = sorted((month, count) for month, count in counts.items()
                                  if re.fullmatch(r"\d{4}-\d\d", month))
        months["liked"] = sorted((month, count) for month, count in self._db.execute(
            "SELECT substr(liked_before,1,7),COUNT(*) FROM likes WHERE liked_after IS NOT NULL GROUP BY 1")
            if isinstance(month, str) and re.fullmatch(r"\d{4}-\d\d", month))
        return {"authors": authors if top is None else authors[:top], "months": months}

    @_safe
    def author_samples(self, author: str, limit: int = 8) -> list[dict]:
        """The author's files from their newest tweets, one row per file."""
        if (not isinstance(author, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", author)
                or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100):
            raise HistoryError(_HISTORY_HELP)
        samples, seen = [], set()
        for row in self._db.execute(
                "SELECT * FROM media WHERE author=? COLLATE NOCASE AND path!='' "
                "ORDER BY COALESCE(posted_at,'') DESC,tweet_id DESC,num,folder_mode", (author,)):
            key = self._file_key(row["path"])
            path = self._media_path(row["path"])
            if key not in seen and path is not None and path.is_file():
                seen.add(key)
                samples.append(dict(row))
                if len(samples) == limit:
                    break
        return samples

    @_safe
    def close(self) -> None:
        self._db.close()


class LikeOrder:
    """Remember the order of Likes and when each was first seen.

    X lists Likes newest first but does not say when a tweet was liked. While a
    run walks that list, each newly seen tweet gets a sort key between the
    already known tweets around it, so sorting by key restores Like order. The
    tweet was liked before this run started (``liked_before``). Tweets newer
    than every known one were also liked after the previous complete run
    started (``liked_after``); older ones have no known lower bound.
    """

    STEP = 1024.0

    def __init__(self, store: HistoryStore, run_id: int):
        self._store = store
        self._keys = {row[0]: row[1] for row in store._db.execute("SELECT tweet_id,like_key FROM likes")}
        self._sorted = sorted(self._keys.values())
        started = store._db.execute("SELECT started_at FROM runs WHERE id=?", (run_id,)).fetchone()
        if started is None:
            raise HistoryError(_HISTORY_HELP)
        self._seen_by = started[0]
        with store._db:
            store._db.execute("INSERT OR IGNORE INTO catalog_meta(key,value) VALUES ('likes_tracked_since',?)",
                              (self._seen_by,))
        since = store._db.execute("SELECT value FROM catalog_meta WHERE key='likes_tracked_since'").fetchone()[0]
        # Only a normally finished run that also tracked Likes saw every Like
        # made before it started; a newer Like above it is from after then.
        previous = store._db.execute(
            "SELECT started_at FROM runs WHERE status='finished' AND id<? AND started_at>=? "
            "AND scan_mode!='retry' ORDER BY started_at DESC,id DESC LIMIT 1", (run_id, since)).fetchone()
        self._after = previous[0] if previous else None
        self._upper = None      # key of the last known tweet seen in this run
        self._pending = []      # new tweets since then, newest first
        self._visited = set()

    @_safe
    def visit(self, tweet_id: str) -> None:
        tweet_id = str(tweet_id)
        if tweet_id in self._visited or not re.fullmatch(r"[0-9]{1,20}", tweet_id):
            return
        self._visited.add(tweet_id)
        key = self._keys.get(tweet_id)
        if key is None:
            self._pending.append(tweet_id)
            return
        self._place(key)
        self._upper = key

    @_safe
    def flush(self) -> None:
        """Place tweets seen after the last known one (call when a run ends)."""
        self._place(None)

    def _above(self, key):
        index = bisect.bisect_right(self._sorted, key)
        return self._sorted[index] if index < len(self._sorted) else None

    def _below(self, key):
        index = bisect.bisect_left(self._sorted, key)
        return self._sorted[index - 1] if index else None

    @staticmethod
    def _between(low, high, count):
        """``count`` keys strictly between low and high, largest first."""
        if high is None:
            low = 0.0 if low is None else low
            return [low + LikeOrder.STEP * (count - index) for index in range(count)]
        if low is None:
            return [high - LikeOrder.STEP * (index + 1) for index in range(count)]
        return [low + (high - low) * (count - index) / (count + 1) for index in range(count)]

    def _place(self, lower):
        pending, self._pending = self._pending, []
        if not pending:
            return
        top = self._upper is None
        if top:
            # Newest Likes: above every key, including those of un-liked tweets.
            keys = self._between(self._sorted[-1] if self._sorted else None, None, len(pending))
        elif lower is not None:
            high = self._upper if self._upper > lower else self._above(lower)
            keys = self._between(lower, high, len(pending))
        else:
            keys = self._between(self._below(self._upper), self._upper, len(pending))
        after = self._after if top else None
        rows = [(tweet_id, key, after, self._seen_by) for tweet_id, key in zip(pending, keys)]
        with self._store._db:
            self._store._db.executemany(
                "INSERT OR IGNORE INTO likes(tweet_id,like_key,liked_after,liked_before) VALUES (?,?,?,?)", rows)
        for tweet_id, key, *_ in rows:
            self._keys[tweet_id] = key
            bisect.insort(self._sorted, key)
