"""Small, structured terminal outcomes for manual and scheduled saves.

The catalog still owns per-account scan records. This ledger also includes
attempts that fail before a catalog/worker opens. Rows are immutable and IDs
are idempotent; only the oldest outcomes are pruned after the retention cap.
"""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat
import threading
import uuid

from .settings import app_directory
from .file_mutex import file_mutex


MAX_OUTCOMES = 1000
_WRITE_LOCK = threading.RLock()
_KOREA = timezone(timedelta(hours=9))
_ERROR = "실행 기록을 읽거나 저장하지 못했습니다. 앱 데이터 폴더의 권한을 확인해 주세요."
_COLUMNS = ("run_id", "origin", "account", "output_dir", "started_at", "finished_at", "status",
            "saved", "skipped", "size_skipped", "visited", "message", "code", "retry_at", "log",
            "history_run_id", "legacy")


class ActivityError(OSError):
    pass


def activity_path() -> Path:
    return app_directory() / "activity.sqlite3"


def _now():
    return datetime.now(_KOREA).isoformat(timespec="microseconds")


def _text(value, maximum):
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value if char in "\n\t" or ord(char) >= 32 and not 127 <= ord(char) <= 159)[:maximum]


def _count(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else 0


def _row(outcome, origin, account, output_dir, started_at, *, legacy=False):
    if not isinstance(outcome, dict) or origin not in {"manual", "scheduled"}:
        raise ValueError("invalid activity outcome")
    ident = outcome.get("run_id")
    if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident):
        if legacy:
            data = {key: value for key, value in outcome.items() if key != "seen"}
            ident = "legacy-" + hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        else:
            ident = uuid.uuid4().hex
    status = outcome.get("status")
    status = "busy" if outcome.get("busy") is True else status if status in {
        "finished", "stopped", "error", "limited", "interrupted", "cancelled", "partial"
    } else "error"
    code = outcome.get("code")
    history_id = outcome.get("history_run_id")
    return dict(run_id=ident, origin=origin, account=_text(account, 100), output_dir=_text(output_dir, 32768),
                started_at=_text(started_at or outcome.get("started_at"), 80),
                finished_at=_text(outcome.get("finished_at"), 80) or _now(), status=status,
                saved=_count(outcome.get("saved")), skipped=_count(outcome.get("skipped")),
                size_skipped=_count(outcome.get("size_skipped")), visited=_count(outcome.get("visited")),
                message=_text(outcome.get("message"), 4000),
                code=code if type(code) is int and -(2**31) <= code < 2**31 else None,
                retry_at=_text(outcome.get("retry_at"), 80), log=_text(outcome.get("log"), 32768),
                history_run_id=history_id if type(history_id) is int and 0 < history_id < 2**63 else None,
                legacy=int(legacy))


def _legacy_row():
    # Read only the one structured legacy outcome. Text logs are not a complete
    # historical database and must not be guessed into one.
    from .scheduled import last_run

    outcome = last_run()
    if outcome is None:
        return None
    return _row(outcome, "scheduled", outcome.get("account", ""), outcome.get("output_dir", ""),
                outcome.get("started_at", ""), legacy=True)


def _plain(path):
    for current in (path.parent, path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            details = current.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400
                or getattr(current, "is_junction", lambda: False)()
                or (stat.S_ISREG(details.st_mode) and details.st_nlink > 1)
                or (current != path.parent and not stat.S_ISREG(details.st_mode))):
            raise ActivityError(_ERROR)


def _insert(database, row):
    database.execute("INSERT OR IGNORE INTO outcomes(" + ",".join(_COLUMNS) + ") VALUES (" +
                     ",".join("?" for _ in _COLUMNS) + ")", tuple(row[key] for key in _COLUMNS))


