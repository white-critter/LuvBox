"""Media a save could not store, kept in the app data folder beside the activity ledger.

The catalog only describes files that exist, and it is also what keeps a
'do not save' choice. This small ledger remembers media that failed or went
over the size limit, so a person can see them and ask for another try. A row
is keyed by account, save folder and media identity: repeating a failing save
updates one row instead of adding more, and saving the media later removes it.
Failed and size-limited rows are kept to the newest MAX_ITEMS per account and
folder. An older excluded row may still be here, but the excluded screen reads
the catalog, so trimming this ledger cannot hide that choice.

A listed media may also carry a count of the saves that X's image host
stopped by refusing it (``refusals``): a reply that the file is not served,
such as 403, never a server error. After a few, a save passes that media by
without asking (core.REFUSED_LIMIT); only a retry the person starts asks
again. The count goes with its row: saving the media, removing the row, or
trimming it forgets the count. Nothing else here decides what is saved.
"""

from contextlib import closing, contextmanager
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
_ERROR = "저장하지 못한 미디어 목록을 읽거나 갱신하지 못했습니다. LuvBox 기록 폴더의 권한을 확인해 주세요."
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
    database.execute("CREATE TABLE IF NOT EXISTS refused ("
                     "account TEXT NOT NULL, folder TEXT NOT NULL, tweet_id TEXT NOT NULL,"
                     "media_type TEXT NOT NULL, num INTEGER NOT NULL, runs INTEGER NOT NULL,"
                     "PRIMARY KEY(account, folder, tweet_id, media_type, num))")


def _forget_orphan_refusals(database, account, folder):
    """A refusal count lives only as long as its media is listed."""
    database.execute("DELETE FROM refused WHERE account=? AND folder=? AND NOT EXISTS ("
                     "SELECT 1 FROM unsaved WHERE unsaved.account=refused.account AND unsaved.folder=refused.folder "
                     "AND unsaved.tweet_id=refused.tweet_id AND unsaved.media_type=refused.media_type "
                     "AND unsaved.num=refused.num)", (account, folder))


def _scope(account, output_dir):
    return str(account or "").strip().lstrip("@").lower(), folder_key(output_dir)


def record(items, *, account, output_dir, resolved=(), refused=None, counted_attempt: bool = True) -> None:
    """Add or refresh unsaved media and forget the ones that were saved meanwhile.

    ``refused`` is the (tweet_id, num, media_type) whose refusal by X's image
    host (the file is not served, not a server error) stopped this save; its
    count goes up by one.
    """
    account, folder = _scope(account, output_dir)
    paired = [(row, bool(item.get("counted_attempt", counted_attempt)))
              for item in list(items)[:MAX_ITEMS]
              if (row := _item(item, account, folder)) is not None]
    gone = [(account, folder, str(tweet_id), media_type, num) for tweet_id, num, media_type in resolved]
    if not account or (not paired and not gone):
        return
    names = ",".join(_COLUMNS)
    base_update = ("reason=excluded.reason,author=COALESCE(NULLIF(excluded.author,''),author),"
                   "posted_at=COALESCE(NULLIF(excluded.posted_at,''),posted_at),"
                   + ",".join(f"{name}=CASE WHEN excluded.{name}>0 THEN excluded.{name} ELSE {name} END"
                              for name in ("width", "height", "duration", "size_bytes"))
                   + ",limit_mb=excluded.limit_mb,detail=excluded.detail,")
    update_counted = base_update + "attempts=attempts+1,last_seen=excluded.last_seen"
    update_uncounted = base_update + "attempts=attempts,last_seen=excluded.last_seen"
    try:
        path = unsaved_path()
        with _WRITE_LOCK, file_mutex(path):
            _plain(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(path, timeout=5)) as database, database:
                _prepare(database)
                database.executemany("DELETE FROM unsaved WHERE account=? AND folder=? AND tweet_id=? "
                                     "AND media_type=? AND num=?", gone)
                for row, count_attempt in paired:
                    update = update_counted if count_attempt else update_uncounted
                    database.execute(f"INSERT INTO unsaved({names}) VALUES ({','.join('?' for _ in _COLUMNS)}) "
                                     "ON CONFLICT(account,folder,tweet_id,media_type,num) DO UPDATE SET " + update,
                                     tuple(row[name] for name in _COLUMNS))
                # The documented cap is per account and folder, and only for media the
                # app failed to store. Excluded choices live in the catalog.
                database.execute(
                    "DELETE FROM unsaved WHERE rowid IN (SELECT rowid FROM unsaved "
                    "WHERE account=? AND folder=? AND reason IN (?,?) "
                    "ORDER BY last_seen DESC,rowid DESC LIMIT -1 OFFSET ?)",
                    (account, folder, FAILED, SIZE, MAX_ITEMS))
                if refused is not None:
                    tweet_id, num, media_type = refused
                    database.execute("INSERT INTO refused(account,folder,tweet_id,media_type,num,runs) "
                                     "VALUES (?,?,?,?,?,1) ON CONFLICT(account,folder,tweet_id,media_type,num) "
                                     "DO UPDATE SET runs=runs+1", (account, folder, str(tweet_id), media_type, num))
                _forget_orphan_refusals(database, account, folder)
    except (OSError, ValueError, sqlite3.Error):
        raise UnsavedError(_ERROR) from None


