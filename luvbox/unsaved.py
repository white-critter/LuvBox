"""Media a save could not store, kept in the app data folder beside the activity ledger.

The catalog only describes files that exist. This small ledger remembers the
media that failed, went over the size limit or were excluded on purpose, so a
person can see them and ask for another try. A row is keyed by account, save
folder and media identity: repeating a failing save updates one row instead of
adding more, saving the media later removes it, and only the newest MAX_ITEMS
rows are kept. Nothing here decides what is saved; the catalog stays the record.
"""

from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import threading

from .activity import ActivityError, _count, _now, _plain, _text
from .file_mutex import file_mutex
from .settings import app_directory


MAX_ITEMS = 500
FAILED, SIZE, EXCLUDED = "failed", "size", "excluded"
REASONS = (FAILED, SIZE, EXCLUDED)
_WRITE_LOCK = threading.RLock()
_ERROR = "저장하지 못한 미디어 목록을 읽거나 갱신하지 못했습니다. 앱 데이터 폴더의 권한을 확인해 주세요."
_MEDIA_TYPES = {"photo", "video", "animated_gif"}
_COLUMNS = ("account", "folder", "tweet_id", "media_type", "num", "reason", "author", "posted_at", "width",
            "height", "duration", "size_bytes", "limit_mb", "detail", "attempts", "first_seen", "last_seen")


class UnsavedError(ActivityError):
    pass


def unsaved_path():
    return app_directory() / "unsaved.sqlite3"


def folder_key(output_dir) -> str:
    """The same folder spelled differently still names one list.

    A save resolves its folder (``~``, links, relative paths) before it runs, so the
    window's spelling and the worker's must reach the same key.
    """
    text = str(output_dir or "").strip()
    if not text:
        return ""
    try:
        return os.path.normcase(str(Path(text).expanduser().resolve()))
    except (OSError, RuntimeError):
        return os.path.normcase(os.path.normpath(text))


def _item(item, account, folder):
    if not isinstance(item, dict):
        return None
    tweet_id, media_type, num, reason = (item.get("tweet_id"), item.get("media_type"), item.get("num"),
                                         item.get("reason"))
    author = item.get("author")
    if (not isinstance(tweet_id, str) or not re.fullmatch(r"[0-9]{1,20}", tweet_id)
            or media_type not in _MEDIA_TYPES or type(num) is not int or not 1 <= num <= 10000
            or reason not in REASONS):
        return None
    now = _now()
    return dict(account=account, folder=folder, tweet_id=tweet_id, media_type=media_type, num=num,
                reason=reason, author=author if isinstance(author, str) and re.fullmatch(r"[A-Za-z0-9_]{1,15}", author) else "",
                posted_at=_text(item.get("posted_at"), 64), width=_count(item.get("width")),
                height=_count(item.get("height")), duration=_count(item.get("duration")),
                size_bytes=_count(item.get("size_bytes")), limit_mb=_count(item.get("limit_mb")),
                detail=_text(item.get("detail"), 300), attempts=1, first_seen=now, last_seen=now)


def _prepare(database):
    database.execute("PRAGMA busy_timeout=5000")
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("CREATE TABLE IF NOT EXISTS unsaved ("
                     "account TEXT NOT NULL, folder TEXT NOT NULL, tweet_id TEXT NOT NULL,"
                     "media_type TEXT NOT NULL, num INTEGER NOT NULL, reason TEXT NOT NULL,"
                     "author TEXT NOT NULL, posted_at TEXT NOT NULL, width INTEGER NOT NULL,"
                     "height INTEGER NOT NULL, duration INTEGER NOT NULL, size_bytes INTEGER NOT NULL,"
                     "limit_mb INTEGER NOT NULL, detail TEXT NOT NULL, attempts INTEGER NOT NULL,"
                     "first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,"
                     "PRIMARY KEY(account, folder, tweet_id, media_type, num))")


def _scope(account, output_dir):
    return str(account or "").strip().lstrip("@").lower(), folder_key(output_dir)


