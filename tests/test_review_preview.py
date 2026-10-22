"""Playback checks use generated local media and never access an account."""

from io import BytesIO
from fractions import Fraction
import math
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
from threading import Event, Lock, Thread
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from luvbox.review_preview import FRAME_QUEUE_SIZE, ReviewFrame, ReviewPreview, _FrameQueue, _GifSampler


class FakeProcess:
    def __init__(self, data=b"", *, blocked=False):
        self.killed = Event()
        self.stdout = BytesIO(data)
        if blocked:
            self.stdout = Mock()
            self.stdout.read.side_effect = lambda _size: (self.killed.wait(3) and b"") or b""
        self.returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = 0
        self.killed.set()

    def wait(self, timeout=None):
        self.killed.wait(timeout)
        return self.returncode


class GifSamplerTests(unittest.TestCase):
    def test_small_source_frames_stay_detached_at_original_resolution(self):
        frames = []
        sampler = _GifSampler((640, 400), Fraction(1000, 30), lambda frame: frames.append(frame) or True, "GIF")
        self.addCleanup(sampler.close)
        with Image.new("RGBA", (160, 100), (70, 110, 190, 128)) as image:
            self.assertTrue(sampler.add(image, 1000))
            image.putpixel((0, 0), (0, 0, 0, 0))
        self.assertEqual(len(frames), 1)
        self.addCleanup(frames[0].image.close)
        self.assertEqual(frames[0].image.size, (160, 100))
        self.assertEqual(frames[0].image.getpixel((0, 0)), (70, 110, 190, 128))
        self.assertEqual(frames[0].duration_ms, 1000)

    def test_thirty_fps_keeps_exact_sampling_time_across_short_source_loops(self):
        frames = []
        sampler = _GifSampler((16, 16), Fraction(1000, 30), lambda frame: frames.append(frame) or True, "GIF")
        self.addCleanup(sampler.close)
        with Image.new("RGB", (16, 16), "red") as red, Image.new("RGB", (16, 16), "blue") as blue:
            for index in range(300):
                self.assertTrue(sampler.add((red, blue)[index % 2], 10))
        self.assertEqual(len(frames), 90)
        self.assertEqual(sampler.duration, 0)
        self.assertAlmostEqual(math.fsum(frame.duration_ms for frame in frames), 3000)
        for index, frame in enumerate(frames):
            self.addCleanup(frame.image.close)
            source_index = int(Fraction(index * 1000, 30) // 10)
            self.assertEqual(frame.image.getpixel((1, 1)),
                             ((255, 0, 0, 255), (0, 0, 255, 255))[source_index % 2])

    def test_thirty_fps_holds_a_whole_second_without_rounding_it_away(self):
        frames = []
        sampler = _GifSampler((16, 16), Fraction(1000, 30), lambda frame: frames.append(frame) or True, "GIF")
        self.addCleanup(sampler.close)
        with Image.new("RGB", (16, 16), "red") as image:
            self.assertTrue(sampler.add(image, 1000))
        self.assertEqual([frame.duration_ms for frame in frames], [1000])
        self.assertEqual(sampler.duration, 0)
        self.addCleanup(frames[0].image.close)

    def test_sampling_preserves_elapsed_time_and_carries_partial_intervals(self):
        emitted = []

        def send(frame):
            emitted.append(frame)
            self.addCleanup(frame.image.close)
            return True

        sampler = _GifSampler((16, 16), 83, send, "GIF")
        self.addCleanup(sampler.close)
        elapsed = 0
        with Image.new("RGB", (16, 16), "red") as red, Image.new("RGB", (16, 16), "blue") as blue:
            for image, duration in ((red, 80), (blue, 120), (red, 49)):
                self.assertTrue(sampler.add(image, duration))
                elapsed += duration
                self.assertEqual(sum(frame.duration_ms for frame in emitted) + sampler.duration, elapsed)
            self.assertEqual([frame.duration_ms for frame in emitted], [83, 83, 83])
            self.assertEqual([frame.image.getpixel((1, 1)) for frame in emitted],
                             [(255, 0, 0, 255), (0, 0, 255, 255), (0, 0, 255, 255)])

    def test_long_source_frames_are_held_without_redundant_output(self):
        emitted = []

        def send(frame):
            emitted.append(frame)
            self.addCleanup(frame.image.close)
            return True

        sampler = _GifSampler((16, 16), 83, send, "GIF")
        self.addCleanup(sampler.close)
        with Image.new("RGB", (16, 16), "red") as image:
            sampler.add(image, 1000)
        self.assertEqual([frame.duration_ms for frame in emitted], [996])
        self.assertEqual(sampler.duration, 4)

    def test_close_releases_the_frame_not_yet_queued(self):
        sampler = _GifSampler((16, 16), 83, lambda _: True, "GIF")
        with Image.new("RGB", (16, 16), "red") as image:
            sampler.add(image, 20)
        pending = sampler.image
        sampler.close()
        sampler.close()
        with self.assertRaises(ValueError):
            pending.getpixel((1, 1))


class PreviewQueueTests(unittest.TestCase):
    def sender(self):
        preview = object.__new__(ReviewPreview)
        preview.frames = _FrameQueue(maxsize=1)
        preview.stop = Event()
        preview._lock = Lock()
        preview._process = None
        preview._heartbeat = time.monotonic()
        self.addCleanup(preview.close)
        return preview

    def send_while_full(self, preview, frame, action):
        entered = Event()
        original_wait = preview.frames.not_full.wait
        results = []

        def wait(_timeout=None):
            entered.set()
            # The action must notify this condition; a short timeout must not
            # accidentally make a polling sender appear to wake on consumption.
            return original_wait(3)

        thread = Thread(target=lambda: results.append(preview._send(frame)), daemon=True)
        with patch.object(preview.frames.not_full, "wait", side_effect=wait):
            thread.start()
            self.assertTrue(entered.wait(2))
            action()
            thread.join(1)
            self.assertFalse(thread.is_alive(), "queue consumption/close did not wake the producer")
        return results

    def test_consumption_wakes_the_producer_without_waiting_for_a_poll(self):
        preview = self.sender()
        first = ReviewFrame(Image.new("RGB", (16, 16)))
        second = ReviewFrame(Image.new("RGB", (16, 16)))
        preview.frames.put_nowait(first)
        consumed = []
        results = self.send_while_full(preview, second, lambda: consumed.append(preview.frames.get_nowait()))
        self.assertEqual(results, [True])
        self.assertIs(consumed[0], first)
        first.image.close()
        preview.close()
        with self.assertRaises(ValueError):
            second.image.getpixel((0, 0))

    def test_closing_a_full_queue_wakes_sender_and_never_retains_a_late_frame(self):
        preview = self.sender()
        first = ReviewFrame(Image.new("RGB", (16, 16)))
        second = ReviewFrame(Image.new("RGB", (16, 16)))
        preview.frames.put_nowait(first)
        results = self.send_while_full(preview, second, preview.close)
        self.assertEqual(results, [False])
        self.assertTrue(preview.frames.empty())
        for frame in (first, second):
            with self.assertRaises(ValueError):
                frame.image.getpixel((0, 0))


class ReviewPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def preview(self, path, kind="photo", **kwargs):
        preview = ReviewPreview(path, kind, **kwargs)
        self.addCleanup(self.close_preview, preview)
        return preview

    @staticmethod
    def close_preview(preview):
        preview.close()
        if not preview.finished.wait(5):
            raise AssertionError("preview did not release file handles")

    def photo(self, filename="확인할 사진.part", size=(900, 600)):
        path = self.root / filename
        Image.new("RGB", size, "red").save(path, "JPEG")
        return path

    def fresh_decoder(self, script, *paths):
        # Fixture creation registers Pillow readers in this interpreter.
        # Start a fresh child to exercise the first worker's actual imports.
        completed = subprocess.run(
            [sys.executable, "-c", script, *(str(path) for path in paths)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True,
            text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_first_photo_and_gif_workers_avoid_global_plugin_initialization(self):
        for kind, file_format in (("photo", "PNG"), ("animated_gif", "GIF")):
            with self.subTest(kind=kind):
                path = self.root / (kind + ".part")
                with Image.new("RGB", (32, 16), "red") as image:
                    image.save(path, file_format, duration=100)
                before = path.read_bytes(), path.stat().st_mtime_ns
                self.fresh_decoder("""
import sys
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from luvbox.review_preview import ReviewPreview
assert not Image.OPEN, tuple(Image.OPEN)
with patch.object(Image, 'init', side_effect=AssertionError('global plugin initialization')):
    preview = ReviewPreview(Path(sys.argv[1]), sys.argv[2], size=(32, 16))
    try:
        frame = preview.frames.get(timeout=3)
        assert frame.image is not None, frame.note
        assert frame.image.size == (32, 16), frame.image.size
        assert frame.image.getpixel((1, 1)) == (255, 0, 0, 255)
        frame.image.close()
    finally:
        preview.close()
        assert preview.finished.wait(3), 'worker did not release handles'
assert 'PIL.EpsImagePlugin' not in sys.modules
assert 'PIL.PdfImagePlugin' not in sys.modules
""", path, kind)
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_first_video_worker_decodes_bmp_without_global_plugin_initialization(self):
        path = self.root / "video.part"
        path.write_bytes(b"offline video fixture")
        frame = self.root / "frame.bmp"
        with Image.new("RGB", (16, 16), (80, 100, 120)) as image:
            image.save(frame)
        self.fresh_decoder("""
import sys
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from luvbox.review_preview import ReviewPreview
from tests.test_review_preview import FakeProcess
assert not Image.OPEN, tuple(Image.OPEN)
with patch.object(Image, 'init', side_effect=AssertionError('global plugin initialization')), \\
        patch('luvbox.media.ffmpeg_path', return_value=Path('bundled.exe')), \\
        patch('luvbox.review_preview.subprocess.Popen',
              side_effect=lambda *a, **kw: FakeProcess(Path(sys.argv[2]).read_bytes() * 10)):
    preview = ReviewPreview(Path(sys.argv[1]), 'video', size=(16, 16))
    try:
        frame = preview.frames.get(timeout=3)
        assert frame.image is not None, frame.note
        assert frame.image.size == (16, 16), frame.image.size
        assert frame.image.getpixel((1, 1)) == (80, 100, 120)
        frame.image.close()
    finally:
        preview.close()
        assert preview.finished.wait(3), 'worker did not release handles'
assert 'PIL.EpsImagePlugin' not in sys.modules
assert 'PIL.PdfImagePlugin' not in sys.modules
""", path, frame)

    def test_photo_part_file_is_bounded_and_local_without_modifying_input(self):
        path = self.photo()
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with patch("requests.Session.request") as network:
            preview = self.preview(path)
            result = preview.frames.get(timeout=3)
            self.assertTrue(preview.finished.wait(3))
        network.assert_not_called()
        self.assertEqual(result.image.size, (600, 400))
        self.assertEqual(result.image.mode, "RGBA")
        self.assertIn("사진", result.note)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(list(self.root.iterdir()), [path])
        result.image.close()

    def test_disguised_eps_finishes_without_dispatching_an_external_renderer(self):
        path = self.root / "photo.jpg.part"
        path.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 16 16\nshowpage\n")
        with patch("PIL.EpsImagePlugin.Ghostscript", side_effect=AssertionError("external renderer")) as renderer:
            preview = self.preview(path)
            self.assertTrue(preview.finished.wait(3))
            self.assertIsNone(preview.frames.get(timeout=1).image)
        renderer.assert_not_called()

    def test_photo_orientation_is_applied_before_rendering(self):
        path = self.root / "rotated.part"
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (400, 800), "blue").save(path, "JPEG", exif=exif)
        preview = self.preview(path)
        result = preview.frames.get(timeout=3)
        self.assertGreater(result.image.width, result.image.height)
        result.image.close()

    def test_rotated_png_fits_review_bounds_and_preserves_transparency(self):
        path = self.root / "rotated.png.part"
        exif = Image.Exif()
        exif[274] = 6
        with Image.new("RGBA", (400, 800), (0, 0, 255, 128)) as image:
            image.save(path, "PNG", exif=exif)
        before = path.read_bytes(), path.stat().st_mtime_ns
        result = self.preview(path, size=(150, 112)).frames.get(timeout=3)
        self.assertEqual(result.image.size, (150, 75))
        self.assertEqual(result.image.getpixel((50, 30)), (0, 0, 255, 128))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        result.image.close()

    def test_missing_corrupt_cloud_only_and_large_photos_return_notices(self):
        corrupt = self.root / "corrupt.part"
        corrupt.write_bytes(b"not an image")
        for path in (self.root / "missing.part", corrupt):
            with self.subTest(path=path.name):
                result = self.preview(path).frames.get(timeout=3)
                self.assertIsNone(result.image)
                self.assertIn("미리보기", result.note)
        with patch("luvbox.review_preview.cloud_only", return_value=True), \
                patch("luvbox.review_preview.Image.open") as image:
            result = self.preview(self.photo()).frames.get(timeout=3)
            self.assertIsNone(result.image)
            image.assert_not_called()
        with patch("luvbox.review_preview.MAX_PIXELS", 100):
            result = self.preview(self.photo()).frames.get(timeout=3)
            self.assertIsNone(result.image)
            self.assertIn("커서", result.note)

    def test_changed_photo_is_not_displayed_as_the_original_selection(self):
        path = self.photo()
        real_signature = (path.stat().st_dev, path.stat().st_ino, path.stat().st_size, path.stat().st_mtime_ns)
        with patch("luvbox.review_preview._signature", side_effect=[real_signature, (0, 0, 0, 0)]):
            result = self.preview(path).frames.get(timeout=3)
        self.assertIsNone(result.image)
        self.assertIn("변경", result.note)

    def test_media_type_drives_part_file_video_and_frames_are_strictly_bounded(self):
        path = self.root / "video.part"
        path.write_bytes(b"offline mp4")
        processes = []

        encoded = BytesIO()
        Image.new("RGB", (16, 16), (80, 100, 120)).save(encoded, "BMP")

        def launch(*_args, **_kwargs):
            process = FakeProcess(encoded.getvalue() * 10)
            processes.append(process)
            return process

        with patch("luvbox.media.ffmpeg_path", return_value=Path("bundled.exe")), \
                patch("luvbox.review_preview.subprocess.Popen", side_effect=launch) as popen:
            preview = self.preview(path, "video", size=(16, 16))
            result = preview.frames.get(timeout=3)
            self.assertEqual(result.image.getpixel((1, 1)), (80, 100, 120))
            self.assertAlmostEqual(result.duration_ms, 1000 / 30)
            self.assertIn("소리 없이", result.note)
            # Back pressure retains only three frames, even when no UI reads.
            deadline = time.monotonic() + 2
            while preview.frames.qsize() < FRAME_QUEUE_SIZE and time.monotonic() < deadline:
                preview.stop.wait(.01)
            self.assertEqual(preview.frames.qsize(), FRAME_QUEUE_SIZE)
            self.assertEqual(len(processes), 1)
            self.close_preview(preview)
            self.assertTrue(processes[0].killed.is_set())
            self.assertTrue(processes[0].stdout.closed)
            self.assertTrue(preview.frames.empty())
        args = popen.call_args.args[0]
        self.assertEqual(args[args.index("-protocol_whitelist") + 1], "file,pipe")
        self.assertEqual(args[args.index("-f") + 1], "mov")
        self.assertIn("-an", args)
        self.assertIn("-sn", args)
        self.assertIn("-dn", args)
        self.assertIn("-max_alloc", args)
        self.assertEqual(args[args.index("-threads") + 1], "1")
        self.assertEqual(args[args.index("-r") + 1], "30")
        self.assertEqual(args[args.index("-t") + 1], "30")
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(popen.call_args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
        result.image.close()

    def test_thirty_video_frames_preserve_one_second_and_the_bounded_queue(self):
        path = self.root / "video.part"
        path.write_bytes(b"offline mp4")
        encoded = BytesIO()
        with Image.new("RGB", (16, 16), "red") as image:
            image.save(encoded, "BMP")
        process = FakeProcess(encoded.getvalue() * 50)
        with patch("luvbox.media.ffmpeg_path", return_value=Path("bundled.exe")), \
                patch("luvbox.review_preview.subprocess.Popen", return_value=process):
            preview = self.preview(path, "video", size=(16, 16))
            frames = [preview.frames.get(timeout=3) for _ in range(30)]
            try:
                self.assertAlmostEqual(math.fsum(frame.duration_ms for frame in frames), 1000)
                self.assertTrue(all(frame.image is not None and frame.image.size == (16, 16) for frame in frames))
                self.assertLessEqual(preview.frames.qsize(), FRAME_QUEUE_SIZE)
                self.close_preview(preview)
                self.assertTrue(preview.frames.empty())
            finally:
                for frame in frames:
                    frame.image.close()

    def test_paused_viewer_backpressure_keeps_decoder_watchdog_alive(self):
        path = self.root / "paused.part"
        path.write_bytes(b"offline mp4")
        encoded = BytesIO()
        with Image.new("RGB", (16, 16), "red") as image:
            image.save(encoded, "BMP")
        process = FakeProcess(encoded.getvalue() * 50)
        with patch("luvbox.media.ffmpeg_path", return_value=Path("bundled.exe")), \
                patch("luvbox.review_preview.DECODER_TIMEOUT", .2), \
                patch("luvbox.review_preview.subprocess.Popen", return_value=process):
            preview = self.preview(path, "video", size=(16, 16))
            deadline = time.monotonic() + 3
            while preview.frames.qsize() < FRAME_QUEUE_SIZE and time.monotonic() < deadline:
                preview.stop.wait(.001)
            self.assertEqual(preview.frames.qsize(), FRAME_QUEUE_SIZE)
            self.assertFalse(preview.finished.wait(.5), "a full viewer queue must not time out the decoder")
            self.assertFalse(process.killed.is_set())
            self.close_preview(preview)

    def test_stalled_decoder_times_out_and_releases_its_file(self):
        path = self.root / "stall.part"
        path.write_bytes(b"offline mp4")
        process = FakeProcess(blocked=True)
        with patch("luvbox.media.ffmpeg_path", return_value=Path("bundled.exe")), \
                patch("luvbox.review_preview.DECODER_TIMEOUT", .15), \
                patch("luvbox.review_preview.subprocess.Popen", return_value=process):
            preview = self.preview(path, "video", size=(16, 16))
            result = preview.frames.get(timeout=3)
            self.assertIsNone(result.image)
            self.assertIn("재생", result.note)
            self.assertTrue(preview.finished.wait(3))
        self.assertTrue(process.killed.is_set())
        process.stdout.close.assert_called_once()

    def test_closing_while_process_starts_kills_late_process(self):
        path = self.root / "startup.part"
        path.write_bytes(b"offline mp4")
        entered, release = Event(), Event()
        process = FakeProcess()

        def launch(*_args, **_kwargs):
            entered.set()
            release.wait(3)
            return process

        with patch("luvbox.media.ffmpeg_path", return_value=Path("bundled.exe")), \
                patch("luvbox.review_preview.subprocess.Popen", side_effect=launch):
            preview = self.preview(path, "video", size=(16, 16))
            self.assertTrue(entered.wait(3))
            started = time.monotonic()
            preview.close()
            self.assertLess(time.monotonic() - started, .25)
            release.set()
            self.assertTrue(preview.finished.wait(3))
        self.assertTrue(process.killed.is_set())
        self.assertTrue(process.stdout.closed)
        self.assertTrue(preview.frames.empty())

    def test_missing_decoder_and_unsupported_type_are_safe_notices(self):
        path = self.photo()
        with patch("luvbox.media.ffmpeg_path", return_value=None):
            result = self.preview(path, "video").frames.get(timeout=3)
            self.assertIsNone(result.image)
            self.assertIn("도구", result.note)
        result = self.preview(path, "unknown").frames.get(timeout=3)
        self.assertIsNone(result.image)
        self.assertIn("지원", result.note)

    def test_preview_rejects_unbounded_sizes_before_starting_threads(self):
        for size in ((0, 400), (640, 0), (99999, 400), (640, 99999)):
            with self.subTest(size=size), self.assertRaises(ValueError):
                ReviewPreview(self.root / "missing.part", "photo", size=size)

    def test_short_fast_gif_keeps_animating_across_loop_boundaries(self):
        path = self.root / "fast-loop.gif.part"
        colors = ((255, 0, 0), (0, 0, 255), (0, 128, 0), (255, 255, 0), (128, 0, 128))
        images = [Image.new("RGB", (16, 16), color) for color in colors]
        try:
            images[0].save(path, "GIF", save_all=True, append_images=images[1:], duration=20, loop=0)
        finally:
            for image in images:
                image.close()
        preview = self.preview(path, "animated_gif", size=(16, 16))
        frames = [preview.frames.get(timeout=3) for _ in range(6)]
        try:
            self.assertTrue(all(math.isclose(frame.duration_ms, 1000 / 30) for frame in frames))
            # Exact 30fps samples span successive 100ms source loops.
            expected = [colors[int((Fraction(index * 1000, 30) % 100) // 20)] + (255,)
                        for index in range(6)]
            self.assertEqual([frame.image.getpixel((1, 1)) for frame in frames], expected)
            self.close_preview(preview)
            self.assertTrue(preview.frames.empty())
            path.unlink()
        finally:
            for frame in frames:
                frame.image.close()

    def test_real_video_and_gif_play_multiple_frames_and_close_handles(self):
        from luvbox.media import ffmpeg_path
        executable = ffmpeg_path()
        if executable is None:
            self.skipTest("Bundled FFmpeg is not installed")
        video = self.root / "확인 영상.part"
        generated = subprocess.run([str(executable), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                                    "testsrc2=size=160x96:rate=12", "-t", "0.5", "-an", "-c:v", "mpeg4",
                                    "-f", "mp4", str(video)], capture_output=True, timeout=10,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(generated.returncode, 0, generated.stderr.decode("utf-8", errors="replace"))
        gif = self.root / "확인 GIF.part"
        Image.new("RGB", (160, 96), "red").save(gif, "GIF", save_all=True,
                    append_images=[Image.new("RGB", (160, 96), "blue")], duration=200, loop=0)
        for path, kind in ((video, "video"), (gif, "animated_gif")):
            with self.subTest(kind=kind), patch("requests.Session.request") as network:
                before = (path.read_bytes(), path.stat().st_mtime_ns)
                preview = self.preview(path, kind, size=(160, 96))
                frames = [preview.frames.get(timeout=5) for _ in range(8)]
                self.assertTrue(all(frame.image is not None for frame in frames))
                self.assertGreater(len({frame.image.tobytes() for frame in frames}), 1)
                self.close_preview(preview)
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
                for frame in frames:
                    frame.image.close()
                path.unlink()  # Windows succeeds only after native source handles close.
            network.assert_not_called()
