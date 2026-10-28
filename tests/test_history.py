"""Offline catalog tests with synthetic local archives and media files."""

import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from itertools import product
from pathlib import Path
from unittest.mock import patch

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox.history import HistoryError, HistoryStore, _within


SECRET = "offline-cookie-never-store-in-history"
KST = timezone(timedelta(hours=9))


def tweet_id(posted):
    return str((int(posted.timestamp() * 1000) - 1_288_834_974_657) << 22)


class WithinTests(unittest.TestCase):
    def test_prefix_check_matches_pathlib(self):
        here = Path.cwd()
        folders = [here, here / "Media", here / "media" / "Likes", Path(here.anchor), here / "Med",
                   Path("relative"), Path(".")]
        children = [here / "Media" / "a.jpg", here / "MEDIA" / "Likes" / "b.jpg", here / "Media2" / "c.jpg",
                    here / "Med" / "d.jpg", here, Path(here.anchor), here / "media", Path("relative") / "e.jpg",
                    Path("other.jpg")]
        if os.name == "nt":
            unc = Path(r"\\server\share")
            folders += [unc, unc / "Media", Path("D:/")]
            children += [unc / "media" / "f.jpg", unc / "Mediax" / "g.jpg", Path("D:/h.jpg"), Path("d:/Media/i.jpg")]
            # Case mapping that changes with context or length (final sigma, dotted I).
            folders += [here / "ΑΣ", here / "ας", here / "İstanbul", here / "i\u0307stanbul"]
            children += [here / "ΑΣ" / "j.jpg", here / "ΑΣΑ" / "k.jpg", here / "İstanbul" / "l.jpg"]
        for child in children:
            for folder in folders:
                with self.subTest(child=str(child), folder=str(folder)):
                    self.assertEqual(_within(child, folder), child.is_relative_to(folder))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.account = Path(self.temp.name) / "account"
        self.store = HistoryStore(self.account)
        self.addCleanup(self.store.close)

    def file(self, name, content=b"offline-media"):
        path = self.account / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_file_scan_uses_the_loaded_catalog_instead_of_per_file_queries(self):
        for index in range(30):
            self.file(f"{1000 + index}_1.jpg")
        with patch.object(self.store, "media_record", side_effect=AssertionError("per-file catalog lookup")):
            result = self.store.import_legacy(refresh=True)
        self.assertEqual(result["records"], 30)
        kept = self.store.media_record("flat", {"tweet_id": "1000", "num": 1, "type": "photo"})
        self.assertEqual(kept["filename"], "1000_1.jpg")

    def test_source_url_backfill_commits_in_short_batches(self):
        for index in range(5):
            self.file(f"{1100 + index}_1.jpg")
        self.store.import_legacy(refresh=True)
        self.store._db.execute("UPDATE media SET source_url=''")
        self.store._db.commit()
        updates = []
        self.store._db.set_trace_callback(
            lambda sql: updates.append(sql) if sql.startswith("UPDATE media SET source_url") else None)
        with patch("luvbox.history._SOURCE_URL_BATCH", 2):
            self.store._backfill_source_urls()
        self.store._db.set_trace_callback(None)
        self.assertEqual(len(updates), 3)
        self.assertIsNone(self.store._db.execute("SELECT 1 FROM media WHERE source_url=''").fetchone())
        self.assertEqual(self.store._db.execute(
            "SELECT COUNT(*) FROM media WHERE source_url LIKE 'https://x.com/i/web/status/%'").fetchone()[0], 5)

    def test_file_scan_rejects_non_ascii_handle_prefixes(self):
        names = ("İd_900_1.jpg", "ıd_901_1.jpg", "ſam_902_1.jpg", "Kate_903_1.jpg", "valid_904_1.JPG")
        paths = [self.file(name) for name in names]
        self.store.import_legacy(refresh=True)
        self.assertEqual([row["tweet_id"] for row in self.store.records()], ["904"])
        self.assertTrue(all(path.is_file() for path in paths))

    def test_file_scan_recovers_author_prefixed_media_in_all_layouts(self):
        names = ("artist_801_1.jpg", "2026-10-23/under_score_802_video_2.mp4",
                 "old_folder/new_handle_803_gif_3.gif", "123_804_1.jpg", "805_1.jpg")
        for name in names:
            self.file(name)
        self.store.import_legacy(refresh=True)
        records = {row["tweet_id"]: row for row in self.store.records()}
        expected = {"801": ("artist", "photo", "flat"), "802": ("under_score", "video", "date"),
                    "803": ("new_handle", "animated_gif", "author"), "804": ("123", "photo", "flat"),
                    "805": ("", "photo", "flat")}
        self.assertEqual(set(records), set(expected))
        for tweet, values in expected.items():
            row = records[tweet]
            self.assertEqual((row["author"], row["media_type"], row["folder_mode"]), values)
            self.assertEqual(row["filename"], Path(row["path"]).name)

    def test_opening_never_waits_for_another_writer_but_still_migrates_old_rows(self):
        path = self.file("100_1.jpg")
        self.store.record_media("flat", {"tweet_id": "100", "num": 1, "type": "photo"}, path)
        self.store.close()
        writer = sqlite3.connect(self.account / ".luvbox-history.sqlite3", isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")  # e.g. a save recording a file
        started = time.monotonic()
        with closing(HistoryStore(self.account)) as reader:
            self.assertEqual(len(reader.records()), 1)
        self.assertLess(time.monotonic() - started, 1.0)
        writer.execute("UPDATE media SET source_url=''")  # a row from an early version
        writer.execute("COMMIT")
        with closing(HistoryStore(self.account)) as migrated:
            self.assertEqual(migrated.records()[0]["source_url"], "https://x.com/i/web/status/100")

    def test_saved_pages_and_migration_probe_do_not_sort_or_scan_the_catalog(self):
        from luvbox.history import _ORDERS

        sql = ("SELECT media.rowid FROM media ORDER BY " + _ORDERS["saved"] +
               ",media.num,media.folder_mode,media.media_type LIMIT 100 OFFSET 10000")
        details = [row[3] for row in self.store._db.execute("EXPLAIN QUERY PLAN " + sql)]
        # SQLite before 3.49 does not report an index on expressions as
        # covering, although paging still walks it in order without sorting.
        # The app bundles a newer SQLite; the program check below still
        # proves the page never reads the path column from the table.
        used = "USING COVERING INDEX" if sqlite3.sqlite_version_info >= (3, 49) else "USING"
        self.assertTrue(any(f"{used} " in detail and "media_saved_order" in detail for detail in details), details)
        self.assertFalse(any("TEMP B-TREE" in detail for detail in details), details)
        where, params = self.store._record_filter("all", "", include_deleted=False)
        visible = ("SELECT media.rowid FROM media" + where + " ORDER BY " + _ORDERS["saved"] +
                   ",media.num,media.folder_mode,media.media_type LIMIT 100 OFFSET 10000")
        program = self.store._db.execute("EXPLAIN " + visible, params).fetchall()
        media_page = self.store._db.execute(
            "SELECT rootpage FROM sqlite_master WHERE type='table' AND name='media'").fetchone()[0]
        media_cursors = {row["p1"] for row in program if row["opcode"] == "OpenRead" and row["p2"] == media_page}
        path_column = next(row["cid"] for row in self.store._db.execute("PRAGMA table_info(media)")
                           if row["name"] == "path")
        self.assertFalse(any(row["opcode"] == "Column" and row["p1"] in media_cursors and row["p2"] == path_column
                             for row in program), "paging must read the empty-path flag from the index")
        migration = [row[3] for row in self.store._db.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM media WHERE source_url='' LIMIT 1")]
        self.assertTrue(any("SEARCH media USING COVERING INDEX" in detail for detail in migration), migration)
        author = [row[3] for row in self.store._db.execute(
            "EXPLAIN QUERY PLAN SELECT rowid FROM media WHERE author=? COLLATE NOCASE", ("ALICE",))]
        self.assertTrue(any("SEARCH media USING COVERING INDEX" in detail for detail in author), author)

    def test_indexed_deletion_filter_preserves_null_and_restored_file_semantics(self):
        where, _ = self.store._record_filter("all", "", include_deleted=False)
        collation = " COLLATE NOCASE" if os.name == "nt" else ""
        reference = (" WHERE NOT (media.path='' AND media.record_source='declined') "
                     "AND NOT (media.path='' AND media.last_path!='' AND EXISTS ("
                     "SELECT 1 FROM deletion_items WHERE deletion_items.tweet_id=media.tweet_id "
                     "AND deletion_items.num=media.num AND deletion_items.media_type=media.media_type "
                     "AND deletion_items.path=media.last_path" + collation +
                     " AND deletion_items.status IN ('recycled','interrupted')))")
        fixture = ("WITH media(tweet_id,num,media_type,path,record_source,last_path) AS (VALUES (?,?,?,?,?,?)),"
                   "deletion_items(tweet_id,num,media_type,path,status) AS (VALUES (?,?,?,?,?)) "
                   "SELECT EXISTS(SELECT 1 FROM media")
        for path, source, last_path, receipt in product(
                ("", "present.jpg", None), ("saved", "declined", None),
                ("", "removed.jpg", "REMOVED.JPG", None), (None, "pending", "recycled", "interrupted")):
            values = ("100", 1, "photo", path, source, last_path, "100", 1, "photo", "removed.jpg", receipt)
            with self.subTest(path=path, source=source, last_path=last_path, receipt=receipt):
                actual = self.store._db.execute(fixture + where + ")", values).fetchone()[0]
                expected = self.store._db.execute(fixture + reference + ")", values).fetchone()[0]
                self.assertEqual(actual, expected)

    def test_read_indexes_survive_early_primary_key_and_column_migrations(self):
        early = self.account / "early"
        early.mkdir()
        with closing(sqlite3.connect(early / ".luvbox-history.sqlite3")) as db:
            db.execute("""CREATE TABLE media (
                folder_mode TEXT NOT NULL, tweet_id TEXT NOT NULL, num INTEGER NOT NULL,
                media_type TEXT NOT NULL, author TEXT NOT NULL DEFAULT '',
                filename TEXT NOT NULL DEFAULT '', posted_at TEXT, posted_source TEXT NOT NULL DEFAULT '',
                saved_at TEXT, path TEXT NOT NULL DEFAULT '', filesize INTEGER,
                status TEXT NOT NULL, record_source TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(folder_mode,tweet_id,num)
            )""")
            db.execute("INSERT INTO media(folder_mode,tweet_id,num,media_type,status,record_source,updated_at) "
                       "VALUES ('flat','100',1,'photo','saved','saved','2026-01-01')")
            db.commit()
        with closing(HistoryStore(early)) as migrated:
            row = migrated.records()[0]
            self.assertEqual(row["source_url"], "https://x.com/i/web/status/100")
            indices = {row["name"] for row in migrated._db.execute("PRAGMA index_list(media)")}
            self.assertTrue({"media_saved_order", "media_author", "media_source_url_missing"}.issubset(indices))
            # The migrated unique identity still accepts a distinct media kind.
            path = early / "100_video_1.mp4"
            path.write_bytes(b"offline")
            migrated.record_media("flat", {"tweet_id": "100", "num": 1, "type": "video"}, path)
            self.assertEqual(len(migrated.records()), 2)

    def test_pages_match_one_sorted_joined_query_for_every_order_and_filter(self):
        from luvbox.history import _JOINED, _ORDERS
        for index in range(23):
            tweet = str(1000 + index % 9)  # several media per tweet and layout
            path = self.file(f"{tweet}_{index}.jpg")
            for mode in ("flat", "author") if index % 4 == 0 else ("flat",):
                self.store.record_media(mode, {"tweet_id": tweet, "num": index + 1, "type": "photo",
                                               "author": {"name": "alice" if index % 2 else "bob"},
                                               "date": datetime(2020, 1, 1 + index % 5, tzinfo=timezone.utc)}, path)
        self.store.record_tweets([("1003", "고양이 사진", ["cat"]), ("1004", "강아지", [])])
        with self.store._db:
            self.store._db.execute("UPDATE media SET saved_at=NULL WHERE num % 3 = 0")  # ties and fallbacks
            self.store._db.executemany("INSERT INTO likes VALUES (?,?,NULL,?)",
                                       [("1001", 5.0, "x"), ("1002", 9.0, "x"), ("1005", 5.0, "x")])
        cases = [dict(), dict(query="고양이"), dict(query="alice"), dict(media_type="video"),
                 dict(author="BOB"), dict(period=("posted", "2020-01-02", "2020-01-03"))]
        for order in _ORDERS:
            for case in cases:
                where, params = self.store._record_filter(case.get("media_type", "all"), case.get("query", ""),
                                                          case.get("period"), case.get("author", ""))
                expected = [dict(row) for row in self.store._db.execute(
                    "SELECT media.*,likes.like_key,likes.liked_after,likes.liked_before,tweets.content,tweets.hashtags "
                    "FROM " + _JOINED + where + " ORDER BY " + _ORDERS[order] +
                    ",media.num,media.folder_mode,media.media_type", params)]
                count = self.store.record_page(0, 4, order=order, **case)["pages"]
                pages = [self.store.record_page(page, 4, order=order, **case) for page in range(count)]
                with self.subTest(order=order, case=case):
                    self.assertEqual([row for page in pages for row in page["rows"]], expected)
                    self.assertTrue(all(page["total"] == len(expected) for page in pages))
                    self.assertEqual(bool(expected), case.get("media_type") != "video")  # no video fixtures

    def test_file_view_fast_path_matches_the_general_merge(self):
        import random
        from luvbox.history import _identity

        def reference(rows):
            # General merge, with author completion scoped to the chosen identity.
            def priority(row):
                return (row["record_source"] in {"saved", "existing"}, row["updated_at"],
                        row["folder_mode"], row["tweet_id"], row["num"])
            groups = {}
            for row in rows:
                if row["path"]:
                    groups.setdefault(self.store._file_key(row["path"]), []).append(row)
            files, matched, found = [], 0, set()
            for group in groups.values():
                group = sorted(group, key=priority, reverse=True)
                size = next((row["filesize"] for row in group if row["filesize"] is not None), None)
                if size is None:
                    continue
                matched += len(group)
                found.update(_identity(item) for item in group)
                row = dict(group[0], filesize=size)
                author = next((item for item in group if item["author"]), None)
                if author:
                    row["author"], row["author_id"] = author["author"], author["author_id"]
                posted = next((item for item in group if item["posted_source"] == "metadata" and item["posted_at"]),
                              next((item for item in group if item["posted_at"]), None))
                if posted:
                    row["posted_at"], row["posted_source"] = posted["posted_at"], posted["posted_source"]
                saved = [item["saved_at"] for item in group if item["saved_at"]]
                row["saved_at"] = min(saved) if saved else None
                if any(item["metadata_status"] == "embedded" for item in group):
                    row["metadata_status"] = "embedded"
                verified_authors = {(item["author"], item["author_id"]) for item in group
                                    if item["author_metadata_status"] == "embedded"}
                if (row["author"], row["author_id"]) in verified_authors:
                    row["author_metadata_status"] = "embedded"
                elif row["author_metadata_status"] == "embedded":
                    row["author_metadata_status"] = "pending"
                files.append(row)
            return files, matched, found

        generator = random.Random(7)
        rows = []
        for index in range(400):
            shared = generator.random() < 0.3
            name = f"{index % 60 if shared else 1000 + index}_1.jpg"
            rows.append({
                "folder_mode": generator.choice(["flat", "author", "date"]), "tweet_id": str(index % 60 if shared else 1000 + index),
                "num": 1, "media_type": "photo", "author": generator.choice(["", "alice", "Bob"]),
                "author_id": generator.choice(["", "42"]), "filename": name,
                "posted_at": generator.choice([None, "", "2020-01-02T00:00:00+09:00", "2021-05-06T00:00:00+09:00"]),
                "posted_source": generator.choice(["", "metadata", "id_estimate"]),
                "saved_at": generator.choice([None, "", "2026-01-01T00:00:00+09:00", "2025-01-01T00:00:00+09:00"]),
                "path": "" if generator.random() < 0.1 else str(self.account / (name.upper() if generator.random() < 0.2 else name)),
                "filesize": generator.choice([None, 10, 20]), "status": "saved",
                "record_source": generator.choice(["saved", "existing", "archive", "file_scan"]),
                "updated_at": f"2026-01-0{generator.randint(1, 9)}", "source_url": "",
                "metadata_status": generator.choice(["pending", "embedded"]),
                "author_metadata_status": generator.choice(["pending", "embedded", "unknown"]), "last_path": ""})
        expected = reference([dict(row) for row in rows])
        actual = self.store._unique_files([dict(row) for row in rows], verify_files=False)
        key = lambda row: (row["path"].lower(), row["folder_mode"], row["tweet_id"])
        self.assertEqual(sorted(actual[0], key=key), sorted(expected[0], key=key))
        self.assertEqual(actual[1:], expected[1:])

    def test_overview_ranks_twenty_authors_unless_all_are_requested(self):
        authors = [{"name": f"author{index:02d}"} for index in range(25)]
        for index, author in enumerate(authors):
            path = self.file(f"{500 + index}_1.jpg")
            self.store.record_media("flat", {"tweet_id": str(500 + index), "num": 1, "type": "photo", "author": author}, path)
        self.assertEqual(len(self.store.overview(verify_files=False)["stats"]["authors"]), 20)
        self.assertEqual(len(self.store.overview(verify_files=False, top=None)["stats"]["authors"]), 25)

    def test_gif_without_a_kind_marker_imports_as_an_animation(self):
        self.file("100_1.gif", b"GIF89a" + b"offline-local-animation")
        self.store.import_legacy(refresh=True)
        rows = self.store.records()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["media_type"], "animated_gif")

    def metadata(self, number=1, kind="photo", *, ident="100", author="alice", posted=None):
        return {"tweet_id": ident, "num": number, "type": kind, "author": {"name": author},
                "date": posted, "url": "https://invalid.example/?token=" + SECRET,
                "auth_token": SECRET, "cookies": {"auth_token": SECRET}}

    def archive(self, entries, mode="author"):
        name = ".luvbox-archive.sqlite3" if mode == "author" else f".luvbox-archive.{mode}.sqlite3"
        path = self.account / name
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE archive (entry TEXT PRIMARY KEY) WITHOUT ROWID")
            db.executemany("INSERT INTO archive(entry) VALUES (?)", ((entry,) for entry in entries))
            db.commit()
        return path

    def test_summary_counts_animated_gifs_still_saved_as_mp4(self):
        self.store.record_media("flat", self.metadata(kind="animated_gif"), self.file("100_gif_1.mp4"))
        self.store.record_media("flat", self.metadata(kind="animated_gif", ident="101"), self.file("101_gif_1.gif"))
        self.store.record_media("flat", self.metadata(kind="video", ident="102"), self.file("102_video_1.mp4"))
        self.assertEqual(self.store.summary()["mp4_gifs"], 1)

    def test_source_metadata_status_does_not_change_original_save_time(self):
        path = self.file("100_1.jpg")
        data = self.metadata()
        self.store.record_media("flat", data, path)
        original = self.store.records()[0]
        self.assertEqual(original["source_url"], "https://x.com/i/web/status/100")
        self.assertEqual(self.store.media_record("flat", data)["metadata_status"], "pending")
        path.write_bytes(b"updated-offline-file-with-tags")
        self.store.mark_metadata(original, "embedded")
        record = self.store.records()[0]
        self.assertEqual(record["saved_at"], original["saved_at"])
        self.assertEqual(record["filesize"], path.stat().st_size)
        self.assertEqual(self.store.media_record("flat", data)["metadata_status"], "embedded")
        self.store.import_legacy(refresh=True)
        self.assertEqual(self.store.records()[0]["metadata_status"], "embedded")

    def test_tweet_author_numeric_id_and_status_survive_refresh(self):
        data = self.metadata()
        data["author"]["id"] = 123456789
        data["user"] = {"name": "liking_account", "id": 999}
        data["_luvbox_metadata_status"] = "embedded"
        data["_luvbox_author_metadata_status"] = "embedded"
        self.store.record_media("flat", data, self.file("100_1.jpg"))
        original = self.store.records()[0]
        self.assertEqual((original["author"], original["author_id"]), ("alice", "123456789"))
        self.assertEqual(self.store.summary()["embedded_authors"], 1)
        self.store.import_legacy(refresh=True)
        self.assertEqual(self.store.records()[0]["author_metadata_status"], "embedded")

    def test_author_is_unknown_without_tweet_author_even_when_liking_user_exists(self):
        data = self.metadata()
        data.pop("author")
        data["user"] = {"name": "liking_account", "id": 999}
        data["_luvbox_author_metadata_status"] = "unknown"
        self.store.record_media("flat", data, self.file("100_1.jpg"))
        row = self.store.records()[0]
        self.assertEqual((row["author"], row["author_id"], row["author_metadata_status"]), ("", "", "unknown"))

    def test_case_changed_author_stays_pending_through_refresh(self):
        data = self.metadata()
        data["author"] = {"name": "Writer", "id": 123}
        data["_luvbox_metadata_status"] = "embedded"
        data["_luvbox_author_metadata_status"] = "embedded"
        path = self.file("100_1.jpg")
        self.store.record_media("flat", data, path)
        original = self.store.records()[0]

        data["author"]["name"] = "writer"
        data["_luvbox_author_metadata_status"] = "pending"
        self.store.record_media("flat", data, path, status="existing")
        self.store.import_legacy(refresh=True)
        row = self.store.records()[0]
        self.assertEqual((row["author"], row["author_id"], row["author_metadata_status"]),
                         ("writer", "123", "pending"))
        self.assertEqual(row["saved_at"], original["saved_at"])

        # An unchanged author with no new write result still keeps the last
        # successful result when refreshing or importing a record.
        self.store.mark_metadata(row, "embedded", author_status="embedded")
        del data["_luvbox_author_metadata_status"]
        self.store.record_media("flat", data, path, status="existing")
        self.assertEqual(self.store.records()[0]["author_metadata_status"], "embedded")

    def test_author_columns_upgrade_old_database_without_losing_source_or_save_time(self):
        data = self.metadata()
        data["_luvbox_metadata_status"] = "embedded"
        self.store.record_media("flat", data, self.file("100_1.jpg"))
        original = self.store.records()[0]
        self.store._db.execute("ALTER TABLE media DROP COLUMN author_id")
        self.store._db.execute("ALTER TABLE media DROP COLUMN author_metadata_status")
        self.store._db.commit()
        with closing(HistoryStore(self.account)) as upgraded:
            row = upgraded.records()[0]
            for key in ("saved_at", "path", "author", "metadata_status", "source_url"):
                self.assertEqual(row[key], original[key])
            self.assertEqual(row["author_id"], "")
            self.assertEqual(row["author_metadata_status"], "pending")

    def test_author_backfill_failure_preserves_source_save_time_and_relocation(self):
        data = self.metadata()
        data["author"]["id"] = 123
        data["_luvbox_metadata_status"] = "embedded"
        self.store.record_media("flat", data, self.file("100_1.jpg"))
        relocated = self.relocated_store()
        original = relocated.records()[0]
        relocated.mark_metadata(original, "embedded", author_status="error")
        updated = relocated.records()[0]
        self.assertEqual(updated["metadata_status"], "embedded")
        self.assertEqual(updated["author_metadata_status"], "error")
        for key in ("saved_at", "path", "author", "author_id"):
            self.assertEqual(updated[key], original[key])

    def test_copied_catalog_refresh_matches_new_paths_without_accessing_old_folder(self):
        self.store.record_media("flat", self.metadata(), self.file("100_1.jpg"))
        new_account = Path(self.temp.name) / "moved_account"
        new_account.mkdir()
        new_file = new_account / "100_1.jpg"
        new_file.write_bytes(b"moved-local-media")
        with closing(sqlite3.connect(new_account / ".luvbox-history.sqlite3")) as copied:
            self.store._db.backup(copied)
        copied = HistoryStore(new_account)
        self.addCleanup(copied.close)
        copied.import_legacy(refresh=True)
        row = copied.records()[0]
        self.assertEqual(row["path"], str(new_file))
        self.assertEqual(row["filesize"], new_file.stat().st_size)

    def relocated_store(self):
        """A verified catalog transfer authorizes its former selected output."""
        new_output = Path(self.temp.name) / "new-output"
        records = new_output / ".luvbox" / "account"
        records.mkdir(parents=True)
        with closing(sqlite3.connect(records / ".luvbox-history.sqlite3")) as copied:
            self.store._db.backup(copied)
            copied.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('allowed_media_roots',?)",
                           (json.dumps([str(self.account), str(new_output)]),))
            copied.commit()
        copied = HistoryStore(records, media_dir=new_output, output_dir=new_output)
        self.addCleanup(copied.close)
        return copied

    def test_relocated_catalog_retains_existing_paths_save_time_and_source_status(self):
        path = self.file("alice/100_1.jpg")
        self.store.record_media("author", self.metadata(), path)
        self.store.mark_metadata(self.store.records()[0], "embedded")
        original = self.store.records()[0]
        relocated = self.relocated_store()
        relocated.import_legacy(refresh=True)
        row = relocated.records()[0]
        for field in ("path", "saved_at", "source_url", "metadata_status", "author", "filesize"):
            self.assertEqual(row[field], original[field])
        self.assertEqual(relocated.media_roots, (relocated.output_dir, self.account))
        self.assertTrue(relocated.is_media_path(path))
        self.assertFalse(relocated.is_download_path(path))
        self.assertTrue(relocated.is_download_path(relocated.output_dir / "100_2.jpg"))
        path.write_bytes(b"updated-tags")
        relocated.mark_metadata(row, "embedded")
        updated = relocated.records()[0]
        self.assertEqual(updated["filesize"], len(b"updated-tags"))
        self.assertEqual(updated["saved_at"], original["saved_at"])

    def test_old_roots_only_authorize_known_media_and_never_records_or_linked_files(self):
        known = self.file("100_1.jpg")
        unrelated = self.file("100_2.jpg")
        self.store.record_media("flat", self.metadata(), known)
        relocated = self.relocated_store()
        self.assertFalse(relocated.is_media_path(unrelated))
        self.assertFalse(relocated.is_media_path(self.account / ".luvbox" / "100_1.jpg"))
        self.assertFalse(relocated.is_download_path(known))
        with self.assertRaises(HistoryError):
            relocated.record_media("flat", self.metadata(number=2), unrelated)
        relocated.import_legacy(refresh=True)
        self.assertEqual(len(relocated.records()), 1)
        # A historical root itself can later become a junction/symlink.
        is_symlink = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda candidate: candidate == self.account or is_symlink(candidate)):
            self.assertFalse(relocated.is_media_path(known))

    def test_missing_former_file_keeps_location_until_drive_or_file_returns(self):
        path = self.file("100_1.jpg")
        self.store.record_media("flat", self.metadata(), path)
        relocated = self.relocated_store()
        original = relocated.records()[0]
        path.unlink()
        relocated.import_legacy(refresh=True)
        missing = relocated.records()[0]
        self.assertEqual(missing["path"], str(path))
        self.assertIsNone(missing["filesize"])
        self.assertEqual(missing["saved_at"], original["saved_at"])
        path.write_bytes(b"reconnected")
        relocated.import_legacy(refresh=True)
        self.assertEqual(relocated.records()[0]["filesize"], len(b"reconnected"))

    def test_local_scan_does_not_replace_valid_former_file_with_duplicate(self):
        old = self.file("100_1.jpg")
        self.store.record_media("flat", self.metadata(), old)
        relocated = self.relocated_store()
        (relocated.output_dir / old.name).write_bytes(b"unrelated-duplicate")
        relocated.import_legacy(refresh=True)
        self.assertEqual(relocated.records()[0]["path"], str(old))

    def test_records_three_media_types_without_ordinal_collision(self):
        for kind, name, payload in (("photo", "alice/100_1.jpg", b"aa"),
                                    ("video", "alice/100_video_1.mp4", b"bbb"),
                                    ("animated_gif", "alice/100_gif_1.mp4", b"cccc")):
            self.store.record_media("author", self.metadata(kind=kind), self.file(name, payload))
        summary = self.store.summary()
        self.assertEqual((summary["files"], summary["posts"], summary["bytes"]), (3, 1, 9))
        self.assertEqual(summary["counts"], {"photo": 1, "video": 1, "animated_gif": 1})
        self.assertEqual(summary["unknown_bytes"], 0)
        self.assertEqual(len(self.store.records()), 3)

    def test_exact_media_lookup_separates_layout_type_and_number_without_writing(self):
        identities = (("date", "photo", 1, "2026-10-04/100_1.jpg"),
                      ("date", "video", 1, "2026-10-04/100_video_1.mp4"),
                      ("date", "photo", 2, "2026-10-04/100_2.jpg"),
                      ("flat", "photo", 1, "100_1.jpg"))
        for mode, kind, number, name in identities:
            self.store.record_media(mode, self.metadata(number=number, kind=kind), self.file(name))
        changes = self.store._db.total_changes
        for mode, kind, number, name in identities:
            row = self.store.media_record(mode, self.metadata(number=number, kind=kind))
            self.assertEqual(row["path"], str(self.account / name))
            self.assertTrue(row["saved_at"])
        self.assertIsNone(self.store.media_record("date", self.metadata(kind="animated_gif")))
        self.assertIsNone(self.store.media_record("author", self.metadata()))
        self.assertIsNone(self.store.media_record("date", self.metadata(ident="101")))
        self.assertEqual(self.store._db.total_changes, changes)

    def test_duplicate_skip_enriches_without_replacing_known_saved_time(self):
        path = self.file("alice/100_1.jpg")
        with patch("luvbox.history._now", return_value="2026-10-04T22:00:00+09:00"):
            self.store.record_media("author", self.metadata(author="", posted=None), path)
        with patch("luvbox.history._now", return_value="2026-10-05T22:00:00+09:00"):
            self.store.record_media("author", self.metadata(posted=datetime(2024, 1, 1)), path, status="existing")
        row = self.store.records()[0]
        self.assertEqual(row["saved_at"], "2026-10-04T22:00:00+09:00")
        self.assertEqual(row["author"], "alice")
        self.assertEqual(row["posted_at"], "2024-01-01T09:00:00+09:00")
        self.assertEqual(row["status"], "saved")
        self.assertEqual(self.store.summary()["files"], 1)

    def test_old_existing_file_save_time_stays_unknown_and_metadata_is_whitelisted(self):
        path = self.file("alice/100_1.jpg")
        os.utime(path, (946684800, 946684800))
        self.store.record_media("author", self.metadata(posted=datetime(2026, 10, 3, 16)), path, status="existing")
        row = self.store.records()[0]
        self.assertIsNone(row["saved_at"])
        self.assertEqual(row["posted_at"], "2026-10-04T01:00:00+09:00")
        self.assertNotIn(SECRET, json.dumps(row))
        self.assertNotIn("url", row)
        self.assertNotIn("cookies", row)

    def test_archive_only_moved_files_have_unknown_path_size_and_saved_time(self):
        posted = datetime(2024, 3, 2, 16, tzinfo=timezone.utc)
        ident = tweet_id(posted)
        archive = self.archive([f"twitter{ident}_1", "twitter100_1", "ignore-unrecognized-entry"])
        before = archive.read_bytes()
        self.store.import_legacy()
        records = {row["tweet_id"]: row for row in self.store.records()}
        row = records[ident]
        self.assertEqual(row["posted_at"], "2024-03-03T01:00:00+09:00")
        self.assertEqual(row["posted_source"], "id_estimate")
        self.assertEqual(row["path"], "")
        self.assertIsNone(row["filesize"])
        self.assertIsNone(row["saved_at"])
        self.assertIsNone(records["100"]["posted_at"])
        self.assertEqual(archive.read_bytes(), before)
        summary = self.store.summary()
        self.assertEqual((summary["files"], summary["record_count"], summary["unknown_bytes"], summary["estimated_dates"]),
                         (0, 2, 2, 1))
        self.assertEqual(summary["archivepaths"], [str(archive)])

    def test_import_merges_archive_and_file_by_kind_layout_and_number(self):
        entries = ["twitter100_1", "twitter100_video_1", "twitter100_animated_gif_1"]
        archive = self.archive(entries)
        before = archive.read_bytes()
        self.file("alice/100_1.jpg", b"a")
        self.file("alice/100_video_1.mp4", b"bb")
        self.file("alice/100_gif_1.mp4", b"ccc")
        self.file("100_1.png", b"dddd")
        self.file("2026-10-04/100_1.webp", b"eeeee")
        self.file("alice/101_1.gif", b"gif-animation-format")
        self.file("alice/100_2.jpg.part", b"partial")
        self.file("alice/random.jpg", b"unrecognized")
        self.file("alice/100_0.jpg", b"invalid-number")
        self.store.import_legacy()
        summary = self.store.summary()
        self.assertEqual(summary["files"], 6)
        self.assertEqual(summary["posts"], 2)
        self.assertEqual(summary["photo"], 3)
        self.assertEqual(summary["video"], 1)
        self.assertEqual(summary["animated_gif"], 2)
        self.assertTrue(all(row["saved_at"] is None for row in self.store.records()))
        self.assertEqual(archive.read_bytes(), before)
        alice_rows = [row for row in self.store.records() if row["folder_mode"] == "author"]
        self.assertTrue(all(row["author"] == "alice" for row in alice_rows))

    def test_default_import_is_cached_and_manual_refresh_finds_new_or_changed_files(self):
        path = self.file("alice/100_1.jpg", b"a")
        first = self.store.import_legacy()
        self.assertTrue(first["imported"])
        self.file("alice/101_1.jpg", b"new")
        with patch.object(self.store, "_files", side_effect=AssertionError("must not rescan")):
            self.assertFalse(self.store.import_legacy()["imported"])
        self.assertEqual(self.store.summary()["files"], 1)
        path.write_bytes(b"changed-length")
        self.store.import_legacy(refresh=True)
        self.assertEqual(self.store.summary()["files"], 2)
        self.assertEqual(next(row for row in self.store.records() if row["tweet_id"] == "100")["filesize"], 14)
        path.unlink()
        self.store.import_legacy(refresh=True)
        row = next(row for row in self.store.records() if row["tweet_id"] == "100")
        self.assertEqual(row["path"], "")
        self.assertIsNone(row["filesize"])

    def test_import_does_not_overwrite_real_metadata_or_actual_save_time(self):
        ident = tweet_id(datetime(2024, 1, 1, tzinfo=timezone.utc))
        path = self.file(f"alice/{ident}_1.jpg")
        self.archive([f"twitter{ident}_1"])
        posted = datetime(2023, 12, 31, 23, tzinfo=timezone.utc)
        with patch("luvbox.history._now", return_value="2026-10-04T23:00:00+09:00"):
            self.store.record_media("author", self.metadata(ident=ident, author="Alice", posted=posted), path)
        self.store.import_legacy()
        row = self.store.records()[0]
        self.assertEqual(row["posted_at"], "2024-01-01T08:00:00+09:00")
        self.assertEqual(row["posted_source"], "metadata")
        self.assertEqual(row["saved_at"], "2026-10-04T23:00:00+09:00")
        self.assertEqual(row["author"], "Alice")
        self.assertEqual(row["filename"], path.name)

    def test_runs_preserve_partial_counts_position_and_safe_fields(self):
        ident = tweet_id(datetime(2024, 1, 1, tzinfo=timezone.utc))
        run = self.store.start_run("author")
        self.store.update_run(run, saved=2, skipped=3, size_skipped=1, visited=9,
                              last_tweet_id=ident, last_posted_at=datetime(2024, 1, 1))
        self.store.finish_run(run, "stopped", message="사용자가 중단했습니다.")
        row = self.store.runs()[0]
        self.assertEqual((row["id"], row["status"], row["folder_mode"]), (run, "stopped", "author"))
        self.assertEqual((row["saved"], row["skipped"], row["size_skipped"], row["visited"]), (2, 3, 1, 9))
        self.assertEqual(row["last_tweet_id"], ident)
        self.assertEqual(row["last_posted_at"], "2024-01-01T09:00:00+09:00")
        self.assertTrue(row["finished_at"])
        self.assertEqual(self.store.summary()["last_run"], row)
        later = self.store.start_run("flat")
        self.store.finish_run(later, "error", saved=1, message="저장 오류")
        self.assertEqual(self.store.summary()["last_run"]["status"], "error")
        with self.assertRaises(HistoryError):
            self.store.update_run(later, auth_token=SECRET)

    def test_failed_run_update_is_atomic_and_does_not_destroy_previous_counts(self):
        run = self.store.start_run("author")
        self.store.update_run(run, saved=4)
        with self.assertRaises(HistoryError):
            self.store.finish_run(run, "error", saved=-1)
        row = self.store.runs()[0]
        self.assertEqual((row["saved"], row["status"], row["finished_at"]), (4, "running", None))

    def test_abandoned_run_recovery_keeps_progress_and_unknown_end_time(self):
        finished = self.store.start_run("author")
        self.store.finish_run(finished, "finished", saved=1, message="completed")
        abandoned = self.store.start_run("flat")
        self.store.update_run(abandoned, saved=3, skipped=5, visited=7,
                              last_tweet_id="100", last_posted_at="2026-10-04T10:00:00+09:00")
        original = next(row for row in self.store.runs() if row["id"] == abandoned)
        self.store.close()
        self.store = HistoryStore(self.account)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.recover_interrupted_runs(), 1)
        recovered = next(row for row in self.store.runs() if row["id"] == abandoned)
        self.assertEqual(recovered["status"], "interrupted")
        self.assertIsNone(recovered["finished_at"])
        for key in ("started_at", "saved", "skipped", "size_skipped", "visited", "last_tweet_id", "last_posted_at"):
            self.assertEqual(recovered[key], original[key])
        self.assertIn("비정상 종료", recovered["message"])
        self.assertEqual(next(row for row in self.store.runs() if row["id"] == finished)["status"], "finished")
        self.assertEqual(self.store.recover_interrupted_runs(), 0)

    def test_record_reader_does_not_mark_active_run_interrupted(self):
        active = self.store.start_run("date")
        observer = HistoryStore(self.account)
        self.addCleanup(observer.close)
        observer.summary()
        self.assertEqual(observer.runs()[0]["id"], active)
        self.assertEqual(observer.runs()[0]["status"], "running")

    def test_abandoned_run_recovery_failure_rolls_back_all_status_changes(self):
        first = self.store.start_run("author")
        second = self.store.start_run("flat")
        self.store.update_run(first, saved=2)
        self.store.update_run(second, saved=3)
        before = self.store.runs()
        self.store._db.execute(f"""CREATE TRIGGER reject_recovery BEFORE UPDATE ON runs
            WHEN NEW.id={second} AND NEW.status='interrupted'
            BEGIN SELECT RAISE(ABORT,'{SECRET}'); END""")
        self.store._db.commit()
        with self.assertRaises(HistoryError) as error:
            self.store.recover_interrupted_runs()
        self.assertNotIn(SECRET, str(error.exception))
        self.assertEqual(self.store.runs(), before)

    def test_wal_reader_observes_autocommitted_writer_progress(self):
        other = HistoryStore(self.account)
        self.addCleanup(other.close)
        run = self.store.start_run("author")
        self.store.update_run(run, visited=3)
        self.store.record_media("author", self.metadata(), self.file("alice/100_1.jpg"))
        self.assertEqual(other.summary()["files"], 1)
        self.assertEqual(other.runs()[0]["visited"], 3)
        self.assertEqual(self.store._db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.store._db.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertEqual(other._db.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_filter_search_pagination(self):
        for number, kind, author in ((1, "photo", "=danger"), (2, "video", "bob_50%"),
                                     (3, "animated_gif", "@danger")):
            self.store.record_media("flat", self.metadata(number, kind, author=author), self.file(f"100_{number}.mp4"))
        self.assertEqual(len(self.store.records(media_type="video")), 1)
        self.assertEqual(len(self.store.records(query="bob_50%")), 1)
        self.assertEqual(len(self.store.records(query="100_2")), 1)
        self.assertEqual(len(self.store.records(limit=1, offset=1)), 1)



    def test_list_and_sample_decode_failures_release_cursors_while_errors_are_retained(self):
        readers = (lambda store: store.records(), lambda store: store.missing_media(verify_files=False))
        for index, read in enumerate(readers):
            with self.subTest(reader=index):
                with closing(HistoryStore(self.account / f"decode-{index}")) as store:
                    path = store.media_dir / "100_gif_1.mp4"
                    path.write_bytes(b"offline")
                    store.record_media("flat", self.metadata(kind="animated_gif"), path)
                    with store._db:
                        store._db.execute("UPDATE media SET filename=CAST(X'FF' AS TEXT)")
                    with self.assertRaises(HistoryError) as failure:
                        read(store)
                # Windows refuses this rename while a cursor in the retained
                # translated exception still pins the closed connection.
                store.path.rename(store.path.with_suffix(".closed"))
                self.assertIsNotNone(failure.exception)

    def test_pages_use_filtered_totals_and_clamp_empty_or_outdated_positions(self):
        for number, kind, author in ((1, "photo", "bob_50%"), (2, "photo", "bob_50%"),
                                     (3, "video", "bob_50%"), (4, "photo", "other")):
            self.store.record_media("flat", self.metadata(number, kind, author=author), self.file(f"100_{number}.mp4"))
        first = self.store.record_page(page_size=1, media_type="photo", query="bob_50%")
        last = self.store.record_page(page=99, page_size=1, media_type="photo", query="bob_50%")
        self.assertEqual((first["total"], first["pages"], first["page"]), (2, 2, 0))
        self.assertEqual((last["total"], last["pages"], last["page"]), (2, 2, 1))
        self.assertNotEqual(first["rows"][0]["num"], last["rows"][0]["num"])
        empty = self.store.record_page(page=99, page_size=50, query="absent")
        self.assertEqual((empty["total"], empty["pages"], empty["page"], empty["rows"]), (0, 0, 0, []))
        for bad in ({"page": -1}, {"page": True}, {"page_size": 0}, {"page_size": 1001}, {"page_size": "100"}):
            with self.subTest(bad=bad), self.assertRaises(HistoryError):
                self.store.record_page(**bad)

    def test_page_total_and_rows_share_a_read_snapshot_without_blocking_writer(self):
        self.store.record_media("flat", self.metadata(), self.file("100_1.jpg"))
        other = HistoryStore(self.account)
        self.addCleanup(other.close)
        original = self.store.records

        def concurrent_insert(*args, **kwargs):
            other.record_media("flat", self.metadata(2), self.file("100_2.jpg"))
            return original(*args, **kwargs)

        with patch.object(self.store, "records", side_effect=concurrent_insert):
            page = self.store.record_page()
        self.assertEqual((page["total"], len(page["rows"])), (1, 1))
        self.assertEqual(self.store.record_page()["total"], 2)

    def test_overview_shares_choices_snapshot_without_blocking_another_writer(self):
        self.store.record_media("flat", self.metadata(), self.file("100_1.jpg"))
        with self.store._db:
            self.store._db.execute("UPDATE media SET path='',filesize=NULL")
        other = HistoryStore(self.account)
        self.addCleanup(other.close)
        original = self.store._ignored
        calls = 0

        def concurrent_choice():
            nonlocal calls
            ignored = original()
            calls += 1
            if calls == 1:
                other.set_missing_ignored([("100", 1, "photo")], True)
            return ignored

        with patch.object(self.store, "_ignored", side_effect=concurrent_choice):
            snapshot = self.store.overview(verify_files=False)
        self.assertEqual((snapshot["missing_files"], snapshot["ignored_missing"]), (1, 0))
        self.assertFalse(snapshot["missing"][0]["ignored"])
        current = self.store.overview(verify_files=False)
        self.assertEqual((current["missing_files"], current["ignored_missing"]), (0, 1))
        self.assertTrue(current["missing"][0]["ignored"])

    def test_overview_and_pages_join_an_existing_transaction_without_ending_it(self):
        self.store.record_media("flat", self.metadata(), self.file("100_1.jpg"))
        self.store._db.execute("BEGIN")
        try:
            self.assertEqual(self.store.overview(verify_files=False)["record_count"], 1)
            self.assertEqual(self.store.record_page()["total"], 1)
            self.assertEqual(self.store.run_page()["total"], 0)
            self.assertEqual(self.store.deletion_page()["total"], 0)
            self.assertTrue(self.store._db.in_transaction)
        finally:
            self.store._db.rollback()

    def test_page_boundaries_have_stable_order_when_saved_times_tie(self):
        path = self.file("100_1.jpg")
        for mode in ("flat", "author", "date"):
            self.store.record_media(mode, self.metadata(), path)
        self.store._db.execute("UPDATE media SET saved_at='2026-10-05T00:00:00+09:00'")
        self.store._db.commit()
        rows = [self.store.record_page(page=index, page_size=1)["rows"][0] for index in range(3)]
        self.assertEqual([row["folder_mode"] for row in rows], ["author", "date", "flat"])
        self.assertEqual(rows, self.store.record_page()["rows"])

    def test_all_execution_history_is_pageable_with_clamped_last_page(self):
        ids = [self.store.start_run("flat") for _ in range(7)]
        first = self.store.run_page(page_size=3)
        last = self.store.run_page(page=99, page_size=3)
        self.assertEqual((first["total"], first["pages"]), (7, 3))
        self.assertEqual([row["id"] for row in first["rows"]], ids[-1:-4:-1])
        self.assertEqual((last["page"], [row["id"] for row in last["rows"]]), (2, [ids[0]]))
        self.assertEqual([row["id"] for row in self.store.runs(limit=2, offset=3)], [ids[3], ids[2]])
        with self.assertRaises(HistoryError):
            self.store.runs(offset=-1)

    def test_missing_snapshot_contains_more_than_five_thousand_media(self):
        self.archive((f"twitter{ident}_1" for ident in range(10000, 15001)), mode="flat")
        self.store.import_legacy()
        self.store.record_tweets([("10000", "oldest missing media", ["old"]),
                                  ("15000", "newest missing media", ["new"])])

        snapshot = self.store.overview()

        self.assertEqual((snapshot["missing_files"], len(snapshot["missing"])), (5001, 5001))
        self.assertEqual((snapshot["missing"][0]["tweet_id"], snapshot["missing"][-1]["tweet_id"]),
                         ("15000", "10000"))
        self.assertEqual(snapshot["missing"][-1]["content"], "oldest missing media")
        self.assertEqual(len(self.store.missing_media()), 5001)

    def test_hidden_missing_media_never_displaces_older_visible_media(self):
        self.archive((f"twitter{ident}_1" for ident in range(10000, 15001)), mode="flat")
        self.store.import_legacy()
        hidden = [(str(ident), 1, "photo") for ident in range(10001, 15001)]
        self.store.set_missing_ignored(hidden, True)

        snapshot = self.store.overview()
        visible = [item for item in snapshot["missing"] if not item["ignored"]]

        self.assertEqual((snapshot["missing_files"], snapshot["ignored_missing"]), (1, 5000))
        self.assertEqual([item["tweet_id"] for item in visible], ["10000"])
        self.assertEqual(len(snapshot["missing"]), 5001)
        self.store.set_missing_ignored([("10001", 1, "photo")], False)
        snapshot = self.store.overview()
        self.assertEqual(snapshot["missing_files"], 2)
        self.assertEqual({item["tweet_id"] for item in snapshot["missing"] if not item["ignored"]},
                         {"10000", "10001"})

    def test_outside_paths_and_missing_saved_files_are_rejected_without_catalog_rows(self):
        outside = Path(self.temp.name) / "outside.jpg"
        outside.write_bytes(b"outside")
        for path in (outside, self.account / "missing.jpg"):
            with self.subTest(path=path.name), self.assertRaises(HistoryError) as error:
                self.store.record_media("author", self.metadata(), path)
            self.assertNotIn(str(path), str(error.exception))
        self.assertEqual(self.store.summary()["files"], 0)

    def test_scan_excludes_symlink_targets_and_directory_loops(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "100_1.jpg").write_bytes(b"outside")
        link = self.account / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
            (self.account / "loop").symlink_to(self.account, target_is_directory=True)
        except OSError:
            # Symbolic links need a Windows privilege; junctions redirect the
            # same way and need none.
            try:
                import _winapi
                _winapi.CreateJunction(str(outside), str(link))
                _winapi.CreateJunction(str(self.account), str(self.account / "loop"))
            except (ImportError, OSError):
                self.skipTest("Windows directory links are unavailable")
        self.store.import_legacy()
        self.assertEqual(self.store.summary()["files"], 0)

    def test_bad_archive_does_not_prevent_actual_file_import_and_is_not_modified(self):
        archive = self.account / ".luvbox-archive.sqlite3"
        archive.write_bytes(b"bad archive " + SECRET.encode())
        self.file("alice/100_1.jpg")
        result = self.store.import_legacy()
        self.assertTrue(result["warnings"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(self.store.summary()["files"], 1)
        self.assertEqual(archive.read_bytes(), b"bad archive " + SECRET.encode())

    def test_refresh_never_blocks_a_concurrent_download_writer(self):
        # A download worker shares this catalog with a 5 s busy timeout. The
        # library scan must not hold the write lock while it inspects files.
        self.archive([f"twitter{100 + index}_1" for index in range(30)])
        for index in range(30):
            self.file(f"alice/{100 + index}_1.jpg")
        self.store.import_legacy()
        for index in range(30):
            self.file(f"alice/{200 + index}_1.jpg")   # only the folder scan finds these
        run = self.store.start_run("author")
        worker = sqlite3.connect(self.store.path, timeout=0.2)
        self.addCleanup(worker.close)
        writes = []
        original = HistoryStore._file

        def concurrent_write(store, path):
            with worker:
                worker.execute("UPDATE runs SET saved=saved+1 WHERE id=?", (run,))
            writes.append(path)
            return original(store, path)

        with patch.object(HistoryStore, "_file", concurrent_write):
            self.store.import_legacy(refresh=True)
        # Recorded files are checked once (the scan reuses that result) and
        # new files by the scan; a writer succeeds during every check.
        self.assertEqual(len(writes), 60)
        self.assertEqual(len({str(path) for path in writes}), 60)
        self.assertEqual(self.store.runs(limit=1)[0]["saved"], len(writes))
        self.assertEqual(self.store.summary()["files"], 60)

    def test_refresh_checks_shared_present_paths_once_without_changing_records(self):
        former = Path(self.temp.name) / "former-output"
        former.mkdir()
        paths = []
        for index, (location, kind) in enumerate(
                (("current", "photo"), ("current", "animated_gif"),
                 ("former", "photo"), ("former", "animated_gif"))):
            tweet = str(100 + index)
            name = f"{tweet}_1.jpg" if kind == "photo" else f"{tweet}_gif_1.gif"
            path = self.file("alice/" + name)
            metadata = self.metadata(ident=tweet, kind=kind, posted=datetime(2020, 1, 1, tzinfo=timezone.utc))
            metadata["author"]["id"] = 123
            metadata.update(_luvbox_metadata_status="embedded", _luvbox_author_metadata_status="embedded")
            for mode in ("author", "date", "flat"):
                self.store.record_media(mode, metadata, path)
            if location == "former":
                moved = former / name
                path.rename(moved)
                path = moved
                with self.store._db:
                    self.store._db.execute("UPDATE media SET path=? WHERE tweet_id=?", (str(path), tweet))
            paths.append(str(path))
        with self.store._db:
            self.store._db.execute("INSERT INTO catalog_meta VALUES ('allowed_media_roots',?)",
                                   (json.dumps([str(former)]),))
        self.store._media_roots = self.store._read_media_roots()
        (former / "999_1.jpg").write_bytes(b"unrelated former-root file")
        original = self.store.records()

        with patch.object(self.store, "_file", wraps=self.store._file) as checked:
            self.store.import_legacy(refresh=True)

        self.assertEqual(sorted(str(call.args[0]) for call in checked.call_args_list), sorted(paths))
        self.assertEqual(self.store.records(), original)
        self.assertEqual((self.store.summary()["files"], self.store.summary()["record_count"]), (4, 12))

    def test_refresh_rechecks_missing_shared_gif_paths_and_preserves_history(self):
        path = self.file("100_gif_1.gif")
        metadata = self.metadata(kind="animated_gif", posted=datetime(2020, 1, 1, tzinfo=timezone.utc))
        metadata.update(_luvbox_metadata_status="embedded", _luvbox_author_metadata_status="embedded")
        for mode in ("author", "date", "flat"):
            self.store.record_media(mode, metadata, path)
        original = {row["folder_mode"]: row for row in self.store.records()}
        path.unlink()

        with patch.object(self.store, "_file", wraps=self.store._file) as checked:
            self.store.import_legacy(refresh=True)

        self.assertEqual([str(call.args[0]) for call in checked.call_args_list], [str(path)] * 3)
        for row in self.store.records():
            self.assertEqual((row["path"], row["last_path"], row["filesize"]), ("", str(path), None))
            for field in ("author", "posted_at", "saved_at", "metadata_status", "author_metadata_status"):
                self.assertEqual(row[field], original[row["folder_mode"]][field])
        [missing] = self.store.missing_media()
        self.assertEqual((missing["tweet_id"], missing["media_type"], missing["folder_modes"]),
                         ("100", "animated_gif", ["author", "date", "flat"]))
        self.assertEqual(self.store.summary()["files"], 0)

    def test_linked_file_is_missing_and_outside_file_is_refused(self):
        inside = self.file("alice/100_1.jpg")
        self.assertEqual(self.store._file(inside), (str(inside.resolve()), len(b"offline-media"), inside.name))
        is_symlink = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda candidate: candidate == inside or is_symlink(candidate)):
            self.assertEqual(self.store._file(inside), ("", None, inside.name))
        outside = Path(self.temp.name) / "elsewhere" / "101_1.jpg"
        outside.parent.mkdir()
        outside.write_bytes(b"offline-media")
        with self.assertRaises(HistoryError):
            self.store._file(outside)

    @unittest.skipUnless(os.name == "nt", "Windows junction")
    def test_folder_scan_skips_a_junction_to_another_folder(self):
        import _winapi
        elsewhere = Path(self.temp.name) / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "300_1.jpg").write_bytes(b"offline-media")
        self.file("alice/100_1.jpg")
        _winapi.CreateJunction(str(elsewhere), str(self.account / "linked"))
        self.store.import_legacy(refresh=True)
        paths = [row["path"] for row in self.store.records()]
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith("100_1.jpg"))

    def test_import_commits_in_short_batches(self):
        self.archive([f"twitter{100 + index}_1" for index in range(5)])
        for index in range(5):
            self.file(f"alice/{100 + index}_1.jpg")
        starts = []
        original = HistoryStore._upsert

        def observe(store, row, **kwargs):
            starts.append(store._db.in_transaction)
            return original(store, row, **kwargs)

        with patch("luvbox.history._WRITE_BATCH", 1), patch.object(HistoryStore, "_upsert", observe):
            self.store.import_legacy(refresh=True)
        self.assertEqual(len(starts), 10)
        self.assertFalse(any(starts))  # Every row began a new, already-committed transaction.
        self.assertEqual(self.store.summary()["files"], 5)

    def test_quick_scan_needs_a_finished_latest_run_with_the_same_media_settings(self):
        self.assertEqual(self.store.quick_scan_readiness("flat", "v1:a"), "incomplete")
        for status in ("stopped", "error", "limited"):
            run = self.store.start_run("flat", media_signature="v1:a")
            self.store.finish_run(run, status)
            self.assertEqual(self.store.quick_scan_readiness("flat", "v1:a"), "incomplete")
        run = self.store.start_run("flat", scan_mode="quick", media_signature="v1:a")
        self.store.finish_run(run, "finished")
        self.assertEqual(self.store.quick_scan_readiness("flat", "v1:a"), "ready")
        self.assertEqual(self.store.quick_scan_readiness("flat", "v1:b"), "settings-changed")
        self.assertEqual(self.store.quick_scan_readiness("author", "v1:a"), "incomplete")
        self.store.start_run("flat", media_signature="v1:a")
        self.assertEqual(self.store.recover_interrupted_runs(), 1)
        self.assertEqual(self.store.quick_scan_readiness("flat", "v1:a"), "incomplete")
        for bad in ({"scan_mode": "new"}, {"media_signature": None}):
            with self.subTest(bad=bad), self.assertRaises(HistoryError):
                self.store.start_run("flat", **bad)

    def test_runs_from_earlier_versions_gain_scan_columns_and_require_a_full_scan(self):
        legacy = Path(self.temp.name) / "legacy"
        legacy.mkdir()
        with closing(sqlite3.connect(legacy / ".luvbox-history.sqlite3")) as db:
            db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, folder_mode TEXT NOT NULL, "
                       "started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, saved INTEGER NOT NULL DEFAULT 0, "
                       "skipped INTEGER NOT NULL DEFAULT 0, size_skipped INTEGER NOT NULL DEFAULT 0, "
                       "visited INTEGER NOT NULL DEFAULT 0, last_tweet_id TEXT NOT NULL DEFAULT '', "
                       "last_posted_at TEXT, message TEXT NOT NULL DEFAULT '')")
            db.execute("INSERT INTO runs(folder_mode,started_at,finished_at,status) VALUES "
                       "('flat','2026-10-04T20:00:00+09:00','2026-10-04T20:10:00+09:00','finished')")
            db.commit()
        store = HistoryStore(legacy)
        try:
            self.assertEqual(store.runs()[0]["scan_mode"], "full")
            self.assertEqual(store.quick_scan_readiness("flat", "v1:videos=1:gifs=1:max_mb=200"), "settings-changed")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
