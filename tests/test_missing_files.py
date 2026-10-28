"""Missing files: counted once per media, kept deleted on request, or downloaded again by tweet."""

import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from gallery_dl import exception
from gallery_dl.extractor.common import Extractor
from gallery_dl.extractor.message import Message

from tests import test_core
from tests.test_core import SECRET, FakeHttpDownloader
from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox.core import JobOptions, run_worker
from luvbox.history import HistoryStore, archive_key
from luvbox.recovery import rebuild_history
from luvbox.storage import _merge_history


ARCHIVE = ".luvbox-archive.flat.sqlite3"


def photo(store, mode, tweet, number, path, author="alice"):
    store.record_media(mode, {"tweet_id": tweet, "num": number, "type": "photo", "author": {"name": author}}, path)


class MissingRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "account"
        self.store = HistoryStore(self.folder)
        self.addCleanup(self.store.close)

    def file(self, name):
        path = self.folder / name
        path.write_bytes(b"photo")
        return path

    def test_media_is_missing_once_whatever_layouts_recorded_it(self):
        kept = self.file("100_1.jpg")
        photo(self.store, "flat", "100", 1, kept)
        (self.folder / "alice").mkdir()
        old = self.file("alice/100_1.jpg")
        photo(self.store, "author", "100", 1, old)
        old.unlink()                      # moved to the flat layout: not missing
        gone = self.file("200_1.jpg")
        photo(self.store, "flat", "200", 1, gone, author="bob")
        (self.folder / "alice" / "200_1.jpg").write_bytes(b"photo")
        photo(self.store, "author", "200", 1, self.folder / "alice" / "200_1.jpg", author="bob")
        gone.unlink()
        (self.folder / "alice" / "200_1.jpg").unlink()
        self.store.import_legacy(refresh=True)
        summary = self.store.overview()
        self.assertEqual((summary["missing_files"], summary["unmatched_records"]), (1, 3))
        [item] = summary["missing"]
        self.assertEqual((item["tweet_id"], item["author"], item["folder_modes"]), ("200", "bob", ["author", "flat"]))
        self.assertEqual(item["archive_key"], "200_1")
        self.assertFalse(item["ignored"])
        self.assertEqual(self.store.missing_media(), summary["missing"])

    def test_choice_to_keep_a_file_deleted_hides_it_and_can_be_undone(self):
        path = self.file("300_2.jpg")
        photo(self.store, "flat", "300", 2, path)
        path.unlink()
        self.store.import_legacy(refresh=True)
        self.store.set_missing_ignored([("300", 2, "photo")], True)
        summary = self.store.overview()
        self.assertEqual((summary["missing_files"], summary["ignored_missing"]), (0, 1))
        self.assertTrue(summary["missing"][0]["ignored"])
        self.assertEqual(self.store.ignored_media(), {("300", 2, "photo")})
        self.store.set_missing_ignored([("300", 2, "photo")], False)
        self.assertEqual((self.store.summary()["missing_files"], self.store.ignored_media()), (1, set()))
        for bad in ([("x", 1, "photo")], [("1", 0, "photo")], [("1", 1, "card")]):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                self.store.set_missing_ignored(bad, True)

    def test_archive_keys_match_the_worker(self):
        self.assertEqual([archive_key("1", 2, kind) for kind in ("photo", "video", "animated_gif")],
                         ["1_2", "1_video_2", "1_animated_gif_2"])

    def test_newest_choice_wins_when_another_pc_is_merged_and_recovery_keeps_choices(self):
        self.store.set_missing_ignored([("1", 1, "photo"), ("2", 1, "photo")], True)
        other_folder = Path(self.temp.name) / "other"
        with closing(HistoryStore(other_folder)) as other:
            other.set_missing_ignored([("1", 1, "photo")], False)      # later: download again
            with closing(sqlite3.connect(other.path)) as db:
                db.execute("UPDATE missing_choices SET updated_at='2000-01-01' WHERE tweet_id='2'")
                db.execute("INSERT INTO missing_choices VALUES ('2',1,'photo',0,'2000-01-01')")
                db.commit()
        with closing(sqlite3.connect(self.store.path)) as target, \
                closing(sqlite3.connect(other_folder / ".luvbox-history.sqlite3")) as source:
            with target:
                _merge_history(target, source)
        self.assertEqual(self.store.ignored_media(), {("2", 1, "photo")})
        rebuilt = Path(self.temp.name) / "rebuilt.sqlite3"
        result = rebuild_history(self.store.path, rebuilt)
        self.assertEqual(result["counts"]["missing_choices"], 2)

    def test_retry_runs_never_count_as_a_complete_like_scan(self):
        first = self.store.start_run("flat", media_signature="v1")
        self.store.finish_run(first, "stopped")
        retry = self.store.start_run("flat", scan_mode="retry", media_signature="v1")
        self.store.finish_run(retry, "finished")
        self.assertEqual(self.store.quick_scan_readiness("flat", "v1"), "incomplete")
        full = self.store.start_run("flat", media_signature="v1")
        self.store.like_order(full)       # Like tracking starts with this run
        self.store.finish_run(full, "finished")
        retry = self.store.start_run("flat", scan_mode="retry", media_signature="v1")
        self.store.finish_run(retry, "finished")
        latest = self.store.start_run("flat", media_signature="v1")
        order = self.store.like_order(latest)
        with closing(sqlite3.connect(self.store.path)) as db:
            started = db.execute("SELECT started_at FROM runs WHERE id=?", (full,)).fetchone()[0]
        self.assertEqual(order._after, started)


