"""Automatic kept previews use real local media and fake download responses."""

from contextlib import closing
from dataclasses import replace
from io import BytesIO
import os
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

from PIL import Image

from luvbox import thumbcache
from luvbox.history import HistoryError, HistoryStore
from luvbox.media import ffmpeg_path
from tests import test_core


class AutomaticPreviewTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    write_cookies = test_core.CoreTests.write_cookies

    def payload(self, media_type):
        if media_type == "photo":
            stream = BytesIO()
            with Image.new("RGB", (320, 240), "red") as image:
                image.save(stream, "JPEG")
            return stream.getvalue()
        if not hasattr(self, "video_payload"):
            executable = ffmpeg_path()
            if executable is None:
                self.skipTest("Bundled local video decoder is unavailable")
            source = self.base / "synthetic-source.mp4"
            generated = subprocess.run(
                [str(executable), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                 "testsrc2=size=160x96:rate=10", "-t", "0.2", "-an", "-c:v", "mpeg4", str(source)],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(generated.returncode, 0, generated.stderr.decode("utf8", errors="replace"))
            self.video_payload = source.read_bytes()
        return self.video_payload

    def download(self, rows, **kwargs):
        payloads = {row[2]: self.payload(row[2]) for row in rows}

        def save(downloader, url, pathfmt):
            test_core.FakeHttpDownloader.downloaded.append(url)
            with pathfmt.open() as stream:
                stream.write(payloads[pathfmt.kwdict["type"]])
            return True

        with patch.object(thumbcache, "directory", return_value=self.base / "thumbnails"), \
                patch.object(test_core.FakeHttpDownloader, "download", save), \
                patch("requests.Session.request") as network:
            result = test_core.CoreTests.worker(self, rows, **kwargs)
        network.assert_not_called()
        return result

    def kept(self, tweet, media_type="photo"):
        with patch.object(thumbcache, "directory", return_value=self.base / "thumbnails"):
            image = thumbcache.recall("myaccount", (tweet, 1, media_type))
        self.assertIsNotNone(image)
        self.assertLessEqual(image.width, thumbcache.SIZE[0])
        self.assertLessEqual(image.height, thumbcache.SIZE[1])
        image.close()

    @unittest.skipUnless(os.name == "nt", "Windows bundled FFmpeg execution")
    def test_new_downloads_keep_previews_in_every_folder_and_account_layout(self):
        for index, (mode, separate) in enumerate((mode, separate)
                                               for mode in ("author", "date", "flat")
                                               for separate in (False, True)):
            with self.subTest(mode=mode, separate_accounts=separate):
                options = replace(self.options, folder_mode=mode, separate_accounts=separate,
                                  include_videos=True, include_gifs=True, gif_format="gif" if separate else "mp4")
                rows = [(str(100 + index * 10 + offset), 1, kind, "alice")
                        for offset, kind in enumerate(("photo", "video", "animated_gif"))]
                code, events = self.download(rows, options=options)
                self.assertEqual((code, events[-1]["saved"]), (0, 3))
                for tweet, _, kind, _ in rows:
                    self.kept(tweet, kind)

    def test_visited_existing_file_fills_only_its_missing_preview(self):
        rows = [("100", 1, "photo", "alice")]
        code, events = self.download(rows)
        self.assertEqual(code, 0)
        path = Path(next(event["path"] for event in events if event["type"] == "saved"))
        before = path.read_bytes(), path.stat().st_mtime_ns
        with patch.object(thumbcache, "directory", return_value=self.base / "thumbnails"):
            with closing(thumbcache.Cache("myaccount")) as cache:
                cache.prune(1)
        with patch.object(thumbcache, "make", wraps=thumbcache.make) as make, \
                patch.object(HistoryStore, "present_files", side_effect=AssertionError("no full-library scan")):
            code, events = self.download(rows)
        self.assertEqual((code, events[-1]["saved"], events[-1]["skipped"]), (0, 0, 1))
        make.assert_called_once()
        self.kept("100")
        with patch.object(thumbcache, "make", wraps=thumbcache.make) as make:
            self.assertEqual(self.download(rows)[0], 0)
        make.assert_not_called()
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(len(test_core.FakeHttpDownloader.downloaded), 1)

    def test_catalog_recording_failure_does_not_lose_the_saved_file_preview(self):
        with patch.object(HistoryStore, "record_media", side_effect=HistoryError("offline catalog failure")):
            code, events = self.download([("100", 1, "photo", "alice")])
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertTrue(Path(next(event["path"] for event in events if event["type"] == "saved")).is_file())
        self.kept("100")

    def test_stopping_or_reaching_a_limit_flushes_a_temporarily_busy_cache(self):
        original = thumbcache.Cache.put
        for index, limited in enumerate((False, True)):
            with self.subTest(limited=limited):
                calls = 0

                def busy_once(cache, key, image):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise sqlite3.OperationalError("offline temporary cache lock")
                    return original(cache, key, image)

                tweet = str(100 + index * 10)
                flag = self.base / "stop.flag"
                rows = [(tweet, 1, "photo", "alice"), (str(int(tweet) + 1), 1, "photo", "alice")]

                def before(_extractor, row):
                    if row[0] != tweet and not limited:
                        flag.touch()

                with patch.object(thumbcache.Cache, "put", busy_once):
                    code, events = self.download(rows, options=replace(self.options, photo_limit=1 if limited else 0),
                                                 before_row=before, stop_file=str(flag))
                self.assertEqual((code, events[-1]["saved"]), (0, 1))
                self.assertEqual(events[-1]["status"], "finished" if limited else "stopped")
                self.assertGreaterEqual(calls, 2)
                self.kept(tweet)
                flag.unlink(missing_ok=True)

    def test_permanent_cache_failure_preserves_download_and_reports_one_safe_warning(self):
        with patch.object(thumbcache.Cache, "put", side_effect=sqlite3.OperationalError(test_core.SECRET)):
            code, events = self.download([("100", 1, "photo", "alice")])
        self.assertEqual((code, events[-1]["saved"]), (0, 1))
        warnings = [event["message"] for event in events if event["type"] == "log" and
                    event.get("level") == "warning" and "작은 미리보기" in event.get("message", "")]
        self.assertEqual(len(warnings), 1)
        self.assertNotIn(test_core.SECRET, warnings[0])
        self.assertTrue(Path(next(event["path"] for event in events if event["type"] == "saved")).is_file())


if __name__ == "__main__":
    unittest.main()
