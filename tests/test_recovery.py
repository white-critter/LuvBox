"""Damaged records databases are detected, kept, and rebuilt on this PC."""

import queue
import sqlite3
import struct
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from xlikesaver import recovery
from xlikesaver.history import CatalogDamaged, HistoryError, HistoryStore
from xlikesaver.history_gui import HistoryWindow


def damage_media_table(path: Path):
    """Add an unreadable cell to a leaf page, leaving real cells intact.

    SQLite then refuses the table, while every real row stays on its page.
    """
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size = connection.execute("PRAGMA page_size").fetchone()[0]
        root = connection.execute("SELECT rootpage FROM sqlite_master WHERE name='media'").fetchone()[0]
        connection.execute("PRAGMA journal_mode=DELETE")
    data = bytearray(path.read_bytes())
    pending, leaves = [root], []
    while pending:
        number = pending.pop()
        page = data[(number - 1) * size:number * size]
        cells = int.from_bytes(page[3:5], "big")
        if page[0] == 0x05:
            pending.append(int.from_bytes(page[8:12], "big"))
            pending.extend(int.from_bytes(page[int.from_bytes(page[12 + 2 * i:14 + 2 * i], "big"):][:4], "big")
                           for i in range(cells))
        elif page[0] == 0x0D:
            leaves.append(number)
    target = (leaves[len(leaves) // 2] - 1) * size
    cells = int.from_bytes(data[target + 3:target + 5], "big")
    offset = 8 + 2 * (cells + 1)
    content = int.from_bytes(data[target + 5:target + 7], "big") or size
    if content - offset < 6:
        raise AssertionError("The corruption fixture needs six unused bytes")
    # payload size 3, rowid 800, record header size 2, one-byte TEXT (serial 15),
    # invalid UTF-8. SQL decoding fails, and page salvage skips just this fake
    # cell. Pointing at the last row's data could accidentally produce a valid
    # phantom SQL row depending on the path/timestamp bytes at the page end.
    data[target + 3:target + 5] = (cells + 1).to_bytes(2, "big")
    data[target + 8 + 2 * cells:target + 10 + 2 * cells] = offset.to_bytes(2, "big")
    data[target + offset:target + offset + 6] = b"\x03\x86\x20\x02\x0f\xff"
    path.write_bytes(bytes(data))


def damage_media_constraint(path: Path):
    """Leave one NULL identity under the original NOT NULL schema.

    This reproduces corruption reported as a constraint error, rather than
    SQLITE_CORRUPT, when the catalog's source URL migration visits the row.
    """
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        schema = connection.execute("SELECT sql FROM sqlite_master WHERE name='media'").fetchone()[0]
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute("UPDATE sqlite_master SET sql=? WHERE name='media'",
                           (schema.replace("tweet_id TEXT NOT NULL", "tweet_id TEXT"),))
        connection.commit()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE media SET tweet_id=NULL,source_url='' WHERE tweet_id='1000'")
        connection.commit()
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute("UPDATE sqlite_master SET sql=? WHERE name='media'", (schema,))
        connection.commit()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        protection = patch("xlikesaver.protection.state_directory", return_value=self.root / "app" / "request-protection")
        protection.start()
        self.addCleanup(protection.stop)
        self.account = self.root / "records" / "myaccount"
        store = HistoryStore(self.account)
        with closing(store):
            for index in range(600):
                path = self.account / f"{1000 + index}_1.jpg"
                path.write_bytes(b"offline")
                store.record_media("flat", {"tweet_id": str(1000 + index), "num": 1, "type": "photo",
                                            "author": {"name": "alice", "id": 7},
                                            "_xlikesaver_metadata_status": "embedded"}, path)
            run = store.start_run("flat")
            store.finish_run(run, "finished", saved=600)
            store.record_tweets([("1000", "본문", ["tag"])])
            self.saved = {row["tweet_id"]: row["saved_at"] for row in store.records(limit=1000)}
        self.live = self.account / recovery.HISTORY_NAME

    def test_damage_is_reported_and_rows_are_salvaged_by_page(self):
        recovery.backup_catalog(self.account, "myaccount")
        damage_media_table(self.live)
        with self.assertRaises(CatalogDamaged) as failure:
            HistoryStore(self.account).summary()
        self.assertIn("기록 복구", str(failure.exception))
        tables = recovery.salvage(self.live)
        self.assertEqual(tables["media"]["method"], "pages")
        self.assertEqual(len({row["tweet_id"] for _, row in tables["media"]["rows"]}), 600)
        self.assertEqual(tables["runs"]["method"], "sql")
        # A damaged file never replaces the last good backup.
        self.assertIsNone(recovery.backup_catalog(self.account, "myaccount"))
        self.assertEqual(len(list(recovery._catalog_backups("myaccount", self.account).glob("history-*"))), 1)

    def test_constraint_error_from_damaged_catalog_still_offers_recovery(self):
        damage_media_constraint(self.live)
        with closing(sqlite3.connect(self.live)) as connection:
            with self.assertRaises(sqlite3.IntegrityError) as failure:
                connection.execute("UPDATE media SET source_url='https://x.com/i/web/status/' || tweet_id "
                                   "WHERE source_url=''")
            self.assertEqual(failure.exception.sqlite_errorcode, sqlite3.SQLITE_CONSTRAINT_NOTNULL)
            self.assertNotEqual(connection.execute("PRAGMA quick_check").fetchall(), [("ok",)])

        with self.assertRaises(CatalogDamaged):
            HistoryStore(self.account)
        results = queue.Queue()
        HistoryWindow._load_worker(results, 1, self.account, "all", "", 0, False)
        self.assertEqual(results.get_nowait()[1], "damaged")

    def test_valid_catalog_constraint_error_does_not_offer_damage_recovery(self):
        with closing(sqlite3.connect(self.live)) as connection:
            connection.execute("UPDATE media SET source_url='' WHERE tweet_id='1000'")
            connection.execute("CREATE TRIGGER deny_source_update BEFORE UPDATE OF source_url ON media "
                               "BEGIN SELECT RAISE(ABORT, 'offline constraint fixture'); END")
            connection.commit()
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchall(), [("ok",)])

        with self.assertRaises(HistoryError) as failure:
            HistoryStore(self.account)
        self.assertNotIsInstance(failure.exception, CatalogDamaged)

    def test_repair_rebuilds_a_verified_database_and_keeps_the_damaged_one(self):
        damage_media_table(self.live)
        (self.account / (recovery.HISTORY_NAME + "-wal")).write_bytes(b"")
        result = recovery.repair_history(self.account, "myaccount")
        self.assertEqual(result["counts"]["media"], 600)
        kept = Path(result["kept"])
        self.assertTrue((kept / recovery.HISTORY_NAME).is_file())
        self.assertFalse((self.account / (recovery.HISTORY_NAME + "-wal")).exists())
        self.assertEqual(recovery._integrity(self.live), "ok")
        with closing(HistoryStore(self.account)) as store:
            rows = store.records(limit=1000)
            self.assertEqual({row["tweet_id"]: row["saved_at"] for row in rows}, self.saved)
            self.assertEqual(store.runs()[0]["saved"], 600)
            self.assertEqual(store.record_page(query="본문")["total"], 1)
            self.assertTrue(all(row["metadata_status"] == "embedded" for row in rows))
        self.assertFalse(list(self.account.glob("*.repaired-*")))

    def test_records_window_offers_repair_and_reloads(self):
        damage_media_table(self.live)
        results = queue.Queue()
        HistoryWindow._load_worker(results, 1, self.account, "all", "", 0, False)
        kind, message = results.get_nowait()[1:]
        self.assertEqual(kind, "damaged")
        HistoryWindow._repair_worker(results, 2, self.account)
        kind, message = results.get_nowait()[1:]
        self.assertEqual(kind, "repaired")
        self.assertIn("600개", message)
        HistoryWindow._load_worker(results, 3, self.account, "all", "", 0, False)
        kind, snapshot = results.get_nowait()[1:]
        self.assertEqual((kind, snapshot[1]["files"]), ("loaded", 600))

    def test_backups_are_daily_and_limited(self):
        folder = recovery._catalog_backups("myaccount", self.account)
        for day in range(1, 10):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"history-202601{day:02d}.sqlite3").write_bytes(b"old")
        target = recovery.backup_catalog(self.account, "myaccount")
        self.assertEqual(recovery._integrity(target), "ok")
        backups = sorted(folder.glob("history-*.sqlite3"))
        self.assertEqual(len(backups), 7)
        self.assertEqual(backups[-1], target)
        self.assertEqual(recovery.latest_backup("myaccount", self.account), target)

    def test_same_account_catalogs_have_separate_backups_and_repairs(self):
        first = recovery.backup_catalog(self.account, "myaccount")
        other = self.root / "other-output" / "myaccount"
        with closing(HistoryStore(other)) as store:
            path = other / "99999_1.jpg"
            path.write_bytes(b"other media")
            store.record_media("flat", {"tweet_id": "99999", "num": 1, "type": "photo"}, path)
        second = recovery.backup_catalog(other, "myaccount")
        self.assertNotEqual(first, second)
        self.assertEqual(recovery.latest_backup("myaccount", self.account), first)
        self.assertEqual(recovery.latest_backup("myaccount", other), second)
        # Leave an old unscoped backup in place; it is not a proven fallback
        # for either output folder, and must not be silently combined.
        legacy = recovery._catalog_backups("myaccount") / "history-20990101.sqlite3"
        legacy.write_bytes(second.read_bytes())
        damage_media_table(self.live)
        recovery.repair_history(self.account, "myaccount")
        with closing(HistoryStore(self.account)) as store:
            self.assertEqual({row["tweet_id"] for row in store.records(limit=1000)}, set(self.saved))
        self.assertTrue(legacy.is_file())
        with closing(HistoryStore(other)) as store:
            self.assertEqual([row["tweet_id"] for row in store.records()], ["99999"])


class RecoveryRecordTests(unittest.TestCase):
    def test_truncated_numbers_are_rejected_instead_of_inventing_values(self):
        for serial, size in enumerate((1, 2, 3, 4, 6, 8, 8), start=1):
            with self.subTest(serial=serial):
                with self.assertRaises(ValueError):
                    recovery._record(bytes((2, serial)) + b"\x00" * (size - 1))

    def test_invalid_headers_and_split_serial_types_are_rejected(self):
        for payload in (b"\x08\x00", b"\x00", b"\x02\x80\x00"):
            with self.subTest(payload=payload.hex()):
                with self.assertRaises(ValueError):
                    recovery._record(payload)

    def test_valid_numbers_and_text_still_decode(self):
        payload = bytes((6, 1, 6, 7, 15, 12)) + b"\xff" + (123).to_bytes(8, "big") + struct.pack(">d", 2.5) + b"x"
        self.assertEqual(recovery._record(payload), [-1, 123, 2.5, "x", b""])


if __name__ == "__main__":
    unittest.main()
