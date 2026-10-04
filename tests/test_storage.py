"""Offline checks for shared media, isolated ledgers, and safe upgrades."""

import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests import test_core
from xlikesaver.history import HistoryStore
from xlikesaver.protection import ProtectionError
from xlikesaver.storage import OutputGuard, prepare_storage, resolve_storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_default_flat_root_and_account_option_use_one_record_location(self):
        direct = resolve_storage(self.root, "@Alice")
        split = resolve_storage(self.root, "Alice", True)
        self.assertEqual(direct.media_dir, self.root)
        self.assertEqual(split.media_dir, self.root / "alice")
        self.assertEqual(direct.record_dir, self.root / ".xlikesaver" / "alice")
        self.assertEqual(direct.record_dir, split.record_dir)
        self.assertFalse(direct.record_dir.exists())
        self.assertEqual(resolve_storage(self.root, "CON", True).media_dir.name, "_con")

    def test_migration_is_sqlite_backup_and_never_changes_or_replaces_originals(self):
        paths = resolve_storage(self.root, "alice")
        paths.legacy_dir.mkdir()
        source = paths.legacy_dir / ".xlikesaver-archive.flat.sqlite3"
        with closing(sqlite3.connect(source)) as db:
            db.execute("CREATE TABLE archive(entry TEXT PRIMARY KEY)")
            db.execute("INSERT INTO archive VALUES ('twitter100_1')")
            db.commit()
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        with patch("xlikesaver.storage.os.link", side_effect=AssertionError("Windows does not require hard links")):
            prepare_storage(paths)
        copied = paths.record_dir / source.name
        with closing(sqlite3.connect(copied)) as db:
            self.assertEqual(db.execute("SELECT entry FROM archive").fetchone()[0], "twitter100_1")
            db.execute("INSERT INTO archive VALUES ('twitter101_1')")
            db.commit()
        prepare_storage(paths)
        with closing(sqlite3.connect(copied)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM archive").fetchone()[0], 2)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)

    def test_nested_account_ledger_preserves_media_path_dates_sources_and_original_database(self):
        paths = resolve_storage(self.root, "alice")
        nested = paths.legacy_dir / ".xlikesaver" / "alice"
        media = paths.legacy_dir / "100_1.jpg"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"user-media-unchanged")
        store = HistoryStore(nested, media_dir=paths.legacy_dir, output_dir=self.root)
        metadata = {"tweet_id": "100", "num": 1, "type": "photo",
                    "_xlikesaver_metadata_status": "embedded", "date": "2026-10-04T20:00:00+09:00"}
        store.record_media("flat", metadata, media)
        old_row = store.records()[0]
        store.close()
        source = nested / ".xlikesaver-history.sqlite3"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        with closing(sqlite3.connect(nested / ".xlikesaver-archive.flat.sqlite3")) as db:
            db.execute("CREATE TABLE archive(entry TEXT PRIMARY KEY)")
            db.execute("INSERT INTO archive VALUES ('twitter100_1')")
            db.commit()
        migrated = HistoryStore.for_storage(paths)
        try:
            migrated.import_legacy()
            row = migrated.records()[0]
            for field in ("path", "saved_at", "posted_at", "source_url", "metadata_status"):
                self.assertEqual(row[field], old_row[field])
            self.assertEqual(migrated.summary()["files"], 1)
        finally:
            migrated.close()
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
        self.assertEqual(media.read_bytes(), b"user-media-unchanged")

    def test_flat_legacy_database_has_priority_over_nested_and_existing_destination(self):
        paths = resolve_storage(self.root, "alice")
        nested = paths.legacy_dir / ".xlikesaver" / "alice"
        nested.mkdir(parents=True)
        filename = ".xlikesaver-archive.flat.sqlite3"
        for directory, ident in ((nested, "nested"), (paths.legacy_dir, "flat")):
            with closing(sqlite3.connect(directory / filename)) as db:
                db.execute("CREATE TABLE archive(entry TEXT PRIMARY KEY)")
                db.execute("INSERT INTO archive VALUES (?)", (ident,))
                db.commit()
        prepare_storage(paths)
        destination = paths.record_dir / filename
        with closing(sqlite3.connect(destination)) as db:
            self.assertEqual(db.execute("SELECT entry FROM archive").fetchone()[0], "flat")
            db.execute("DELETE FROM archive")
            db.execute("INSERT INTO archive VALUES ('new-destination')")
            db.commit()
        prepare_storage(paths)
        with closing(sqlite3.connect(destination)) as db:
            self.assertEqual(db.execute("SELECT entry FROM archive").fetchone()[0], "new-destination")

    def test_nested_legacy_parent_link_is_ignored_without_opening_database(self):
        paths = resolve_storage(self.root, "alice")
        nested = paths.legacy_dir / ".xlikesaver" / "alice"
        nested.mkdir(parents=True)
        source = nested / ".xlikesaver-archive.flat.sqlite3"
        with closing(sqlite3.connect(source)) as db:
            db.execute("CREATE TABLE archive(entry TEXT PRIMARY KEY)")
            db.commit()
        linked_parent = paths.legacy_dir / ".xlikesaver"
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", autospec=True,
                          side_effect=lambda path: path == linked_parent or original(path)), \
                patch("xlikesaver.storage.sqlite3.connect", side_effect=AssertionError("linked legacy DB must not be read")):
            prepare_storage(paths)
        self.assertFalse((paths.record_dir / source.name).exists())

    def test_shared_output_import_accepts_only_account_archive_or_catalog_keys(self):
        alice, bob = resolve_storage(self.root, "alice"), resolve_storage(self.root, "bob")
        prepare_storage(alice)
        with closing(sqlite3.connect(alice.record_dir / ".xlikesaver-archive.flat.sqlite3")) as db:
            db.execute("CREATE TABLE archive(entry TEXT PRIMARY KEY)")
            db.execute("INSERT INTO archive VALUES ('twitter100_1')")
            db.commit()
        (self.root / "100_1.jpg").write_bytes(b"alice-file")
        (self.root / "101_1.jpg").write_bytes(b"unrelated-bob-file")
        for paths, expected in ((alice, ["100"]), (bob, [])):
            store = HistoryStore.for_storage(paths)
            try:
                store.import_legacy()
                self.assertEqual([row["tweet_id"] for row in store.records()], expected)
                self.assertFalse(store.is_media_path(paths.record_dir / "100_1.jpg"))
            finally:
                store.close()

    def test_linked_record_database_is_rejected_before_opening_for_write(self):
        paths = resolve_storage(self.root, "alice")
        linked = paths.record_dir / ".xlikesaver-history.sqlite3"
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", autospec=True,
                          side_effect=lambda path: path == linked or original(path)):
            with self.assertRaisesRegex(ValueError, "기록 파일"):
                prepare_storage(paths)
        self.assertFalse(linked.exists())

    def test_output_lock_excludes_different_accounts_and_releases_on_close(self):
        with patch("xlikesaver.protection.state_directory", return_value=self.root / "locks"):
            first, second = OutputGuard(self.root), OutputGuard(self.root)
            first.acquire()
            try:
                with self.assertRaisesRegex(ProtectionError, "다른 작업"):
                    second.acquire()
            finally:
                first.close()
            second.acquire()
            second.close()


class StorageWorkerTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    write_cookies = test_core.CoreTests.write_cookies
    worker = test_core.CoreTests.worker

    def direct(self, **changes):
        return replace(self.options, separate_accounts=False, **changes)

    def catalog(self, account="myaccount", separate_accounts=False):
        store = HistoryStore.for_storage(resolve_storage(self.output, account, separate_accounts))
        try:
            return store.records(), store.runs()
        finally:
            store.close()

    def test_flat_default_places_media_directly_in_selected_folder(self):
        options = self.direct(folder_mode="flat")
        code, events = self.worker([("100", 1, "photo", "alice")], options=options)
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertTrue((self.output / "100_1.jpg").is_file())
        self.assertFalse((self.output / "myaccount").exists())
        self.assertTrue((self.record_dir / ".xlikesaver-history.sqlite3").is_file())
        self.assertFalse((self.output / ".xlikesaver-history.sqlite3").exists())

    def test_author_and_saving_day_are_direct_children_without_account_option(self):
        rows = [("100", 1, "photo", "alice")]
        code, _ = self.worker(rows, options=self.direct(folder_mode="author"))
        self.assertEqual(code, 0)
        self.assertTrue((self.output / "alice" / "100_1.jpg").is_file())
        from datetime import date
        with patch("xlikesaver.core._save_day", return_value=date(2026, 10, 4)):
            code, _ = self.worker(rows, options=self.direct(folder_mode="date"))
        self.assertEqual(code, 0)
        self.assertTrue((self.output / "2026-10-04" / "100_1.jpg").is_file())
        self.assertFalse((self.output / "myaccount").exists())

    def test_toggling_account_folder_keeps_old_files_and_skip_paths_for_every_layout(self):
        for mode in ("flat", "author", "date"):
            with self.subTest(mode=mode):
                options = self.direct(folder_mode=mode)
                ident = {"flat": "100", "author": "101", "date": "102"}[mode]
                rows = [(ident, 1, "photo", "alice")]
                code, events = self.worker(rows, options=options)
                self.assertEqual(code, 0)
                original = next(row for row in events if row["type"] == "saved")["path"]
                code, events = self.worker(rows + [(ident + "0", 1, "photo", "alice")],
                                           options=replace(options, separate_accounts=True))
                self.assertEqual(code, 0)
                self.assertEqual((events[-1]["saved"], events[-1]["skipped"]), (1, 1))
                self.assertEqual(next(row for row in events if row["type"] == "skipped")["path"], original)
                saved = next(row for row in events if row["type"] == "saved")["path"]
                self.assertTrue(Path(saved).is_relative_to(self.output / "myaccount"))
                self.assertTrue(Path(original).is_file())

    def test_legacy_account_files_and_archive_are_preserved_on_first_direct_save(self):
        from xlikesaver.history import HistoryStore
        legacy = self.output / "myaccount"
        legacy.mkdir(parents=True)
        old_file = legacy / "100_1.jpg"
        old_file.write_bytes(b"previous-user-photo")
        old_archive = legacy / ".xlikesaver-archive.flat.sqlite3"
        with closing(sqlite3.connect(old_archive)) as db:
            db.execute("CREATE TABLE archive(entry TEXT PRIMARY KEY) WITHOUT ROWID")
            db.execute("INSERT INTO archive VALUES ('twitter100_1')")
            db.commit()
        old_store = HistoryStore(legacy)
        metadata = {"tweet_id": "100", "num": 1, "type": "photo", "_xlikesaver_metadata_status": "embedded"}
        old_store.record_media("flat", metadata, old_file)
        old_row = old_store.records()[0]
        old_store.close()
        original_databases = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in (old_archive, legacy / ".xlikesaver-history.sqlite3")}
        code, events = self.worker([("100", 1, "photo", "alice"), ("101", 1, "photo", "alice")],
                                   options=self.direct(folder_mode="flat"))
        self.assertEqual(code, 0)
        self.assertEqual((events[-1]["saved"], events[-1]["skipped"]), (1, 1))
        self.assertEqual(next(row for row in events if row["type"] == "skipped")["path"], str(old_file))
        self.assertEqual(old_file.read_bytes(), b"previous-user-photo")
        self.assertTrue((self.output / "101_1.jpg").is_file())
        records, _ = self.catalog()
        migrated = next(row for row in records if row["tweet_id"] == "100")
        self.assertEqual(migrated["saved_at"], old_row["saved_at"])
        self.assertEqual(migrated["metadata_status"], "embedded")
        for path, digest in original_databases.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_accounts_sharing_root_keep_catalogs_and_archives_separate(self):
        options = self.direct(folder_mode="flat")
        self.worker([("100", 1, "photo", "alice")], options=options)
        code, events = self.worker([("101", 1, "photo", "bob")], options=replace(options, account="otheraccount"))
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertEqual([row["tweet_id"] for row in self.catalog()[0]], ["100"])
        self.assertEqual([row["tweet_id"] for row in self.catalog("otheraccount")[0]], ["101"])
        code, events = self.worker([("100", 1, "photo", "alice")], options=replace(options, account="otheraccount"))
        self.assertEqual(code, 0)
        self.assertEqual((events[-1]["saved"], events[-1]["skipped"]), (0, 1))
        self.assertEqual({row["tweet_id"] for row in self.catalog("otheraccount")[0]}, {"100", "101"})
        self.assertEqual(len(test_core.FakeHttpDownloader.downloaded), 2)

    def test_author_matching_account_name_is_not_imported_twice_as_flat(self):
        code, _ = self.worker([("100", 1, "photo", "myaccount")], options=self.direct(folder_mode="author"))
        self.assertEqual(code, 0)
        store = HistoryStore.for_storage(resolve_storage(self.output, "myaccount"))
        try:
            store.import_legacy(refresh=True)
            self.assertEqual(store.summary()["files"], 1)
            self.assertEqual(store.records()[0]["folder_mode"], "author")
        finally:
            store.close()

    def test_output_lock_blocks_other_account_before_media_or_login_requests(self):
        guard = OutputGuard(self.output)
        guard.acquire()
        try:
            with patch("xlikesaver.login.load_session_cookies") as login:
                code, events = self.worker(options=self.direct(auth_mode="managed", account="otheraccount"))
            login.assert_not_called()
            self.assertNotEqual(code, 0)
            self.assertIn("다른 작업", events[-1]["message"])
            self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
        finally:
            guard.close()

    def test_redirected_author_folder_cannot_download_or_write_outside_output(self):
        target = self.output / "alice" / "100_1.jpg"
        outside = self.base / "outside" / target.name
        original = Path.resolve
        with patch.object(Path, "resolve", autospec=True,
                          side_effect=lambda path, *args, **kwargs: outside if path == target else original(path, *args, **kwargs)):
            code, events = self.worker([("100", 1, "photo", "alice")], options=self.direct(folder_mode="author"))
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 0)
        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
        self.assertFalse(outside.exists())
