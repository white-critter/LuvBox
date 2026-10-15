"""Compare catalog reads and writes with synthetic records in a temporary folder.

Run from the project root: python tools/benchmark_history.py --rows 100000
No account login, media download, or existing catalog is used.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from luvbox import history


_INSERT = ("INSERT INTO media(folder_mode,tweet_id,num,media_type,author,filename,posted_at,"
           "posted_source,saved_at,path,filesize,status,record_source,updated_at,source_url) "
           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


def _rows(root: Path, start: int, count: int):
    for index in range(start, start + count):
        tweet = str(1_000_000_000_000_000_000 + index)
        stamp = f"2026-{index % 12 + 1:02d}-{index % 28 + 1:02d}T00:00:00+09:00"
        yield ("flat", tweet, 1, "photo", f"author{index % 100}", f"{tweet}_1.jpg",
               None if index % 10 == 0 else stamp, "metadata", None if index % 20 == 0 else stamp,
               str(root / f"{tweet}_1.jpg"), 1024, "saved", "saved", stamp,
               "https://x.com/i/web/status/" + tweet)


def _median_ms(call, repeats: int) -> float:
    call()
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(samples), 3)


def _size(db) -> int:
    return db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]


def _measure(store, count: int, repeats: int) -> dict:
    def reopen():
        with closing(history.HistoryStore(store.account_dir, media_dir=store.media_dir)):
            pass

    result = {
        "open_ms": _median_ms(reopen, repeats),
        "empty_source_probe_ms": _median_ms(
            lambda: store._db.execute("SELECT 1 FROM media WHERE source_url='' LIMIT 1").fetchone(), repeats),
        "saved_first_page_ms": _median_ms(lambda: store.record_page(0, 100), repeats),
        "saved_late_page_ms": _median_ms(lambda: store.record_page(max(0, count // 100 - 100), 100), repeats),
        "library_first_page_ms": _median_ms(lambda: store.record_page(0, 100, include_deleted=False), repeats),
        "library_late_page_ms": _median_ms(lambda: store.record_page(
            max(0, count // 100 - 100), 100, include_deleted=False), repeats),
        "posted_first_page_ms": _median_ms(lambda: store.record_page(0, 100, order="posted"), repeats),
        "oldest_first_page_ms": _median_ms(lambda: store.record_page(0, 100, order="posted_oldest"), repeats),
        "author_first_page_ms": _median_ms(lambda: store.record_page(0, 100, author="AUTHOR12"), repeats),
        "library_author_first_page_ms": _median_ms(lambda: store.record_page(
            0, 100, author="AUTHOR12", include_deleted=False), repeats),
        "author_sample_query_ms": _median_ms(lambda: store._db.execute(
            "SELECT * FROM media WHERE author=? COLLATE NOCASE AND path!='' "
            "ORDER BY COALESCE(posted_at,'') DESC,tweet_id DESC,num,folder_mode LIMIT 8", ("AUTHOR12",)).fetchall(), repeats),
        "database_bytes": _size(store._db),
    }
    additions = list(_rows(store.media_dir, count, 1000))
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        with store._db:
            store._db.executemany(_INSERT, additions)
        samples.append((time.perf_counter() - started) * 1000)
        with store._db:
            store._db.executemany("DELETE FROM media WHERE folder_mode=? AND tweet_id=? AND num=? AND media_type=?",
                                 [row[:4] for row in additions])
    result["insert_1000_rows_ms"] = round(statistics.median(samples), 3)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100000)
    parser.add_argument("--repeats", type=int, default=7)
    options = parser.parse_args()
    if options.rows < 1 or options.repeats < 1:
        parser.error("rows and repeats must be positive")
    index_statements = history._MEDIA_READ_INDEX_SQL
    with tempfile.TemporaryDirectory(prefix="luvbox-history-benchmark-") as temporary:
        root = Path(temporary)
        with patch.object(history, "records_root", return_value=root / "app-data" / "records"):
            with patch.object(history, "_MEDIA_READ_INDEX_SQL", ()):
                store = history.HistoryStore(root / "records", media_dir=root / "media")
                try:
                    with store._db:
                        store._db.executemany(_INSERT, _rows(store.media_dir, 0, options.rows))
                    baseline = _measure(store, options.rows, options.repeats)
                except BaseException:
                    store.close()
                    raise
            try:
                started = time.perf_counter()
                with store._db:
                    for statement in index_statements:
                        store._db.execute(statement)
                build_ms = round((time.perf_counter() - started) * 1000, 3)
                optimized = _measure(store, options.rows, options.repeats)
            finally:
                store.close()
    print(json.dumps({"python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
                      "rows": options.rows, "repeats": options.repeats,
                      "index_build_ms": build_ms, "baseline": baseline, "optimized": optimized}, indent=2))


if __name__ == "__main__":
    main()