class FakeTweetExtractor(Extractor):
    """One tweet by ID, as gallery-dl's tweet extractor yields it."""
    category = "twitter"
    subcategory = "tweet"
    cookies_domain = ".x.com"
    filename_fmt = "{tweet_id}_{num}.{extension}"
    archive_fmt = "{tweet_id}_{num}"
    requested = []

    def __init__(self, url, tweets, unavailable=(), error=None):
        match = re.match(r"https://x\.com/i/web/status/([0-9]+)\Z", url)
        super().__init__(match)
        self.tweet_id = match[1]
        self.tweets, self.unavailable, self.error = tweets, unavailable, error
        FakeTweetExtractor.requested.append(self.tweet_id)

    def items(self):
        if self.error is not None:
            raise self.error
        if self.tweet_id in self.unavailable:
            raise exception.AbortExtraction("'Unavailable'")
        rows = self.tweets.get(self.tweet_id, ())
        for number, kind in rows:
            metadata = {"tweet_id": self.tweet_id, "num": number, "type": kind,
                        "extension": "mp4" if kind != "photo" else "jpg", "author": {"name": "alice"},
                        "content": "text " + self.tweet_id}
            yield Message.Directory, "", metadata
            url = (f"https://video.twimg.com/{self.tweet_id}_{number}.mp4" if kind != "photo"
                   else f"https://pbs.twimg.com/media/{self.tweet_id}_{number}?format=jpg&name=orig")
            yield Message.Url, url, metadata


class RetryWorkerTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    set_session = test_core.CoreTests.set_session
    worker = test_core.CoreTests.worker
    runs = test_core.CoreTests.runs

    def flat(self, **values):
        return replace(self.options, folder_mode="flat", separate_accounts=False, **values)

    def retry(self, targets, tweets, *, unavailable=(), error=None, stop_after=None):
        FakeTweetExtractor.requested = []
        stop_file = self.base / "stop.flag"
        stop_file.unlink(missing_ok=True)

        def extractor(url):
            if stop_after is not None and len(FakeTweetExtractor.requested) >= stop_after:
                stop_file.touch()
            return FakeTweetExtractor(url, tweets, unavailable, error)

        events = []
        with patch("gallery_dl.extractor.find", side_effect=extractor), \
                patch("gallery_dl.downloader.find", return_value=FakeHttpDownloader):
            result = run_worker(self.flat(targets=tuple(targets)), events.append, stop_file)
        self.assertNotIn(SECRET, json.dumps(events))
        self.assertEqual(events[-1]["type"], "finished")
        return result, events

    def entries(self):
        with closing(sqlite3.connect(self.record_dir / ARCHIVE)) as db:
            return {row[0] for row in db.execute("SELECT entry FROM archive")}

    def saved_then_deleted(self):
        rows = [("100", 1, "photo", "alice"), ("100", 2, "photo", "alice"), ("200", 1, "photo", "bob"),
                ("300", 1, "photo", "carol")]
        code, _ = self.worker(rows, options=self.flat())
        self.assertEqual(code, 0)
        for name in ("alice_100_1.jpg", "bob_200_1.jpg", "carol_300_1.jpg"):
            (self.output / name).unlink()
        FakeHttpDownloader.downloaded = []

    def test_chosen_files_come_back_one_request_per_tweet(self):
        self.saved_then_deleted()
        tweets = {"100": [(1, "photo"), (2, "photo")], "300": [(1, "photo")]}
        code, events = self.retry(["100_1", "200_1", "300_1"], tweets, unavailable={"200"})
        self.assertEqual(code, 0)
        self.assertEqual(FakeTweetExtractor.requested, ["100", "200", "300"])
        # Only the chosen files; 100_2 is present and was never requested.
        self.assertEqual(sorted(FakeHttpDownloader.downloaded),
                         ["https://pbs.twimg.com/media/100_1?format=jpg&name=orig",
                          "https://pbs.twimg.com/media/300_1?format=jpg&name=orig"])
        self.assertTrue((self.output / "alice_100_1.jpg").is_file() and (self.output / "alice_300_1.jpg").is_file())
        final = events[-1]
        self.assertEqual((final["status"], final["saved"], final["recovered"], final["unavailable_tweets"]),
                         ("finished", 2, 2, 1))
        self.assertEqual(final["scan_mode"], "retry")
        self.assertIn("볼 수 없는 트윗 1개", final["message"])
        self.assertFalse([event for event in events if event.get("level") in {"error", "warning"}])
        notice = next(event for event in events if event["type"] == "log" and "status/200" in event["message"])
        self.assertIn("볼 수 없어", notice["message"])
        # The unavailable tweet keeps its duplicate record; nothing else changed.
        self.assertEqual(self.entries(), {"twitter100_1", "twitter100_2", "twitter200_1", "twitter300_1"})
        with closing(HistoryStore.for_storage(test_core.resolve_storage(self.output, "myaccount"))) as store:
            self.assertEqual([item["tweet_id"] for item in store.missing_media()], ["200"])
        self.assertEqual(self.runs()[-1]["scan_mode"], "retry")
        # A retry is not a complete Like scan, so a quick scan still checks all.
        with closing(HistoryStore.for_storage(test_core.resolve_storage(self.output, "myaccount"))) as store:
            self.assertEqual(store.quick_scan_readiness("flat", self.flat().validate().media_signature), "ready")
            self.assertEqual(store._db.execute("SELECT COUNT(*) FROM likes").fetchone()[0], 3)

    def test_stop_leaves_unprocessed_choices_untouched(self):
        self.saved_then_deleted()
        tweets = {"100": [(1, "photo"), (2, "photo")], "200": [(1, "photo")]}
        code, events = self.retry(["100_1", "200_1"], tweets, stop_after=1)
        self.assertEqual((code, events[-1]["status"]), (0, "stopped"))
        # The stop is noticed before the second tweet sends any request.
        self.assertEqual(FakeHttpDownloader.downloaded, ["https://pbs.twimg.com/media/100_1?format=jpg&name=orig"])
        self.assertIn("twitter200_1", self.entries())
        self.assertFalse((self.output / "200_1.jpg").exists())

    def test_retry_keeps_archive_entries_even_during_download_and_constructor_failure(self):
        self.saved_then_deleted()
        before = self.entries()
        observed = []
        original = FakeHttpDownloader.download

        def download(downloader, url, pathfmt):
            observed.append(self.entries())
            return original(downloader, url, pathfmt)

        with patch.object(FakeHttpDownloader, "download", download):
            code, events = self.retry(["100_1"], {"100": [(1, "photo")]})
        self.assertEqual((code, events[-1]["status"]), (0, "finished"))
        self.assertEqual(observed, [before])
        self.assertEqual(self.entries(), before)

        def unavailable_extractor(url):
            self.assertEqual(self.entries(), before)
            raise RuntimeError("extractor initialization failed")

        with patch("gallery_dl.extractor.find", side_effect=unavailable_extractor):
            events = []
            code = run_worker(self.flat(targets=("200_1",)), events.append)
        self.assertEqual((code, events[-1]["status"]), (1, "error"))
        self.assertEqual(self.entries(), before)

    def test_rate_limit_ends_the_run_and_restores_the_archive(self):
        self.saved_then_deleted()
        code, events = self.retry(["100_1", "200_1"], {}, error=exception.AbortExtraction("429 rate limit"))
        self.assertEqual((code, events[-1]["status"]), (1, "error"))
        self.assertEqual(FakeTweetExtractor.requested, ["100"])
        self.assertIn("요청 한도", events[-1]["message"])
        self.assertTrue({"twitter100_1", "twitter200_1"} <= self.entries())

    def test_files_kept_deleted_are_skipped_in_every_layout(self):
        self.saved_then_deleted()
        with closing(HistoryStore.for_storage(test_core.resolve_storage(self.output, "myaccount"))) as store:
            store.set_missing_ignored([("100", 1, "photo")], True)
        rows = [("100", 1, "photo", "alice"), ("100", 2, "photo", "alice")]
        # The author layout has its own archive without these files.
        code, events = self.worker(rows, options=replace(self.flat(), folder_mode="author"))
        self.assertEqual(code, 0)
        self.assertEqual(FakeHttpDownloader.downloaded, ["https://pbs.twimg.com/media/100_2?format=jpg&name=orig"])
        self.assertFalse((self.output / "alice" / "100_1.jpg").exists())

    def test_hidden_file_chosen_again_is_shown_again_once_back(self):
        self.saved_then_deleted()
        with closing(HistoryStore.for_storage(test_core.resolve_storage(self.output, "myaccount"))) as store:
            store.set_missing_ignored([("100", 1, "photo"), ("200", 1, "photo")], True)
        code, events = self.retry(["100_1", "200_1"], {"100": [(1, "photo")]}, unavailable={"200"})
        self.assertEqual((code, events[-1]["recovered"]), (0, 1))
        with closing(HistoryStore.for_storage(test_core.resolve_storage(self.output, "myaccount"))) as store:
            self.assertEqual(store.ignored_media(), {("200", 1, "photo")})

    def test_targets_are_validated(self):
        for targets in (["x_1"], ["1_0"], ["1_card_1"], "1_1", ["1_1"] * 0 + ["1"]):
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                self.flat(targets=targets).validate()
        normalized = self.flat(targets=["5_1", "5_1", "6_video_2"]).validate()
        self.assertEqual(normalized.targets, ("5_1", "6_video_2"))
        with patch("luvbox.core.RETRY_LIMIT", 1), self.assertRaises(ValueError):
            self.flat(targets=["5_1", "6_1"]).validate()
        self.assertEqual(JobOptions("me", str(self.output)).targets, ())






if __name__ == "__main__":
    unittest.main()
