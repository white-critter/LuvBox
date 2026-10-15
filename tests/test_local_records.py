"""Live records on this PC; complete snapshots travel with the media folder."""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tests import test_core
from luvbox.history import HistoryStore
from luvbox.storage import (MigrationError, migrate_records, prepare_storage,
                               publish_snapshot, resolve_storage)

HISTORY = ".luvbox-history.sqlite3"
ARCHIVE = ".luvbox-archive.flat.sqlite3"


def count(path, table="media"):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def written_later(*paths):
    """Give files another PC changed a later time, as a synced copy would have.

    Within one coarse Windows clock tick, a same-size test write right after
    publishing could keep the published size and time and look unchanged.
    """
    for path in paths:
        written = path.stat()
        os.utime(path, ns=(written.st_atime_ns, written.st_mtime_ns + 1_000_000_000))


class LocalRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        protection = patch("luvbox.protection.state_directory", return_value=self.root / "pc" / "request-protection")
        protection.start()
        self.addCleanup(protection.stop)
        self.output = self.root / "Google Drive" / "사진"
        self.output.mkdir(parents=True)
        self.paths = resolve_storage(self.output, "alice")

    def earlier_version_records(self, tweets):
        """Records as 1.10 and earlier kept them: live in the media folder."""
        self.paths.snapshot_dir.mkdir(parents=True)
        store = HistoryStore(self.paths.snapshot_dir, media_dir=self.output, output_dir=self.output)
        with closing(store):
            for tweet in tweets:
                path = self.output / f"{tweet}_1.jpg"
                path.write_bytes(b"offline")
                store.record_media("flat", {"tweet_id": tweet, "num": 1, "type": "photo", "author": {"name": "bob"}}, path)
        with closing(sqlite3.connect(self.paths.snapshot_dir / ARCHIVE)) as db:
            db.execute("CREATE TABLE archive (entry TEXT PRIMARY KEY) WITHOUT ROWID")
            db.executemany("INSERT INTO archive VALUES (?)", [(f"twitter{tweet}_1",) for tweet in tweets])
            db.commit()

    def test_live_records_are_outside_the_media_folder(self):
        self.assertFalse(self.paths.record_dir.is_relative_to(self.output))
        self.assertTrue(self.paths.record_dir.is_relative_to(self.root / "pc" / "records"))
        self.assertTrue(self.paths.record_dir.parent.name.startswith("사진-"))
        self.assertNotEqual(resolve_storage(self.root / "other" / "사진", "alice").record_dir, self.paths.record_dir)

    def external_snapshot(self, *, restore_trigger=False, metadata_view=False):
        outside = self.root / "not-selected"
        outside.mkdir(exist_ok=True)
        victim = outside / "100_1.jpg"
        victim.write_bytes(b"synthetic outside file")
        with closing(HistoryStore(self.paths.snapshot_dir, media_dir=outside, output_dir=outside)) as supplied:
            supplied.record_media("flat", {"tweet_id": "100", "num": 1, "type": "photo"}, victim)
            declaration = json.dumps([str(outside)])
            with supplied._db:
                supplied._db.execute("INSERT OR REPLACE INTO catalog_meta VALUES ('allowed_media_roots',?)",
                                     (declaration,))
                literal = "'" + declaration.replace("'", "''") + "'"
                if restore_trigger:
                    supplied._db.execute(
                        'CREATE TRIGGER "restore""roots" AFTER DELETE ON catalog_meta '
                        "WHEN old.key='allowed_media_roots' BEGIN INSERT OR REPLACE INTO catalog_meta "
                        "VALUES ('allowed_media_roots'," + literal + "); END")
                    supplied._db.execute(
                        "CREATE TRIGGER grant_roots_later AFTER INSERT ON catalog_meta "
                        "WHEN new.key='legacy_imported' BEGIN INSERT OR REPLACE INTO catalog_meta "
                        "VALUES ('allowed_media_roots'," + literal + "); END")
                    supplied._db.execute("ALTER TABLE catalog_meta RENAME TO metadata_case_temp")
                    supplied._db.execute("ALTER TABLE metadata_case_temp RENAME TO Catalog_Meta")
                if metadata_view:
                    supplied._db.execute("DROP TABLE catalog_meta")
                    supplied._db.execute("CREATE VIEW Catalog_Meta AS SELECT 'allowed_media_roots' AS key," +
                                         literal + " AS value")
        return victim

    def test_first_shared_import_cannot_authorize_or_recycle_an_outside_file(self):
        from luvbox.deletion import run_deletion

        victim = self.external_snapshot(restore_trigger=True)
        source = self.paths.snapshot_dir / HISTORY
        previous = source.read_bytes()
        for _ in range(2):
            with closing(HistoryStore.for_storage(self.paths, create_media=False)) as imported:
                self.assertEqual(imported.media_roots, (self.output,))
                self.assertFalse(imported.is_media_path(victim))
                self.assertIsNone(imported._media_path(victim))
                self.assertEqual(imported.overview(verify_files=True)["files"], 0)
                imported.import_legacy()
                rows = imported.records()
                self.assertEqual(imported._db.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0], 0)
        self.assertEqual(source.read_bytes(), previous)
        with patch("luvbox.deletion.recycle_file", side_effect=AssertionError("native recycle forbidden")) as recycle, \
                patch("luvbox.deletion.publish_snapshot"):
            result = run_deletion(self.paths, rows, True, threading.Event(), lambda event: None)
        recycle.assert_not_called()
        self.assertEqual((result["recycled"], result["failed"]), (0, 1))
        self.assertEqual(victim.read_bytes(), b"synthetic outside file")

    def test_shared_metadata_view_is_rejected_before_publishing_local_records(self):
        victim = self.external_snapshot(metadata_view=True)
        source = self.paths.snapshot_dir / HISTORY
        previous = source.read_bytes()
        with self.assertRaises(MigrationError):
            prepare_storage(self.paths, create_media=False)
        self.assertFalse((self.paths.record_dir / HISTORY).exists())
        self.assertEqual(source.read_bytes(), previous)
        self.assertEqual(victim.read_bytes(), b"synthetic outside file")

    def test_shared_virtual_metadata_cannot_keep_root_authority_after_its_delete(self):
        self.paths.snapshot_dir.mkdir(parents=True)
        source = self.paths.snapshot_dir / HISTORY
        with closing(sqlite3.connect(source)) as db, db:
            db.execute("CREATE TABLE supplied_meta(key TEXT,value TEXT)")
            db.execute("INSERT INTO supplied_meta VALUES ('allowed_media_roots',?)",
                       (json.dumps([str(self.root / "not-selected")]),))
            try:
                db.execute("CREATE VIRTUAL TABLE catalog_meta USING fts5("
                           "key,value,content='supplied_meta',content_rowid='rowid')")
            except sqlite3.OperationalError as error:
                if "no such module" in str(error):
                    self.skipTest("SQLite FTS5 is unavailable")
                raise
            db.execute("INSERT INTO catalog_meta(rowid,key,value) SELECT rowid,key,value FROM supplied_meta")
        previous = source.read_bytes()
        with self.assertRaises(MigrationError):
            prepare_storage(self.paths, create_media=False)
        self.assertFalse((self.paths.record_dir / HISTORY).exists())
        self.assertEqual(source.read_bytes(), previous)

    def test_changed_shared_roots_do_not_replace_locally_approved_former_roots(self):
        former = resolve_storage(self.root / "approved-former", "alice")
        with closing(HistoryStore.for_storage(former)) as store:
            old_file = former.media_dir / "200_1.jpg"
            old_file.write_bytes(b"approved former file")
            store.record_media("flat", {"tweet_id": "200", "num": 1, "type": "photo"}, old_file)
        migrate_records(former, self.paths)
        victim = self.external_snapshot()
        written_later(self.paths.snapshot_dir / HISTORY)
        prepare_storage(self.paths, protected=True)
        with closing(HistoryStore.for_storage(self.paths, create_media=False)) as store:
            self.assertIn(former.output_dir, store.media_roots)
            self.assertNotIn(victim.parent, store.media_roots)
            self.assertTrue(store.is_media_path(old_file))
            self.assertFalse(store.is_media_path(victim))
            other = former.media_dir / "999_1.jpg"
            other.write_bytes(b"unrecorded former file")
            self.assertFalse(store.is_media_path(other))

    def test_first_open_imports_the_media_folder_records_once(self):
        self.earlier_version_records(["100", "200"])
        with closing(HistoryStore.for_storage(self.paths)) as store:
            self.assertEqual(store.summary()["files"], 2)
        self.assertEqual(count(self.paths.record_dir / ARCHIVE, "archive"), 2)
        state = json.loads((self.paths.record_dir / "snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(set(state["files"]), {HISTORY, ARCHIVE})
        # The media folder's copy is left as it was until the next save.
        self.assertEqual(count(self.paths.snapshot_dir / HISTORY), 2)

    def test_snapshot_is_complete_single_file_and_replaces_old_live_files(self):
        self.earlier_version_records(["100"])
        (self.paths.snapshot_dir / (HISTORY + "-wal")).write_bytes(b"")
        prepare_storage(self.paths)
        with closing(HistoryStore.for_storage(self.paths)) as store:
            path = self.output / "300_1.jpg"
            path.write_bytes(b"offline")
            store.record_media("flat", {"tweet_id": "300", "num": 1, "type": "photo"}, path)
        self.assertEqual(publish_snapshot(self.paths), 2)
        self.assertFalse((self.paths.snapshot_dir / (HISTORY + "-wal")).exists())
        with closing(sqlite3.connect(self.paths.snapshot_dir / HISTORY)) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM media").fetchone()[0], 2)
        self.assertFalse(list(self.paths.snapshot_dir.glob(".snapshot-*")))

    def test_changes_from_another_pc_are_merged_and_bad_copies_ignored(self):
        self.earlier_version_records(["100"])
        prepare_storage(self.paths, protected=True)
        publish_snapshot(self.paths)
        # Another PC saved 200 into the same synced folder and published it.
        with closing(sqlite3.connect(self.paths.snapshot_dir / ARCHIVE)) as db:
            db.execute("INSERT INTO archive VALUES ('twitter200_1')")
            db.commit()
        other = self.paths.snapshot_dir / HISTORY
        path = self.output / "200_1.jpg"
        path.write_bytes(b"offline")
        with closing(HistoryStore(self.paths.snapshot_dir, media_dir=self.output, output_dir=self.output)) as store:
            store.record_media("flat", {"tweet_id": "200", "num": 1, "type": "photo"}, path)
        with closing(sqlite3.connect(other)) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        written_later(self.paths.snapshot_dir / ARCHIVE, other)
        prepare_storage(self.paths)                 # a records window does not merge
        self.assertEqual(count(self.paths.record_dir / ARCHIVE, "archive"), 1)
        prepare_storage(self.paths, protected=True)  # a save does
        self.assertEqual(count(self.paths.record_dir / ARCHIVE, "archive"), 2)
        self.assertEqual(count(self.paths.record_dir / HISTORY), 2)
        # A damaged copy in the media folder never blocks this PC's records.
        (self.paths.snapshot_dir / ARCHIVE).write_bytes(b"not a database" * 100)
        prepare_storage(self.paths, protected=True)
        self.assertEqual(count(self.paths.record_dir / ARCHIVE, "archive"), 2)
        publish_snapshot(self.paths)
        self.assertEqual(count(self.paths.snapshot_dir / ARCHIVE, "archive"), 2)

    def changed_archive_snapshot(self):
        self.earlier_version_records(["100"])
        prepare_storage(self.paths, protected=True)
        publish_snapshot(self.paths)
        with closing(sqlite3.connect(self.paths.snapshot_dir / ARCHIVE)) as db:
            db.execute("INSERT INTO archive VALUES ('twitter200_1')")
            db.commit()
        written_later(self.paths.snapshot_dir / ARCHIVE)

    def test_locked_merge_does_not_mark_snapshot_consumed_and_retries(self):
        self.changed_archive_snapshot()
        state_path = self.paths.record_dir / "snapshot.json"
        state_before = json.loads(state_path.read_text(encoding="utf-8"))["files"][ARCHIVE]
        with closing(sqlite3.connect(self.paths.record_dir / ARCHIVE)) as locker:
            locker.execute("BEGIN IMMEDIATE")
            prepare_storage(self.paths, protected=True)
            state_after = json.loads(state_path.read_text(encoding="utf-8"))["files"][ARCHIVE]
            self.assertEqual(state_after, state_before)
            self.assertEqual(count(self.paths.record_dir / ARCHIVE, "archive"), 1)
            locker.rollback()
        prepare_storage(self.paths, protected=True)
        self.assertEqual(count(self.paths.record_dir / ARCHIVE, "archive"), 2)
        self.assertFalse(list(self.paths.record_dir.parent.glob(".incoming-*")))

    def test_publish_preserves_unmerged_snapshot_then_retries_after_lock(self):
        self.changed_archive_snapshot()
        shared = self.paths.snapshot_dir / ARCHIVE
        shared_before = shared.read_bytes()
        with closing(sqlite3.connect(self.paths.record_dir / ARCHIVE)) as locker:
            locker.execute("BEGIN IMMEDIATE")
            with self.assertRaises(MigrationError):
                publish_snapshot(self.paths)
            self.assertEqual(shared.read_bytes(), shared_before)
            self.assertEqual(count(shared, "archive"), 2)
            locker.rollback()
        publish_snapshot(self.paths)
        self.assertEqual(count(self.paths.record_dir / ARCHIVE, "archive"), 2)
        self.assertEqual(count(shared, "archive"), 2)
        self.assertFalse(list(self.paths.record_dir.parent.glob(".incoming-*")))

    def test_unreadable_snapshot_retries_without_being_assumed_corrupt(self):
        self.changed_archive_snapshot()
        shared = self.paths.snapshot_dir / ARCHIVE
        before = shared.read_bytes()
        with patch("luvbox.storage._backup_database", side_effect=PermissionError("sync file lock")):
            prepare_storage(self.paths, protected=True)
            with self.assertRaises(MigrationError):
                publish_snapshot(self.paths)
        self.assertEqual(shared.read_bytes(), before)
        publish_snapshot(self.paths)
        self.assertEqual(count(self.paths.record_dir / ARCHIVE, "archive"), 2)
        self.assertEqual(count(shared, "archive"), 2)

    def test_publish_merges_snapshot_that_arrived_after_storage_preparation(self):
        self.changed_archive_snapshot()
        # No further prepare call: a sync client can supply new data while a
        # save is still running. Publication must include it in both copies.
        publish_snapshot(self.paths)
        self.assertEqual(count(self.paths.record_dir / ARCHIVE, "archive"), 2)
        self.assertEqual(count(self.paths.snapshot_dir / ARCHIVE, "archive"), 2)

    def test_folder_change_carries_records_found_only_in_the_old_snapshot(self):
        self.earlier_version_records(["100"])
        destination = resolve_storage(self.root / "new", "alice")
        result = migrate_records(self.paths, destination)
        self.assertTrue(result.changed)
        self.assertEqual(count(destination.record_dir / HISTORY), 1)
        self.assertEqual(count(destination.snapshot_dir / HISTORY), 1)


class LocalRecordWorkerTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    write_cookies = test_core.CoreTests.write_cookies
    worker = test_core.CoreTests.worker

    def test_each_save_publishes_a_snapshot_into_the_media_folder(self):
        code, events = self.worker([("100", 1, "photo", "alice")])
        self.assertEqual(code, 0)
        snapshot = resolve_storage(self.output, "myaccount").snapshot_dir
        self.assertFalse(self.record_dir.is_relative_to(self.output))
        self.assertEqual(count(snapshot / HISTORY), 1)
        self.assertEqual(count(snapshot / ".luvbox-archive.sqlite3", "archive"), 1)
        self.assertFalse(list(snapshot.glob("*-wal")))
        self.assertFalse([event for event in events if event.get("level") == "warning"])


if __name__ == "__main__":
    unittest.main()
