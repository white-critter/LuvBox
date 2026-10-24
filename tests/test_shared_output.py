"""Two X accounts saving into one output folder, and records an earlier
version combined across accounts. gallery-dl runs offline."""

import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import patch

from tests import test_core as core_support
from luvbox.core import JobOptions, run_worker
from luvbox.history import HistoryStore
from luvbox.storage import resolve_storage, saved_media_missing


class SharedOutputTests(unittest.TestCase):
    write_cookies = core_support.CoreTests.write_cookies

    def setUp(self):
        core_support.CoreTests.setUp(self)
        self.output = self.base / "Likes"

    def tearDown(self):
        core_support.CoreTests.tearDown(self)

    def job(self, account, mode="date", separate=False):
        return JobOptions(account, str(self.output), "file", str(self.cookie_file), folder_mode=mode,
                          include_videos=False, include_gifs=False, separate_accounts=separate)

    def save(self, options, rows, day):
        core_support.FakeHttpDownloader.downloaded = []
        events = []
        with patch("gallery_dl.extractor.find",
                   side_effect=lambda url: core_support.FakeLikesExtractor(url, rows, None, None)), \
                patch("gallery_dl.downloader.find", return_value=core_support.FakeHttpDownloader), \
                patch("luvbox.core._save_day", return_value=day):
            self.assertEqual(run_worker(options, events.append), 0, events[-1])
        return [url.split("/")[-1].split("?")[0] for url in core_support.FakeHttpDownloader.downloaded]

    def library(self, account, separate=False):
        paths = resolve_storage(self.output, account, separate)
        with closing(HistoryStore.for_storage(paths, create_media=False)) as store:
            store.import_legacy(refresh=True)
            summary = store.overview(verify_files=True, top=None)
            files = sorted(Path(row["path"]).name for row in store.present_files())
            return files, sorted(item["tweet_id"] for item in summary["missing"]), store

    def old_account_saved(self, mode="date", separate=False):
        rows = [("100", 1, "photo", "artistA"), ("101", 1, "photo", "artistB")]
        self.assertEqual(self.save(self.job("old_account", mode, separate), rows, date(2026, 9, 1)), ["100_1", "101_1"])

    def test_without_merging_a_date_folder_downloads_the_same_like_again(self):
        self.old_account_saved()
        rows = [("100", 1, "photo", "artistA"), ("102", 1, "photo", "artistC")]
        self.assertEqual(self.save(self.job("new_account"), rows, date(2026, 10, 12)), ["100_1", "102_1"])
        files, _, _ = self.library("new_account")
        self.assertNotIn("artistB_101_1.jpg", files)   # the other account's file is not this account's