def record(items, *, account, output_dir, resolved=()) -> None:
    """Add or refresh unsaved media and forget the ones that were saved meanwhile."""
    account, folder = _scope(account, output_dir)
    rows = [row for row in (_item(item, account, folder) for item in list(items)[:MAX_ITEMS]) if row]
    gone = [(account, folder, str(tweet_id), media_type, num) for tweet_id, num, media_type in resolved]
    if not account or (not rows and not gone):
        return
    names = ",".join(_COLUMNS)
    update = ("reason=excluded.reason,author=COALESCE(NULLIF(excluded.author,''),author),"
              "posted_at=COALESCE(NULLIF(excluded.posted_at,''),posted_at),"
              + ",".join(f"{name}=CASE WHEN excluded.{name}>0 THEN excluded.{name} ELSE {name} END"
                         for name in ("width", "height", "duration", "size_bytes"))
              + ",limit_mb=excluded.limit_mb,detail=excluded.detail,attempts=attempts+1,last_seen=excluded.last_seen")
    try:
        path = unsaved_path()
        with _WRITE_LOCK, file_mutex(path):
            _plain(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(path, timeout=5)) as database, database:
                _prepare(database)
                database.executemany("DELETE FROM unsaved WHERE account=? AND folder=? AND tweet_id=? "
                                     "AND media_type=? AND num=?", gone)
                for row in rows:
                    database.execute(f"INSERT INTO unsaved({names}) VALUES ({','.join('?' for _ in _COLUMNS)}) "
                                     "ON CONFLICT(account,folder,tweet_id,media_type,num) DO UPDATE SET " + update,
                                     tuple(row[name] for name in _COLUMNS))
                database.execute("DELETE FROM unsaved WHERE rowid IN (SELECT rowid FROM unsaved "
                                 "ORDER BY last_seen DESC,rowid DESC LIMIT -1 OFFSET ?)", (MAX_ITEMS,))
    except (OSError, ValueError, sqlite3.Error):
        raise UnsavedError(_ERROR) from None


def _read(query, parameters=()):
    """Rows of one read-only query; a missing ledger is an empty one, never created."""
    try:
        path = unsaved_path()
        with _WRITE_LOCK, file_mutex(path):
            _plain(path)
            if not path.is_file():
                return []
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as database:
                database.row_factory = sqlite3.Row
                database.execute("PRAGMA query_only=ON")
                try:
                    return [dict(row) for row in database.execute(query, parameters)]
                except sqlite3.OperationalError as error:
                    if "no such table" in str(error):
                        return []
                    raise
    except (OSError, ValueError, sqlite3.Error):
        raise UnsavedError(_ERROR) from None


def identities(account, output_dir) -> set:
    """(tweet_id, num, media_type) of every row, so a save can tell which got saved."""
    account, folder = _scope(account, output_dir)
    rows = _read("SELECT tweet_id,num,media_type FROM unsaved WHERE account=? AND folder=?", (account, folder))
    return {(row["tweet_id"], row["num"], row["media_type"]) for row in rows}


def counts(account, output_dir) -> dict:
    account, folder = _scope(account, output_dir)
    found = {row["reason"]: row["total"] for row in _read(
        "SELECT reason,COUNT(*) total FROM unsaved WHERE account=? AND folder=? GROUP BY reason", (account, folder))}
    return {reason: found.get(reason, 0) for reason in REASONS}


def page(account, output_dir, kinds=(FAILED, SIZE), page=0, page_size=50) -> dict:
    kinds = [kind for kind in kinds if kind in REASONS]
    if (type(page) is not int or page < 0 or type(page_size) is not int or not 1 <= page_size <= 200
            or not kinds):
        raise ValueError("invalid unsaved page")
    account, folder = _scope(account, output_dir)
    marks = ",".join("?" for _ in kinds)
    where = f" FROM unsaved WHERE account=? AND folder=? AND reason IN ({marks})"
    parameters = (account, folder, *kinds)
    total = next(iter(_read("SELECT COUNT(*) total" + where, parameters)), {"total": 0})["total"]
    pages = (total + page_size - 1) // page_size
    page = min(page, max(0, pages - 1))
    rows = _read("SELECT *" + where + " ORDER BY last_seen DESC,rowid DESC LIMIT ? OFFSET ?",
                 (*parameters, page_size, page * page_size))
    return {"rows": rows, "total": total, "page": page, "pages": pages, "page_size": page_size}


def remove(account, output_dir, items=None, kinds=None) -> int:
    """Forget chosen rows ``(tweet_id, num, media_type)`` or every row of the given reasons."""
    account, folder = _scope(account, output_dir)
    try:
        path = unsaved_path()
        with _WRITE_LOCK, file_mutex(path):
            _plain(path)
            if not path.is_file():
                return 0
            with closing(sqlite3.connect(path, timeout=5)) as database, database:
                _prepare(database)
                removed = 0
                if items is not None:
                    for tweet_id, num, media_type in items:
                        removed += database.execute(
                            "DELETE FROM unsaved WHERE account=? AND folder=? AND tweet_id=? AND media_type=? AND num=?",
                            (account, folder, str(tweet_id), media_type, num)).rowcount
                else:
                    kinds = [kind for kind in (kinds or REASONS) if kind in REASONS]
                    removed = database.execute(
                        "DELETE FROM unsaved WHERE account=? AND folder=? AND reason IN (%s)" % ",".join("?" for _ in kinds),
                        (account, folder, *kinds)).rowcount
                return removed
    except (OSError, ValueError, sqlite3.Error):
        raise UnsavedError(_ERROR) from None
