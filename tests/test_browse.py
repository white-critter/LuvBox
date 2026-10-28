"""Tweet text search, author filter, statistics."""

import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tests import test_core
from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox import storage
from luvbox.core import run_worker
from luvbox.history import HistoryError, HistoryStore


class BrowseCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.account = Path(self.temp.name) / "account"
        self.store = HistoryStore(self.account)
        self.addCleanup(self.store.close)

    def media(self, tweet, author, month=9, day=1, kind="photo", number=1):
        path = self.account / f"{tweet}_{number}.jpg"
        path.write_bytes(b"offline")
        self.store.record_media("flat", {"tweet_id": tweet, "num": number, "type": kind, "author": {"name": author},
                                         "date": datetime(2026, month, day, 3, tzinfo=timezone.utc)}, path)

    def ids(self, **filters):
        return sorted({row["tweet_id"] for row in self.store.record_page(**filters)["rows"]})

    def test_text_and_hashtags_are_searchable_and_shown(self):
        self.media("100", "alice")
        self.media("200", "bob")
        self.store.record_tweets([("100", "오늘의\n고양이 사진 \x07", ["cat", "고양이"]), ("200", "", []),
                                  ("bad", "ignored", [])])
        self.assertEqual(self.ids(query="고양이 사진"), ["100"])
        self.assertEqual(self.ids(query="#cat"), ["100"])
        row = next(row for row in self.store.records() if row["tweet_id"] == "100")
        self.assertEqual((row["content"], row["hashtags"]), ("오늘의 고양이 사진", "#cat #고양이"))
        # Later runs replace the text with the latest one.
        self.store.record_tweets([("100", "수정된 글", [])])
        self.assertEqual(self.ids(query="고양이 사진"), [])
        self.assertEqual(self.ids(query="수정된"), ["100"])

    def test_exact_author_filter_ignores_letter_case_but_not_partial_names(self):
        self.media("100", "Alice")
        self.media("200", "alice_two")
        self.media("300", "bob")
        self.assertEqual(self.ids(author="alice"), ["100"])
        self.assertEqual(self.ids(query="alice"), ["100", "200"])
        self.assertEqual(self.store.record_page(author="ALICE")["total"], 1)
        for bad in ("a b", "x" * 16, "@alice"):
            with self.subTest(bad=bad), self.assertRaises(HistoryError):
                self.store.record_page(author=bad)

    def test_statistics_rank_authors_and_count_months(self):
        for index in range(3):
            self.media(str(100 + index), "Alice", month=9, day=index + 1)
        self.media("200", "alice", month=8)
        self.media("300", "bob", month=8)
        self.media("300", "bob", month=8, number=2)
        stats = self.store.statistics()
        top = stats["authors"][0]
        self.assertEqual((top["author"].lower(), top["files"], top["posts"]), ("alice", 4, 4))
        self.assertEqual((stats["authors"][1]["author"], stats["authors"][1]["files"], stats["authors"][1]["posts"]),
                         ("bob", 2, 1))
        self.assertEqual(stats["months"]["posted"], [("2026-08", 3), ("2026-09", 3)])
        self.assertEqual(sum(count for _, count in stats["months"]["saved"]), 6)
        self.assertEqual(stats["months"]["liked"], [])
        run = self.store.start_run("flat")
        self.store.finish_run(run, "finished")
        time.sleep(0.02)  # Windows' clock is coarse; real runs are far apart.
        order = self.store.like_order(self.store.start_run("flat"))
        for tweet in ("900", "100"):
            order.visit(tweet)
        order.flush()
        liked = self.store.statistics()["months"]["liked"]
        self.assertEqual(sum(count for _, count in liked), 0)   # no earlier tracked run yet

    def test_relocation_carries_tweet_text(self):
        self.store.record_tweets([("100", "글", ["tag"])])
        target = HistoryStore(Path(self.temp.name) / "moved")
        self.addCleanup(target.close)
        with closing(sqlite3.connect(self.store.path)) as source, closing(sqlite3.connect(target.path)) as moved:
            storage._merge_history(moved, source)
            moved.commit()
        self.assertEqual(tuple(target._db.execute("SELECT content,hashtags FROM tweets").fetchone()), ("글", "#tag"))




class TweetTextWorkerTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    set_session = test_core.CoreTests.set_session

    def test_worker_keeps_the_text_of_every_walked_tweet(self):
        class TextExtractor(test_core.FakeLikesExtractor):
            def items(self):
                for message in super().items():
                    if message[0] == test_core.Message.Directory:
                        message[2]["content"] = f"글 {message[2]['tweet_id']}\n두 줄"
                        message[2]["hashtags"] = ["tag"]
                    yield message

        rows = [("100", 1, "photo", "alice"), ("200", 1, "video", "bob")]
        events = []
        with patch("gallery_dl.extractor.find", side_effect=lambda url: TextExtractor(url, rows)), \
                patch("gallery_dl.downloader.find", return_value=test_core.FakeHttpDownloader):
            code = run_worker(self.options, events.append)
        self.assertEqual(code, 0)
        self.assertNotIn(test_core.SECRET, json.dumps(events))
        with closing(sqlite3.connect(self.record_dir / ".luvbox-history.sqlite3")) as db:
            texts = dict(db.execute("SELECT tweet_id,content FROM tweets"))
        # The video is not saved (videos are off), but its text is kept.
        self.assertEqual(texts, {"100": "글 100 두 줄", "200": "글 200 두 줄"})


if __name__ == "__main__":
    unittest.main()
