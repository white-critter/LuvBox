"""Three rules of the save engine found by the 1.10.8 inspection.

A chosen retry goes past a file the photo server still refuses; a save that
ended before it read a single Like does not turn the next save into a full
check; and an author id that merely looks like a diagnosis ends nothing.
"""
from dataclasses import replace
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # noqa: F401
from tests import test_core, test_missing_files, test_save_failures
from tests.test_save_failures import QuietDownloader, _response, _tweet

import requests

from luvbox import unsaved
from luvbox.core import REFUSED_LIMIT, _REFUSED_RETRY_DETAIL, run_worker
from luvbox.history import HistoryStore, archive_key
from luvbox.storage import resolve_storage

BODY = bytes([0xFF, 0xD8, 0xFF]) + b"offline-photo" * 8
OLD = [(str(tweet), 1, "photo", "alice") for tweet in range(100, 110)]


class _Fixture(unittest.TestCase):
    """The fixture of SaveFailureTests without its tests (a subclass would run them all again)."""


for _name in ("setUp_core", "tearDown", "set_session", "runs", "setUp", "send", "save", "listed", "detail"):
    setattr(_Fixture, _name, test_save_failures.SaveFailureTests.__dict__[_name])


class RetryPastRefusedTests(_Fixture):
    def retry(self, targets, tweets):
        from gallery_dl.downloader.http import HttpDownloader

        events = []
        self.sent = []
        test_missing_files.FakeTweetExtractor.requested = []
        with patch("gallery_dl.extractor.find",
                   side_effect=lambda url: test_missing_files.FakeTweetExtractor(url, tweets)), \
                patch("gallery_dl.downloader.find", return_value=HttpDownloader), \
                patch("requests.Session.send", side_effect=self.send):
            code = run_worker(replace(self.options, targets=tuple(targets)), events.append)
        return code, events[-1]

    def listed_keys(self):
        """What "모두 다시 저장" sends, in the list's own order."""
        rows = unsaved.page("myaccount", self.options.output_dir, (unsaved.FAILED, unsaved.SIZE), 0, 200)["rows"]
        return [archive_key(row["tweet_id"], row["num"], row["media_type"]) for row in rows]

    def listed_tweets(self):
        return sorted(identity[0] for identity in self.listed())

    def test_a_retry_of_everything_goes_past_the_file_the_server_still_refuses(self):
        from gallery_dl.downloader.http import HttpDownloader

        gone = {"6000", "6001"}

        def answer(request):
            tweet = _tweet(request.url)
            if tweet == "5000":
                return _response(request.url, 403)
            return _response(request.url, 404) if tweet in gone else _response(request.url, 200, BODY)

        self.answer = answer
        rows = [("6000", 1, "photo", "bob"), ("6001", 1, "photo", "bob"), ("5000", 1, "photo", "carol")] + OLD[:4]
        with patch("luvbox.core.QUICK_STOP_AFTER", 3):
            for _ in range(REFUSED_LIMIT + 1):
                self.save(rows, downloader=HttpDownloader)
        self.assertEqual(self.listed_tweets(), ["5000", "6000", "6001"])
        refusals = unsaved.refusals("myaccount", self.options.output_dir)
        self.assertEqual(list(refusals.values()), [REFUSED_LIMIT])
        keys = self.listed_keys()
        # The refused one must not be last, or the old behaviour would also have served the others.
        self.assertLess(keys.index("5000_1"), len(keys) - 1, keys)
        gone.clear()
        tweets = {"5000": [(1, "photo")], "6000": [(1, "photo")], "6001": [(1, "photo")]}
        code, finished = self.retry(keys, tweets)
        self.assertEqual((code, finished["status"], finished["saved"]), (0, "finished", 2), finished["message"])
        self.assertEqual(sorted(test_missing_files.FakeTweetExtractor.requested), ["5000", "6000", "6001"])
        # The refused file stays listed, says why, and keeps the count that lets Like saves pass it by.
        self.assertEqual(self.listed_tweets(), ["5000"])
        self.assertEqual(self.detail("5000"), _REFUSED_RETRY_DETAIL)
        self.assertEqual(unsaved.refusals("myaccount", self.options.output_dir), refusals)
        self.assertIn("받지 못한 미디어 1개", finished["message"])

    def test_a_server_that_refuses_file_after_file_still_ends_the_retry(self):
        self.answer = lambda request: _response(request.url, 403)
        tweets = {str(tweet): [(1, "photo")] for tweet in range(5000, 5005)}
        code, finished = self.retry([f"{tweet}_1" for tweet in tweets], tweets)
        self.assertEqual((code, finished["status"]), (1, "error"))
        # The third refusal in a row ends it: the fourth and fifth tweets are never asked for.
        self.assertEqual(test_missing_files.FakeTweetExtractor.requested, ["5000", "5001", "5002"])
        self.assertEqual(unsaved.refusals("myaccount", self.options.output_dir), {})

    def test_a_file_between_two_refusals_starts_the_count_again(self):
        def answer(request):
            refused = _tweet(request.url) in {"5000", "5001", "5003", "5004"}
            return _response(request.url, 403) if refused else _response(request.url, 200, BODY)

        self.answer = answer
        tweets = {str(tweet): [(1, "photo")] for tweet in range(5000, 5005)}
        code, finished = self.retry([f"{tweet}_1" for tweet in tweets], tweets)
        self.assertEqual((code, finished["status"], finished["saved"]), (0, "finished", 1), finished["message"])
        self.assertEqual(test_missing_files.FakeTweetExtractor.requested, ["5000", "5001", "5002", "5003", "5004"])


