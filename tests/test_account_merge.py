"""Two X accounts saving into one output folder, and combining an old
account's records into the current one. gallery-dl runs offline."""

import queue
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import tkinter as tk
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

from tests import test_core as core_support
from tests.legacy_ui import previous_window
from tests.test_gui import destroy_tk, release_tk
from luvbox.core import JobOptions, run_worker
from luvbox.history import HistoryStore
from luvbox.history_gui import HistoryWindow
from luvbox.recovery import _catalog_backups
from luvbox.storage import (MigrationError, OutputGuard, merge_account_records, merge_accounts_records,
                                other_account_records, publish_snapshot, resolve_storage, saved_media_missing)


class AccountMergeTests(unittest.TestCase):
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

    def test_merged_account_files_are_managed_and_never_downloaded_again(self):
        self.old_account_saved()
        paths = resolve_storage(self.output, "new_account")
        self.assertEqual(other_account_records(paths), [{"account": "old_account", "records": 2}])
        result = merge_account_records(paths, "@Old_Account")
        self.assertEqual(result, {"records": 2, "added": 2, "keys": 2})
        self.assertEqual(merge_account_records(paths, "old_account"), {"records": 2, "added": 0, "keys": 0})
        backups = list(_catalog_backups("new_account", paths.record_dir).glob("before-merge-old_account-*.sqlite3"))
        self.assertEqual(len(backups), 2)
        rows = [("100", 1, "photo", "artistA"), ("101", 1, "photo", "artistB"), ("102", 1, "photo", "artistC")]
        self.assertEqual(self.save(self.job("new_account"), rows, date(2026, 10, 12)), ["102_1"])
        files, missing, store = self.library("new_account")
        self.assertEqual(files, ["artistA_100_1.jpg", "artistB_101_1.jpg", "artistC_102_1.jpg"])
        self.assertEqual(missing, [])
        next(self.output.rglob("artistB_101_1.jpg")).unlink()
        files, missing, _ = self.library("new_account")
        self.assertEqual(missing, ["101"])
        self.assertEqual(self.library("old_account")[1], ["101"])   # the old account's records still work

    def test_likes_timeline_and_runs_stay_with_their_account(self):
        self.old_account_saved()
        paths = resolve_storage(self.output, "new_account")
        merge_account_records(paths, "old_account")
        with closing(sqlite3.connect(paths.record_dir / ".luvbox-history.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM likes").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT value FROM catalog_meta WHERE key='merged_accounts'").fetchone()[0],
                             '["old_account"]')

    def test_per_account_subfolders_and_records_only_in_the_output_copy(self):
        self.old_account_saved(separate=True)
        cobi = resolve_storage(self.output, "old_account", True)
        # Records that exist only in the output folder (saved on another PC).
        shutil.rmtree(cobi.record_dir)
        paths = resolve_storage(self.output, "new_account", True)
        self.assertEqual(other_account_records(paths), [{"account": "old_account", "records": 2}])
        self.assertEqual(merge_account_records(paths, "old_account")["added"], 2)
        files, missing, _ = self.library("new_account", True)
        self.assertEqual((files, missing), (["artistA_100_1.jpg", "artistB_101_1.jpg"], []))

    def test_unknown_or_own_account_is_refused(self):
        paths = resolve_storage(self.output, "new_account")
        for name in ("new_account", "nobody", "bad name!"):
            with self.subTest(name=name), self.assertRaises(MigrationError):
                merge_account_records(paths, name)


