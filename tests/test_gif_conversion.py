"""Real local FFmpeg conversions and resumable worker failure paths."""

import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageSequence

from tests import test_core
from xlikesaver import core, media


class GifConversionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "한글 GIF 변환"
        self.directory.mkdir()
        self.source = self.directory / "100_gif_1.mp4.part"
        self.executable = media.ffmpeg_path()
        self.assertIsNotNone(self.executable, "Pinned imageio-ffmpeg binary is required")
        subprocess.run([str(self.executable), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                        "-f", "lavfi", "-i", "testsrc2=size=80x64:rate=10:duration=0.8",
                        "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-f", "mp4", str(self.source)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
        self.original = self.source.read_bytes()

    def assert_source_only(self):
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(list(self.directory.iterdir()), [self.source])

    def test_real_conversion_keeps_dimensions_timing_loop_and_source(self):
        converted = media.convert_mp4_to_gif(self.source)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(converted.parent, self.source.parent)
        with Image.open(converted) as image:
            self.assertEqual(image.format, "GIF")
            self.assertEqual(image.size, (80, 64))
            self.assertEqual(image.info.get("loop"), 0)
            durations = [frame.info["duration"] for frame in ImageSequence.Iterator(image)]
            self.assertEqual(len(durations), 8)
            self.assertEqual(sum(durations), 800)
        converted.unlink()
        self.assert_source_only()

    def test_converter_does_not_accept_environment_override(self):
        with patch.dict(os.environ, {"IMAGEIO_FFMPEG_EXE": "C:/untrusted/ffmpeg.exe"}):
            self.assertEqual(media.ffmpeg_path(), self.executable)

    def test_output_size_failure_cleans_generated_files_and_keeps_mp4(self):
        with self.assertRaises(media.ConversionSizeLimit):
            media.convert_mp4_to_gif(self.source, max_bytes=32)
        self.assert_source_only()

    def test_bad_mp4_or_missing_engine_keeps_source(self):
        self.source.write_bytes(b"not a movie")
        self.original = self.source.read_bytes()
        with self.assertRaises(media.ConversionError):
            media.convert_mp4_to_gif(self.source)
        self.assert_source_only()
        with patch.object(media, "ffmpeg_path", return_value=None), self.assertRaises(media.ConversionError):
            media.convert_mp4_to_gif(self.source)
        self.assert_source_only()

    def test_stop_terminates_running_ffmpeg_and_cleans_partial_output(self):
        class Stopped(Exception):
            pass
        calls = 0
        def stop():
            nonlocal calls
            calls += 1
            if calls >= 3:
                raise Stopped()
        with self.assertRaises(Stopped):
            media.convert_mp4_to_gif(self.source, check_stop=stop)
        self.assert_source_only()


class GifWorkerTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    write_cookies = test_core.CoreTests.write_cookies
    worker = test_core.CoreTests.worker

    def options_all(self, **changes):
        return replace(self.options, include_gifs=True, **changes)

    def keys(self):
        with closing(sqlite3.connect(self.record_dir / ".xlikesaver-archive.sqlite3")) as database:
            return {row[0] for row in database.execute("SELECT entry FROM archive")}

    def records(self):
        with closing(sqlite3.connect(self.record_dir / ".xlikesaver-history.sqlite3")) as database:
            database.row_factory = sqlite3.Row
            return [dict(row) for row in database.execute("SELECT * FROM media")]

    def convert(self, source, **kwargs):
        self.assertTrue(source.name.endswith(".mp4.part"))
        self.assertTrue(core._gif_source_complete(source))
        kwargs["check_stop"]()
        output = source.parent / ".test-gif.gif"
        Image.new("RGB", (10, 10), "green").save(output, "GIF")
        return output

    def test_new_gif_is_finalized_catalogued_and_archived_once(self):
        posted = test_core.datetime(2020, 1, 1, tzinfo=test_core.timezone.utc)
        rows = [("100", 1, "animated_gif", "alice", posted, 123)]
        with patch("xlikesaver.core.convert_mp4_to_gif", side_effect=self.convert) as convert:
            code, events = self.worker(rows, options=self.options_all())
            self.assertEqual(code, 0)
            self.assertEqual(self.keys(), {"twitter100_animated_gif_1"})
            row = self.records()[0]
            self.assertEqual(Path(row["path"]).suffix, ".gif")
            self.assertEqual((row["author"], row["author_id"], row["metadata_status"]), ("alice", "123", "embedded"))
            self.assertEqual(self.embed_source.call_args.args[0], row["path"])
            self.assertFalse(list(self.output.rglob("*.part*")))
            self.assertFalse(list(self.output.rglob("*.mp4")))
            self.assertEqual([event["stage"] for event in events if event["type"] == "converting"], ["started", "finished"])
            self.assertTrue(all(event["author"] == "alice" and event["posted_at"]
                                for event in events if event["type"] == "converting"))
            code, events = self.worker(rows, options=self.options_all())
            self.assertEqual((code, events[-1]["saved"], events[-1]["skipped"]), (0, 0, 1))
            convert.assert_called_once()
        self.assertEqual(len(test_core.FakeHttpDownloader.downloaded), 1)

    def test_failed_conversion_preserves_complete_mp4_and_retries_without_redownload(self):
        rows = [("100", 1, "animated_gif", "alice")]
        with patch("xlikesaver.core.convert_mp4_to_gif", side_effect=media.ConversionError("GIF 변환을 다시 시도해 주세요.")):
            code, events = self.worker(rows, options=self.options_all())
        self.assertNotEqual(code, 0)
        self.assertEqual((events[-1]["saved"], self.keys(), self.records()), (0, set(), []))
        source = self.output / "myaccount" / "alice" / "100_gif_1.mp4.part"
        self.assertTrue(core._gif_source_complete(source))
        with patch("xlikesaver.core.convert_mp4_to_gif", side_effect=self.convert):
            code, events = self.worker(rows, options=self.options_all())
        self.assertEqual((code, events[-1]["saved"]), (0, 1))
        self.assertEqual(len(test_core.FakeHttpDownloader.downloaded), 1)
        self.assertFalse(source.exists())
        self.assertTrue(self.records()[0]["path"].endswith(".gif"))

    def test_conversion_cancel_preserves_source_and_does_not_archive(self):
        flag = self.base / "stop"
        def stop(source, **kwargs):
            flag.touch()
            kwargs["check_stop"]()
        with patch("xlikesaver.core.convert_mp4_to_gif", side_effect=stop):
            code, events = self.worker([("100", 1, "animated_gif", "alice")], options=self.options_all(), stop_file=str(flag))
        self.assertEqual((code, events[-1]["status"], events[-1]["saved"]), (0, "stopped", 0))
        self.assertEqual(self.keys(), set())
        self.assertEqual(self.records(), [])
        source = self.output / "myaccount" / "alice" / "100_gif_1.mp4.part"
        self.assertTrue(core._gif_source_complete(source))

    def test_conversion_size_cap_is_not_archived_and_next_file_can_save(self):
        with patch("xlikesaver.core.convert_mp4_to_gif", side_effect=media.ConversionSizeLimit()):
            code, events = self.worker([("100", 1, "animated_gif", "alice"), ("101", 1, "photo", "alice")],
                                      options=self.options_all())
        self.assertEqual((code, events[-1]["saved"], events[-1]["size_skipped"]), (0, 1, 1))
        self.assertEqual(self.keys(), {"twitter101_1"})
        self.assertTrue(list(self.output.rglob("*.mp4.part")))

    def test_destination_created_during_conversion_is_not_overwritten_or_archived(self):
        destination = self.output / "myaccount" / "alice" / "100_gif_1.gif"
        def collision(source, **kwargs):
            output = self.convert(source, **kwargs)
            destination.write_bytes(b"another process created this file")
            return output
        with patch("xlikesaver.core.convert_mp4_to_gif", side_effect=collision):
            code, events = self.worker([("100", 1, "animated_gif", "alice")], options=self.options_all())
        self.assertNotEqual(code, 0)
        self.assertEqual(destination.read_bytes(), b"another process created this file")
        self.assertEqual(self.keys(), set())
        self.assertEqual(self.records(), [])
        self.assertTrue(list(self.output.rglob("*.mp4.part")))
        self.assertFalse(list(self.output.rglob(".test-gif.gif")))

    def test_marker_does_not_treat_an_arbitrary_partial_download_as_complete(self):
        source = self.base / "unfinished.mp4.part"
        source.write_bytes(b"only part of the source")
        self.assertFalse(core._gif_source_complete(source))
        core._mark_gif_source_complete(source)
        self.assertTrue(core._gif_source_complete(source))
        source.write_bytes(b"changed incomplete source with another size")
        self.assertFalse(core._gif_source_complete(source))

    def test_existing_mp4_gif_is_not_rewritten(self):
        path = self.output / "myaccount" / "alice" / "100_gif_1.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"previous downloaded MP4")
        with patch("xlikesaver.core.convert_mp4_to_gif") as convert:
            code, events = self.worker([("100", 1, "animated_gif", "alice")], options=self.options_all())
        self.assertEqual((code, events[-1]["saved"], events[-1]["skipped"]), (0, 0, 1))
        convert.assert_not_called()
        self.assertEqual(path.read_bytes(), b"previous downloaded MP4")
        self.assertEqual(self.records()[0]["path"], str(path))

    def test_existing_gif_without_ledger_is_recognized(self):
        path = self.output / "myaccount" / "alice" / "100_gif_1.gif"
        path.parent.mkdir(parents=True)
        Image.new("RGB", (10, 10), "green").save(path)
        with patch("xlikesaver.core.convert_mp4_to_gif") as convert:
            code, events = self.worker([("100", 1, "animated_gif", "alice")], options=self.options_all())
        self.assertEqual((code, events[-1]["saved"], events[-1]["skipped"]), (0, 0, 1))
        convert.assert_not_called()
        self.assertEqual(self.records()[0]["path"], str(path))


if __name__ == "__main__":
    unittest.main()
