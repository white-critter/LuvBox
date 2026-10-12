"""Two X accounts saving into one output folder, and combining an old
account's records into the current one. gallery-dl runs offline."""

import queue
import shutil
import sqlite3
import tkinter as tk
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

from tests import test_core as core_support
from tests.test_gui import destroy_tk, release_tk
from xlikesaver.core import JobOptions, run_worker
from xlikesaver.history import HistoryStore
from xlikesaver.history_gui import HistoryWindow
from xlikesaver.recovery import _catalog_backups
from xlikesaver.storage import (MigrationError, OutputGuard, merge_account_records, other_account_records,
                                resolve_storage)


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
                patch("xlikesaver.core._save_day", return_value=day):
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
        self.assertNotIn("101_1.jpg", files)   # the other account's file is not this account's

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
        self.assertEqual(files, ["100_1.jpg", "101_1.jpg", "102_1.jpg"])
        self.assertEqual(missing, [])
        next(self.output.rglob("101_1.jpg")).unlink()
        files, missing, _ = self.library("new_account")
        self.assertEqual(missing, ["101"])
        self.assertEqual(self.library("old_account")[1], ["101"])   # the old account's records still work

    def test_likes_timeline_and_runs_stay_with_their_account(self):
        self.old_account_saved()
        paths = resolve_storage(self.output, "new_account")
        merge_account_records(paths, "old_account")
        with closing(sqlite3.connect(paths.record_dir / ".xlikesaver-history.sqlite3")) as db:
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
        self.assertEqual((files, missing), (["100_1.jpg", "101_1.jpg"], []))

    def test_unknown_or_own_account_is_refused(self):
        paths = resolve_storage(self.output, "new_account")
        for name in ("new_account", "nobody", "bad name!"):
            with self.subTest(name=name), self.assertRaises(MigrationError):
                merge_account_records(paths, name)


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
        with patch("xlikesaver.history_gui.threading.Thread"):
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
        with patch("xlikesaver.history_gui.dialogs.askyesno", return_value=True) as ask, \
                patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.merge_accounts()
        self.assertIn("@old_account · 기록 2개", ask.call_args.args[1])
        self.assertTrue(self.window.choice_active)
        results = queue.Queue()
        HistoryWindow._merge_worker(results, *thread.call_args.kwargs["args"])
        generation, kind, message = results.get_nowait()
        self.assertEqual(kind, "accounts-merged")
        self.assertIn("새로 더함 2개", message)
        self.window.results.put((generation, kind, message))
        with patch("xlikesaver.history_gui.threading.Thread"):
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
