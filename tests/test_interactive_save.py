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
from xlikesaver import core


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
        with closing(sqlite3.connect(self.record_dir / ".xlikesaver-history.sqlite3")) as database:
            database.row_factory = sqlite3.Row
            return [dict(row) for row in database.execute("SELECT * FROM media")]

    def keys(self):
        with closing(sqlite3.connect(self.record_dir / ".xlikesaver-archive.sqlite3")) as database:
            return {row[0] for row in database.execute("SELECT entry FROM archive")}

    def options_all(self, **changes):
        return replace(self.options, include_videos=True, include_gifs=True, **changes)

    def convert(self, source, **kwargs):
        self.assertTrue(source.name.endswith(".mp4.part"))
        self.assertTrue(core._gif_source_complete(source))
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
        self.assertEqual(self.runs()[-1]["status"], "partial")
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
        self.assertEqual((result, events[-1]["scan_mode"], events[-1]["saved"]), (0, "full", 1))
        self.assertEqual(len(second.requests), 1)
        self.assertEqual(len(test_core.FakeHttpDownloader.downloaded), 2)

    def test_declined_old_like_after_saved_streak_is_revisited_by_next_quick_run(self):
        # Only a full run may establish the checkpoint; refusing its oldest
        # entry must invalidate that checkpoint even after many archived files.
        known = [(str(number), 1, "photo", "alice") for number in range(150, 100, -1)]
        with patch("xlikesaver.core.QUICK_STOP_AFTER", 4):
            self.worker(known)
            older = ("99", 1, "photo", "alice")
            first = Review(("skip",))
            result, events = self.worker(known + [older], review=first)
            self.assertEqual((result, events[-1]["review_skipped"]), (0, 1))
            second = Review(("save",))
            result, events = self.worker(known + [older], review=second,
                                         options=replace(self.options, scan_mode="quick"))
        self.assertEqual((result, events[-1]["scan_mode"], events[-1]["saved"]), (0, "full", 1))
        self.assertEqual(second.requests[0][1]["tweet_id"], "99")

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
        with patch("xlikesaver.core.QUICK_STOP_AFTER", 3):
            result, events = self.worker(rows, review=Review(("skip", "save")),
                                         options=replace(self.options, scan_mode="quick"))
        self.assertEqual((result, events[-1]["scan_mode"], events[-1]["saved"], events[-1]["review_skipped"]),
                         (0, "quick", 1, 1))
        self.assertIn("twitter101_1", self.keys())
        self.assertNotIn("twitter102_1", self.keys())
        self.assertEqual(self.runs()[-1]["status"], "partial")

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
        with patch("xlikesaver.core.convert_mp4_to_gif", side_effect=self.convert) as convert:
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
        with patch("xlikesaver.core.convert_mp4_to_gif") as convert:
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
        core._mark_gif_source_complete(path)
        with patch("xlikesaver.core.convert_mp4_to_gif") as convert:
            result, events = self.worker([("100", 1, "animated_gif", "alice")], review=Review(("skip",)),
                                         options=self.options_all())
        self.assertEqual((result, events[-1]["review_skipped"]), (0, 1))
        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
        convert.assert_not_called()
        self.assertFalse(path.exists())
        self.assertFalse(Path(str(path) + ".xlikesaver-complete").exists())
        self.assertEqual(self.keys(), set())

    def test_changed_gif_completion_marker_is_preserved_with_source(self):
        path = self.output / "myaccount" / "alice" / "100_gif_1.mp4.part"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"completed-local-gif")
        core._mark_gif_source_complete(path)
        marker = Path(str(path) + ".xlikesaver-complete")

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
