"""Retry visibility errors and completed-file choices across interrupted runs."""

import json
import re
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from gallery_dl import exception

from tests import test_core, test_missing_files
from tests.test_core import FakeHttpDownloader, SECRET
from luvbox.core import run_worker
from luvbox.history import HistoryStore


class RetryRecoveryTests(unittest.TestCase):
    setUp = test_missing_files.RetryWorkerTests.setUp
    tearDown = test_missing_files.RetryWorkerTests.tearDown
    write_cookies = test_missing_files.RetryWorkerTests.write_cookies
    worker = test_missing_files.RetryWorkerTests.worker
    flat = test_missing_files.RetryWorkerTests.flat
    entries = test_missing_files.RetryWorkerTests.entries
    saved_then_deleted = test_missing_files.RetryWorkerTests.saved_then_deleted
    retry = test_missing_files.RetryWorkerTests.retry

    def retry_errors(self, targets, tweets, errors):
        test_missing_files.FakeTweetExtractor.requested = []
        events = []

        def extractor(url):
            tweet_id = re.fullmatch(r"https://x\.com/i/web/status/([0-9]+)", url)[1]
            return test_missing_files.FakeTweetExtractor(url, tweets, error=errors.get(tweet_id))

        with patch("gallery_dl.extractor.find", side_effect=extractor), \
                patch("gallery_dl.downloader.find", return_value=FakeHttpDownloader):
            code = run_worker(self.flat(targets=tuple(targets)), events.append)
        self.assertNotIn(SECRET, json.dumps(events))
        self.assertEqual(events[-1]["type"], "finished")
        return code, events

    def check_unavailable_tweet(self, reason):
        self.saved_then_deleted()
        code, events = self.retry_errors(
            ["100_1", "200_1"], {"200": [(1, "photo")]},
            {"100": exception.AuthRequired(message=reason)})
        self.assertEqual(test_missing_files.FakeTweetExtractor.requested, ["100", "200"])
        self.assertEqual((code, events[-1]["status"], events[-1]["saved"], events[-1]["unavailable_tweets"]),
                         (0, "finished", 1, 1))
        self.assertTrue((self.output / "alice_200_1.jpg").is_file())
        self.assertFalse((self.output / "alice_100_1.jpg").exists())
        self.assertIn("twitter100_1", self.entries())
        self.assertFalse([event for event in events if event.get("level") in {"warning", "error"}])
        notices = [event["message"] for event in events if event["type"] == "log"]
        self.assertTrue(any("볼 수 없어" in text and "status/100" in text for text in notices))

    def test_protected_tweet_is_skipped_and_next_selected_tweet_downloads(self):
        self.check_unavailable_tweet("Protected Tweet")

    def test_age_restricted_tweet_is_skipped_and_next_selected_tweet_downloads(self):
        self.check_unavailable_tweet("NSFW Tweet")

    def test_visibility_exemption_does_not_hide_real_authentication_errors(self):
        self.saved_then_deleted()
        for reason in (None, "authenticated cookies", "Protected Tweet (401)", "NSFW Tweet: 403"):
            with self.subTest(reason=reason):
                code, events = self.retry_errors(
                    ["100_1", "200_1"], {"200": [(1, "photo")]},
                    {"100": exception.AuthRequired(message=reason)})
                self.assertEqual((code, events[-1]["status"]), (1, "error"))
                self.assertEqual(test_missing_files.FakeTweetExtractor.requested, ["100"])
                self.assertFalse((self.output / "alice_200_1.jpg").exists())

    def test_visibility_exemption_only_applies_to_individual_tweet_retries(self):
        for reason in ("Protected Tweet", "NSFW Tweet"):
            with self.subTest(reason=reason):
                code, events = self.worker(options=self.flat(), error=exception.AuthRequired(message=reason))
                self.assertNotEqual(code, 0)
                self.assertEqual(events[-1]["status"], "error")

    def hide_deleted_files(self):
        self.saved_then_deleted()
        location = test_core.resolve_storage(self.output, "myaccount")
        with closing(HistoryStore.for_storage(location)) as store:
            store.set_missing_ignored([("100", 1, "photo"), ("200", 1, "photo")], True)

    def check_partial_recovery(self, code, events, status):
        self.assertEqual((code, events[-1]["status"], events[-1]["recovered"]),
                         (0 if status == "stopped" else 1, status, 1))
        self.assertTrue((self.output / "alice_100_1.jpg").is_file())
        self.assertFalse((self.output / "alice_200_1.jpg").exists())
        self.assertTrue({"twitter100_1", "twitter200_1"} <= self.entries())
        location = test_core.resolve_storage(self.output, "myaccount")
        with closing(HistoryStore.for_storage(location)) as store:
            self.assertEqual(store.ignored_media(), {("200", 1, "photo")})
        # The folder snapshot carries the completed choice to another PC too.
        with closing(sqlite3.connect(location.snapshot_dir / ".luvbox-history.sqlite3")) as db:
            self.assertEqual(set(db.execute("SELECT tweet_id,num,media_type FROM missing_choices WHERE ignored=1")),
                             {("200", 1, "photo")})

    def test_successful_hidden_retry_is_restored_before_user_stop(self):
        self.hide_deleted_files()
        code, events = self.retry(["100_1", "200_1"], {"100": [(1, "photo")]}, stop_after=1)
        self.check_partial_recovery(code, events, "stopped")

    def test_successful_hidden_retry_is_restored_before_authentication_failure(self):
        self.hide_deleted_files()
        code, events = self.retry_errors(
            ["100_1", "200_1", "300_1"], {"100": [(1, "photo")], "300": [(1, "photo")]},
            {"200": exception.AuthRequired("authenticated cookies")})
        self.check_partial_recovery(code, events, "error")
        self.assertEqual(test_missing_files.FakeTweetExtractor.requested, ["100", "200"])

    def test_successful_hidden_retry_is_restored_before_rate_limit(self):
        self.hide_deleted_files()
        code, events = self.retry_errors(
            ["100_1", "200_1", "300_1"], {"100": [(1, "photo")], "300": [(1, "photo")]},
            {"200": exception.AbortExtraction("429 rate limit")})
        self.check_partial_recovery(code, events, "error")
        self.assertEqual(test_missing_files.FakeTweetExtractor.requested, ["100", "200"])
        self.assertIn("요청 한도", events[-1]["message"])


if __name__ == "__main__":
    unittest.main()