class QuickSaveAfterAnEmptyRunTests(_Fixture):
    def offline(self):
        events = []
        error = requests.exceptions.ConnectionError("offline")
        with patch("gallery_dl.extractor.find", side_effect=lambda url: test_core.FakeLikesExtractor(url, [], error)), \
                patch("gallery_dl.downloader.find", return_value=QuietDownloader), \
                patch("requests.Session.send", side_effect=self.send):
            code = run_worker(self.quick, events.append)
        return code, events[-1]

    def store(self):
        paths = resolve_storage(self.options.output_dir, self.options.account, self.options.separate_accounts)
        return HistoryStore(paths.record_dir, media_dir=paths.media_dir, output_dir=paths.output_dir,
                            legacy_dir=paths.legacy_dir)

    def ended(self, store, status, **counts):
        """One more run of the same settings that ended this way; returns what a quick save may then do."""
        signature = store._db.execute("SELECT media_signature FROM runs WHERE status='finished' "
                                      "ORDER BY id DESC LIMIT 1").fetchone()[0]
        run = store.start_run(self.options.folder_mode, scan_mode="quick", media_signature=signature)
        store.finish_run(run, status, **counts)
        return store.quick_scan_readiness(self.options.folder_mode, signature)

    def test_a_save_that_read_no_like_leaves_the_next_save_quick(self):
        with patch("luvbox.core.QUICK_STOP_AFTER", 3):
            self.save(OLD)
            _code, finished, _events = self.save([("200", 1, "photo", "bob")] + OLD)
            self.assertEqual(finished["scan_mode"], "quick")
            code, finished = self.offline()
            self.assertEqual((code != 0, finished["status"], finished["visited"], finished["saved"]),
                             (True, "error", 0, 0))
            _code, finished, _events = self.save([("201", 1, "photo", "bob"), ("200", 1, "photo", "bob")] + OLD)
        self.assertEqual((finished["scan_mode"], finished["saved"]), ("quick", 1))
        self.assertLess(finished["visited"], len(OLD) + 2)

    def test_only_runs_that_read_nothing_are_passed_over(self):
        with patch("luvbox.core.QUICK_STOP_AFTER", 3):
            self.save(OLD)
        store = self.store()
        try:
            for status in ("error", "stopped", "limited"):
                self.assertEqual(self.ended(store, status), "ready", status)
            # A run that read or stored anything and then failed may have left gaps: full check.
            self.assertEqual(self.ended(store, "stopped", visited=3), "incomplete")
            self.assertEqual(self.ended(store, "error"), "incomplete")      # still behind the one above
            self.assertEqual(self.ended(store, "finished", visited=5), "ready")
            self.assertEqual(self.ended(store, "error", saved=1, visited=1), "incomplete")
        finally:
            store.close()


class AuthorIdThatLooksLikeADiagnosisTests(_Fixture):
    def test_one_missing_file_of_such_an_author_does_not_end_the_save(self):
        self.save(OLD[:4])
        for tweet, author in (("300", "permission_art"), ("301", "ratelimit_fan"), ("302", "bob")):
            QuietDownloader.missing = {tweet}
            with patch("luvbox.core.QUICK_STOP_AFTER", 3):
                code, finished, _events = self.save([(tweet, 1, "photo", author)] + OLD[:4])
            self.assertEqual((code, finished["status"]), (0, "finished"), (author, finished["message"]))
            self.assertFalse(finished.get("retry_at"), author)


if __name__ == "__main__":
    unittest.main()
