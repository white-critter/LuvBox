"""One list of past save runs: the app-wide ledger plus runs only a catalog remembers.

The ledger (activity.py) records manual and scheduled runs of every account.
Runs saved before the ledger existed live only in an account's catalog; they are
added for the current account unless the ledger already holds the same run.
"""

from contextlib import closing
import os
import sqlite3

from .activity import MAX_OUTCOMES, outcome_page
from .history import HistoryError
from .qt_library import _ReadOnlyHistory


PAGE_SIZE = 50


def _same_folder(first, second):
    return (isinstance(first, str) and bool(first) and os.path.isabs(first)
            and os.path.normcase(os.path.normpath(first)) == os.path.normcase(os.path.normpath(str(second))))


def _legacy_runs(paths, ledger):
    """Catalog-only runs of the current account; an unreadable catalog adds nothing."""
    try:
        with closing(_ReadOnlyHistory(paths)) as store:
            roots = store.media_roots
            rows = store.runs(limit=MAX_OUTCOMES, offset=0)
    except FileNotFoundError:
        return [], False  # no catalog yet: nothing was saved by an older version either
    except (ValueError, OSError, sqlite3.Error, HistoryError):
        return [], True
    account_rows = [row for row in ledger
                    if row.get("history_run_id")
                    and row.get("account", "").casefold() == paths.account.casefold()]
    linked = {row.get("history_run_id") for row in account_rows
              if any(_same_folder(row.get("output_dir"), root) for root in roots)}
    linked_starts = {(row.get("history_run_id"), row.get("started_at")) for row in account_rows
                     if row.get("started_at")}
    return [dict(run_id=str(row["id"]), origin="legacy", account=paths.account, started_at=row["started_at"],
                 finished_at=row["finished_at"] or "", status=row["status"], saved=row["saved"],
                 skipped=row["skipped"], size_skipped=row["size_skipped"], failed_media=0,
                 message=row["message"], log="")
            for row in rows
            if row["id"] not in linked and (row["id"], row["started_at"]) not in linked_starts], False


def run_page(paths, page=0, page_size=PAGE_SIZE) -> dict:
    """Newest runs first. ``paths`` (the current storage) is None without an account."""
    ledger = outcome_page(0, MAX_OUTCOMES)["rows"]
    legacy, unreadable = [], False
    if paths is not None:
        legacy, unreadable = _legacy_runs(paths, ledger)
    rows = sorted(ledger + legacy, key=lambda row: (row.get("started_at") or row.get("finished_at") or "",
                                                    str(row.get("run_id", ""))), reverse=True)
    pages = (len(rows) + page_size - 1) // page_size
    page = min(max(0, int(page)), max(0, pages - 1))
    return dict(rows=rows[page * page_size:(page + 1) * page_size], total=len(rows), page=page, pages=pages,
                page_size=page_size, legacy_unreadable=unreadable)