class AccountMergeStorageTests(unittest.TestCase):
    """Pure database/file fixtures; no account, Tk window or network access."""

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

    def archive(self, directory, ids, mode="flat", kind="photo"):
        with closing(sqlite3.connect(directory / f".luvbox-archive.{mode}.sqlite3")) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS archive(entry TEXT PRIMARY KEY) WITHOUT ROWID")
            prefix = "animated_gif_" if kind == "animated_gif" else ""
            db.executemany("INSERT OR IGNORE INTO archive VALUES (?)", ((f"twitter{tweet}_{prefix}1",) for tweet in ids))

    def digest(self, directory):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.iterdir()
                if path.name.endswith(".sqlite3") or path.name == "snapshot.json"}

    def rows(self, paths):
        with closing(HistoryStore(paths.record_dir, media_dir=paths.media_dir, output_dir=paths.output_dir)) as store:
            return store.records(limit=100), store

    def shared_record(self, paths, tweet, *, former=None):
        folder = former or paths.media_dir
        folder.mkdir(parents=True, exist_ok=True)
        file = folder / f"{tweet}_1.jpg"
        file.write_bytes(b"received media")
        with closing(HistoryStore(paths.snapshot_dir, media_dir=folder, output_dir=folder)) as store:
            store.record_media("flat", {"tweet_id": str(tweet), "num": 1, "type": "photo"}, file)
        history = paths.snapshot_dir / ".luvbox-history.sqlite3"
        if former:
            with closing(sqlite3.connect(history)) as db, db:
                db.execute("INSERT OR REPLACE INTO catalog_meta VALUES ('allowed_media_roots',?)",
                           (json.dumps([str(former)]),))
        self.archive(paths.snapshot_dir, [tweet])
        for path in (history, paths.snapshot_dir / ".luvbox-archive.flat.sqlite3"):
            info = path.stat()
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
        return file

    def test_bad_late_archive_keeps_all_accounts_and_changed_target_snapshot_unpublished(self):
        for paths, tweet in ((self.target, 400), (self.a, 100), (self.b, 200)):
            self.record(paths, tweet)
            self.archive(paths.record_dir, [tweet])
        publish_snapshot(self.target)
        self.shared_record(self.target, 401)
        (self.b.record_dir / ".luvbox-archive.date.sqlite3").write_bytes(b"not a database")
        before = self.digest(self.target.record_dir)
        source = [self.digest(paths.record_dir) for paths in (self.a, self.b)]
        with self.assertRaises(MigrationError):
            merge_accounts_records(self.target, ["old_a", "old_b"])
        self.assertEqual(self.digest(self.target.record_dir), before)
        self.assertEqual([self.digest(paths.record_dir) for paths in (self.a, self.b)], source)
        self.assertEqual({row["tweet_id"] for row in self.rows(self.target)[0]}, {"400"})
        self.assertFalse(list(self.target.record_dir.parent.glob(".relocate-*")))
        self.assertFalse(list(self.target.record_dir.parent.glob(".snapshot-union-*")))

    def test_single_merge_failure_does_not_create_an_empty_target_catalog(self):
        self.record(self.a, 100)
        (self.a.record_dir / ".luvbox-archive.date.sqlite3").write_bytes(b"not a database")
        with self.assertRaises(MigrationError):
            merge_account_records(self.target, "old_a")
        self.assertFalse(self.target.record_dir.exists())

    def test_changed_snapshot_union_preserves_locally_approved_former_roots_and_sources(self):
        former = self.root / "former-output"
        for paths, tweet in ((self.target, 400), (self.a, 100)):
            self.record(paths, tweet)
            self.archive(paths.record_dir, [tweet])
            if paths == self.a:
                # This PC previously approved the former root during relocation.
                with closing(sqlite3.connect(paths.record_dir / ".luvbox-history.sqlite3")) as db, db:
                    db.execute("INSERT OR REPLACE INTO catalog_meta VALUES ('allowed_media_roots',?)",
                               (json.dumps([str(former)]),))
            publish_snapshot(paths)
        self.shared_record(self.target, 401)
        old_file = self.shared_record(self.a, 101, former=former)
        local_before, shared_before = self.digest(self.a.record_dir), self.digest(self.a.snapshot_dir)
        result = merge_account_records(self.target, "old_a")
        self.assertEqual(result, {"records": 2, "added": 2, "keys": 2})
        with closing(HistoryStore(self.target.record_dir, media_dir=self.target.media_dir,
                                  output_dir=self.target.output_dir)) as store:
            store.import_legacy(refresh=True)
            self.assertEqual({row["tweet_id"] for row in store.present_files()}, {"100", "101", "400", "401"})
            self.assertIn(former, store.media_roots)
            self.assertTrue(store.is_media_path(old_file))
            other = former / "999_1.jpg"
            other.write_bytes(b"unrelated")
            self.assertFalse(store.is_media_path(other))
        self.assertEqual(self.digest(self.a.record_dir), local_before)
        self.assertEqual(self.digest(self.a.snapshot_dir), shared_before)
        self.assertEqual(merge_account_records(self.target, "old_a"), {"records": 2, "added": 0, "keys": 0})

    def test_snapshot_only_account_merge_does_not_approve_its_declared_outside_root(self):
        self.record(self.target, 400)
        outside = self.root / "not-selected"
        victim = self.shared_record(self.a, 101, former=outside)
        shared_before = self.digest(self.a.snapshot_dir)
        self.assertFalse(self.a.record_dir.exists())
        merge_account_records(self.target, "old_a")
        with closing(HistoryStore.for_storage(self.target)) as store:
            self.assertNotIn(outside, store.media_roots)
            self.assertFalse(store.is_media_path(victim))
            self.assertEqual(store.overview(verify_files=True)["files"], 1)
            self.assertEqual({row["tweet_id"] for row in store.records()}, {"101", "400"})
        self.assertEqual(self.digest(self.a.snapshot_dir), shared_before)
        self.assertEqual(victim.read_bytes(), b"received media")

    def test_changed_snapshot_union_cannot_add_a_root_that_local_records_never_approved(self):
        self.record(self.target, 400)
        self.record(self.a, 100)
        publish_snapshot(self.a)
        outside = self.root / "not-selected"
        victim = self.shared_record(self.a, 101, former=outside)
        local_before = self.digest(self.a.record_dir)
        shared_before = self.digest(self.a.snapshot_dir)
        merge_account_records(self.target, "old_a")
        with closing(HistoryStore.for_storage(self.target)) as store:
            self.assertNotIn(outside, store.media_roots)
            self.assertFalse(store.is_media_path(victim))
            self.assertEqual(store.overview(verify_files=True)["files"], 2)
            self.assertEqual({row["tweet_id"] for row in store.records()}, {"100", "101", "400"})
        self.assertEqual(self.digest(self.a.record_dir), local_before)
        self.assertEqual(self.digest(self.a.snapshot_dir), shared_before)

    def test_unreadable_valid_snapshot_and_publication_error_leave_target_unchanged(self):
        from luvbox import storage
        self.record(self.target, 400)
        self.record(self.a, 100)
        self.archive(self.a.record_dir, [100])
        publish_snapshot(self.a)
        self.shared_record(self.a, 101)
        before = self.digest(self.target.record_dir)
        original = storage._backup_database

        def unreadable(source, destination):
            if source == self.a.snapshot_dir / ".luvbox-history.sqlite3":
                raise PermissionError("private sync lock fixture")
            return original(source, destination)

        with patch("luvbox.storage._backup_database", side_effect=unreadable), self.assertRaises(MigrationError):
            merge_account_records(self.target, "old_a")
        self.assertEqual(self.digest(self.target.record_dir), before)
        with patch("luvbox.storage._publish_records", side_effect=PermissionError("private publish fixture")), \
                self.assertRaises(MigrationError):
            merge_account_records(self.target, "old_a")
        self.assertEqual(self.digest(self.target.record_dir), before)
        self.assertEqual(merge_account_records(self.target, "old_a")["added"], 2)

    def test_batch_is_idempotent_keeps_missing_gif_policy_metadata_and_run_watermark(self):
        self.record(self.target, 400)
        self.archive(self.target.record_dir, [400])
        for paths in (self.a, self.b):
            self.record(paths, 100)
            self.archive(paths.record_dir, [100])
        gif = self.record(self.b, 101, "animated_gif")
        self.archive(self.b.record_dir, [101], kind="animated_gif")
        Path(gif["path"]).unlink()
        with closing(HistoryStore.for_storage(self.b)) as store:
            store.import_legacy(refresh=True)
            store.set_missing_ignored([("101", 1, "animated_gif")], True)
        with closing(HistoryStore.for_storage(self.target)) as store:
            run = store.start_run("flat")
            store.finish_run(run, "finished", saved=1, visited=1)
        self.assertEqual(merge_accounts_records(self.target, ["@OLD_A", "old_a", "old_b"]),
                         {"records": 3, "added": 2, "keys": 2})
        with closing(HistoryStore.for_storage(self.target)) as store:
            row = store.media_record("flat", {"tweet_id": "101", "num": 1, "type": "animated_gif"})
            self.assertEqual((row["path"], row["last_path"], row["saved_at"], row["metadata_status"]),
                             ("", gif["path"], gif["saved_at"], gif["metadata_status"]))
            self.assertIn(("101", 1, "animated_gif"), store.ignored_media())
            self.assertEqual(len(store.runs()), 1)
        with closing(sqlite3.connect(self.target.record_dir / ".luvbox-history.sqlite3")) as db:
            marker = db.execute("SELECT value FROM catalog_meta WHERE key='quick_scan_merge_run'").fetchone()[0]
            self.assertEqual(marker, str(run))
        self.assertEqual(merge_accounts_records(self.target, ["old_a", "old_b"]),
                         {"records": 3, "added": 0, "keys": 0})
        with closing(sqlite3.connect(self.target.record_dir / ".luvbox-history.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT value FROM catalog_meta WHERE key='quick_scan_merge_run'").fetchone()[0], marker)

    def test_successful_publication_survives_private_snapshot_cleanup_error(self):
        from luvbox import storage
        self.record(self.target, 400)
        self.record(self.a, 100)
        self.archive(self.a.record_dir, [100])
        publish_snapshot(self.a)
        self.shared_record(self.a, 101)
        union = storage._merge_snapshot_copies

        def cleanup_fails(*args):
            temporary, files = union(*args)
            if temporary is not None:
                original = temporary.cleanup

                def cleanup():
                    original()
                    raise PermissionError("private cleanup fixture")

                temporary.cleanup = cleanup
            return temporary, files

        with patch("luvbox.storage._merge_snapshot_copies", side_effect=cleanup_fails):
            self.assertEqual(merge_account_records(self.target, "old_a")["added"], 2)
        self.assertEqual({row["tweet_id"] for row in self.rows(self.target)[0]}, {"100", "101", "400"})

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
        merge_account_records(target, "old_a")
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


