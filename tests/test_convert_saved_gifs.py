"""Converting already saved MP4 GIFs in place, with local FFmpeg only."""

import os
import queue
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from xlikesaver import media, metadata
from xlikesaver.history import HistoryError, HistoryStore
from xlikesaver.history_gui import HistoryWindow


ORIGINAL_TIME = 1_600_000_000


class ConvertSavedGifTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.account = Path(self.temp.name) / "한글 계정"
        self.store = HistoryStore(self.account)
        self.addCleanup(self.store.close)
        self.recycled = []

    def movie(self, name):
        path = self.account / name
        subprocess.run([str(media.ffmpeg_path()), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                        "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=10:duration=0.5",
                        "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-f", "mp4", str(path)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=20)
        os.utime(path, (ORIGINAL_TIME, ORIGINAL_TIME))
        return path

    def record(self, tweet, path, author="alice"):
        self.store.record_media("flat", {"tweet_id": tweet, "num": 1, "type": "animated_gif",
                                         "author": {"name": author, "id": 123}}, path)

    def recycle(self, path):
        self.recycled.append(Path(path))
        Path(path).unlink()
        return True

    def convert(self, *, recycle=None, can_recycle=True, stop=None, size="original", max_mb=200):
        results = queue.Queue()
        with patch("xlikesaver.media.recycle", side_effect=recycle or self.recycle) as recycled, \
                patch("xlikesaver.media.can_recycle", return_value=can_recycle), \
                patch("requests.Session.request") as network:
            HistoryWindow._convert_worker(results, stop or threading.Event(), 1, self.account, size, max_mb)
        network.assert_not_called()
        messages = []
        while not results.empty():
            messages.append(results.get_nowait())
        return messages, recycled

    def row(self, tweet):
        return next(row for row in self.store.records() if row["tweet_id"] == tweet)

    def assert_no_temporary_files(self):
        self.assertFalse([path for path in self.account.iterdir() if path.name.startswith(".xlikesaver-gif")])

    def test_mp4_gifs_become_gifs_and_originals_go_to_the_recycle_bin(self):
        good = self.movie("100_gif_1.mp4")
        self.record("100", good)
        broken = self.account / "101_gif_1.mp4"
        broken.write_bytes(b"not a movie")
        self.record("101", broken)
        missing = self.movie("102_gif_1.mp4")
        self.record("102", missing)
        missing.unlink()
        before = self.row("100")
        messages, _ = self.convert(size="480")
        kind, final = messages[-1][1:]
        self.assertEqual(kind, "converted")
        self.assertIn("바꿈 1개 / MP4로 둠 0개 / 실패 1개 / 파일 없음 1개", final)
        gif = self.account / "100_gif_1.gif"
        with Image.open(gif) as image:
            self.assertEqual((image.format, image.size), ("GIF", (96, 64)))
        self.assertEqual(self.recycled, [good])
        self.assertEqual(int(gif.stat().st_mtime), ORIGINAL_TIME)
        row = self.row("100")
        self.assertEqual((row["path"], row["filename"], row["saved_at"]), (str(gif.resolve()), gif.name, before["saved_at"]))
        if metadata.available():
            self.assertEqual((row["metadata_status"], row["author_metadata_status"]), ("embedded", "embedded"))
            self.assertIn("100", metadata.read_source(gif))
        # The failed one stays an MP4 and is offered again next time.
        self.assertEqual(self.row("101")["path"], str(broken.resolve()))
        self.assertTrue(broken.is_file())
        self.assertEqual({row["tweet_id"] for row in self.store.mp4_gif_records()}, {"101", "102"})
        self.assert_no_temporary_files()

    def test_mp4_that_cannot_be_recycled_is_kept_and_the_new_gif_removed(self):
        source = self.movie("100_gif_1.mp4")
        self.record("100", source)
        messages, _ = self.convert(recycle=lambda path: False)
        self.assertIn("실패 1개", messages[-1][2])
        self.assertTrue(source.is_file())
        self.assertFalse((self.account / "100_gif_1.gif").exists())
        self.assertEqual(self.row("100")["path"], str(source.resolve()))
        self.assert_no_temporary_files()

    def test_record_update_failure_keeps_the_mp4_and_continues(self):
        first, second = self.movie("100_gif_1.mp4"), self.movie("101_gif_1.mp4")
        self.record("100", first)
        self.record("101", second)
        original = HistoryStore.replace_media_file

        def refuse_first(store, row, *args, **kwargs):
            if row["tweet_id"] == "100":
                raise HistoryError("catalog busy")
            return original(store, row, *args, **kwargs)

        with patch.object(HistoryStore, "replace_media_file", refuse_first):
            messages, _ = self.convert()
        self.assertIn("바꿈 1개 / MP4로 둠 0개 / 실패 1개", messages[-1][2])
        self.assertEqual(self.recycled, [second])
        self.assertTrue(first.is_file())
        self.assertFalse((self.account / "100_gif_1.gif").exists())
        self.assertEqual(self.row("100")["path"], str(first.resolve()))
        self.assertTrue((self.account / "101_gif_1.gif").is_file())

    def test_source_edited_during_conversion_is_preserved_without_a_gif(self):
        source = self.movie("100_gif_1.mp4")
        self.record("100", source)
        changed = source.read_bytes() + b"newer edit during conversion"
        convert = media.convert_mp4_to_gif

        def edit_after_conversion(path, **options):
            result = convert(path, **options)
            path.write_bytes(changed)
            return result

        with patch("xlikesaver.media.convert_mp4_to_gif", side_effect=edit_after_conversion):
            messages, recycled = self.convert()
        recycled.assert_not_called()
        self.assertIn("실패 1개", messages[-1][2])
        self.assertEqual(source.read_bytes(), changed)
        self.assertEqual(self.row("100")["path"], str(source.resolve()))
        self.assertFalse(source.with_suffix(".gif").exists())
        self.assert_no_temporary_files()

    def test_source_edited_after_catalog_update_is_preserved_and_record_restored(self):
        source = self.movie("100_gif_1.mp4")
        self.record("100", source)
        changed = source.read_bytes() + b"newer edit before recycling"
        replace_file = HistoryStore.replace_media_file

        def edit_after_record(store, row, path, *args, **options):
            updated = replace_file(store, row, path, *args, **options)
            if Path(path).suffix == ".gif":
                source.write_bytes(changed)
            return updated

        with patch.object(HistoryStore, "replace_media_file", edit_after_record):
            messages, recycled = self.convert()
        recycled.assert_not_called()
        self.assertIn("실패 1개", messages[-1][2])
        self.assertEqual(source.read_bytes(), changed)
        self.assertEqual(self.row("100")["path"], str(source.resolve()))
        self.assertFalse(source.with_suffix(".gif").exists())
        self.assert_no_temporary_files()

    def test_failed_catalog_rollback_keeps_both_files_and_the_referenced_gif(self):
        source = self.movie("100_gif_1.mp4")
        self.record("100", source)
        replace_file = HistoryStore.replace_media_file

        def refuse_rollback(store, row, path, *args, **options):
            if Path(path) == source:
                return False
            return replace_file(store, row, path, *args, **options)

        with patch.object(HistoryStore, "replace_media_file", refuse_rollback):
            messages, _ = self.convert(recycle=lambda path: False)
        destination = source.with_suffix(".gif")
        self.assertEqual(messages[-1][1], "error")
        self.assertTrue(source.is_file() and destination.is_file())
        self.assertEqual(self.row("100")["path"], str(destination.resolve()))
        self.assert_no_temporary_files()

    def test_no_recycle_bin_existing_gif_or_size_limit_keep_the_mp4_untouched(self):
        source = self.movie("100_gif_1.mp4")
        self.record("100", source)
        with patch("xlikesaver.media.convert_mp4_to_gif") as convert:
            messages, recycled = self.convert(can_recycle=False)
        convert.assert_not_called()
        recycled.assert_not_called()
        self.assertIn("MP4로 둠 1개", messages[-1][2])
        other = self.account / "100_gif_1.gif"
        other.write_bytes(b"GIF89a someone else's file")
        with patch("xlikesaver.media.convert_mp4_to_gif") as convert:
            messages, _ = self.convert()
        convert.assert_not_called()
        self.assertEqual(other.read_bytes(), b"GIF89a someone else's file")
        other.unlink()
        with patch("xlikesaver.media.convert_mp4_to_gif", side_effect=media.ConversionSizeLimit()):
            messages, recycled = self.convert(max_mb=1)
        recycled.assert_not_called()
        self.assertIn("MP4로 둠 1개", messages[-1][2])
        self.assertTrue(source.is_file())
        self.assertEqual(self.row("100")["path"], str(source.resolve()))

    def test_stop_ends_before_the_next_file_and_cleans_up(self):
        first, second = self.movie("100_gif_1.mp4"), self.movie("101_gif_1.mp4")
        self.record("100", first)
        self.record("101", second)
        stop = threading.Event()
        real_convert = media.convert_mp4_to_gif

        def convert_then_stop(source, **kwargs):
            stop.set()
            # The running conversion notices the stop and removes its output.
            return real_convert(source, **kwargs)

        with patch("xlikesaver.media.convert_mp4_to_gif", side_effect=convert_then_stop):
            messages, recycled = self.convert(stop=stop)
        recycled.assert_not_called()
        self.assertIn("GIF 변환 중단 · 바꿈 0개", messages[-1][2])
        self.assertTrue(first.is_file() and second.is_file())
        self.assertFalse(list(self.account.glob("*.gif")))
        self.assert_no_temporary_files()


if __name__ == "__main__":
    unittest.main()
