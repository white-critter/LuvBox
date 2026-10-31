"""A save lists the media it could not store, never the ones it merely skipped as already saved."""
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

import unittest

from gallery_dl.extractor.message import Message

from tests import test_core, test_media
from luvbox import unsaved
from luvbox.core import run_worker

MIB = test_media.MIB


class QuietFailure(test_core.FakeHttpDownloader):
    """A download that simply does not complete, like a dropped connection."""

    def download(self, url, pathfmt):
        return False


class Review:
    def __init__(self, *choices):
        self.choices = iter(choices)

    def enabled(self):
        return True

    def ask(self, path, media, check_stop):
        return next(self.choices)


class UnsavedRecordingTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    set_session = test_core.CoreTests.set_session
    worker = test_core.CoreTests.worker
    http_worker = test_media.MediaTests.http_worker
    response = staticmethod(test_media.MediaTests.response)
    all_media = test_media.MediaTests.all_media

    def run(self, result=None):
        # The ledger lives in app data; keep each test's rows to itself.
        with patch("luvbox.unsaved.unsaved_path", return_value=Path(__file__).with_name("_never_created.sqlite3")) as path:
            self._ledger = path
            return super().run(result)

    def use_ledger(self):
        path = self.base / "unsaved.sqlite3"
        self._ledger.return_value = path
        return path

    def rows(self, kinds=unsaved.REASONS):
        return unsaved.page(self.options.account, self.options.output_dir, kinds)["rows"]

    def run_with(self, downloader, rows, *, options=None, review=None):
        events = []
        with patch("gallery_dl.extractor.find", side_effect=lambda url: test_core.FakeLikesExtractor(url, rows)), \
                patch("gallery_dl.downloader.find", return_value=downloader):
            code = run_worker(options or self.options, events.append, review=review)
        self.assertNotIn(test_core.SECRET, json.dumps(events))
        return code, events

    def test_a_download_that_does_not_complete_is_listed_with_the_media_facts(self):
        self.use_ledger()
        code, events = self.run_with(QuietFailure, [("100", 1, "photo", "alice", None)])
        self.assertEqual(events[-1]["failed_media"], 1)
        self.assertEqual(events[-1]["saved"], 0)
        self.assertEqual([event["type"] for event in events if event["type"] == "failed"], ["failed"])
        [row] = self.rows()
        self.assertEqual((row["reason"], row["tweet_id"], row["num"], row["media_type"], row["author"], row["attempts"]),
                         ("failed", "100", 1, "photo", "alice", 1))
        self.assertIn("받지 못했습니다", row["detail"])
        self.assertIn("저장하지 못한 미디어 1개", events[-1]["message"])

    def test_repeating_a_failure_keeps_one_row_and_saving_it_later_clears_it(self):
        self.use_ledger()
        rows = [("100", 1, "photo", "alice", None)]
        self.run_with(QuietFailure, rows)
        self.run_with(QuietFailure, rows)
        [row] = self.rows()
        self.assertEqual(row["attempts"], 2)
        code, events = self.run_with(test_core.FakeHttpDownloader, rows)
        self.assertEqual((code, events[-1]["saved"], events[-1]["failed_media"]), (0, 1, 0))
        self.assertEqual(self.rows(), [])

    def test_media_that_is_already_saved_or_saved_now_is_never_listed(self):
        path = self.use_ledger()
        rows = [("100", 1, "photo", "alice", None), ("101", 1, "photo", "bob", None)]
        self.run_with(test_core.FakeHttpDownloader, rows)
        code, events = self.run_with(test_core.FakeHttpDownloader, rows)
        self.assertEqual((events[-1]["saved"], events[-1]["skipped"], events[-1]["failed_media"]), (0, 2, 0))
        self.assertFalse(path.exists())

    def test_a_size_limited_video_is_listed_with_its_size_and_leaves_once_saved(self):
        self.use_ledger()
        rows = [("100", 1, "video", "alice")]
        code, events, _ = self.http_worker(rows, [self.response([test_media.MP4_HEADER], size=3 * MIB)])
        self.assertEqual((events[-1]["size_skipped"], events[-1]["failed_media"]), (1, 0))
        [row] = self.rows()
        self.assertEqual((row["reason"], row["size_bytes"], row["limit_mb"], row["media_type"]),
                         ("size", 3 * MIB, 1, "video"))
        chunks = [test_media.MP4_HEADER, b"v" * (2 * MIB)]
        code, events, _ = self.http_worker(rows, [self.response(chunks, size=sum(map(len, chunks)))],
                                           options=self.all_media(max_video_mb=5))
        self.assertEqual((code, events[-1]["saved"]), (0, 1))
        self.assertEqual(self.rows(), [])

    def test_choosing_not_to_save_is_listed_as_excluded_not_as_a_failure(self):
        self.use_ledger()
        options = replace(self.options, confirm_each=True)
        code, events = self.run_with(test_core.FakeHttpDownloader, [("100", 1, "photo", "alice", None)],
                                     options=options, review=Review("never"))
        self.assertEqual((events[-1]["review_never"], events[-1]["failed_media"], events[-1]["size_skipped"]), (1, 0, 0))
        [row] = self.rows()
        self.assertEqual(row["reason"], "excluded")
        self.assertEqual(unsaved.counts(self.options.account, self.options.output_dir),
                         {"failed": 0, "size": 0, "excluded": 1})

    def test_skipping_once_is_asked_again_so_it_is_not_listed(self):
        path = self.use_ledger()
        options = replace(self.options, confirm_each=True)
        code, events = self.run_with(test_core.FakeHttpDownloader, [("100", 1, "photo", "alice", None)],
                                     options=options, review=Review("skip"))
        self.assertEqual(events[-1]["review_skipped"], 1)
        self.assertFalse(path.exists())

    def test_a_stopped_save_keeps_what_it_found_and_a_stop_is_not_a_failure(self):
        self.use_ledger()
        stop = self.base / "stop.flag"
        rows = [("100", 1, "photo", "alice", None), ("101", 1, "photo", "bob", None)]

        def stop_before_second(extractor, row):
            if row[0] == "101":
                stop.touch()

        events = []
        with patch("gallery_dl.extractor.find", side_effect=lambda url: test_core.FakeLikesExtractor(
                url, rows, before_row=stop_before_second)),                 patch("gallery_dl.downloader.find", return_value=QuietFailure):
            run_worker(self.options, events.append, stop)
        self.assertEqual(events[-1]["status"], "stopped")
        self.assertEqual([row["tweet_id"] for row in self.rows()], ["100"])

    def test_stopping_during_a_download_does_not_list_that_media(self):
        path = self.use_ledger()
        stop = self.base / "stop.flag"

        class Interrupted(QuietFailure):
            def download(self, url, pathfmt):
                stop.touch()
                return False

        events = []
        with patch("gallery_dl.extractor.find", side_effect=lambda url: test_core.FakeLikesExtractor(
                url, [("100", 1, "photo", "alice", None)])),                 patch("gallery_dl.downloader.find", return_value=Interrupted):
            run_worker(self.options, events.append, stop)
        self.assertEqual(events[-1]["status"], "stopped")
        self.assertFalse(path.exists())

    def test_a_ledger_that_cannot_be_written_never_fails_the_save(self):
        self.use_ledger()
        with patch("luvbox.unsaved.record", side_effect=unsaved.UnsavedError("blocked")):
            code, events = self.run_with(QuietFailure, [("100", 1, "photo", "alice", None)])
        self.assertEqual(events[-1]["type"], "finished")
        self.assertTrue(any("목록을 갱신하지 못했습니다" in event.get("message", "") for event in events))


if __name__ == "__main__":
    unittest.main()
