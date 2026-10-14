"""Manual preview decisions against the real gallery job and archive pipeline."""

import json
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from gallery_dl.extractor.message import Message
from PIL import Image

import test_core
from luvbox import core


class Review:
    def __init__(self, choices=("save",), *, enabled=True, on_ask=None):
        self.active = enabled
        self.choices = iter(choices)
        self.on_ask = on_ask
        self.requests = []

    def enabled(self):
        return self.active

    def ask(self, path, media, check_stop):
        self.requests.append((path, media.copy()))
        if self.on_ask:
            self.on_ask(path, media, check_stop)
        return next(self.choices)


class InteractiveSaveTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    write_cookies = test_core.CoreTests.write_cookies
    runs = test_core.CoreTests.runs

    def worker(self, rows=(), *, options=None, review=None, before_row=None, stop_file=None, extractor=None):
        events = []
        with patch("gallery_dl.extractor.find", side_effect=lambda url:
                   (extractor or test_core.FakeLikesExtractor)(url, rows, before_row=before_row)), \
                patch("gallery_dl.downloader.find", return_value=test_core.FakeHttpDownloader):
            result = core.run_worker(options or self.options, events.append, stop_file, review=review)
        self.assertNotIn(test_core.SECRET, json.dumps(events))
        self.assertEqual(events[-1]["type"], "finished")
        return result, events

    def records(self):
        with closing(sqlite3.connect(self.record_dir / ".luvbox-history.sqlite3")) as database:
            database.row_factory = sqlite3.Row
            return [dict(row) for row in database.execute("SELECT * FROM media")]

    def keys(self):
        with closing(sqlite3.connect(self.record_dir / ".luvbox-archive.sqlite3")) as database:
            return {row[0] for row in database.execute("SELECT entry FROM archive")}

    def options_all(self, **changes):
        return replace(self.options, include_videos=True, include_gifs=True, **changes)

    def convert(self, source, **kwargs):
        self.assertTrue(source.name.endswith(".mp4.part"))
        self.assertTrue(core._transfer_complete(source))
        kwargs["check_stop"]()
        target = source.parent / ".test-review.gif"
        Image.new("RGB", (10, 10), "green").save(target, "GIF")
        return target

    def test_confirmation_setting_accepts_only_bool(self):
        self.assertFalse(self.options.validate().confirm_each)
        self.assertTrue(replace(self.options, confirm_each=True).validate().confirm_each)
        for invalid in (None, 1, "true"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                replace(self.options, confirm_each=invalid).validate()

    def test_confirmation_without_manual_channel_stops_before_request_or_output(self):
        with patch("gallery_dl.extractor.find") as find:
            result, events = self.worker(options=replace(self.options, confirm_each=True))
        find.assert_not_called()
        self.assertEqual((result, events[-1]["saved"], events[-1]["status"]), (1, 0, "error"))
        self.assertIn("프로그램에서 직접", events[-1]["message"])
        self.assertFalse(self.output.exists())

    def test_photo_is_previewed_as_temporary_before_save_metadata_and_archive(self):
        def preview(path, media, check_stop):
            self.assertTrue(path.is_file())
            self.assertTrue(path.name.endswith(".jpg.part"))
            self.assertFalse(Path(str(path)[:-5]).exists())
            self.embed_source.assert_not_called()
            self.assertEqual(self.keys(), set())
            self.assertEqual(media["type"], "photo")
            self.assertEqual(media["source_url"], "https://x.com/i/web/status/100")
            self.assertEqual(media["author"], "alice")
            self.assertNotIn("user", media)
            check_stop()

        review = Review(on_ask=preview)
        result, events = self.worker([("100", 1, "photo", "alice")], review=review,
                                     options=replace(self.options, confirm_each=True))
        self.assertEqual((result, events[-1]["saved"], events[-1]["review_skipped"]), (0, 1, 0))
        self.assertEqual(self.keys(), {"twitter100_1"})
        self.assertEqual(self.records()[0]["status"], "saved")
        self.assertFalse(list(self.output.rglob("*.part")))
        self.embed_source.assert_called_once()

    def test_skip_does_not_archive_record_success_or_try_fallback_and_next_attachment_saves(self):
        class FallbackExtractor(test_core.FakeLikesExtractor):
            def items(self):
                for item in super().items():
                    if item[0] == Message.Url:
                        item[2]["_fallback"] = ["https://pbs.twimg.com/media/fallback?format=jpg&name=large"]
                    yield item

        review = Review(("skip", "save"))
        result, events = self.worker([("100", 1, "photo", "alice"), ("100", 2, "photo", "alice")],
                                     review=review, extractor=FallbackExtractor)
        done = events[-1]
        self.assertEqual((result, done["saved"], done["skipped"], done["size_skipped"], done["review_skipped"]),
                         (0, 1, 0, 0, 1))
        self.assertEqual(self.keys(), {"twitter100_2"})
        self.assertEqual([row["num"] for row in self.records()], [2])
        self.assertEqual(len(test_core.FakeHttpDownloader.downloaded), 2)
        self.assertEqual(len(review.requests), 2)
        self.assertEqual([event["num"] for event in events if event["type"] == "review-skipped"], [1])
        self.assertEqual(self.runs()[-1]["status"], "finished")
        self.assertEqual(self.pending(), {("100", 1, "photo")})
        self.assertFalse(list(self.output.rglob("*.part*")))

    def test_all_declined_is_a_successful_choice_run_and_next_run_asks_again(self):
        rows = [("100", 1, "photo", "alice")]
        first = Review(("skip",))
        result, events = self.worker(rows, review=first)
        self.assertEqual((result, events[-1]["status"], events[-1]["saved"], events[-1]["review_skipped"]),
                         (0, "finished", 0, 1))
        self.assertEqual(self.keys(), set())
        self.assertEqual(self.records(), [])
        second = Review(("save",))
        result, events = self.worker(rows, review=second, options=replace(self.options, scan_mode="quick"))
        self.assertEqual((result, events[-1]["scan_mode"], events[-1]["saved"]), (0, "quick", 1))
        self.assertEqual(len(second.requests), 1)
        self.assertEqual(len(test_core.FakeHttpDownloader.downloaded), 2)

    def test_declined_old_like_after_saved_streak_is_revisited_by_next_quick_run(self):
        # The quick run continues past the saved-file streak until it reaches
        # the file refused in the previous run, without scanning everything.
        known = [(str(number), 1, "photo", "alice") for number in range(150, 100, -1)]
        with patch("luvbox.core.QUICK_STOP_AFTER", 4):
            self.worker(known)
            older = ("99", 1, "photo", "alice")
            first = Review(("skip",))
            result, events = self.worker(known + [older], review=first)
            self.assertEqual((result, events[-1]["review_skipped"]), (0, 1))
            second = Review(("save",))
            result, events = self.worker(known + [older], review=second,
                                         options=replace(self.options, scan_mode="quick"))
        self.assertEqual((result, events[-1]["scan_mode"], events[-1]["saved"]), (0, "quick", 1))
        self.assertEqual(second.requests[0][1]["tweet_id"], "99")

    def pending(self):
        with closing(sqlite3.connect(self.record_dir / ".luvbox-history.sqlite3")) as database:
            row = database.execute("SELECT value FROM catalog_meta WHERE key='review_pending'").fetchone()
        return {tuple(item) for item in json.loads(row[0])} if row else set()

    def test_skipped_file_keeps_quick_runs_quick_and_is_forgotten_once_saved(self):
        # A skip must not turn every later quick run into a full scan: only
        # reaching the remembered file is required before the usual stop.
        known = [(str(number), 1, "photo", "alice") for number in range(150, 100, -1)]
        older = ("99", 1, "photo", "alice")
        quick = replace(self.options, scan_mode="quick")
        with patch("luvbox.core.QUICK_STOP_AFTER", 4):
            self.worker(known)
            self.worker(known + [older], review=Review(("skip",)))
            self.assertEqual(self.pending(), {("99", 1, "photo")})
            again = Review(("skip",))
            result, events = self.worker(known + [older], review=again, options=quick)
            self.assertEqual((result, events[-1]["scan_mode"], events[-1]["review_skipped"]), (0, "quick", 1))
            self.assertIn("건너뛴 파일 1개", next(event["message"] for event in events
                                              if event.get("type") == "log" and "새 Like만" in event.get("message", "")))
            self.assertEqual(self.pending(), {("99", 1, "photo")})
            # An unattended run (no review channel) saves it as before.
            result, events = self.worker(known + [older], options=quick)
            self.assertEqual((result, events[-1]["scan_mode"], events[-1]["saved"]), (0, "quick", 1))
            self.assertEqual(self.pending(), set())
            result, events = self.worker(known + [older], options=quick)
        self.assertEqual((result, events[-1]["scan_mode"], events[-1]["saved"]), (0, "quick", 0))
        self.assertEqual(events[-1]["visited"], 4)
        self.assertEqual(self.runs()[-1]["status"], "finished")

    def test_skipped_file_no_longer_liked_is_forgotten_after_a_complete_pass(self):
        known = [(str(number), 1, "photo", "alice") for number in range(110, 100, -1)]
        quick = replace(self.options, scan_mode="quick")
        with patch("luvbox.core.QUICK_STOP_AFTER", 3):
            self.worker(known + [("99", 1, "photo", "alice")], review=Review(["save"] * 10 + ["skip"]))
            self.assertEqual(self.pending(), {("99", 1, "photo")})
            # The Like was removed: the run looks through the list once.
            result, events = self.worker(known, options=quick)
            self.assertEqual((result, events[-1]["scan_mode"], events[-1]["visited"]), (0, "quick", 10))
            self.assertEqual(self.pending(), set())
            result, events = self.worker(known, options=quick)
        self.assertEqual((events[-1]["scan_mode"], events[-1]["visited"]), ("quick", 3))

    def test_stopped_run_keeps_skipped_files_it_did_not_reach(self):
        known = [(str(number), 1, "photo", "alice") for number in range(110, 100, -1)]
        self.worker(known + [("99", 1, "photo", "alice")], review=Review(["save"] * 10 + ["skip"]))
        stop_file = self.base / "stop.flag"

        def before_row(extractor, row):
            if row[0] == "105":
                stop_file.touch()

        result, events = self.worker(known + [("99", 1, "photo", "alice")], before_row=before_row,
                                     stop_file=stop_file, options=replace(self.options, scan_mode="quick"))
        self.assertEqual(events[-1]["status"], "stopped")
        self.assertEqual(self.pending(), {("99", 1, "photo")})

    def test_damaged_skip_memory_makes_the_quick_run_check_everything(self):
        rows = [("100", 1, "photo", "alice")]
        self.worker(rows)
        with closing(sqlite3.connect(self.record_dir / ".luvbox-history.sqlite3")) as database, database:
            database.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('review_pending','not json')")
        result, events = self.worker(rows, options=replace(self.options, scan_mode="quick"))
        self.assertEqual((result, events[-1]["scan_mode"]), (0, "full"))
        self.assertEqual(self.pending(), set())
        result, events = self.worker(rows, options=replace(self.options, scan_mode="quick"))
        self.assertEqual(events[-1]["scan_mode"], "quick")

    def test_merged_same_account_skip_memory_keeps_both_old_choices_reachable(self):
        from luvbox.history import HistoryStore
        from luvbox.storage import _merge_database

        known = [(str(number), 1, "photo", "alice") for number in (*range(150, 144, -1), *range(120, 114, -1))]
        older = ("99", 1, "photo", "alice")
        other = ("98", 1, "photo", "alice")
        self.worker(known + [older], review=Review(["save"] * len(known) + ["skip"]))
        peer_dir = self.base / "peer-records"
        with closing(HistoryStore(peer_dir, media_dir=self.output, output_dir=self.output)) as peer:
            peer.set_review_pending({("98", 1, "photo")})
            run = peer.start_run(self.options.folder_mode, media_signature=self.options.validate().media_signature)
            peer.finish_run(run, "finished")
        _merge_database(".luvbox-history.sqlite3", self.record_dir / ".luvbox-history.sqlite3",
                        peer_dir / ".luvbox-history.sqlite3")
        self.assertEqual(self.pending(), {("99", 1, "photo"), ("98", 1, "photo")})
        review = Review(("skip", "save"))
        with patch("luvbox.core.QUICK_STOP_AFTER", 4):
            result, events = self.worker(known[:6] + [older] + known[6:] + [other], review=review,
                                         options=replace(self.options, scan_mode="quick"))
        self.assertEqual((result, events[-1]["scan_mode"], events[-1]["saved"]), (0, "quick", 1))
        self.assertEqual([media["tweet_id"] for _path, media in review.requests], ["99", "98"])
        self.assertEqual(self.pending(), {("99", 1, "photo")})

    def test_damaged_skip_memory_in_same_account_merge_still_requires_full_scan(self):
        from luvbox.history import HistoryStore
        from luvbox.storage import _merge_database

        rows = [("100", 1, "photo", "alice")]
        self.worker(rows)
        store = self.store()
        try:
            store.set_review_pending({("99", 1, "photo")})
        finally:
            store.close()
        peer_dir = self.base / "peer-records"
        with closing(HistoryStore(peer_dir, media_dir=self.output, output_dir=self.output)) as peer:
            with peer._db:
                peer._db.execute("INSERT INTO catalog_meta(key,value) VALUES ('review_pending','damaged')")
        _merge_database(".luvbox-history.sqlite3", self.record_dir / ".luvbox-history.sqlite3",
                        peer_dir / ".luvbox-history.sqlite3")
        result, events = self.worker(rows, options=replace(self.options, scan_mode="quick"))
        self.assertEqual((result, events[-1]["scan_mode"]), (0, "full"))
        self.assertEqual(self.pending(), set())

    def store(self):
        from luvbox.history import HistoryStore
        from luvbox.storage import resolve_storage
        return HistoryStore.for_storage(resolve_storage(self.output, "myaccount"))

    def retry(self, targets, rows, review=None):
        from test_missing_files import FakeTweetExtractor
        tweets = {}
        for tweet, number, kind, _author in rows:
            tweets.setdefault(tweet, []).append((number, kind))
        events = []
        with patch("gallery_dl.extractor.find", side_effect=lambda url: FakeTweetExtractor(url, tweets)), \
                patch("gallery_dl.downloader.find", return_value=test_core.FakeHttpDownloader):
            result = core.run_worker(replace(self.options, targets=tuple(targets)), events.append, review=review)
        self.assertNotIn(test_core.SECRET, json.dumps(events))
        return result, events

    def test_never_is_kept_like_a_hidden_deleted_file_and_is_not_asked_again(self):
        rows = [("100", 1, "photo", "alice"), ("101", 1, "photo", "bob")]
        recorder = []
        with patch("luvbox.thumbcache.Recorder.add",
                   side_effect=lambda key, path, suffix=None: recorder.append((key, Path(path).is_file(), suffix))):
            result, events = self.worker(rows, review=Review(("never", "save")))
        done = events[-1]
        self.assertEqual((result, done["status"], done["saved"], done["review_skipped"], done["review_never"]),
                         (0, "finished", 1, 0, 1))
        self.assertIn("‘저장 안 함’으로 고른 파일 1개", done["message"])
        self.assertEqual([event["tweet_id"] for event in events if event["type"] == "review-never"], ["100"])
        # A small preview is kept from the temporary file before it is removed.
        self.assertIn((("100", 1, "photo"), True, ".jpg"), recorder)
        self.assertFalse(list(self.output.rglob("*.part*")))
        self.assertFalse(list(self.output.rglob("100_1.jpg")))
        self.assertEqual(self.keys(), {"twitter100_1", "twitter101_1"})
        declined = next(row for row in self.records() if row["tweet_id"] == "100")
        self.assertEqual((declined["path"], declined["record_source"], declined["author"]), ("", "declined", "alice"))
        self.assertTrue(declined["last_path"].endswith("100_1.jpg") and declined["missing_since"])
        self.assertEqual(self.pending(), set())
        store = self.store()
        try:
            self.assertIn(("100", 1, "photo"), store.ignored_media())
            [item] = store.missing_media()
            store.import_legacy(refresh=True)
            [again] = store.missing_media()
        finally:
            store.close()
        self.assertEqual((item["tweet_id"], item["removed"], item["ignored"]), ("100", "declined", True))
        self.assertEqual(again["removed"], "declined")
        # Neither a later reviewed run nor an unattended one asks or saves it.
        later = Review(("save",))
        result, events = self.worker(rows, review=later)
        self.assertEqual((result, events[-1]["saved"], later.requests), (0, 0, []))
        result, events = self.worker(rows)
        self.assertEqual((result, events[-1]["saved"]), (0, 0))
        self.assertFalse(list(self.output.rglob("100_1.jpg")))

    def test_declined_file_can_still_be_downloaded_from_the_missing_list(self):
        rows = [("100", 1, "photo", "alice")]
        self.worker(rows, review=Review(("never",)))
        store = self.store()
        try:
            self.assertEqual(store.record_page(include_deleted=False)["total"], 0)
            self.assertEqual(len(store.records()), 1)
            self.assertEqual(len(store.missing_media()), 1)
        finally:
            store.close()
        result, events = self.retry(["100_1"], rows)
        self.assertEqual((result, events[-1]["saved"]), (0, 1))
        self.assertTrue(list(self.output.rglob("100_1.jpg")))
        store = self.store()
        try:
            self.assertNotIn(("100", 1, "photo"), store.ignored_media())
            self.assertEqual(store.missing_media(), [])
            self.assertEqual(store.record_page(include_deleted=False)["total"], 1)
        finally:
            store.close()

    def test_only_previously_declined_media_finishes_without_false_empty_list_error(self):
        rows = [("100", 1, "photo", "alice")]
        self.worker(rows, review=Review(("never",)))
        review = Review()
        result, events = self.worker(rows, review=review)
        self.assertEqual((result, events[-1]["status"], events[-1]["saved"]), (0, "finished", 0))
        self.assertEqual(review.requests, [])
        self.assertNotIn("저장할 미디어를 찾지 못했습니다", events[-1]["message"])
        self.assertEqual(self.runs()[-1]["status"], "finished")

    def test_never_rechecks_temporary_source_after_caching_preview(self):
        review = Review(("never",))

        def replace_source(key, path, *, suffix=None):
            Path(path).write_bytes(b"external-change-during-cache")

        with patch("luvbox.thumbcache.Recorder.add", side_effect=replace_source):
            result, events = self.worker([("100", 1, "photo", "alice")], review=review)
        self.assertEqual((result, events[-1]["status"], events[-1]["review_never"]), (1, "error", 0))
        self.assertEqual(review.requests[0][0].read_bytes(), b"external-change-during-cache")
        self.assertEqual(self.keys(), set())
        self.assertEqual(self.records(), [])

    def test_never_honors_stop_received_while_caching_preview(self):
        flag = self.base / "stop.flag"
        review = Review(("never",))

        with patch("luvbox.thumbcache.Recorder.add", side_effect=lambda *args, **kwargs: flag.touch()):
            result, events = self.worker([("100", 1, "photo", "alice")], review=review, stop_file=flag)
        self.assertEqual((result, events[-1]["status"], events[-1]["review_never"]), (0, "stopped", 0))
        self.assertTrue(review.requests[0][0].is_file())
        self.assertEqual(self.keys(), set())
        self.assertEqual(self.records(), [])

    def test_never_rechecks_gif_completion_marker_after_caching_preview(self):
        path = self.output / "myaccount" / "alice" / "100_gif_1.mp4.part"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"completed-local-gif")
        core._mark_transfer_complete(path)
        marker = Path(str(path) + ".luvbox-complete")

        with patch("luvbox.thumbcache.Recorder.add",
                   side_effect=lambda *args, **kwargs: marker.write_bytes(b"changed-during-cache")):
            result, events = self.worker([("100", 1, "animated_gif", "alice")],
                                         review=Review(("never",)), options=self.options_all())
        self.assertEqual((result, events[-1]["status"], events[-1]["review_never"]), (1, "error", 0))
        self.assertEqual(path.read_bytes(), b"completed-local-gif")
        self.assertEqual(marker.read_bytes(), b"changed-during-cache")
        self.assertEqual(self.keys(), set())

    def test_never_in_a_chosen_redownload_hides_it_without_new_records(self):
        rows = [("100", 1, "photo", "alice")]
        self.worker(rows)
        saved = next(self.output.rglob("100_1.jpg"))
        saved.unlink()  # Only this test's temporary fixture.
        result, events = self.retry(["100_1"], rows, review=Review(("never",)))
        self.assertEqual((result, events[-1]["saved"], events[-1]["review_never"]), (0, 0, 1))
        self.assertNotIn("받지 못한", json.dumps(events, ensure_ascii=False))
        self.assertIn("twitter100_1", self.keys())
        store = self.store()
        try:
            self.assertIn(("100", 1, "photo"), store.ignored_media())
            [item] = store.missing_media()
        finally:
            store.close()
        self.assertEqual(item["removed"], "")

    def test_declined_gif_records_the_gif_name_it_would_have_had(self):
        rows = [("100", 1, "animated_gif", "alice")]
        with patch("luvbox.thumbcache.Recorder.add"):
            result, events = self.worker(rows, review=Review(("never",)), options=self.options_all())
        self.assertEqual((result, events[-1]["review_never"]), (0, 1))
        declined = next(row for row in self.records() if row["tweet_id"] == "100")
        self.assertTrue(declined["last_path"].endswith("100_gif_1.gif"))
        self.assertFalse(list(self.output.rglob("*.part*")) + list(self.output.rglob("*.luvbox-complete")))

    def test_live_option_enables_and_disables_between_files(self):
        review = Review(("save",), enabled=False)

        def before_row(extractor, row):
            review.active = row[0] == "101"

        result, events = self.worker([("100", 1, "photo", "alice"), ("101", 1, "photo", "alice"),
                                     ("102", 1, "photo", "alice")], review=review, before_row=before_row)
        self.assertEqual((result, events[-1]["saved"]), (0, 3))
        self.assertEqual([media["tweet_id"] for path, media in review.requests], ["101"])
        self.assertFalse(list(self.output.rglob("*.part")))

    def test_review_skip_breaks_current_quick_scan_known_file_streak(self):
        known = [(str(number), 1, "photo", "alice") for number in range(100, 94, -1)]
        self.worker(known)
        choices = [("102", 1, "photo", "alice"), ("101", 1, "photo", "alice")]
        rows = known[:1] + choices[:1] + known[1:3] + choices[1:] + known[3:]
        with patch("luvbox.core.QUICK_STOP_AFTER", 3):
            result, events = self.worker(rows, review=Review(("skip", "save")),
                                         options=replace(self.options, scan_mode="quick"))
        self.assertEqual((result, events[-1]["scan_mode"], events[-1]["saved"], events[-1]["review_skipped"]),
                         (0, "quick", 1, 1))
        self.assertIn("twitter101_1", self.keys())
        self.assertNotIn("twitter102_1", self.keys())
        self.assertEqual(self.runs()[-1]["status"], "finished")
        self.assertEqual(self.pending(), {("102", 1, "photo")})

    def test_existing_file_or_archive_hit_never_prompts_or_discards_file(self):
        rows = [("100", 1, "photo", "alice")]
        self.worker(rows)
        saved_path = Path(self.records()[0]["path"])
        saved_data = saved_path.read_bytes()
        review = Review(("skip",))
        result, events = self.worker(rows, review=review)
        self.assertEqual((result, events[-1]["saved"], events[-1]["skipped"], events[-1]["review_skipped"]),
                         (0, 0, 1, 0))
        self.assertEqual(review.requests, [])
        self.assertEqual(saved_path.read_bytes(), saved_data)

    def test_stop_before_confirmation_preserves_transfer_and_does_not_prompt(self):
        flag = self.base / "stop.flag"
        original = test_core.FakeHttpDownloader.download

        def download(instance, url, pathfmt):
            result = original(instance, url, pathfmt)
            flag.touch()
            return result

        review = Review()
        with patch.object(test_core.FakeHttpDownloader, "download", download):
            result, events = self.worker([("100", 1, "photo", "alice")], review=review, stop_file=flag)
        self.assertEqual((result, events[-1]["status"], events[-1]["saved"]), (0, "stopped", 0))
        self.assertEqual(review.requests, [])
        self.assertEqual(len(list(self.output.rglob("*.part"))), 1)
        self.assertEqual(self.keys(), set())

    def test_stop_or_window_close_while_waiting_preserves_file_without_archive(self):
        flag = self.base / "stop.flag"

        def close(path, media, check_stop):
            flag.touch()
            check_stop()

        review = Review(on_ask=close)
        result, events = self.worker([("100", 1, "photo", "alice"), ("101", 1, "photo", "alice")],
                                     review=review, stop_file=flag)
        self.assertEqual((result, events[-1]["status"], events[-1]["saved"]), (0, "stopped", 0))
        self.assertEqual(len(test_core.FakeHttpDownloader.downloaded), 1)
        self.assertEqual(len(list(self.output.rglob("*.part"))), 1)
        self.assertEqual(self.keys(), set())
        self.assertEqual(self.records(), [])

    def test_broken_channel_or_invalid_answer_fails_closed_with_fixed_secret_free_error(self):
        for answer in ("unknown", None):
            with self.subTest(answer=answer):
                review = Review((answer,))
                result, events = self.worker([("100", 1, "photo", "alice")], review=review)
                self.assertEqual((result, events[-1]["status"], events[-1]["saved"]), (1, "error", 0))
                self.assertIn("저장 여부 확인", events[-1]["message"])
                self.assertEqual(self.keys(), set())

        def broken(path, media, check_stop):
            raise RuntimeError(test_core.SECRET)

        result, events = self.worker([("100", 1, "photo", "alice")], review=Review(on_ask=broken))
        self.assertEqual((result, events[-1]["status"], events[-1]["saved"]), (1, "error", 0))
        self.assertIn("임시 파일은 보존", events[-1]["message"])
        self.assertEqual(self.keys(), set())

    def test_broken_live_option_state_never_implicitly_saves(self):
        class BrokenReview(Review):
            def enabled(self):
                raise RuntimeError(test_core.SECRET)

        review = BrokenReview()
        result, events = self.worker([("100", 1, "photo", "alice")], review=review)
        self.assertEqual((result, events[-1]["saved"]), (1, 0))
        self.assertEqual(review.requests, [])
        self.assertEqual(self.keys(), set())
        self.assertEqual(len(list(self.output.rglob("*.part"))), 1)

    def test_changed_preview_source_is_not_discarded_or_saved(self):
        def edit(path, media, check_stop):
            path.write_bytes(b"external-change")

        review = Review(("skip",), on_ask=edit)
        result, events = self.worker([("100", 1, "photo", "alice")], review=review)
        self.assertEqual((result, events[-1]["status"], events[-1]["review_skipped"]), (1, "error", 0))
        self.assertEqual(review.requests[0][0].read_bytes(), b"external-change")
        self.assertEqual(self.keys(), set())

    def test_final_file_created_during_preview_is_never_overwritten(self):
        def collision(path, media, check_stop):
            Path(str(path)[:-5]).write_bytes(b"existing-user-file")

        review = Review(on_ask=collision)
        result, events = self.worker([("100", 1, "photo", "alice")], review=review)
        self.assertEqual((result, events[-1]["saved"]), (1, 0))
        source = review.requests[0][0]
        self.assertTrue(source.is_file())
        self.assertEqual(Path(str(source)[:-5]).read_bytes(), b"existing-user-file")
        self.assertEqual(self.keys(), set())

    def test_downloader_cannot_supply_an_existing_file_for_discard(self):
        outside = self.base / "user-original.jpg"
        outside.write_bytes(b"existing-user-file")

        def unexpected_path(instance, url, pathfmt):
            pathfmt.temppath = str(outside)
            return True

        review = Review(("skip",))
        with patch.object(test_core.FakeHttpDownloader, "download", unexpected_path):
            result, events = self.worker([("100", 1, "photo", "alice")], review=review)
        self.assertEqual((result, events[-1]["saved"], events[-1]["review_skipped"]), (1, 0, 0))
        self.assertEqual(outside.read_bytes(), b"existing-user-file")
        self.assertEqual(review.requests, [])
        self.assertEqual(self.keys(), set())

    def test_incomplete_or_failed_download_is_never_previewed(self):
        test_core.FakeHttpDownloader.failure = True
        review = Review()
        result, events = self.worker([("100", 1, "photo", "alice")], review=review)
        self.assertNotEqual(result, 0)
        self.assertEqual(events[-1]["saved"], 0)
        self.assertEqual(review.requests, [])

    def test_video_skip_then_save_are_independent_choices(self):
        review = Review(("skip", "save"))
        result, events = self.worker([("100", 1, "video", "alice"), ("100", 2, "video", "alice")],
                                     review=review, options=self.options_all())
        self.assertEqual((result, events[-1]["saved"], events[-1]["review_skipped"]), (0, 1, 1))
        self.assertEqual(self.keys(), {"twitter100_video_2"})
        self.assertEqual([media["type"] for path, media in review.requests], ["video", "video"])
        self.assertEqual(Path(self.records()[0]["path"]).name, "100_video_2.mp4")

    def test_gif_skip_does_not_convert_and_saved_gif_keeps_success_metadata(self):
        review = Review(("skip", "save"))
        with patch("luvbox.core.convert_mp4_to_gif", side_effect=self.convert) as convert:
            result, events = self.worker([("100", 1, "animated_gif", "alice"), ("100", 2, "animated_gif", "alice")],
                                         review=review, options=self.options_all())
        self.assertEqual((result, events[-1]["saved"], events[-1]["review_skipped"]), (0, 1, 1))
        convert.assert_called_once()
        record = self.records()[0]
        self.assertEqual((record["media_type"], record["metadata_status"], record["status"]),
                         ("animated_gif", "embedded", "saved"))
        self.assertEqual(Path(record["path"]).suffix, ".gif")
        self.assertEqual(self.keys(), {"twitter100_animated_gif_2"})
        self.assertFalse(list(self.output.rglob("*.part*")))
        self.assertFalse(list(self.output.rglob("*.mp4")))

    def test_mp4_gif_choice_is_reviewed_before_save(self):
        review = Review(("save",))
        with patch("luvbox.core.convert_mp4_to_gif") as convert:
            result, events = self.worker([("100", 1, "animated_gif", "alice")], review=review,
                                         options=self.options_all(gif_format="mp4"))
        self.assertEqual((result, events[-1]["saved"]), (0, 1))
        convert.assert_not_called()
        self.assertEqual(review.requests[0][1]["type"], "animated_gif")
        self.assertEqual(Path(self.records()[0]["path"]).suffix, ".mp4")

    def test_locally_complete_gif_can_be_declined_without_redownload_or_conversion(self):
        # A cancelled conversion preserves the complete MP4 transfer. The next
        # prompt may discard only that transfer and its verified completion tag.
        path = self.output / "myaccount" / "alice" / "100_gif_1.mp4.part"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"completed-local-gif")
        core._mark_transfer_complete(path)
        with patch("luvbox.core.convert_mp4_to_gif") as convert:
            result, events = self.worker([("100", 1, "animated_gif", "alice")], review=Review(("skip",)),
                                         options=self.options_all())
        self.assertEqual((result, events[-1]["review_skipped"]), (0, 1))
        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
        convert.assert_not_called()
        self.assertFalse(path.exists())
        self.assertFalse(Path(str(path) + ".luvbox-complete").exists())
        self.assertEqual(self.keys(), set())

    def test_changed_gif_completion_marker_is_preserved_with_source(self):
        path = self.output / "myaccount" / "alice" / "100_gif_1.mp4.part"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"completed-local-gif")
        core._mark_transfer_complete(path)
        marker = Path(str(path) + ".luvbox-complete")

        def edit_marker(path, media, check_stop):
            marker.write_bytes(b"user-changed-marker")

        result, events = self.worker([("100", 1, "animated_gif", "alice")],
                                     review=Review(("skip",), on_ask=edit_marker), options=self.options_all())
        self.assertEqual((result, events[-1]["review_skipped"]), (1, 0))
        self.assertEqual(path.read_bytes(), b"completed-local-gif")
        self.assertEqual(marker.read_bytes(), b"user-changed-marker")
        self.assertEqual(self.keys(), set())


if __name__ == "__main__":
    unittest.main()
