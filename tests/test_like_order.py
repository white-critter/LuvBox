"""Like order and first-seen periods, which X itself does not report."""

import math
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from tests import test_core
from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox import storage
from luvbox.history import HistoryError, HistoryStore


class LikeOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.account = Path(self.temp.name) / "account"
        self.store = HistoryStore(self.account)
        self.addCleanup(self.store.close)

    def walk(self, tweets, status="finished", *, track=True):
        """One run walking the Likes list (newest first)."""
        time.sleep(0.02)  # Windows' clock is coarse; real runs are far apart.
        run = self.store.start_run("flat")
        if track:
            order = self.store.like_order(run)
            for tweet in tweets:
                order.visit(tweet)
            order.flush()
        self.store.finish_run(run, status)
        return self.store._db.execute("SELECT started_at FROM runs WHERE id=?", (run,)).fetchone()[0]

    def likes(self):
        return {row[0]: dict(key=row[1], after=row[2], before=row[3])
                for row in self.store._db.execute("SELECT tweet_id,like_key,liked_after,liked_before FROM likes")}

    def order(self):
        return [row[0] for row in self.store._db.execute("SELECT tweet_id FROM likes ORDER BY like_key DESC")]

    def test_first_run_keeps_list_order_and_only_an_upper_bound(self):
        started = self.walk(["5", "4", "3", "2", "1"])
        self.assertEqual(self.order(), ["5", "4", "3", "2", "1"])
        self.assertEqual({value["after"] for value in self.likes().values()}, {None})
        self.assertEqual({value["before"] for value in self.likes().values()}, {started})

    def test_new_likes_on_top_were_liked_after_the_previous_finished_run(self):
        first = self.walk(["3", "2", "1"])
        second = self.walk(["5", "4", "3", "2"])
        stopped = self.walk(["9"], status="stopped")
        self.walk(["9", "7", "5"])
        self.assertEqual(self.order(), ["9", "7", "5", "4", "3", "2", "1"])
        likes = self.likes()
        self.assertEqual((likes["5"]["after"], likes["5"]["before"]), (first, second))
        # A stopped run is not a lower bound; the finished one before it is.
        self.assertEqual((likes["9"]["after"], likes["9"]["before"]), (second, stopped))
        # Found between known tweets: it may be an old Like that was hidden
        # before (e.g. a protected account), so no lower bound is claimed.
        self.assertIsNone(likes["7"]["after"])
        # Tweets already known keep their first period.
        self.assertIsNone(likes["3"]["after"])

    def test_tweets_found_between_or_below_known_ones_have_no_lower_bound(self):
        self.walk(["5", "4"], status="limited")
        self.walk(["5", "4", "3", "2"])
        self.walk(["5", "45", "4", "3", "25", "2", "1"])
        self.assertEqual(self.order(), ["5", "45", "4", "3", "25", "2", "1"])
        likes = self.likes()
        self.assertTrue(all(likes[tweet]["after"] is None for tweet in ("45", "25", "1", "3")))

    def test_a_stopped_run_places_its_tail_just_below_the_last_known_tweet(self):
        self.walk(["5", "4", "2", "1"])
        self.walk(["5", "4", "3"], status="stopped")
        self.assertEqual(self.order(), ["5", "4", "3", "2", "1"])

    def test_new_likes_stay_above_tweets_that_were_unliked(self):
        self.walk(["3", "2", "1"])
        self.walk(["4", "2", "1"])
        self.assertEqual(self.order()[:2], ["4", "3"])

    def test_repeated_backfills_keep_order_when_float_gaps_are_exhausted(self):
        self.walk(["900", "100"])
        first_seen = self.likes()["900"]
        previous = "100"
        inserted = []
        for index in range(60):
            tweet = str(200 + index)
            self.walk(["900", tweet, previous])
            inserted.insert(0, tweet)
            previous = tweet
        self.assertEqual(self.order(), ["900", *inserted, "100"])
        self.assertEqual(self.likes()["900"]["before"], first_seen["before"])
        self.assertEqual(self.likes()["900"]["after"], first_seen["after"])
        keys = [value["key"] for value in self.likes().values()]
        self.assertEqual(len(set(keys)), len(keys))

    def test_stopped_tail_keeps_its_place_when_neighboring_keys_are_adjacent(self):
        self.walk(["900", "100"])
        with self.store._db:
            self.store._db.execute("UPDATE likes SET like_key=? WHERE tweet_id='100'", (1024.0,))
            self.store._db.execute("UPDATE likes SET like_key=? WHERE tweet_id='900'",
                                   (math.nextafter(1024.0, math.inf),))
        self.walk(["900", "200"], status="stopped")
        self.assertEqual(self.order(), ["900", "200", "100"])
        keys = [value["key"] for value in self.likes().values()]
        self.assertEqual(len(set(keys)), len(keys))

    def test_runs_before_tracking_do_not_count_as_a_lower_bound(self):
        self.walk(["3"], track=False)
        self.walk(["3", "2"])
        self.assertIsNone(self.likes()["3"]["after"])

    def test_records_sort_by_like_order_and_filter_by_period(self):
        for tweet, day in (("100", 1), ("200", 2), ("300", 3), ("400", 4)):
            path = self.account / f"{tweet}_1.jpg"
            path.write_bytes(b"offline")
            self.store.record_media("flat", {"tweet_id": tweet, "num": 1, "type": "photo",
                                             "date": datetime(2026, 9, day, 3, tzinfo=timezone.utc)}, path)
        self.walk(["200", "400", "100"])   # 300 was never walked
        liked = [row["tweet_id"] for row in self.store.records(order="liked")]
        self.assertEqual(liked, ["200", "400", "100", "300"])
        self.assertEqual([row["tweet_id"] for row in self.store.records(order="posted")], ["400", "300", "200", "100"])
        self.assertEqual([row["tweet_id"] for row in self.store.records(order="posted_oldest")], ["100", "200", "300", "400"])
        self.assertIsNotNone(self.store.records(order="liked")[0]["liked_before"])
        page = self.store.record_page(order="liked", period=("posted", "2026-09-02", "2026-09-03"))
        self.assertEqual((page["total"], [row["tweet_id"] for row in page["rows"]]), (2, ["200", "300"]))
        today = datetime.now().astimezone().date().isoformat()
        self.assertEqual(self.store.record_page(period=("saved", today, ""))["total"], 4)
        self.assertEqual(self.store.record_page(period=("saved", "", "2000-01-01"))["total"], 0)
        for period in (("posted", "2026-13-01", ""), ("liked", "", ""), ("posted", "9/1", "")):
            with self.subTest(period=period), self.assertRaises(HistoryError):
                self.store.record_page(period=period)
        with self.assertRaises(HistoryError):
            self.store.records(order="random")
        exported = Path(self.temp.name) / "records.csv"
        self.store.export_records(exported)
        header, *rows = exported.read_text(encoding="utf-8-sig").splitlines()
        self.assertTrue(header.endswith("liked_after,liked_before,content,hashtags"))

    def test_record_relocation_carries_like_order(self):
        self.walk(["2", "1"])
        target = HistoryStore(Path(self.temp.name) / "moved")
        self.addCleanup(target.close)
        with closing(sqlite3.connect(self.store.path)) as source, closing(sqlite3.connect(target.path)) as moved:
            storage._merge_history(moved, source)
            moved.commit()
        rows = target._db.execute("SELECT tweet_id FROM likes ORDER BY like_key DESC").fetchall()
        self.assertEqual([row[0] for row in rows], ["2", "1"])


class LikeOrderWorkerTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    write_cookies = test_core.CoreTests.write_cookies
    worker = test_core.CoreTests.worker
    runs = test_core.CoreTests.runs

    def likes(self):
        with closing(sqlite3.connect(self.record_dir / ".luvbox-history.sqlite3")) as db:
            return [row for row in db.execute(
                "SELECT tweet_id,liked_after,liked_before FROM likes ORDER BY like_key DESC")]

    def test_each_run_records_like_order_including_tweets_not_saved(self):
        rows = [("300", 1, "photo", "alice"), ("200", 1, "video", "bob"), ("100", 1, "photo", "carol")]
        code, events = self.worker(rows)
        self.assertEqual(code, 0)
        first = self.runs()[0]["started_at"]
        # The video is not saved (videos are off), but its Like keeps its place.
        self.assertEqual([row[0] for row in self.likes()], ["300", "200", "100"])
        code, events = self.worker([("500", 1, "photo", "dave"), ("400", 1, "photo", "erin")] + rows)
        self.assertEqual(code, 0)
        second = self.runs()[1]["started_at"]
        likes = self.likes()
        self.assertEqual([row[0] for row in likes], ["500", "400", "300", "200", "100"])
        self.assertEqual(likes[0][1:], (first, second))
        self.assertEqual(likes[2][1:], (None, first))


if __name__ == "__main__":
    unittest.main()