@contextmanager
def _reader():
    """Read from one ledger snapshot without creating a missing database."""
    try:
        path = unsaved_path()
        with _WRITE_LOCK, file_mutex(path):
            _plain(path)
            if not path.is_file():
                yield None
                return
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as database:
                database.row_factory = sqlite3.Row
                database.execute("PRAGMA query_only=ON")
                database.execute("BEGIN")
                yield database
    except (OSError, ValueError, sqlite3.Error):
        raise UnsavedError(_ERROR) from None


def _rows(database, query, parameters):
    if database is None:
        return []
    try:
        return [dict(row) for row in database.execute(query, parameters)]
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return []
        raise


def _read(query, parameters):
    with _reader() as database:
        return _rows(database, query, parameters)


def identities(account, output_dir) -> set:
    """(tweet_id, num, media_type) of every row, so a save can tell which got saved."""
    account, folder = _scope(account, output_dir)
    rows = _read("SELECT tweet_id,num,media_type FROM unsaved WHERE account=? AND folder=?", (account, folder))
    return {(row["tweet_id"], row["num"], row["media_type"]) for row in rows}


def refusals(account, output_dir) -> dict:
    """{(tweet_id, num, media_type): saves stopped} for listed media X's image host refused."""
    account, folder = _scope(account, output_dir)
    rows = _read("SELECT refused.tweet_id,refused.num,refused.media_type,refused.runs FROM refused "
                 "JOIN unsaved USING(account,folder,tweet_id,media_type,num) "
                 "WHERE refused.account=? AND refused.folder=?", (account, folder))
    return {(row["tweet_id"], row["num"], row["media_type"]): row["runs"] for row in rows}


def counts(account, output_dir) -> dict:
    account, folder = _scope(account, output_dir)
    found = {row["reason"]: row["total"] for row in _read(
        "SELECT reason,COUNT(*) total FROM unsaved WHERE account=? AND folder=? GROUP BY reason", (account, folder))}
    return {reason: found.get(reason, 0) for reason in REASONS}


def retryable_count(account, output_dir, limit_mb) -> int:
    """Failed rows, plus size-limited rows the current limit can now accept.

    The open page is not the whole list. A page of files that are still too
    large must not hide failures waiting on the next page.
    """
    if isinstance(limit_mb, bool) or not isinstance(limit_mb, int) or limit_mb < 0:
        limit_mb = 0
    account, folder = _scope(account, output_dir)
    row = next(iter(_read(
        "SELECT COUNT(*) total FROM unsaved WHERE account=? AND folder=? AND ("
        "reason=? OR (reason=? AND (?=0 OR (limit_mb>0 AND ?>limit_mb AND (size_bytes=0 OR size_bytes<=?)))))",
        (account, folder, FAILED, SIZE, limit_mb, limit_mb, limit_mb * 1024 * 1024))), {"total": 0})
    return int(row["total"])


def page(account, output_dir, kinds=(FAILED, SIZE), page=0, page_size=50) -> dict:
    kinds = [kind for kind in kinds if kind in REASONS]
    if (type(page) is not int or page < 0 or type(page_size) is not int or not 1 <= page_size <= 200
            or not kinds):
        raise ValueError("invalid unsaved page")
    account, folder = _scope(account, output_dir)
    marks = ",".join("?" for _ in kinds)
    where = f" FROM unsaved WHERE account=? AND folder=? AND reason IN ({marks})"
    parameters = (account, folder, *kinds)
    with _reader() as database:
        count = _rows(database, "SELECT COUNT(*) total" + where, parameters)
        total = count[0]["total"] if count else 0
        pages = (total + page_size - 1) // page_size
        page = min(page, max(0, pages - 1))
        rows = _rows(database, "SELECT *" + where + " ORDER BY last_seen DESC,rowid DESC LIMIT ? OFFSET ?",
                     (*parameters, page_size, page * page_size)) if total else []
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
                    selected = REASONS if kinds is None else kinds
                    kinds = [kind for kind in selected if kind in REASONS]
                    if not kinds:
                        return 0
                    removed = database.execute(
                        "DELETE FROM unsaved WHERE account=? AND folder=? AND reason IN (%s)" % ",".join("?" for _ in kinds),
                        (account, folder, *kinds)).rowcount
                _forget_orphan_refusals(database, account, folder)
                return removed
    except (OSError, ValueError, sqlite3.Error):
        raise UnsavedError(_ERROR) from None
