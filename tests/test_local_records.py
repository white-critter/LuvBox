"""Live records on this PC; complete snapshots travel with the media folder."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tests import test_core
from xlikesaver.history import HistoryStore
from xlikesaver.storage import (MigrationError, migrate_records, prepare_storage,
                               publish_snapshot, resolve_storage)

HISTORY = ".xlikesaver-history.sqlite3"
ARCHIVE = ".xlikesaver-archive.flat.sqlite3"


def count(path, table="media"):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class LocalRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        protection = patch("xlikesaver.protection.state_directory", return_value=self.root / "pc" / "request-protection")
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
        with patch("xlikesaver.storage._backup_database", side_effect=PermissionError("sync file lock")):
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
        self.assertEqual(count(snapshot / ".xlikesaver-archive.sqlite3", "archive"), 1)
        self.assertFalse(list(snapshot.glob("*-wal")))
        self.assertFalse([event for event in events if event.get("level") == "warning"])


if __name__ == "__main__":
    unittest.main()