class SharedOutputStorageTests(unittest.TestCase):
    """Pure database/file fixtures; no account or network access."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        guard = patch("luvbox.protection.state_directory", return_value=self.root / "state" / "locks")
        guard.start()
        self.addCleanup(guard.stop)
        self.output = self.root / "Likes"
        self.output.mkdir()
        self.target = resolve_storage(self.output, "current")
        self.a = resolve_storage(self.output, "old_a")
        self.b = resolve_storage(self.output, "old_b")

    def record(self, paths, tweet, kind="photo"):
        metadata = {"tweet_id": str(tweet), "num": 1, "type": kind, "author": {"name": "artist", "id": "42"},
                    "date": "2026-10-01T00:00:00+09:00", "_luvbox_metadata_status": "embedded"}
        with closing(HistoryStore.for_storage(paths)) as store:
            filename = f"{tweet}_animated_gif_1.gif" if kind == "animated_gif" else f"{tweet}_1.jpg"
            file = paths.media_dir / filename
            file.write_bytes(b"offline-media")
            store.record_media("flat", metadata, file)
            return store.media_record("flat", metadata)

    def digest(self, directory):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.iterdir()
                if path.name.endswith(".sqlite3") or path.name == "snapshot.json"}

    def test_snapshot_union_preserves_pending_quick_scans_without_importing_cleared_run_ids(self):
        from luvbox import storage

        def runs(paths, count, month):
            with closing(HistoryStore.for_storage(paths)) as store:
                for day in range(1, count + 1):
                    run = store.start_run("flat", media_signature="same")
                    with store._db:
                        store._db.execute("UPDATE runs SET started_at=? WHERE id=?",
                                          (f"2026-{month:02d}-{day:02d}T00:00:00+09:00", run))
                    store.finish_run(run, "finished", visited=1)

        def marker(paths, value):
            if value is not None:
                with closing(sqlite3.connect(paths.record_dir / ".luvbox-history.sqlite3")) as db, db:
                    db.execute("INSERT INTO catalog_meta VALUES ('quick_scan_merge_run',?)", (str(value),))

        cases = (("source_pending", None, 1, 1, 11, "records-changed"),
                 ("no_source_runs", None, 0, 0, 9, "records-changed"),
                 ("target_pending", 5, None, 1, 9, "records-changed"),
                 ("both_cleared", 4, 1, 2, 9, "ready"))
        for index, (name, target_marker, source_marker, source_runs, source_month, expected) in enumerate(cases):
            with self.subTest(case=name):
                target = resolve_storage(self.output, "target_" + str(index))
                source = resolve_storage(self.output, "source_" + str(index))
                self.record(target, 400)
                self.record(source, 100)
                runs(target, 5, 10)
                runs(source, source_runs, source_month)
                marker(target, target_marker)
                marker(source, source_marker)
                original = self.digest(source.record_dir)
                storage._merge_database(".luvbox-history.sqlite3",
                                        target.record_dir / ".luvbox-history.sqlite3",
                                        source.record_dir / ".luvbox-history.sqlite3")
                with closing(HistoryStore.for_storage(target)) as store:
                    self.assertEqual(store.quick_scan_readiness("flat", "same"), expected)
                    self.assertEqual(len(store.runs()), 5 + source_runs)
                    if expected == "ready":
                        value = store._db.execute("SELECT value FROM catalog_meta WHERE key='quick_scan_merge_run'").fetchone()[0]
                        self.assertEqual(value, "4")
                    else:
                        run = store.start_run("flat", media_signature="same")
                        with store._db:
                            store._db.execute("UPDATE runs SET started_at='2026-12-01T00:00:00+09:00' WHERE id=?", (run,))
                        store.finish_run(run, "finished", visited=1)
                        self.assertEqual(store.quick_scan_readiness("flat", "same"), "ready")
                self.assertEqual(self.digest(source.record_dir), original)
                # Re-importing the same snapshot must not revive a cleared
                # marker from the other PC or require another complete scan.
                if expected == "ready":
                    storage._merge_database(".luvbox-history.sqlite3",
                                            target.record_dir / ".luvbox-history.sqlite3",
                                            source.record_dir / ".luvbox-history.sqlite3")
                    with closing(HistoryStore.for_storage(target)) as store:
                        self.assertEqual(store.quick_scan_readiness("flat", "same"), "ready")

    def test_never_used_account_subfolder_is_not_missing_but_whole_missing_output_is(self):
        source = resolve_storage(self.output, "old_a", True)
        target = resolve_storage(self.output, "current", True)
        self.record(source, 100)
        # Earlier versions could combine another account's records into this
        # one; its files then live only in that account's subfolder.
        target.record_dir.mkdir(parents=True)
        shutil.copy2(source.record_dir / ".luvbox-history.sqlite3", target.record_dir / ".luvbox-history.sqlite3")
        self.assertFalse(target.media_dir.exists())
        self.assertFalse(saved_media_missing(target))
        moved = self.root / "Likes-moved"
        self.assertTrue(self.output.resolve().is_relative_to(self.root.resolve()))
        self.assertTrue(moved.resolve().is_relative_to(self.root.resolve()))
        self.output.rename(moved)
        self.assertTrue(saved_media_missing(target))

    def test_own_subfolder_missing_uses_last_path_but_not_former_output_records(self):
        paths = resolve_storage(self.output, "current", True)
        row = self.record(paths, 100)
        Path(row["path"]).unlink()
        with closing(HistoryStore.for_storage(paths)) as store:
            store.import_legacy(refresh=True)
            self.assertEqual(store.media_record("flat", {"tweet_id": "100", "num": 1})["path"], "")
        paths.media_dir.rmdir()
        self.assertTrue(saved_media_missing(paths))
        other = resolve_storage(self.root / "new-output", "current", True)
        other.output_dir.mkdir()
        other.record_dir.mkdir(parents=True)
        shutil.copy2(paths.record_dir / ".luvbox-history.sqlite3", other.record_dir / ".luvbox-history.sqlite3")
        self.assertFalse(saved_media_missing(other))


if __name__ == "__main__":
    unittest.main()
