"""Real local FFmpeg conversions and resumable worker failure paths."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageSequence

from tests import test_core, test_missing_files
from luvbox import core, media
from luvbox.history import HistoryStore
from luvbox.storage import migrate_records, resolve_storage


@unittest.skipUnless(os.name == "nt", "Windows bundled FFmpeg execution")
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

    def make_source(self, name, size, rate):
        source = self.directory / name
        subprocess.run([str(self.executable), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration=1",
                        "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-f", "mp4", str(source)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=20)
        return source

    def test_smaller_size_presets_fit_a_box_and_cap_the_frame_rate(self):
        cases = ((("wide.mp4", "1280x720", 30), "480", (480, 270)),
                 (("tall.mp4", "360x640", 30), "480", (270, 480)),
                 (("wide.mp4", "1280x720", 30), "720", (720, 406)),
                 # Small or slow sources are never enlarged or given extra frames.
                 (("slow.mp4", "300x200", 10), "480", (300, 200)))
        for (name, size, rate), preset, dimensions in cases:
            with self.subTest(name=name, preset=preset):
                source = self.make_source(name, size, rate)
                converted = media.convert_mp4_to_gif(source, size=preset)
                try:
                    with Image.open(converted) as image:
                        self.assertEqual(image.size, dimensions)
                        durations = [frame.info["duration"] for frame in ImageSequence.Iterator(image)]
                    limit = media.GIF_SIZES[preset][1]
                    self.assertEqual(len(durations), min(rate, limit))
                    self.assertEqual(sum(durations), 1000)
                finally:
                    converted.unlink()
                    source.unlink()
        with self.assertRaises(ValueError):
            media.convert_mp4_to_gif(self.source, size="1080")
        self.assert_source_only()

    def test_converter_does_not_accept_environment_override(self):
        with patch.dict(os.environ, {"IMAGEIO_FFMPEG_EXE": "C:/untrusted/ffmpeg.exe"}):
            self.assertEqual(media.ffmpeg_path(), self.executable)

    def test_both_conversion_passes_force_the_mp4_source_format(self):
        with patch.object(media, "_run", wraps=media._run) as run:
            converted = media.convert_mp4_to_gif(self.source)
        try:
            self.assertEqual(run.call_count, 2)
            for call in run.call_args_list:
                arguments = call.args[0]
                source_input = arguments.index("-i")
                self.assertEqual(arguments[source_input - 2:source_input + 2],
                                 ["-f", "mov", "-i", str(self.source)])
        finally:
            converted.unlink()
        self.assert_source_only()

    def test_development_decoder_rejects_a_playlist_disguised_as_mp4(self):
        import imageio_ffmpeg
        from luvbox.preview import thumbnail

        directory = Path(imageio_ffmpeg.__file__).resolve().parent / "binaries"
        fallback = next((path for path in sorted(directory.glob("ffmpeg-*"))
                         if path.is_file() and (os.name != "nt" or path.suffix == ".exe")), None)
        if fallback is None:
            self.skipTest("Development FFmpeg binary is not installed")
        playlist = self.directory / "playlist.mp4.part"
        playlist.write_text(f"ffconcat version 1.0\nfile '{self.source.name}'\n", encoding="ascii")
        before = playlist.read_bytes()
        try:
            with patch.object(media, "ffmpeg_path", return_value=fallback):
                self.assertIsNone(thumbnail(playlist, suffix=".mp4").image)
                with self.assertRaises(media.ConversionError):
                    media.convert_mp4_to_gif(playlist)
            self.assertEqual(playlist.read_bytes(), before)
            self.assertEqual(sorted(self.directory.iterdir()), sorted([self.source, playlist]))
        finally:
            playlist.unlink()
        self.assert_source_only()

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

    def test_process_protection_failure_stops_helper_and_preserves_source(self):
        processes = []
        start = subprocess.Popen

        def capture(*args, **kwargs):
            # Keep this disposable helper alive until the failed setup is
            # retired, even on a machine that converts the tiny fixture fast.
            process = start([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
            processes.append(process)
            return process

        with patch.object(media.subprocess, "Popen", side_effect=capture), \
                patch.object(media, "ChildProcessJob", side_effect=OSError("private Windows failure details")), \
                self.assertRaises(media.ConversionError) as error:
            media.convert_mp4_to_gif(self.source)
        self.assertNotIn("private Windows failure details", str(error.exception))
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assert_source_only()

    @unittest.skipUnless(os.name == "nt", "Windows private process jobs")
    def test_completed_conversion_is_accepted_if_job_assignment_loses_the_exit_race(self):
        create_job = media.ChildProcessJob

        def after_exit(process):
            process.wait(timeout=5)
            return create_job(process)

        with patch.object(media, "ChildProcessJob", side_effect=after_exit):
            converted = media.convert_mp4_to_gif(self.source)
        try:
            with Image.open(converted) as image:
                self.assertEqual(image.format, "GIF")
                self.assertEqual(image.size, (80, 64))
        finally:
            converted.unlink()
        self.assert_source_only()


class GifWorkerTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    set_session = test_core.CoreTests.set_session
    worker = test_core.CoreTests.worker

    def options_all(self, **changes):
        return replace(self.options, include_gifs=True, **changes)

    def keys(self):
        with closing(sqlite3.connect(self.record_dir / ".luvbox-archive.sqlite3")) as database:
            return {row[0] for row in database.execute("SELECT entry FROM archive")}

    def records(self):
        with closing(sqlite3.connect(self.record_dir / ".luvbox-history.sqlite3")) as database:
            database.row_factory = sqlite3.Row
            return [dict(row) for row in database.execute("SELECT * FROM media")]

    def convert(self, source, **kwargs):
        self.assertTrue(source.name.endswith(".mp4.part"))
        self.assertTrue(core._transfer_complete(source))
        kwargs["check_stop"]()
        output = source.parent / ".test-gif.gif"
        Image.new("RGB", (10, 10), "green").save(output, "GIF")
        return output

    def retry_gif(self, options=None):
        test_missing_files.FakeTweetExtractor.requested = []
        events = []
        with patch("gallery_dl.extractor.find", side_effect=lambda url:
                   test_missing_files.FakeTweetExtractor(url, {"100": [(1, "animated_gif")]})), \
                patch("gallery_dl.downloader.find", return_value=test_core.FakeHttpDownloader):
            code = core.run_worker(replace(options or self.options_all(), targets=("100_animated_gif_1",)),
                                   events.append)
        self.assertNotIn(test_core.SECRET, json.dumps(events))
        self.assertEqual(events[-1]["type"], "finished")
        self.assertEqual(test_missing_files.FakeTweetExtractor.requested, ["100"])
        return code, events

    def test_new_gif_is_finalized_catalogued_and_archived_once(self):
        posted = test_core.datetime(2020, 1, 1, tzinfo=test_core.timezone.utc)
        rows = [("100", 1, "animated_gif", "alice", posted, 123)]
        with patch("luvbox.core.convert_mp4_to_gif", side_effect=self.convert) as convert:
            code, events = self.worker(rows, options=self.options_all())
            self.assertEqual((code, events[-1]["status"], events[-1]["saved"], events[-1]["skipped"]),
                             (0, "finished", 1, 0))
            self.assertEqual(self.keys(), {"twitter100_animated_gif_1"})
            row = self.records()[0]
            self.assertEqual(Path(row["path"]).suffix, ".gif")
            self.assertEqual((row["status"], row["record_source"], row["media_type"]),
                             ("saved", "saved", "animated_gif"))
            saved_event = next(event for event in events if event["type"] == "saved")
            self.assertEqual(saved_event["path"], row["path"])
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

    def test_externally_removed_converted_gif_is_missing_and_selected_retry_restores_it(self):
        rows = [("100", 1, "animated_gif", "alice")]
        options = self.options_all()
        paths = resolve_storage(self.output, "myaccount", options.separate_accounts)
        with patch("luvbox.core.convert_mp4_to_gif", side_effect=self.convert) as convert:
            code, events = self.worker(rows, options=options)
            self.assertEqual((code, events[-1]["status"], events[-1]["saved"]), (0, "finished", 1))
            before = self.records()[0]
            gif = Path(before["path"])
            content = gif.read_bytes()
            with closing(HistoryStore.for_storage(paths)) as store:
                summary = store.overview(verify_files=False)
                self.assertEqual((summary["files"], summary["animated_gif"], summary["missing_files"]), (1, 1, 0))
                self.assertEqual(summary["bytes"], len(content))

            # The output is an ordinary saved GIF. Removing it outside the app
            # leaves its history and archive key available for selective retry.
            gif.rename(self.base / "externally-removed.gif")
            with closing(HistoryStore.for_storage(paths)) as store:
                store.import_legacy(refresh=True)
                summary = store.overview(verify_files=False)
                self.assertEqual((summary["files"], summary["animated_gif"], summary["bytes"],
                                  summary["missing_files"]), (0, 0, 0, 1))
                self.assertEqual(summary["stats"]["authors"], [])
                self.assertEqual(summary["stats"]["months"]["saved"], [])
                [missing] = summary["missing"]
                self.assertEqual((missing["archive_key"], missing["media_type"], missing["filename"]),
                                 ("100_animated_gif_1", "animated_gif", gif.name))
                retained = store.records()[0]
                self.assertEqual((retained["saved_at"], retained["source_url"]),
                                 (before["saved_at"], before["source_url"]))
            self.assertEqual(self.keys(), {"twitter100_animated_gif_1"})

            # An ordinary run respects the completed archive instead of undoing
            # an external deletion. Only the chosen retry fetches the source.
            test_core.FakeHttpDownloader.downloaded = []
            code, events = self.worker(rows, options=options)
            self.assertEqual((code, events[-1]["saved"], events[-1]["skipped"]), (0, 0, 1))
            self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
            convert.assert_called_once()
            self.assertFalse(gif.exists())

            code, events = self.retry_gif(options)
            self.assertEqual((code, events[-1]["status"], events[-1]["saved"], events[-1]["recovered"]),
                             (0, "finished", 1, 1))
            self.assertEqual(test_core.FakeHttpDownloader.downloaded, ["https://video.twimg.com/100_1.mp4"])
            self.assertEqual(convert.call_count, 2)
            self.assertEqual(gif.read_bytes(), content)
            restored = self.records()[0]
            self.assertEqual((restored["path"], restored["status"]), (str(gif), "saved"))
            self.assertEqual(next(event["path"] for event in events if event["type"] == "saved"), str(gif))
            self.assertEqual(self.keys(), {"twitter100_animated_gif_1"})
            self.assertFalse(list(self.output.rglob("*.part*")))
            self.assertFalse(list(self.output.rglob("*.mp4")))
            with closing(HistoryStore.for_storage(paths)) as store:
                summary = store.overview(verify_files=False)
                self.assertEqual((summary["files"], summary["animated_gif"], summary["missing_files"]), (1, 1, 0))
                self.assertEqual(summary["bytes"], len(content))
                self.assertEqual(summary["missing"], [])

    def test_selected_retry_adopts_existing_gif_at_new_output_when_former_path_disappeared(self):
        options = self.options_all()
        with patch("luvbox.core.convert_mp4_to_gif", side_effect=self.convert):
            code, _ = self.worker([("100", 1, "animated_gif", "alice")], options=options)
        self.assertEqual(code, 0)
        old = self.records()[0]
        original = Path(old["path"])
        content = original.read_bytes()
        source = resolve_storage(self.output, "myaccount", options.separate_accounts)
        destination = resolve_storage(self.base / "new-output", "myaccount", options.separate_accounts)
        migrate_records(source, destination)
        original.rename(self.base / "externally-moved.gif")
        with closing(HistoryStore.for_storage(destination)) as store:
            store.import_legacy(refresh=True)
            # Former roots retain their catalog location even while absent.
            # It must not mask the selected retry's verified current file hit.
            self.assertEqual(store.records()[0]["path"], str(original))
            self.assertEqual(store.summary(verify_files=False)["missing_files"], 1)
            store.set_missing_ignored([("100", 1, "animated_gif")], True)
        gif = destination.media_dir / "alice" / original.name
        gif.parent.mkdir(parents=True, exist_ok=True)
        gif.write_bytes(content)
        test_core.FakeHttpDownloader.downloaded = []
        retry_options = replace(options, output_dir=str(destination.output_dir))
        with patch("luvbox.core.convert_mp4_to_gif") as convert:
            code, events = self.retry_gif(retry_options)
        convert.assert_not_called()
        self.assertEqual((code, events[-1]["status"], events[-1]["saved"], events[-1]["recovered"],
                          events[-1]["skipped"]), (0, "finished", 0, 1, 1))
        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
        self.assertEqual(next(event["path"] for event in events if event["type"] == "skipped"), str(gif))
        self.assertEqual(gif.read_bytes(), content)
        with closing(HistoryStore.for_storage(destination)) as store:
            restored = store.records()[0]
            self.assertEqual((restored["path"], restored["media_type"]), (str(gif), "animated_gif"))
            self.assertEqual(restored["saved_at"], old["saved_at"])
            self.assertEqual(store.ignored_media(), set())
            summary = store.overview(verify_files=False)
            self.assertEqual((summary["files"], summary["animated_gif"], summary["missing_files"]), (1, 1, 0))
            self.assertEqual(summary["bytes"], len(content))

    def assert_saved_as_original_mp4(self, events, message):
        mp4 = self.output / "myaccount" / "alice" / "alice_100_gif_1.mp4"
        self.assertTrue(mp4.is_file())
        self.assertFalse(list(self.output.rglob("*.part*")))
        self.assertFalse(list(self.output.rglob("*.gif")))
        self.assertEqual(self.keys(), {"twitter100_animated_gif_1", "twitter101_1"})
        row = next(row for row in self.records() if row["tweet_id"] == "100")
        self.assertEqual((row["path"], row["media_type"]), (str(mp4), "animated_gif"))
        self.assertEqual([event["stage"] for event in events if event["type"] == "converting"], ["started", "fallback"])
        warnings = [event for event in events if event["type"] == "log" and event.get("level") == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn(message, warnings[0]["message"])
        self.assertEqual(warnings[0]["author"], "alice")

    def test_failed_conversion_saves_original_mp4_and_later_files(self):
        # One unconvertible GIF used to abort every run before later Likes.
        rows = [("100", 1, "animated_gif", "alice"), ("101", 1, "photo", "alice")]
        failure = media.ConversionError("GIF로 변환하지 못했습니다.")
        with patch("luvbox.core.convert_mp4_to_gif", side_effect=failure) as convert:
            code, events = self.worker(rows, options=self.options_all())
            self.assertEqual((code, events[-1]["status"], events[-1]["saved"]), (0, "finished", 2))
            self.assert_saved_as_original_mp4(events, "원본 MP4로 저장")
            code, events = self.worker(rows, options=self.options_all())
            self.assertEqual((code, events[-1]["saved"], events[-1]["skipped"]), (0, 0, 2))
            convert.assert_called_once()
        self.assertEqual(len(test_core.FakeHttpDownloader.downloaded), 2)

    def test_interrupted_conversion_retries_locally_without_redownload(self):
        rows = [("100", 1, "animated_gif", "alice")]
        flag = self.base / "stop"
        def stop(source, **kwargs):
            flag.touch()
            kwargs["check_stop"]()
        with patch("luvbox.core.convert_mp4_to_gif", side_effect=stop):
            self.worker(rows, options=self.options_all(), stop_file=str(flag))
        source = self.output / "myaccount" / "alice" / "alice_100_gif_1.mp4.part"
        self.assertTrue(core._transfer_complete(source))
        flag.unlink()
        with patch("luvbox.core.convert_mp4_to_gif", side_effect=self.convert):
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
        with patch("luvbox.core.convert_mp4_to_gif", side_effect=stop):
            code, events = self.worker([("100", 1, "animated_gif", "alice")], options=self.options_all(), stop_file=str(flag))
        self.assertEqual((code, events[-1]["status"], events[-1]["saved"]), (0, "stopped", 0))
        self.assertEqual(self.keys(), set())
        self.assertEqual(self.records(), [])
        source = self.output / "myaccount" / "alice" / "alice_100_gif_1.mp4.part"
        self.assertTrue(core._transfer_complete(source))

    def test_gif_over_size_cap_saves_original_mp4_and_next_file(self):
        with patch("luvbox.core.convert_mp4_to_gif", side_effect=media.ConversionSizeLimit()):
            code, events = self.worker([("100", 1, "animated_gif", "alice"), ("101", 1, "photo", "alice")],
                                      options=self.options_all())
        self.assertEqual((code, events[-1]["saved"], events[-1]["size_skipped"]), (0, 2, 0))
        self.assert_saved_as_original_mp4(events, "200MB를 넘어 원본 MP4로 저장")

    def test_completed_source_over_a_lowered_cap_is_size_skipped_and_removed(self):
        source = self.output / "myaccount" / "alice" / "100_gif_1.mp4.part"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"0" * (2 * 1024 * 1024))
        core._mark_transfer_complete(source)
        with patch("luvbox.core.convert_mp4_to_gif") as convert:
            code, events = self.worker([("100", 1, "animated_gif", "alice"), ("101", 1, "photo", "alice")],
                                      options=self.options_all(max_video_mb=1))
        convert.assert_not_called()
        self.assertEqual((code, events[-1]["saved"], events[-1]["size_skipped"]), (0, 1, 1))
        self.assertEqual(self.keys(), {"twitter101_1"})
        self.assertFalse(list(self.output.rglob("*.part*")))
        self.assertEqual(len(test_core.FakeHttpDownloader.downloaded), 1)

    def test_destination_created_during_conversion_is_not_overwritten_or_archived(self):
        destination = self.output / "myaccount" / "alice" / "alice_100_gif_1.gif"
        def collision(source, **kwargs):
            output = self.convert(source, **kwargs)
            destination.write_bytes(b"another process created this file")
            return output
        with patch("luvbox.core.convert_mp4_to_gif", side_effect=collision):
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
        self.assertFalse(core._transfer_complete(source))
        core._mark_transfer_complete(source)
        self.assertTrue(core._transfer_complete(source))
        source.write_bytes(b"changed incomplete source with another size")
        self.assertFalse(core._transfer_complete(source))

    def test_existing_mp4_gif_is_not_rewritten(self):
        path = self.output / "myaccount" / "alice" / "100_gif_1.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"previous downloaded MP4")
        with patch("luvbox.core.convert_mp4_to_gif") as convert:
            code, events = self.worker([("100", 1, "animated_gif", "alice")], options=self.options_all())
        self.assertEqual((code, events[-1]["saved"], events[-1]["skipped"]), (0, 0, 1))
        convert.assert_not_called()
        self.assertEqual(path.read_bytes(), b"previous downloaded MP4")
        self.assertEqual(self.records()[0]["path"], str(path))

    def test_gif_choices_are_validated_and_size_reaches_the_converter(self):
        for changes in ({"gif_format": "webm"}, {"gif_size": "1080"}, {"gif_format": ["gif"]}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "GIF 저장 형식"):
                self.options_all(**changes).validate()
        with patch("luvbox.core.convert_mp4_to_gif", side_effect=self.convert) as convert:
            code, events = self.worker([("100", 1, "animated_gif", "alice")], options=self.options_all(gif_size="720"))
        self.assertEqual(code, 0)
        self.assertEqual(convert.call_args.kwargs["size"], "720")

    def test_mp4_choice_saves_the_original_without_converting(self):
        with patch("luvbox.core.convert_mp4_to_gif") as convert:
            code, events = self.worker([("100", 1, "animated_gif", "alice"), ("101", 1, "photo", "alice")],
                                      options=self.options_all(gif_format="mp4"))
        convert.assert_not_called()
        self.assertEqual((code, events[-1]["saved"]), (0, 2))
        mp4 = self.output / "myaccount" / "alice" / "alice_100_gif_1.mp4"
        self.assertTrue(mp4.is_file())
        self.assertFalse(list(self.output.rglob("*.part*")))
        self.assertEqual(self.keys(), {"twitter100_animated_gif_1", "twitter101_1"})
        self.assertEqual(next(row for row in self.records() if row["tweet_id"] == "100")["path"], str(mp4))
        self.assertFalse([event for event in events if event["type"] == "converting"])

    def test_mp4_choice_finishes_an_interrupted_gif_source_without_downloading(self):
        source = self.output / "myaccount" / "alice" / "100_gif_1.mp4.part"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"completed earlier for a GIF")
        core._mark_transfer_complete(source)
        with patch("luvbox.core.convert_mp4_to_gif") as convert:
            code, events = self.worker([("100", 1, "animated_gif", "alice")], options=self.options_all(gif_format="mp4"))
        convert.assert_not_called()
        self.assertEqual((code, events[-1]["saved"]), (0, 1))
        self.assertEqual((source.parent / "100_gif_1.mp4").read_bytes(), b"completed earlier for a GIF")
        self.assertFalse(list(self.output.rglob("*.part*")))
        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])

    def test_existing_gif_without_ledger_is_recognized(self):
        path = self.output / "myaccount" / "alice" / "100_gif_1.gif"
        path.parent.mkdir(parents=True)
        Image.new("RGB", (10, 10), "green").save(path)
        with patch("luvbox.core.convert_mp4_to_gif") as convert:
            code, events = self.worker([("100", 1, "animated_gif", "alice")], options=self.options_all())
        self.assertEqual((code, events[-1]["saved"], events[-1]["skipped"]), (0, 0, 1))
        convert.assert_not_called()
        self.assertEqual(self.records()[0]["path"], str(path))


if __name__ == "__main__":
    unittest.main()
