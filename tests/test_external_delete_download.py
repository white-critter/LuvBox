"""External deletion and legacy policy with gallery-dl's real job/archive."""

import json
import unittest
from contextlib import closing, nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests import test_core as core_support
from tests import test_missing_files as missing_support
from luvbox.core import run_worker
from luvbox.history import HistoryStore
from luvbox.storage import resolve_storage


class ExternalDeleteDownloadTests(unittest.TestCase):
    # Import helper modules rather than TestCase class aliases: discovery must
    # collect only these new tests, not another copy of the entire core suite.
    write_cookies = core_support.CoreTests.write_cookies
    worker = core_support.CoreTests.worker

    def setUp(self):
        core_support.CoreTests.setUp(self)
        self.options = replace(self.options, folder_mode="flat", separate_accounts=False)
        self.paths = resolve_storage(self.output, "myaccount")
        self.recycled = self.base / "mock-recycled-fixtures"
        self.recycled.mkdir()
        # Core's source embedding is already mocked. Avoid unrelated resident
        # ExifTool process startup/teardown in these deletion policy tests.
        resident = patch("luvbox.metadata.persistent", side_effect=nullcontext)
        resident.start()
        self.addCleanup(resident.stop)

    def tearDown(self):
        core_support.CoreTests.tearDown(self)

    def record(self, tweet="100", number=1):
        with closing(HistoryStore.for_storage(self.paths)) as store:
            return next(row for row in store.records() if row["tweet_id"] == tweet and row["num"] == number)

    def remove_externally(self, path):
        path = Path(path)
        destination = self.recycled / path.name
        path.rename(destination)
        return str(destination)

    def delete(self, rows, keep):
        for row in rows:
            self.remove_externally(row["path"])
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store.import_legacy(refresh=True)
            if keep:
                store.set_missing_ignored([(row["tweet_id"], row["num"], row["media_type"]) for row in rows], True)
        core_support.FakeHttpDownloader.downloaded = []

    def retry(self, target, tweets):
        missing_support.FakeTweetExtractor.requested = []
        events = []
        with patch("gallery_dl.extractor.find", side_effect=lambda url:
                   missing_support.FakeTweetExtractor(url, tweets)), \
                patch("gallery_dl.downloader.find", return_value=core_support.FakeHttpDownloader):
            code = run_worker(replace(self.options, targets=(target,)), events.append)
        self.assertEqual(code, 0)
        self.assertNotIn(core_support.SECRET, json.dumps(events))
        return events[-1]

    def test_kept_deleted_media_is_never_downloaded_when_layout_or_account_folder_changes(self):
        code, _ = self.worker([("100", 1, "photo", "alice")])
        self.assertEqual(code, 0)
        self.delete([self.record()], keep=True)
        for index, (layout, separate) in enumerate(
                (("flat", False), ("author", False), ("date", False),
                 ("flat", True), ("author", True), ("date", True))):
            with self.subTest(layout=layout, separate=separate):
                core_support.FakeHttpDownloader.downloaded = []
                fresh = str(101 + index)
                options = replace(self.options, folder_mode=layout, separate_accounts=separate)
                code, events = self.worker([("100", 1, "photo", "alice"), (fresh, 1, "photo", "bob")],
                                           options=options)
                self.assertEqual(code, 0)
                self.assertEqual(events[-1]["saved"], 1)
                self.assertEqual(core_support.FakeHttpDownloader.downloaded,
                                 [f"https://pbs.twimg.com/media/{fresh}_1?format=jpg&name=orig"])
                self.assertEqual(list(self.output.rglob("100_1.jpg")), [])

    def test_external_deletion_can_be_restored_by_explicit_missing_retry(self):
        self.worker([("100", 1, "photo", "alice")])
        self.delete([self.record()], keep=False)
        # The full scan retains its existing archive semantics. A missing-file
        # retry deliberately rechecks this archive entry and restores the file.
        code, events = self.worker([("100", 1, "photo", "alice")])
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 0)
        self.assertEqual(core_support.FakeHttpDownloader.downloaded, [])
        with closing(HistoryStore.for_storage(self.paths)) as store:
            self.assertEqual(store.ignored_media(), set())
            self.assertEqual([item["archive_key"] for item in store.missing_media()], ["100_1"])
        restored = self.retry("100_1", {"100": [(1, "photo")]})
        self.assertEqual((restored["saved"], restored["recovered"]), (1, 1))
        self.assertEqual(core_support.FakeHttpDownloader.downloaded,
                         ["https://pbs.twimg.com/media/100_1?format=jpg&name=orig"])
        self.assertTrue((self.output / "100_1.jpg").exists())

    def test_external_deletion_of_one_attachment_preserves_other_photos_and_retries_only_chosen_attachment(self):
        rows = [("100", 1, "photo", "alice"), ("100", 2, "photo", "alice"),
                ("100", 3, "photo", "alice")]
        self.worker(rows)
        untouched = {number: (self.output / f"100_{number}.jpg").read_bytes() for number in (1, 3)}
        self.delete([self.record(number=2)], keep=False)
        for number, content in untouched.items():
            self.assertEqual((self.output / f"100_{number}.jpg").read_bytes(), content)
        self.assertFalse((self.output / "100_2.jpg").exists())
        restored = self.retry("100_2", {"100": [(1, "photo"), (2, "photo"), (3, "photo")]})
        self.assertEqual(restored["saved"], 1)
        self.assertEqual(core_support.FakeHttpDownloader.downloaded,
                         ["https://pbs.twimg.com/media/100_2?format=jpg&name=orig"])
        self.assertEqual(len(list(self.output.glob("100_*.jpg"))), 3)
        for number, content in untouched.items():
            self.assertEqual((self.output / f"100_{number}.jpg").read_bytes(), content)

    def test_core_reconciles_crashed_deletion_after_gui_refresh_before_reading_ignore_policy(self):
        self.worker([("100", 1, "photo", "alice")])
        row = self.record()
        with closing(HistoryStore.for_storage(self.paths)) as store:
            info = Path(row["path"]).stat()
            store.begin_deletion("fixture-crash", row, True,
                                 (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
        self.remove_externally(row["path"])
        # Library reload refreshes present/missing paths before the next worker
        # settles the intent. This cannot lose the user's keep-deleted choice.
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store.import_legacy(refresh=True)
            self.assertEqual(store.pending_deletions()[0]["status"], "pending")
        core_support.FakeHttpDownloader.downloaded = []
        code, events = self.worker([("100", 1, "photo", "alice"), ("101", 1, "photo", "bob")],
                                   options=replace(self.options, folder_mode="author", separate_accounts=True))
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertEqual(core_support.FakeHttpDownloader.downloaded,
                         ["https://pbs.twimg.com/media/101_1?format=jpg&name=orig"])
        self.assertEqual(list(self.output.rglob("100_1.jpg")), [])
        with closing(HistoryStore.for_storage(self.paths)) as store:
            self.assertEqual(store.pending_deletions(), [])
            self.assertEqual(store.deletion_page()["rows"][0]["status"], "interrupted")
            self.assertIn(("100", 1, "photo"), store.ignored_media())


if __name__ == "__main__":
    unittest.main()