def record_outcome(outcome: dict, *, origin: str, account: str = "", output_dir: str = "",
                   started_at: str = "") -> str:
    """Append one terminal event; a stable ``run_id`` makes repeats harmless."""
    row = _row(outcome, origin, account, output_dir, started_at)
    try:
        path = activity_path()
        with _WRITE_LOCK, file_mutex(path):
            _plain(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(path, timeout=5)) as database, database:
                database.execute("PRAGMA busy_timeout=5000")
                database.execute("PRAGMA journal_mode=WAL")
                database.execute("PRAGMA synchronous=FULL")
                database.execute("CREATE TABLE IF NOT EXISTS outcomes ("
                                 "run_id TEXT PRIMARY KEY, origin TEXT NOT NULL, account TEXT NOT NULL,"
                                 "output_dir TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,"
                                 "status TEXT NOT NULL, saved INTEGER NOT NULL, skipped INTEGER NOT NULL,"
                                 "size_skipped INTEGER NOT NULL, visited INTEGER NOT NULL, message TEXT NOT NULL,"
                                 "code INTEGER, retry_at TEXT NOT NULL, log TEXT NOT NULL, history_run_id INTEGER,"
                                 "legacy INTEGER NOT NULL DEFAULT 0)")
                database.execute("CREATE TABLE IF NOT EXISTS activity_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                # A legacy or temporarily unrecorded last result can appear
                # even after other outcomes have already populated the ledger.
                # Backfill it before the scheduler replaces last-scheduled.json;
                # INSERT OR IGNORE keeps an already recorded result unchanged.
                previous = _legacy_row()
                if previous is not None:
                    imported = database.execute("SELECT value FROM activity_meta WHERE key='last_legacy_id'").fetchone()
                    if imported is None or imported[0] != previous["run_id"]:
                        _insert(database, previous)
                        database.execute("INSERT OR REPLACE INTO activity_meta(key,value) VALUES('last_legacy_id',?)",
                                         (previous["run_id"],))
                _insert(database, row)
                database.execute("DELETE FROM outcomes WHERE rowid IN (SELECT rowid FROM outcomes "
                                 "ORDER BY rowid DESC LIMIT -1 OFFSET ?)", (MAX_OUTCOMES,))
        return row["run_id"]
    except (OSError, ValueError, sqlite3.Error):
        raise ActivityError(_ERROR) from None


def outcome_page(page=0, page_size=100, *, account="", origin="all") -> dict:
    """Read a consistent page without creating or writing a missing ledger."""
    if (type(page) is not int or page < 0 or type(page_size) is not int or not 1 <= page_size <= 1000
            or not isinstance(account, str) or origin not in {"all", "manual", "scheduled"}):
        raise ValueError("invalid activity page")
    try:
        path = activity_path()
        # A newly created SQLite file becomes visible before its first schema
        # is ready. Use the writer's lock so readers see either no ledger or a
        # complete one, including when another app process creates it.
        with _WRITE_LOCK, file_mutex(path):
            _plain(path)
            rows = []
            if path.is_file():
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as database:
                    database.row_factory = sqlite3.Row
                    database.execute("PRAGMA query_only=ON")
                    database.execute("BEGIN")
                    rows = [dict(row) for row in database.execute("SELECT * FROM outcomes ORDER BY rowid DESC LIMIT ?",
                                                                (MAX_OUTCOMES,))]
            previous = _legacy_row()
        if previous is not None and all(row["run_id"] != previous["run_id"] for row in rows):
            if rows and previous["finished_at"] >= rows[0]["finished_at"]:
                rows.insert(0, previous)
            else:
                rows.append(previous)
        rows = rows[:MAX_OUTCOMES]
        rows = [row for row in rows if (not account or row["account"].lower() == account.lower())
                and (origin == "all" or row["origin"] == origin)]
        pages = (len(rows) + page_size - 1) // page_size
        page = min(page, max(0, pages - 1))
        return {"rows": rows[page * page_size:(page + 1) * page_size], "total": len(rows),
                "page": page, "pages": pages, "page_size": page_size}
    except (OSError, ValueError, sqlite3.Error):
        raise ActivityError(_ERROR) from None


page_outcomes = outcome_page