@previous_window
class AccountMergeWindowTests(unittest.TestCase):
    write_cookies = core_support.CoreTests.write_cookies

    def setUp(self):
        core_support.CoreTests.setUp(self)
        self.addCleanup(core_support.CoreTests.tearDown, self)
        self.output = self.base / "Likes"
        self.paths = resolve_storage(self.output, "new_account")
        cobi = resolve_storage(self.output, "old_account")
        with closing(HistoryStore.for_storage(cobi)) as store:
            for tweet in ("100", "101"):
                path = cobi.media_dir / f"{tweet}_1.jpg"
                path.write_bytes(b"offline-image")
                store.record_media("flat", {"tweet_id": tweet, "num": 1, "type": "photo"}, path)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(release_tk, self, "window", "root")
        self.addCleanup(destroy_tk, self.root)
        with patch("luvbox.history_gui.threading.Thread"):
            self.window = HistoryWindow(self.root, lambda: ("new_account", self.paths))
        self.window.withdraw()

    def load(self):
        self.window._load(self.window.generation, self.paths, "all", "", 0, True)
        self.window._poll()

    def test_notice_merges_once_then_disappears(self):
        self.load()
        self.assertIn("@old_account(기록 2개)", self.window.accounts_text.get())
        self.assertTrue(self.window.accounts_notice.winfo_manager())
        self.assertEqual(self.window.tile_values["files"].get(), "0")
        with patch("luvbox.history_gui.dialogs.askyesno", return_value=True) as ask, \
                patch("luvbox.history_gui.threading.Thread") as thread:
            self.window.merge_accounts()
        self.assertIn("@old_account · 기록 2개", ask.call_args.args[1])
        self.assertTrue(self.window.choice_active)
        results = queue.Queue()
        HistoryWindow._merge_worker(results, *thread.call_args.kwargs["args"])
        generation, kind, message = results.get_nowait()
        self.assertEqual(kind, "accounts-merged")
        self.assertIn("새로 더함 2개", message)
        self.window.results.put((generation, kind, message))
        with patch("luvbox.history_gui.threading.Thread"):
            self.window._poll()
        self.load()
        self.assertEqual(self.window.tile_values["files"].get(), "2")
        self.assertFalse(self.window.accounts_notice.winfo_manager())
        self.assertEqual(str(self.window.merge_button.cget("state")), "normal")   # merge again later

    def test_merge_waits_for_other_work_and_reports_failure(self):
        self.load()
        guard = OutputGuard(self.paths.output_dir)
        guard.acquire()
        try:
            results = queue.Queue()
            HistoryWindow._merge_worker(results, 1, self.paths, ["old_account"])
            _, kind, _ = results.get_nowait()
        finally:
            guard.close()
        self.assertEqual(kind, "error")
        with closing(HistoryStore.for_storage(self.paths, create_media=False)) as store:
            self.assertEqual(store.merged_accounts(), set())


if __name__ == "__main__":
    unittest.main()
