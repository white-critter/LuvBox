"""LuvBox imports old records without changing the original databases."""

import hashlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from luvbox.history import HistoryError, HistoryStore
from luvbox.recovery import latest_backup
from luvbox.storage import (MigrationError, other_account_records, prepare_storage,
                            merge_account_records, publish_snapshot, resolve_storage,
                            saved_media_missing)


class LegacyBrandStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = self.root / "LuvBox"
        isolated = patch("luvbox.protection.state_directory", return_value=self.app / "request-protection")
        isolated.start()
        self.addCleanup(isolated.stop)
        self.output = self.root / "media"
        self.output.mkdir()
        self.paths = resolve_storage(self.output, "alice")

    def old_history(self, directory, tweet="100"):
        media = self.output / (tweet + "_1.jpg")
        media.write_bytes(b"legacy fixture")
        with closing(HistoryStore(directory, media_dir=self.output, output_dir=self.output)) as store:
            store.record_media("flat", {"tweet_id": tweet, "num": 1, "type": "photo"}, media)
            row = store.records()[0]
        current = directory / ".luvbox-history.sqlite3"
        old = directory / ".xlikesaver-history.sqlite3"
        current.rename(old)
        return old, row

    def test_direct_history_open_imports_old_local_name_and_preserves_source(self):
        old, expected = self.old_history(self.paths.record_dir)
        digest = hashlib.sha256(old.read_bytes()).hexdigest()
        with closing(HistoryStore.for_storage(self.paths)) as store:
            self.assertEqual(store.records()[0], expected)
            self.assertEqual(store.path.name, ".luvbox-history.sqlite3")
        self.assertEqual(hashlib.sha256(old.read_bytes()).hexdigest(), digest)

    def test_missing_media_guard_reads_old_catalog_before_any_migration(self):
        old, row = self.old_history(self.paths.record_dir)
        Path(row["path"]).unlink()
        self.output.rmdir()
        digest = hashlib.sha256(old.read_bytes()).hexdigest()
        with patch.object(Path, "mkdir", side_effect=AssertionError("missing-media check must not create folders")), \
                patch("luvbox.storage.migrate_legacy_databases",
                      side_effect=AssertionError("missing-media check must stay read-only")):
            self.assertTrue(saved_media_missing(self.paths))
        self.assertFalse(self.output.exists())
        self.assertFalse((self.paths.record_dir / ".luvbox-history.sqlite3").exists())
        self.assertEqual(hashlib.sha256(old.read_bytes()).hexdigest(), digest)

    def test_missing_media_guard_does_not_replace_corrupt_current_catalog_with_old(self):
        _, row = self.old_history(self.paths.record_dir)
        Path(row["path"]).unlink()
        self.output.rmdir()
        current = self.paths.record_dir / ".luvbox-history.sqlite3"
        current.write_bytes(b"damaged current catalog")
        self.assertFalse(saved_media_missing(self.paths))
        self.assertEqual(current.read_bytes(), b"damaged current catalog")

    def test_old_snapshot_folder_and_names_import_then_publish_new_names(self):
        directory = self.output / ".xlikesaver" / "alice"
        old, expected = self.old_history(directory)
        digest = hashlib.sha256(old.read_bytes()).hexdigest()
        with closing(sqlite3.connect(directory / ".xlikesaver-archive.flat.sqlite3")) as db:
            db.execute("CREATE TABLE archive(entry TEXT PRIMARY KEY)")
            db.execute("INSERT INTO archive VALUES ('twitter100_1')")
            db.commit()
        with closing(HistoryStore.for_storage(self.paths, protected=True)) as store:
            self.assertEqual(store.records()[0], expected)
            self.assertFalse(store.is_media_path(old))
        self.assertEqual(publish_snapshot(self.paths), 2)
        self.assertTrue((self.paths.snapshot_dir / ".luvbox-history.sqlite3").is_file())
        self.assertTrue((self.paths.snapshot_dir / ".luvbox-archive.flat.sqlite3").is_file())
        self.assertEqual(hashlib.sha256(old.read_bytes()).hexdigest(), digest)

    def test_archive_migration_reads_committed_wal(self):
        self.paths.record_dir.mkdir(parents=True)
        old = self.paths.record_dir / ".xlikesaver-archive.flat.sqlite3"
        with closing(sqlite3.connect(old)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE archive(entry TEXT PRIMARY KEY)")
            db.execute("INSERT INTO archive VALUES ('twitter200_1')")
            db.commit()
            prepare_storage(self.paths)
        with closing(sqlite3.connect(self.paths.record_dir / ".luvbox-archive.flat.sqlite3")) as copied:
            self.assertEqual(copied.execute("SELECT entry FROM archive").fetchone()[0], "twitter200_1")
        self.assertTrue(old.is_file())

    def test_corrupt_old_database_never_creates_an_empty_current_database(self):
        self.paths.record_dir.mkdir(parents=True)
        old = self.paths.record_dir / ".xlikesaver-history.sqlite3"
        old.write_bytes(b"damaged old database")
        with self.assertRaises((HistoryError, MigrationError, sqlite3.Error)):
            HistoryStore.for_storage(self.paths)
        self.assertEqual(old.read_bytes(), b"damaged old database")
        self.assertFalse((self.paths.record_dir / ".luvbox-history.sqlite3").exists())

    def test_corrupt_unused_sync_state_is_left_alone(self):
        self.paths.record_dir.mkdir(parents=True)
        old = self.paths.record_dir / ".xlikesaver-sync.sqlite3"
        old.write_bytes(b"obsolete state")
        with closing(HistoryStore.for_storage(self.paths)) as store:
            self.assertEqual(store.records(), [])
        self.assertEqual(old.read_bytes(), b"obsolete state")
        self.assertFalse((self.paths.record_dir / ".luvbox-sync.sqlite3").exists())

    def test_existing_corrupt_current_catalog_is_not_reset_from_older_input(self):
        old, _ = self.old_history(self.paths.record_dir)
        current = self.paths.record_dir / ".luvbox-history.sqlite3"
        current.write_bytes(b"current damaged database")
        with self.assertRaises(HistoryError):
            HistoryStore.for_storage(self.paths)
        self.assertEqual(current.read_bytes(), b"current damaged database")
        self.assertTrue(old.is_file())

    def test_locked_old_database_aborts_without_an_empty_replacement(self):
        self.paths.record_dir.mkdir(parents=True)
        old = self.paths.record_dir / ".xlikesaver-archive.flat.sqlite3"
        with closing(sqlite3.connect(old)) as db:
            db.execute("CREATE TABLE archive(entry TEXT PRIMARY KEY)")
            db.execute("INSERT INTO archive VALUES ('twitter100_1')")
            db.commit()
            db.execute("BEGIN EXCLUSIVE")
            with self.assertRaises(MigrationError):
                prepare_storage(self.paths)
            db.rollback()
        self.assertFalse((self.paths.record_dir / ".luvbox-archive.flat.sqlite3").exists())
        self.assertFalse((self.paths.record_dir / ".luvbox-history.sqlite3").exists())

    def test_linked_old_snapshot_root_is_rejected(self):
        old = self.output / ".xlikesaver"
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", autospec=True,
                          side_effect=lambda path: path == old or original(path)):
            with self.assertRaises(ValueError):
                resolve_storage(self.output, "alice")

    def test_old_snapshot_account_is_available_for_account_merge(self):
        directory = self.output / ".xlikesaver" / "bob"
        old, expected = self.old_history(directory)
        self.assertEqual(other_account_records(self.paths), [{"account": "bob", "records": 1}])
        with closing(HistoryStore.for_storage(self.paths)):
            pass
        result = merge_account_records(self.paths, "bob")
        self.assertEqual(result["added"], 1)
        with closing(HistoryStore.for_storage(self.paths)) as store:
            self.assertEqual(store.records()[0]["saved_at"], expected["saved_at"])
        self.assertTrue(old.is_file())

    def test_backup_lookup_keeps_the_scope_hash_from_the_old_app_root(self):
        previous = self.app.with_name("XlikeSaver") / self.paths.record_dir.relative_to(self.app)
        digest = hashlib.sha256(os.path.normcase(str(previous)).encode("utf-8")).hexdigest()[:16]
        backup = self.app / "backups" / "catalog" / "alice" / ("catalog-" + digest) / "history-20261001.sqlite3"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"backup fixture")
        self.assertEqual(latest_backup("alice", self.paths.record_dir), backup)
