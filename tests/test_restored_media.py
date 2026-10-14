"""Restored catalog files in former output roots, with no native recycling."""

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from luvbox.history import HistoryStore
from luvbox.storage import migrate_records, resolve_storage


class RestoredMediaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        state = patch("luvbox.protection.state_directory", return_value=self.root / "app" / "protection")
        state.start()
        self.addCleanup(state.stop)
        self.old = resolve_storage(self.root / "old", "alice")
        self.new = resolve_storage(self.root / "new", "alice")
        self.old.output_dir.mkdir()
        self.new.output_dir.mkdir()
        self.original = self.old.media_dir / "100_1.jpg"
        self.original.write_bytes(b"offline restored photo")
        metadata = {"tweet_id": "100", "num": 1, "type": "photo", "author": {"name": "alice", "id": "42"},
                    "date": "2026-10-05T12:00:00+09:00", "_luvbox_metadata_status": "embedded",
                    "_luvbox_author_metadata_status": "embedded"}
        with closing(HistoryStore.for_storage(self.old)) as store:
            for mode in ("flat", "author"):
                store.record_media(mode, metadata, self.original)
            store.record_tweets([("100", "preserved tweet", ["tag"])])
        migrate_records(self.old, self.new)
        self.store = HistoryStore.for_storage(self.new)
        self.addCleanup(self.store.close)
        self.before_by_mode = {row["folder_mode"]: row for row in self.store.records()}
        self.before = self.store.records()[0]
        info = self.original.stat()
        ident = self.store.begin_deletion("offline-review", self.before, True,
                                          (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
        self.parked = self.root / "simulated-recycled-file.jpg"
        self.original.rename(self.parked)
        self.store.finish_deletion(ident, "recycled", "offline simulated receipt", str(self.parked))

    def test_restore_in_former_root_reconnects_all_layouts_and_preserves_metadata(self):
        self.parked.rename(self.original)
        self.store.import_legacy(refresh=True)
        rows = self.store.records(include_deleted=False)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["path"] == str(self.original) for row in rows))
        for row in rows:
            for field in ("saved_at", "posted_at", "posted_source", "author", "author_id", "source_url",
                          "metadata_status", "author_metadata_status", "status", "record_source", "content", "hashtags"):
                self.assertEqual(row[field], self.before_by_mode[row["folder_mode"]][field], field)
        self.assertEqual(self.store.ignored_media(), set())
        self.assertEqual(self.store.summary(verify_files=False)["files"], 1)
        self.assertEqual(self.store.summary()["files"], 1)
        self.assertEqual(self.store.deletion_page()["rows"][0]["status"], "recycled")

    def test_refresh_does_not_adopt_unrelated_files_from_the_former_root(self):
        unrelated = self.old.media_dir / "200_1.jpg"
        unrelated.write_bytes(b"untracked former-root photo")
        self.parked.rename(self.original)
        self.store.import_legacy(refresh=True)
        self.assertEqual({row["tweet_id"] for row in self.store.records()}, {"100"})
        self.assertFalse(self.store.is_media_path(unrelated))
        self.assertEqual(self.store.summary()["files"], 1)

    def test_reopened_old_catalogs_index_exact_former_path_checks(self):
        self.parked.rename(self.original)
        schema = self.store._db.execute("SELECT sql FROM sqlite_master WHERE name='media'").fetchone()[0]
        identity = "PRIMARY KEY(folder_mode, tweet_id, num, media_type)"
        self.assertIn(identity, schema)
        row = dict(self.store._db.execute("SELECT * FROM media WHERE folder_mode='flat'").fetchone())
        row["path"] = str(self.original)
        unrelated = self.old.media_dir / "200_1.jpg"
        unrelated.write_bytes(b"untracked former-root photo")
        for old_primary in (False, True):
            with self.subTest(old_primary=old_primary):
                directory = self.root / ("old-primary" if old_primary else "old-indexes")
                directory.mkdir()
                with closing(sqlite3.connect(directory / ".luvbox-history.sqlite3")) as db:
                    db.execute(schema.replace(identity, "PRIMARY KEY(folder_mode, tweet_id, num)") if old_primary else schema)
                    db.execute("INSERT INTO media (" + ",".join(row) + ") VALUES (" + ",".join("?" for _ in row) + ")",
                               tuple(row.values()))
                    db.execute("CREATE TABLE catalog_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                    db.execute("INSERT INTO catalog_meta VALUES ('allowed_media_roots',?)",
                               (json.dumps([str(self.old.output_dir)]),))
                    db.commit()
                with closing(HistoryStore(directory, media_dir=self.new.media_dir, output_dir=self.new.output_dir)) as store:
                    self.assertTrue(store.is_media_path(self.original))
                    self.assertFalse(store.is_media_path(unrelated))
                    plan = " ".join(row[3] for row in store._db.execute(
                        "EXPLAIN QUERY PLAN SELECT 1 FROM media WHERE path=? LIMIT 1", (str(self.original),)))
                    self.assertIn("media_path", plan)
                    self.assertNotIn("SCAN media", plan)

    def test_restore_keeps_a_later_explicit_ignore_choice(self):
        with patch("luvbox.history._now", return_value="2099-01-01T00:00:00+09:00"):
            self.store.set_missing_ignored([("100", 1, "photo")], True)
        self.parked.rename(self.original)
        self.store.import_legacy(refresh=True)
        self.assertEqual(self.store.record_page(include_deleted=False)["total"], 2)
        self.assertEqual(self.store.ignored_media(), {("100", 1, "photo")})

    def test_unavailable_former_root_does_not_clear_the_saved_last_location(self):
        disconnected = self.root / "disconnected-old"
        self.assertTrue(self.old.output_dir.resolve().is_relative_to(self.root.resolve()))
        self.assertTrue(disconnected.resolve().is_relative_to(self.root.resolve()))
        self.old.output_dir.rename(disconnected)
        self.store.import_legacy(refresh=True)
        self.assertTrue(all(row["path"] == "" and row["last_path"] == str(self.original)
                            for row in self.store.records()))
        self.assertEqual(self.store.ignored_media(), {("100", 1, "photo")})

    def test_inaccessible_restored_file_remains_missing_until_a_later_refresh(self):
        self.parked.rename(self.original)
        original_stat = Path.stat

        def denied(path, *args, **kwargs):
            if path == self.original:
                raise PermissionError("offline fixture access denial")
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", denied):
            self.store.import_legacy(refresh=True)
        self.assertTrue(all(row["path"] == "" for row in self.store.records()))
        self.assertEqual(self.store.ignored_media(), {("100", 1, "photo")})
        self.store.import_legacy(refresh=True)
        self.assertEqual(self.store.summary()["files"], 1)
        self.assertEqual(self.store.ignored_media(), set())

    def test_symlink_at_the_last_location_is_not_adopted(self):
        try:
            self.original.symlink_to(self.parked)
        except OSError:
            self.skipTest("symlinks unavailable")
        self.store.import_legacy(refresh=True)
        self.assertTrue(all(row["path"] == "" for row in self.store.records()))
        self.assertEqual(self.store.summary()["files"], 0)
        self.assertEqual(self.store.ignored_media(), {("100", 1, "photo")})

    @unittest.skipUnless(os.name == "nt", "Windows junction")
    def test_junction_replacing_the_former_root_is_not_adopted(self):
        import _winapi

        self.parked.rename(self.original)
        redirected = self.root / "redirected-old"
        self.assertTrue(self.old.output_dir.resolve().is_relative_to(self.root.resolve()))
        self.assertTrue(redirected.resolve().is_relative_to(self.root.resolve()))
        self.old.output_dir.rename(redirected)
        _winapi.CreateJunction(str(redirected), str(self.old.output_dir))
        self.addCleanup(os.rmdir, self.old.output_dir)  # Remove only this private link.
        self.store.import_legacy(refresh=True)
        self.assertTrue(all(row["path"] == "" for row in self.store.records()))
        self.assertEqual(self.store.summary()["files"], 0)
        self.assertEqual(self.store.ignored_media(), {("100", 1, "photo")})


if __name__ == "__main__":
    unittest.main()
