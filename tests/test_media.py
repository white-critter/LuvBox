"""Offline media selection, streaming limits, progress and history integration."""

import errno
import json
import sqlite3
import time
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from gallery_dl.downloader.http import HttpDownloader
from gallery_dl.extractor.message import Message
from gallery_dl.extractor.twitter import TwitterExtractor

from tests import test_core
from xlikesaver.core import JobOptions, run_worker
from xlikesaver.history import HistoryError, HistoryStore


MIB = 1024 * 1024
MP4_HEADER = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 12


class VariantExtractor(test_core.FakeLikesExtractor):
    """Use gallery-dl's real media selection without making API requests."""

    _extract_media = TwitterExtractor._extract_media
    _extract_media_source = TwitterExtractor._extract_media_source

    def __init__(self, url, media):
        super().__init__(url, [])
        self.media = media
        self.videos_files = True
        self.videos_ytdl = self.videos_previews = False
        self.unavailable = False

    def items(self):
        files = []
        if self.media:
            # Tweet metadata also invokes this hook before media extraction.
            self._extract_media_source({}, self.media[0])
        self._extract_media({"id_str": "100"}, self.media, files)
        directory = {"tweet_id": "100", "author": {"name": "alice"}, "user": {"name": "alice"},
                     "date": datetime(2020, 1, 1, tzinfo=timezone.utc)}
        yield Message.Directory, "", directory
        for num, file in enumerate(files, 1):
            metadata = dict(directory, **file, num=num, extension="mp4")
            yield Message.Url, file["url"], metadata


class MediaTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    write_cookies = test_core.CoreTests.write_cookies
    worker = test_core.CoreTests.worker

    def all_media(self, **changes):
        return replace(self.options, include_videos=True, include_gifs=True, **changes)

    def archive_keys(self):
        with closing(sqlite3.connect(self.record_dir / ".xlikesaver-archive.sqlite3")) as db:
            return {row[0] for row in db.execute("SELECT entry FROM archive")}

    def history_rows(self, table="media"):
        path = self.record_dir / ".xlikesaver-history.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]

    @staticmethod
    def response(chunks=(), *, size=None, status=200, content_range=None, content_type="video/mp4"):
        response = requests.Response()
        response.status_code = status
        response.reason = "OK"
        response.headers["Content-Type"] = content_type
        if size is not None:
            response.headers["Content-Length"] = str(size)
        if content_range:
            response.headers["Content-Range"] = content_range
        response.raw = SimpleNamespace(chunked=True)
        response.close = Mock()
        response.iter_content = lambda *args, **kwargs: iter(chunks)
        return response

    def http_worker(self, rows, responses, *, options=None, items=None, stop_file=None):
        events = []
        patches = [patch("gallery_dl.extractor.find", side_effect=lambda url: test_core.FakeLikesExtractor(url, rows)),
                   patch("gallery_dl.downloader.find", return_value=HttpDownloader),
                   patch("requests.Session.send", side_effect=responses),
                   patch("xlikesaver.protection.MEDIA_INTERVAL", 0)]
        if items is not None:
            patches.append(patch.object(test_core.FakeLikesExtractor, "items", items))
        from contextlib import ExitStack
        with ExitStack() as stack:
            entered = [stack.enter_context(patch_item) for patch_item in patches]
            result = run_worker(options or self.all_media(max_video_mb=1), events.append, stop_file)
        self.assertNotIn(test_core.SECRET, json.dumps(events))
        return result, events, entered[2]

    def test_defaults_enable_videos_gifs_and_flat_layout_and_validate_cap(self):
        options = JobOptions("me", str(self.output)).validate()
        self.assertEqual(options.folder_mode, "flat")
        self.assertIs(options.include_videos, True)
        self.assertIs(options.include_gifs, True)
        self.assertEqual(options.max_video_mb, 200)
        for cap in (-1, True, 1.5, "200", None):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                replace(options, max_video_mb=cap).validate()
        for field in ("include_videos", "include_gifs"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(options, **{field: 1}).validate()
        self.assertEqual(replace(options, max_video_mb=0).validate().max_video_mb, 0)

    def test_each_supported_type_saves_and_preview_is_excluded(self):
        rows = [("100", 1, "photo", "alice"), ("100", 2, "video", "alice"),
                ("100", 3, "animated_gif", "alice"), ("100", 4, "preview", "alice")]
        code, events = self.worker(rows, options=self.all_media())
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 3)
        folder = self.output / "myaccount" / "alice"
        self.assertEqual({path.name for path in folder.iterdir()},
                         {"100_1.jpg", "100_video_2.mp4", "100_gif_3.mp4"})
        self.assertEqual({event["media_type"] for event in events if event["type"] == "saved"},
                         {"photo", "video", "animated_gif"})
        self.assertEqual(len(self.history_rows()), 3)

    def test_video_and_gif_can_be_enabled_independently(self):
        rows = [("100", 1, "photo", "alice"), ("100", 2, "video", "alice"),
                ("100", 3, "animated_gif", "alice")]
        code, events = self.worker(rows, options=replace(self.options, include_gifs=True))
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 2)
        code, events = self.worker(rows, options=replace(self.options, include_videos=True))
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertEqual(events[-1]["skipped"], 1)
        self.assertEqual(len(test_core.FakeHttpDownloader.downloaded), 3)

    def test_mixed_tweet_preserves_legacy_photo_keys_and_video_cannot_hit_photo_archive(self):
        self.worker([("100", 1, "photo", "alice"), ("100", 2, "photo", "alice")])
        previous_photos = {path.name: path.read_bytes() for path in (self.output / "myaccount" / "alice").glob("*.jpg")}
        rows = [("100", 1, "video", "alice"), ("100", 2, "photo", "alice"),
                ("100", 3, "animated_gif", "alice"), ("100", 4, "photo", "alice")]
        code, events = self.worker(rows, options=self.all_media())
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 2)
        self.assertEqual(events[-1]["skipped"], 2)
        self.assertEqual({path.name: path.read_bytes() for path in (self.output / "myaccount" / "alice").glob("*.jpg")}, previous_photos)
        self.assertEqual(self.archive_keys(), {"twitter100_1", "twitter100_2",
                                               "twitter100_video_1", "twitter100_animated_gif_3"})
        code, events = self.worker(rows, options=self.all_media())
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 0)
        self.assertEqual(events[-1]["skipped"], 4)

    def variant_worker(self, media, **option_changes):
        events = []
        with patch("gallery_dl.extractor.find", side_effect=lambda url: VariantExtractor(url, media)), \
                patch("gallery_dl.downloader.find", return_value=test_core.FakeHttpDownloader):
            code = run_worker(replace(self.all_media(), **option_changes), events.append)
        return code, events

    @staticmethod
    def variant_media(kind, variants):
        return {"type": kind, "original_info": {"width": 1920, "height": 1080},
                "video_info": {"variants": variants, "duration_millis": 2000},
                "media_url_https": "https://pbs.twimg.com/media/preview.jpg"}

    def test_real_gallery_selects_highest_direct_mp4_without_hls_or_preview(self):
        variants = [{"url": "https://video.twimg.com/hls.m3u8", "content_type": "application/x-mpegURL", "bitrate": 99999999},
                    {"url": "https://video.twimg.com/low.mp4", "content_type": "video/mp4", "bitrate": 256000},
                    {"url": "https://video.twimg.com/high.mp4?tag=1", "content_type": "video/mp4", "bitrate": 2048000},
                    {"url": "ytdl:https://x.com/i/web/status/100", "content_type": "video/mp4", "bitrate": 999999999}]
        code, events = self.variant_worker([self.variant_media("video", variants)])
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertEqual(test_core.FakeHttpDownloader.downloaded, ["https://video.twimg.com/high.mp4?tag=1"])

    def test_disabling_gif_does_not_renumber_video_archive_when_reenabled(self):
        variants = [{"url": "https://video.twimg.com/one.mp4", "content_type": "video/mp4", "bitrate": 1000}]
        media = [self.variant_media("animated_gif", variants), self.variant_media("video", variants)]
        code, events = self.variant_worker(media, include_gifs=False)
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertTrue((self.output / "myaccount" / "alice" / "100_video_2.mp4").is_file())
        code, events = self.variant_worker(media)
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertEqual(events[-1]["skipped"], 1)
        self.assertEqual(len(test_core.FakeHttpDownloader.downloaded), 2)

    def test_unsupported_first_attachment_does_not_renumber_later_mp4(self):
        hls = [{"url": "https://video.twimg.com/hls.m3u8", "content_type": "application/x-mpegURL"}]
        mp4 = [{"url": "https://video.twimg.com/direct.mp4", "content_type": "video/mp4", "bitrate": 1000}]
        code, events = self.variant_worker([self.variant_media("video", hls), self.variant_media("video", mp4)])
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertTrue((self.output / "myaccount" / "alice" / "100_video_2.mp4").is_file())
        code, events = self.variant_worker([self.variant_media("video", mp4), self.variant_media("video", mp4)])
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertEqual(events[-1]["skipped"], 1)
        self.assertEqual(self.archive_keys(), {"twitter100_video_1", "twitter100_video_2"})

    def test_known_oversize_is_closed_not_saved_not_archived_and_next_photo_continues(self):
        large = self.response([MP4_HEADER], size=MIB + 1)
        photo = self.response([b"\xff\xd8\xffjpg"], size=6, content_type="image/jpeg")
        rows = [("100", 1, "video", "alice"), ("101", 1, "photo", "alice")]
        code, events, send = self.http_worker(rows, [large, photo])
        self.assertEqual(code, 0)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertEqual(events[-1]["size_skipped"], 1)
        self.assertEqual(events[-1]["skipped"], 0)
        self.assertFalse((self.output / "myaccount" / "alice" / "100_video_1.mp4").exists())
        self.assertFalse(list(self.output.rglob("*.part")))
        large.close.assert_called()
        self.assertEqual(self.archive_keys(), {"twitter101_1"})
        self.assertEqual([row["tweet_id"] for row in self.history_rows()], ["101"])
        self.assertEqual(self.history_rows("runs")[-1]["size_skipped"], 1)

    def test_unknown_length_stream_is_hard_capped_and_can_be_saved_after_cap_increase(self):
        chunks = [MP4_HEADER, b"v" * (MIB // 2), b"v" * (MIB // 2)]
        response = self.response(chunks)
        rows = [("100", 1, "animated_gif", "alice")]
        code, events, send = self.http_worker(rows, [response])
        self.assertEqual(code, 0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(events[-1]["saved"], 0)
        self.assertEqual(events[-1]["size_skipped"], 1)
        self.assertFalse(list(self.output.rglob("*.part")))
        self.assertEqual(self.archive_keys(), set())
        self.assertEqual(self.history_rows(), [])
        response.close.assert_called()
        code, events, send = self.http_worker(rows, [self.response(chunks)], options=self.all_media(max_video_mb=2))
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertEqual(events[-1]["size_skipped"], 0)
        self.assertEqual((self.output / "myaccount" / "alice" / "100_gif_1.mp4").stat().st_size, MIB + len(MP4_HEADER))

    def test_size_skip_never_attempts_fallback(self):
        def items(extractor):
            metadata = {"tweet_id": "100", "num": 1, "type": "video", "extension": "mp4",
                        "author": {"name": "alice"}, "user": {"name": "alice"},
                        "_fallback": ["https://video.twimg.com/smaller.mp4"]}
            yield Message.Directory, "", metadata
            yield Message.Url, "https://video.twimg.com/large.mp4", metadata
        code, events, send = self.http_worker([], [self.response(size=MIB + 1)], items=items)
        self.assertEqual(code, 0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(events[-1]["size_skipped"], 1)

    def test_resumed_bytes_count_toward_cap_and_capped_partial_is_removed(self):
        folder = self.output / "myaccount" / "alice"
        folder.mkdir(parents=True)
        partial = folder / "100_video_1.mp4.part"
        partial.write_bytes(MP4_HEADER + b"a" * (MIB - len(MP4_HEADER) - 10))
        response = self.response([b"b" * 20], status=206, content_range=f"bytes {MIB - 10}-{MIB + 9}/*")
        code, events, send = self.http_worker([("100", 1, "video", "alice")], [response])
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["size_skipped"], 1)
        self.assertEqual(send.call_args.args[0].headers["Range"], f"bytes={MIB - 10}-")
        self.assertFalse(partial.exists())
        response.close.assert_called()
        self.assertEqual(self.archive_keys(), set())

    def test_unlimited_video_and_photo_ignores_video_size_cap(self):
        chunks = [MP4_HEADER, b"a" * MIB]
        code, events, _ = self.http_worker([("100", 1, "video", "alice")],
                                          [self.response(chunks, size=sum(map(len, chunks)))],
                                          options=self.all_media(max_video_mb=0))
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        photo = [b"\xff\xd8\xff", b"a" * MIB]
        code, events, _ = self.http_worker([("101", 1, "photo", "alice")],
                                          [self.response(photo, size=sum(map(len, photo)), content_type="image/jpeg")])
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertEqual(events[-1]["size_skipped"], 0)

    def test_single_oversize_header_chunk_and_oversize_partial_never_finalize(self):
        rows = [("100", 1, "video", "alice")]
        response = self.response([MP4_HEADER + b"a" * MIB])
        code, events, send = self.http_worker(rows, [response])
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["size_skipped"], 1)
        self.assertEqual(send.call_count, 1)
        response.close.assert_called()
        self.assertFalse(list(self.output.rglob("*.part")))
        folder = self.output / "myaccount" / "alice"
        folder.mkdir(parents=True, exist_ok=True)
        partial = folder / "100_video_1.mp4.part"
        partial.write_bytes(MP4_HEADER + b"a" * MIB)
        response = self.response([], status=416, content_range=f"bytes */{MIB + len(MP4_HEADER)}")
        code, events, send = self.http_worker(rows, [response])
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["size_skipped"], 1)
        send.assert_not_called()
        self.assertFalse(partial.exists())
        self.assertFalse((folder / "100_video_1.mp4").exists())
        self.assertEqual(self.archive_keys(), set())

    def test_cap_accepts_exact_size_and_media_limit_run_is_marked_limited(self):
        chunks = [MP4_HEADER, b"a" * (MIB - len(MP4_HEADER))]
        code, events, _ = self.http_worker([("100", 1, "video", "alice"), ("101", 1, "photo", "alice")],
                                          [self.response(chunks, size=MIB)],
                                          options=self.all_media(max_video_mb=1, photo_limit=1))
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertEqual(events[-1]["size_skipped"], 0)
        self.assertEqual(self.history_rows("runs")[-1]["status"], "limited")
        self.assertEqual(len(self.history_rows()), 1)

    def test_cancel_during_video_stream_preserves_partial_and_does_not_archive(self):
        flag = self.base / "stop.flag"
        response = self.response()
        def chunks(*args, **kwargs):
            yield MP4_HEADER
            yield b"a" * 10
            flag.touch()
            yield b"a" * 10
        response.iter_content = chunks
        code, events, send = self.http_worker([("100", 1, "video", "alice"), ("101", 1, "photo", "alice")],
                                             [response], stop_file=flag)
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["status"], "stopped")
        self.assertEqual(events[-1]["saved"], 0)
        self.assertEqual(send.call_count, 1)
        self.assertTrue(list(self.output.rglob("*.part")))
        self.assertEqual(self.archive_keys(), set())
        self.assertEqual(self.history_rows(), [])
        self.assertEqual(self.history_rows("runs")[-1]["status"], "stopped")

    def photo_with_fallback(self):
        def items(extractor):
            metadata = {"tweet_id": "100", "num": 1, "type": "photo", "extension": "jpg",
                        "author": {"name": "alice"}, "user": {"name": "alice"},
                        "_fallback": iter(["https://pbs.twimg.com/media/x?name=4096x4096",
                                           "https://pbs.twimg.com/media/x?name=large"])}
            yield Message.Directory, "", metadata
            yield Message.Url, "https://pbs.twimg.com/media/x?name=orig", metadata
        return items

    def test_cancel_during_photo_stream_requests_no_fallback_size_and_keeps_partial(self):
        flag = self.base / "stop.flag"
        first = self.response(content_type="image/jpeg")

        def chunks(*args, **kwargs):
            yield b"\xff\xd8\xff" + b"a" * 20
            flag.touch()
            yield b"a" * 20
        first.iter_content = chunks
        fallback = [self.response([b"\xff\xd8\xffxx"], content_type="image/jpeg") for _ in range(2)]
        code, events, send = self.http_worker([], [first, *fallback], stop_file=flag, items=self.photo_with_fallback())
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["status"], "stopped")
        self.assertEqual([call.args[0].url for call in send.call_args_list], ["https://pbs.twimg.com/media/x?name=orig"])
        self.assertTrue(list(self.output.rglob("*.part")))
        self.assertEqual(self.archive_keys(), set())
        self.assertFalse(any(event.get("level") == "error" for event in events if event["type"] == "log"))

    def test_cancel_while_pacing_a_photo_request_is_a_clean_stop(self):
        flag = self.base / "stop.flag"
        responses = [self.response([b"\xff\xd8\xffxx"], content_type="image/jpeg") for _ in range(3)]

        def items(extractor):
            for tweet in ("100", "101"):
                metadata = {"tweet_id": tweet, "num": 1, "type": "photo", "extension": "jpg",
                            "author": {"name": "alice"}, "user": {"name": "alice"},
                            "_fallback": iter(["https://pbs.twimg.com/media/fallback?name=large"])}
                yield Message.Directory, "", metadata
                yield Message.Url, f"https://pbs.twimg.com/media/{tweet}?name=orig", metadata

        real_sleep = time.sleep

        def press_stop_during_wait(seconds):
            # The second photo waits for the media interval; stop arrives then.
            flag.touch()
            real_sleep(0)

        events = []
        # http_worker disables the media interval; this test needs a short wait
        # (30 s or more is treated as a server-imposed cooldown).
        with patch("gallery_dl.extractor.find", side_effect=lambda url: test_core.FakeLikesExtractor(url, [])), \
                patch.object(test_core.FakeLikesExtractor, "items", items), \
                patch("gallery_dl.downloader.find", return_value=HttpDownloader), \
                patch("requests.Session.send", side_effect=responses) as send, \
                patch("xlikesaver.protection.MEDIA_INTERVAL", 20), \
                patch("xlikesaver.core.time.sleep", press_stop_during_wait):
            code = run_worker(self.all_media(), events.append, flag)
        self.assertEqual((code, events[-1]["status"], events[-1]["saved"]), (0, "stopped", 1))
        self.assertEqual([call.args[0].url for call in send.call_args_list], ["https://pbs.twimg.com/media/100?name=orig"])
        # Not reported as a failed download: the user asked to stop.
        self.assertFalse(any(event.get("level") == "error" for event in events if event["type"] == "log"))

    def test_416_never_finalizes_partial_or_requests_fallback_and_preserves_completed_archive(self):
        self.worker([("90", 1, "photo", "alice")])
        folder = self.output / "myaccount" / "alice"
        completed = folder / "90_1.jpg"
        completed_bytes = completed.read_bytes()
        previous_archive = self.archive_keys()
        kinds = {"photo": ("1.jpg", b"\xff\xd8\xffphoto-part"),
                 "video": ("video_1.mp4", MP4_HEADER + b"video-part"),
                 "animated_gif": ("gif_1.mp4", MP4_HEADER + b"gif-part")}
        original_items = test_core.FakeLikesExtractor.items
        number = 100
        for media_type, (suffix, partial_bytes) in kinds.items():
            for content_range in (f"bytes */{len(partial_bytes)}",
                                  f"bytes */{len(partial_bytes) + 1}", None,
                                  "private server detail " + test_core.SECRET):
                number += 1
                tweet = str(number)
                partial = folder / f"{tweet}_{suffix}.part"
                partial.write_bytes(partial_bytes)
                response = self.response(status=416, content_range=content_range)
                rows = [("90", 1, "photo", "alice"), (tweet, 1, media_type, "alice"),
                        ("999", 1, "photo", "alice")]

                def items(extractor):
                    for message, url, metadata in original_items(extractor):
                        metadata["_fallback"] = ["https://pbs.twimg.com/media/fallback?name=large"]
                        yield message, url, metadata

                # Keep the real archive/path/downloader pipeline and supply
                # fallback URLs so an accidental retry is observable.
                with self.subTest(media_type=media_type, content_range=content_range):
                    code, events, send = self.http_worker(rows, [response], items=items)
                self.assertNotEqual(code, 0)
                self.assertEqual(events[-1]["status"], "error")
                self.assertEqual(events[-1]["saved"], 0)
                self.assertEqual(events[-1]["skipped"], 1)
                self.assertEqual(events[-1]["size_skipped"], 0)
                self.assertIn("이어받기", events[-1]["message"])
                self.assertIn(".part 파일은 보존", events[-1]["message"])
                self.assertEqual(send.call_count, 1)
                self.assertEqual(send.call_args.args[0].headers["Range"], f"bytes={len(partial_bytes)}-")
                response.close.assert_called_once()
                self.assertEqual(partial.read_bytes(), partial_bytes)
                self.assertFalse((folder / f"{tweet}_{suffix}").exists())
                self.assertEqual(self.archive_keys(), previous_archive)
                self.assertEqual(completed.read_bytes(), completed_bytes)
                self.assertEqual(len(self.history_rows()), 1)

    def test_local_open_and_write_errors_stop_before_fallback_and_preserve_previous_downloads(self):
        from gallery_dl.path import PathFormat

        self.worker([("90", 1, "photo", "alice")])
        folder = self.output / "myaccount" / "alice"
        completed = folder / "90_1.jpg"
        completed_bytes = completed.read_bytes()
        previous_archive = self.archive_keys()
        original_open = PathFormat.open
        original_items = test_core.FakeLikesExtractor.items
        cases = (("open", PermissionError, errno.EACCES),
                 ("write", PermissionError, errno.EACCES),
                 ("write", OSError, errno.ENOSPC))
        for number, (phase, error_type, error_number) in enumerate(cases, 100):
            tweet = str(number)
            partial = folder / f"{tweet}_1.jpg.part"
            partial_bytes = b"\xff\xd8\xffprevious-partial"
            partial.write_bytes(partial_bytes)
            response = self.response([b"next-chunk"], status=206,
                                     content_range=f"bytes {len(partial_bytes)}-{len(partial_bytes) + 9}/{len(partial_bytes) + 10}",
                                     content_type="image/jpeg")
            rows = [("90", 1, "photo", "alice"), (tweet, 1, "photo", "alice"),
                    ("999", 1, "photo", "alice")]

            class UnwritableFile:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self.stream.close()

                def __getattr__(self, name):
                    return getattr(self.stream, name)

                def write(self, data):
                    raise error_type(error_number, test_core.SECRET)

            def failing_open(pathfmt, *args, **kwargs):
                if phase == "open":
                    raise error_type(error_number, test_core.SECRET)
                return UnwritableFile(original_open(pathfmt, *args, **kwargs))

            def items(extractor):
                for message, url, metadata in original_items(extractor):
                    metadata["_fallback"] = ["https://pbs.twimg.com/media/fallback?name=large"]
                    yield message, url, metadata

            with self.subTest(phase=phase, error_number=error_number), \
                    patch.object(PathFormat, "open", failing_open):
                code, events, send = self.http_worker(rows, [response], items=items)
            self.assertNotEqual(code, 0)
            self.assertEqual(events[-1]["status"], "error")
            self.assertEqual(events[-1]["saved"], 0)
            self.assertEqual(events[-1]["skipped"], 1)
            self.assertIn("미디어 파일을 쓰지 못해", events[-1]["message"])
            self.assertIn("다른 프로그램의 파일 사용", events[-1]["message"])
            self.assertEqual(send.call_count, 1)
            response.close.assert_called_once()
            self.assertEqual(partial.read_bytes(), partial_bytes)
            self.assertFalse((folder / f"{tweet}_1.jpg").exists())
            self.assertEqual(self.archive_keys(), previous_archive)
            self.assertEqual(completed.read_bytes(), completed_bytes)
            self.assertEqual(len(self.history_rows()), 1)

    def test_progress_has_start_final_metadata_and_throttled_chunk_updates(self):
        chunks = [MP4_HEADER] + [b"a" * 256 for _ in range(40)]
        size = sum(map(len, chunks))
        posted = datetime(2020, 1, 1, 15, 30, tzinfo=timezone.utc)
        code, events, _ = self.http_worker([("100", 1, "video", "alice", posted)], [self.response(chunks, size=size)])
        self.assertEqual(code, 0)
        progress = [event for event in events if event["type"] == "progress"]
        self.assertEqual(progress[0]["downloaded"], 0)
        self.assertEqual(progress[-1]["downloaded"], size)
        self.assertEqual(progress[-1]["total"], size)
        self.assertLessEqual(len(progress), 4)
        for event in progress:
            self.assertEqual(event["tweet_id"], "100")
            self.assertEqual(event["media_type"], "video")
            self.assertEqual(event["filename"], "100_video_1.mp4")
            self.assertEqual(event["author"], "alice")
            self.assertEqual(event["posted_at"], "2020-01-02T00:30:00+09:00")
            self.assertGreaterEqual(event["speed"], 0)
        self.assertEqual(self.history_rows()[0]["posted_at"], "2020-01-02T00:30:00+09:00")

    def test_history_saved_record_occurs_after_archive_and_run_tracks_last_visited_post(self):
        original = HistoryStore.record_media
        observed = []
        def record(store, mode, metadata, path, *, status="saved"):
            observed.append((status, metadata["tweet_id"]))
            self.assertIn(f"twitter{metadata['_xlikesaver_archive_key']}", self.archive_keys())
            return original(store, mode, metadata, path, status=status)
        rows = [("100", 1, "photo", "alice", datetime(2026, 10, 4, tzinfo=timezone.utc)),
                ("101", 1, "video", "bob", datetime(2019, 1, 1, tzinfo=timezone.utc))]
        with patch.object(HistoryStore, "record_media", record):
            code, events = self.worker(rows, options=self.all_media())
        self.assertEqual(code, 0)
        self.assertEqual(observed, [("saved", "100"), ("saved", "101")])
        self.assertEqual(events[-1]["visited"], 2)
        self.assertEqual(events[-1]["last_tweet_id"], "101")
        self.assertEqual(events[-1]["last_posted_at"], "2019-01-01T09:00:00+09:00")
        runs = self.history_rows("runs")
        self.assertEqual(runs[-1]["last_tweet_id"], "101")
        self.assertEqual(runs[-1]["visited"], 2)

    def test_history_failure_is_reported_without_losing_download_archive(self):
        with patch.object(HistoryStore, "record_media", side_effect=HistoryError("secret " + test_core.SECRET)):
            code, events = self.worker([("100", 1, "photo", "alice")])
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]["status"], "error")
        self.assertIn("저장 이력", events[-1]["message"])
        self.assertEqual(self.archive_keys(), {"twitter100_1"})
        code, events = self.worker([("100", 1, "photo", "alice")])
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 0)
        self.assertEqual(events[-1]["skipped"], 1)
        self.assertEqual(self.history_rows()[0]["status"], "existing")

    def test_embedded_source_is_catalogued_and_existing_file_does_not_repeat_helper(self):
        rows = [("100", 1, "photo", "alice")]
        code, events = self.worker(rows)
        self.assertEqual(code, 0)
        self.embed_source.assert_called_once()
        saved = next(event for event in events if event["type"] == "saved")
        self.assertEqual(saved["source_url"], "https://x.com/i/web/status/100")
        self.assertEqual(saved["metadata_status"], "embedded")
        record = self.history_rows()[0]
        self.assertEqual(record["source_url"], saved["source_url"])
        self.assertEqual(record["metadata_status"], "embedded")
        self.embed_source.reset_mock()
        code, events = self.worker(rows)
        self.assertEqual(code, 0)
        self.embed_source.assert_not_called()
        self.assertEqual(next(event for event in events if event["type"] == "skipped")["metadata_status"], "embedded")
        self.assertEqual(self.history_rows()[0]["saved_at"], record["saved_at"])

    def test_author_and_numeric_id_are_from_tweet_not_liking_account(self):
        rows = [("100", 1, "photo", "alice", None, 123456789)]
        code, events = self.worker(rows)
        self.assertEqual(code, 0)
        self.assertEqual(self.embed_source.call_args.kwargs, {"author": "alice", "author_id": "123456789"})
        saved = next(event for event in events if event["type"] == "saved")
        self.assertEqual((saved["author"], saved["author_id"], saved["author_metadata_status"]), ("alice", "123456789", "embedded"))
        row = self.history_rows()[0]
        self.assertEqual((row["author"], row["author_id"], row["author_metadata_status"]), ("alice", "123456789", "embedded"))
        self.assertNotEqual(row["author_id"], "999")
        self.embed_source.reset_mock()
        self.worker(rows)
        self.embed_source.assert_not_called()

    def test_old_source_tagged_file_gets_author_once_without_redownload(self):
        rows = [("100", 1, "photo", "alice", None, 123)]
        self.worker(rows)
        original = self.history_rows()[0]
        with closing(sqlite3.connect(self.record_dir / ".xlikesaver-history.sqlite3")) as db:
            db.execute("UPDATE media SET author_metadata_status='pending',author_id=''")
            db.commit()
        self.embed_source.reset_mock()
        code, events = self.worker(rows)
        self.assertEqual(code, 0)
        self.assertEqual((events[-1]["saved"], events[-1]["skipped"]), (0, 1))
        self.embed_source.assert_called_once_with(original["path"], "100", author="alice", author_id="123")
        self.assertEqual(self.history_rows()[0]["saved_at"], original["saved_at"])
        self.embed_source.reset_mock()
        self.worker(rows)
        self.embed_source.assert_not_called()
        self.assertEqual(self.archive_keys(), {"twitter100_1"})

    def test_newly_available_numeric_id_triggers_only_one_author_update(self):
        self.worker([("100", 1, "photo", "alice")])
        self.embed_source.reset_mock()
        rows = [("100", 1, "photo", "alice", None, 123)]
        self.worker(rows)
        self.assertEqual(self.embed_source.call_args.kwargs["author_id"], "123")
        self.embed_source.reset_mock()
        self.worker(rows)
        self.embed_source.assert_not_called()

    def test_missing_author_is_unknown_and_source_is_not_rewritten_every_run(self):
        options = replace(self.options, folder_mode="flat")
        rows = [("100", 1, "photo", "")]
        self.worker(rows, options=options)
        self.assertEqual(self.embed_source.call_args.kwargs, {"author": "", "author_id": ""})
        row = self.history_rows()[0]
        self.assertEqual((row["author"], row["author_id"], row["author_metadata_status"]), ("", "", "unknown"))
        self.embed_source.reset_mock()
        self.worker(rows, options=options)
        self.embed_source.assert_not_called()

    def test_old_source_tagged_unknown_author_is_marked_unknown_without_rewriting(self):
        options = replace(self.options, folder_mode="flat")
        rows = [("100", 1, "photo", "")]
        self.worker(rows, options=options)
        with closing(sqlite3.connect(self.record_dir / ".xlikesaver-history.sqlite3")) as db:
            db.execute("UPDATE media SET author_metadata_status='pending'")
            db.commit()
        self.embed_source.reset_mock()
        code, events = self.worker(rows, options=options)
        self.assertEqual(code, 0)
        self.embed_source.assert_not_called()
        self.assertEqual(self.history_rows()[0]["author_metadata_status"], "unknown")
        self.assertEqual(events[-1]["metadata_failed"], 0)

    def test_failed_author_addition_keeps_verified_source_and_archive(self):
        rows = [("100", 1, "photo", "alice")]
        self.worker(rows)
        original = self.history_rows()[0]
        with closing(sqlite3.connect(self.record_dir / ".xlikesaver-history.sqlite3")) as db:
            db.execute("UPDATE media SET author_metadata_status='pending'")
            db.commit()
        self.embed_source.side_effect = lambda *args, **kwargs: {"status": "failed", "author_status": "error"}
        code, events = self.worker(rows)
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["metadata_failed"], 1)
        row = self.history_rows()[0]
        self.assertEqual((row["metadata_status"], row["author_metadata_status"]), ("embedded", "error"))
        self.assertEqual(row["saved_at"], original["saved_at"])
        self.assertEqual(self.archive_keys(), {"twitter100_1"})

    def test_source_failure_is_nonfatal_logged_once_and_existing_failure_can_retry(self):
        self.embed_source.side_effect = lambda *args, **kwargs: {"status": "failed", "url": "", "message": test_core.SECRET, "author_status": "error"}
        rows = [("100", 1, "photo", "alice"), ("101", 1, "video", "alice")]
        code, events = self.worker(rows, options=self.all_media())
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 2)
        self.assertEqual(events[-1]["metadata_failed"], 2)
        self.assertEqual(len([event for event in events if event["type"] == "log" and event.get("level") == "warning"]), 1)
        self.assertEqual({row["metadata_status"] for row in self.history_rows()}, {"failed"})
        self.assertEqual(len(self.archive_keys()), 2)
        self.embed_source.reset_mock()
        self.embed_source.side_effect = lambda path, tweet_id, **kwargs: {"status": "embedded", "url": f"https://x.com/i/web/status/{tweet_id}", "message": "", "author_status": "embedded"}
        code, events = self.worker(rows, options=self.all_media())
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 0)
        self.assertEqual(events[-1]["skipped"], 2)
        self.assertEqual(events[-1]["metadata_failed"], 0)
        self.assertEqual(self.embed_source.call_count, 2)
        self.assertEqual({row["metadata_status"] for row in self.history_rows()}, {"embedded"})

    def test_helper_unavailable_checked_once_and_never_invoked_for_each_file(self):
        with patch("xlikesaver.metadata.available", return_value=False) as available:
            code, events = self.worker([("100", 1, "photo", "alice"), ("101", 1, "photo", "alice")])
        self.assertEqual(code, 0)
        available.assert_called_once_with()
        self.embed_source.assert_not_called()
        self.assertEqual(events[-1]["metadata_failed"], 2)
        self.assertEqual(len([event for event in events if event["type"] == "log" and event.get("level") == "warning"]), 1)
        self.assertEqual(len(self.archive_keys()), 2)

    def test_progress_final_counts_original_payload_after_source_tags_increase_size(self):
        chunks = [MP4_HEADER, b"a" * 20]
        size = sum(map(len, chunks))
        def embed(path, tweet_id, **kwargs):
            target = Path(path)
            target.write_bytes(target.read_bytes() + b"offline-metadata-tags")
            return {"status": "embedded", "url": f"https://x.com/i/web/status/{tweet_id}", "message": "", "author_status": "embedded"}
        self.embed_source.side_effect = embed
        code, events, _ = self.http_worker([("100", 1, "video", "alice")], [self.response(chunks, size=size)])
        self.assertEqual(code, 0)
        final = [event for event in events if event["type"] == "progress"][-1]
        self.assertEqual(final["downloaded"], size)
        self.assertEqual(final["total"], size)
        self.assertIs(final["final"], True)
        self.assertEqual(final["metadata_status"], "embedded")
        self.assertGreater(self.history_rows()[0]["filesize"], size)

    def test_helper_unexpected_exception_is_secret_free_and_nonfatal(self):
        self.embed_source.side_effect = RuntimeError(test_core.SECRET)
        code, events = self.worker([("100", 1, "photo", "alice")])
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertEqual(events[-1]["metadata_failed"], 1)
        self.assertEqual(self.history_rows()[0]["metadata_status"], "failed")

    def test_unsupported_source_status_is_saved_and_does_not_retry(self):
        self.embed_source.side_effect = lambda *args, **kwargs: {"status": "unsupported", "url": "", "message": "", "author_status": "unknown"}
        rows = [("100", 1, "photo", "alice")]
        code, events = self.worker(rows)
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertEqual(events[-1]["metadata_failed"], 0)
        self.assertEqual(self.history_rows()[0]["metadata_status"], "unsupported")
        self.embed_source.reset_mock()
        code, events = self.worker(rows)
        self.assertEqual(code, 0)
        self.embed_source.assert_not_called()
        self.assertEqual(events[-1]["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
