"""Offline account-ledger relocation, unions, and interrupted publications."""

import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from xlikesaver.history import HistoryStore
from xlikesaver.protection import AccountGuard, ProtectionError
from xlikesaver.storage import (MigrationError, OutputGuard, migrate_records,
                               prepare_storage, resolve_storage)


class RelocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.guard_patch = patch("xlikesaver.protection.state_directory", return_value=self.root / "locks")
        self.guard_patch.start()
        self.addCleanup(self.guard_patch.stop)
        self.a, self.b, self.c = [resolve_storage(self.root / name, "alice") for name in ("A", "B", "C")]
        for paths in (self.a, self.b, self.c):
            paths.output_dir.mkdir()

    def archive(self, paths, ids):
        prepare_storage(paths)
        with closing(sqlite3.connect(paths.record_dir / ".xlikesaver-archive.flat.sqlite3")) as db:
            db.execute("CREATE TABLE IF NOT EXISTS archive(entry TEXT PRIMARY KEY) WITHOUT ROWID")
            db.executemany("INSERT OR IGNORE INTO archive(entry) VALUES (?)", (("twitter" + str(ident) + "_1",) for ident in ids))
            db.commit()

    def record(self, paths, ident, *, embedded=True):
        file = paths.media_dir / (str(ident) + "_1.jpg")
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(("photo-" + str(ident)).encode())
        store = HistoryStore.for_storage(paths)
        try:
            metadata = {"tweet_id": str(ident), "num": 1, "type": "photo",
                        "author": {"name": "author"}, "date": "2026-10-04T20:00:00+09:00",
                        "_xlikesaver_metadata_status": "embedded" if embedded else "pending"}
            store.record_media("flat", metadata, file)
            run = store.start_run("flat")
            store.finish_run(run, "finished", saved=1, skipped=2, visited=3, last_tweet_id=str(ident))
            row = store.media_record("flat", metadata)
        finally:
            store.close()
        self.archive(paths, [ident])
        return row

    def rows(self, paths):
        store = HistoryStore.for_storage(paths)
        try:
            return store.records(limit=1000), store.runs(limit=1000), store.media_roots
        finally:
            store.close()

    def archived(self, paths):
        with closing(sqlite3.connect(paths.record_dir / ".xlikesaver-archive.flat.sqlite3")) as db:
            return {row[0] for row in db.execute("SELECT entry FROM archive")}

    def digests(self, paths):
        return {file: hashlib.sha256(file.read_bytes()).hexdigest()
                for file in paths.record_dir.glob("*.sqlite3")}

    def assert_digests(self, values):
        self.assertEqual({path: hashlib.sha256(path.read_bytes()).hexdigest() for path in values}, values)

    def test_first_change_keeps_old_media_and_original_db_bytes_and_paths(self):
        old = self.record(self.a, 100)
        original = self.digests(self.a)
        result = migrate_records(self.a, self.b)
        self.assertTrue(result.changed)
        self.assertEqual(result.media_records, 1)
        self.assertEqual(result.source_dir, self.a.record_dir)
        self.assertEqual(result.destination_dir, self.b.record_dir)
        rows, runs, roots = self.rows(self.b)
        self.assertEqual(len(rows), 1)
        for field in ("path", "saved_at", "posted_at", "source_url", "metadata_status"):
            self.assertEqual(rows[0][field], old[field])
        self.assertEqual((runs[0]["saved"], runs[0]["skipped"], runs[0]["visited"]), (1, 2, 3))
        self.assertEqual(set(roots), {self.a.output_dir, self.b.output_dir})
        self.assertEqual(self.archived(self.b), {"twitter100_1"})
        self.assert_digests(original)
        self.assertEqual(Path(old["path"]).read_bytes(), b"photo-100")
        self.assertFalse((self.b.media_dir / "100_1.jpg").exists())

    def test_multiple_changes_and_return_merge_without_runs_id_collision_or_repeat_duplicates(self):
        self.record(self.a, 100)
        migrate_records(self.a, self.b)
        self.record(self.b, 101)
        migrate_records(self.b, self.c)
        self.record(self.c, 102)
        self.record(self.a, 103)
        migrate_records(self.c, self.a)
        rows, runs, roots = self.rows(self.a)
        self.assertEqual({row["tweet_id"] for row in rows}, {"100", "101", "102", "103"})
        self.assertEqual(len(runs), 4)
        self.assertEqual(len({row["id"] for row in runs}), 4)
        self.assertEqual(sum(row["saved"] for row in runs), 4)
        self.assertEqual(set(roots), {self.a.output_dir, self.b.output_dir, self.c.output_dir})
        self.assertEqual(len(self.archived(self.a)), 4)
        migrate_records(self.a, self.b)
        migrate_records(self.b, self.a)
        self.assertEqual(len(self.rows(self.a)[0]), 4)
        self.assertEqual(len(self.rows(self.a)[1]), 4)

    def test_media_conflict_retains_richer_existing_file_and_source_metadata(self):
        richer = self.record(self.a, 100, embedded=True)
        self.record(self.b, 100, embedded=False)
        migrate_records(self.a, self.b)
        row = self.rows(self.b)[0][0]
        self.assertEqual(row["path"], richer["path"])
        self.assertEqual(row["metadata_status"], "embedded")
        self.assertEqual(row["saved_at"], richer["saved_at"])
        self.assertEqual(len(self.archived(self.b)), 1)

    def test_destination_existing_path_wins_when_source_file_is_missing(self):
        missing = self.record(self.a, 100)
        present = self.record(self.b, 100)
        Path(missing["path"]).unlink()
        migrate_records(self.a, self.b)
        self.assertEqual(self.rows(self.b)[0][0]["path"], present["path"])

    def test_missing_old_media_preserves_known_path_when_no_replacement_exists(self):
        old = self.record(self.a, 100)
        Path(old["path"]).unlink()
        migrate_records(self.a, self.b)
        self.assertEqual(self.rows(self.b)[0][0]["path"], old["path"])
        store = HistoryStore.for_storage(self.b)
        try:
            self.assertTrue(store.is_media_path(old["path"]))
        finally:
            store.close()

    def test_same_path_is_noop_and_different_account_is_rejected(self):
        self.record(self.a, 100)
        values = self.digests(self.a)
        self.assertFalse(migrate_records(self.a, resolve_storage(self.a.output_dir, "alice", True)).changed)
        with self.assertRaisesRegex(MigrationError, "다른 계정"):
            migrate_records(self.a, resolve_storage(self.b.output_dir, "bob"))
        self.assert_digests(values)

    def test_empty_existing_source_can_change_without_copying_untracked_media_or_secrets(self):
        (self.a.output_dir / "unknown.jpg").write_bytes(b"unknown photo")
        (self.a.output_dir / "cookies.txt").write_text("secret", encoding="utf-8")
        result = migrate_records(self.a, self.b)
        self.assertEqual(result.media_records, 0)
        self.assertEqual(len(self.rows(self.b)[0]), 0)
        self.assertEqual({path.name for path in self.b.record_dir.iterdir()}, {".xlikesaver-history.sqlite3"})
        self.assertFalse((self.b.output_dir / "cookies.txt").exists())

    def test_nonexistent_source_does_not_silently_reset_records(self):
        absent = resolve_storage(self.root / "offline", "alice")
        with self.assertRaisesRegex(MigrationError, "이전 저장 폴더"):
            migrate_records(absent, self.b)
        self.assertFalse(self.b.record_dir.exists())

    def test_corrupt_source_or_destination_keeps_both_originals_and_removes_stage(self):
        self.record(self.a, 100)
        self.record(self.b, 101)
        for paths in (self.a, self.b):
            with self.subTest(root=paths.output_dir):
                database = paths.record_dir / ".xlikesaver-archive.flat.sqlite3"
                original = database.read_bytes()
                database.write_bytes(b"not a sqlite database")
                values = self.digests(self.a) | self.digests(self.b)
                with self.assertRaises(MigrationError):
                    migrate_records(self.a, self.b)
                self.assert_digests(values)
                self.assertFalse(list(self.b.record_dir.parent.glob(".relocate-*")))
                database.write_bytes(original)

    def test_merge_failure_cannot_publish_half_of_the_database_set(self):
        self.record(self.a, 100)
        self.record(self.b, 101)
        values = self.digests(self.a) | self.digests(self.b)
        with patch("xlikesaver.storage._merge_database", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(MigrationError):
                migrate_records(self.a, self.b)
        self.assert_digests(values)
        self.assertEqual(self.archived(self.b), {"twitter101_1"})
        self.assertFalse(list(self.b.record_dir.parent.glob(".relocate-*")))

    def test_publication_rename_failure_restores_previous_account_directory(self):
        self.record(self.a, 100)
        self.record(self.b, 101)
        values = self.digests(self.a) | self.digests(self.b)
        rename = Path.rename

        def blocked(path, target):
            if path.name.startswith(".relocate-"):
                raise PermissionError("locked directory")
            return rename(path, target)

        with patch.object(Path, "rename", autospec=True, side_effect=blocked):
            with self.assertRaises(MigrationError):
                migrate_records(self.a, self.b)
        self.assert_digests(values)
        self.assertFalse(list(self.b.record_dir.parent.glob(".*relocation.json")))

    def test_crash_between_renames_recovers_destination_before_next_history_open(self):
        self.record(self.a, 100)
        self.record(self.b, 101)
        values = self.digests(self.b)
        rename = Path.rename

        def interrupted(path, target):
            if path.name.startswith(".relocate-"):
                raise KeyboardInterrupt("simulated process exit")
            return rename(path, target)

        with patch.object(Path, "rename", autospec=True, side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                migrate_records(self.a, self.b)
        self.assertFalse(self.b.record_dir.exists())
        prepare_storage(self.b)
        self.assert_digests(values)
        self.assertEqual(self.archived(self.b), {"twitter101_1"})
        self.assertFalse(list(self.b.record_dir.parent.glob(".relocate-*")))
        self.assertFalse(list(self.b.record_dir.parent.glob(".previous-*")))

    def test_crash_after_publication_keeps_complete_merged_destination(self):
        self.record(self.a, 100)
        self.record(self.b, 101)
        from xlikesaver import storage
        recovery = storage._recover_relocation

        def interrupted(paths, **kwargs):
            if paths == self.b and storage._journal_path(paths).exists() and paths.record_dir.exists():
                raise KeyboardInterrupt("simulated process exit after publish")
            return recovery(paths, **kwargs)

        with patch("xlikesaver.storage._recover_relocation", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                migrate_records(self.a, self.b)
        prepare_storage(self.b)
        self.assertEqual(self.archived(self.b), {"twitter100_1", "twitter101_1"})
        self.assertEqual(len(self.rows(self.b)[0]), 2)

    def test_active_relocation_journal_is_not_cleaned_by_other_history_process(self):
        self.record(self.a, 100)
        self.record(self.b, 101)
        guard = OutputGuard(self.b.output_dir)
        guard.acquire()
        try:
            journal = self.b.record_dir.parent / ".alice.relocation.json"
            journal.write_text("{}", encoding="utf-8")
            with self.assertRaises(ProtectionError):
                prepare_storage(self.b)
            self.assertEqual(journal.read_text(encoding="utf-8"), "{}")
            journal.unlink()
        finally:
            guard.close()

    def test_worker_with_output_lock_can_recover_interrupted_publication(self):
        self.record(self.a, 100)
        self.record(self.b, 101)
        rename = Path.rename

        def interrupted(path, target):
            if path.name.startswith(".relocate-"):
                raise KeyboardInterrupt("simulated process exit")
            return rename(path, target)

        with patch.object(Path, "rename", autospec=True, side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                migrate_records(self.a, self.b)
        guard = OutputGuard(self.b.output_dir)
        guard.acquire()
        try:
            prepare_storage(self.b, protected=True)
            self.assertEqual(self.archived(self.b), {"twitter101_1"})
        finally:
            guard.close()

    def test_account_and_output_locks_reject_parallel_writers_but_cooldown_allows_local_move(self):
        self.record(self.a, 100)
        account = AccountGuard("alice", sleep=lambda seconds: None)
        account.acquire(respect_cooldown=False)
        try:
            with self.assertRaises(ProtectionError):
                migrate_records(self.a, self.b)
        finally:
            account.close()
        output = OutputGuard(self.b.output_dir)
        output.acquire()
        try:
            with self.assertRaises(ProtectionError):
                migrate_records(self.a, self.b)
        finally:
            output.close()
        account.state_path.write_text(json.dumps({"until": time.time() + 3600}), encoding="utf-8")
        self.assertTrue(migrate_records(self.a, self.b).changed)

    def test_linked_record_directory_or_database_is_rejected_without_reading_it(self):
        self.record(self.a, 100)
        original = Path.is_symlink
        for target in (self.b.record_dir, self.a.record_dir / ".xlikesaver-history.sqlite3"):
            with self.subTest(target=target), patch.object(Path, "is_symlink", autospec=True,
                    side_effect=lambda path: path == target or original(path)):
                with self.assertRaises((ValueError, MigrationError)):
                    migrate_records(self.a, self.b)

    def test_prior_authorized_root_validation_rejects_relative_or_redirected_values(self):
        self.record(self.a, 100)
        with closing(sqlite3.connect(self.a.record_dir / ".xlikesaver-history.sqlite3")) as db:
            db.execute("INSERT INTO catalog_meta(key,value) VALUES ('allowed_media_roots',?)", ('["../outside"]',))
            db.commit()
        values = self.digests(self.a)
        with self.assertRaises(MigrationError):
            migrate_records(self.a, self.b)
        self.assert_digests(values)

    def test_record_root_overlap_is_rejected_but_parent_child_media_roots_work(self):
        self.record(self.a, 100)
        with self.assertRaisesRegex(MigrationError, "기록 폴더 안"):
            migrate_records(self.a, resolve_storage(self.a.record_dir / "media", "alice"))
        nested = resolve_storage(self.a.output_dir / "nested", "alice")
        self.assertTrue(migrate_records(self.a, nested).changed)
        self.assertEqual(self.rows(nested)[0][0]["tweet_id"], "100")

    def test_archive_only_legacy_source_gets_old_file_catalog_without_original_changes(self):
        legacy = self.a.legacy_dir
        legacy.mkdir()
        file = legacy / "100_1.jpg"
        file.write_bytes(b"old saved photo")
        archive = legacy / ".xlikesaver-archive.flat.sqlite3"
        with closing(sqlite3.connect(archive)) as db:
            db.execute("CREATE TABLE archive(entry TEXT PRIMARY KEY)")
            db.execute("INSERT INTO archive(entry) VALUES ('twitter100_1')")
            db.commit()
        original = hashlib.sha256(archive.read_bytes()).hexdigest()
        result = migrate_records(self.a, self.b)
        self.assertEqual(result.media_records, 1)
        self.assertEqual(self.rows(self.b)[0][0]["path"], str(file))
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), original)
        self.assertEqual(file.read_bytes(), b"old saved photo")
        self.assertFalse(list(self.b.record_dir.parent.glob(".catalog-*")))

    def test_committed_source_wal_is_included_in_readonly_backup(self):
        self.record(self.a, 100)
        database = self.a.record_dir / ".xlikesaver-archive.flat.sqlite3"
        with closing(sqlite3.connect(database)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO archive(entry) VALUES ('twitter101_1')")
            db.commit()
            self.assertTrue(Path(str(database) + "-wal").exists())
            migrate_records(self.a, self.b)
            self.assertEqual(self.archived(self.b), {"twitter100_1", "twitter101_1"})

    def test_sync_snapshots_keep_latest_complete_baseline_per_mode(self):
        for paths, timestamp, ids in ((self.a, "2026-10-05T01:00:00+09:00", [100, 101]),
                                      (self.b, "2026-10-04T23:00:00+09:00", [99])):
            self.record(paths, ids[0])
            with closing(sqlite3.connect(paths.record_dir / ".xlikesaver-sync.sqlite3")) as db:
                db.execute("CREATE TABLE likes(folder_mode TEXT,tweet_id TEXT,PRIMARY KEY(folder_mode,tweet_id))")
                db.execute("CREATE TABLE sync_state(folder_mode TEXT PRIMARY KEY,last_sync_at TEXT NOT NULL)")
                db.executemany("INSERT INTO likes VALUES ('flat',?)", ((str(ident),) for ident in ids))
                db.execute("INSERT INTO sync_state VALUES ('flat',?)", (timestamp,))
                db.commit()
        migrate_records(self.a, self.b)
        with closing(sqlite3.connect(self.b.record_dir / ".xlikesaver-sync.sqlite3")) as db:
            self.assertEqual({row[0] for row in db.execute("SELECT tweet_id FROM likes")}, {"100", "101"})
            self.assertEqual(db.execute("SELECT last_sync_at FROM sync_state").fetchone()[0], "2026-10-05T01:00:00+09:00")

    def test_destination_custom_files_are_preserved_and_not_replaced(self):
        self.record(self.a, 100)
        prepare_storage(self.b)
        custom = self.b.record_dir / "notes.txt"
        custom.write_text("keep me", encoding="utf-8")
        with self.assertRaisesRegex(MigrationError, "다른 파일"):
            migrate_records(self.a, self.b)
        self.assertEqual(custom.read_text(encoding="utf-8"), "keep me")

    def test_every_folder_mode_archive_is_merged_independently(self):
        self.record(self.a, 100)
        self.record(self.b, 101)
        for paths, ident in ((self.a, 100), (self.b, 101)):
            for name in (".xlikesaver-archive.sqlite3", ".xlikesaver-archive.date.sqlite3"):
                with closing(sqlite3.connect(paths.record_dir / name)) as db:
                    db.execute("CREATE TABLE archive(entry TEXT PRIMARY KEY)")
                    db.execute("INSERT INTO archive VALUES (?)", ("twitter" + str(ident) + "_video_2",))
                    db.commit()
        migrate_records(self.a, self.b)
        for name in (".xlikesaver-archive.sqlite3", ".xlikesaver-archive.date.sqlite3"):
            with closing(sqlite3.connect(self.b.record_dir / name)) as db:
                self.assertEqual({row[0] for row in db.execute("SELECT entry FROM archive")},
                                 {"twitter100_video_2", "twitter101_video_2"})

    def test_source_duplicate_run_timestamps_merge_progress_without_id_collision(self):
        self.record(self.a, 100)
        self.record(self.b, 101)
        with closing(sqlite3.connect(self.a.record_dir / ".xlikesaver-history.sqlite3")) as db:
            row = db.execute("SELECT folder_mode,started_at FROM runs").fetchone()
            db.execute("INSERT INTO runs(folder_mode,started_at,status,saved,skipped,visited) VALUES (?,?,'running',2,4,6)", row)
            db.commit()
        migrate_records(self.a, self.b)
        runs = self.rows(self.b)[1]
        merged = next(item for item in runs if item["started_at"] == row[1])
        self.assertEqual((merged["saved"], merged["skipped"], merged["visited"]), (2, 4, 6))
        self.assertEqual(len(runs), 2)


if __name__ == "__main__":
    unittest.main()
