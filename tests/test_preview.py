from io import BytesIO
from pathlib import Path
import queue
import subprocess
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from PIL import Image

from xlikesaver.preview import PREVIEW_SIZE, PreviewLoader, Thumbnail, thumbnail


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def image(self, name="photo.jpg", size=(1200, 800)):
        path = self.root / name
        Image.new("RGB", size, "red").save(path)
        return path

    def test_photo_is_local_aspect_correct_small_and_does_not_modify_file(self):
        path = self.image()
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with patch("requests.Session.request") as network:
            result = thumbnail(path)
        network.assert_not_called()
        self.assertEqual(result.image.size, (220, 147))
        self.assertEqual(result.image.mode, "RGBA")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_orientation_and_gif_first_frame(self):
        path = self.root / "rotated.jpg"
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (800, 1200), "blue").save(path, exif=exif)
        result = thumbnail(path)
        self.assertGreater(result.image.width, result.image.height)
        gif = self.root / "animation.gif"
        Image.new("RGB", (300, 100), "red").save(gif, save_all=True,
                        append_images=[Image.new("RGB", (300, 100), "blue")], duration=100, loop=0)
        result = thumbnail(gif)
        self.assertIn("첫 프레임", result.note)
        self.assertEqual(result.image.getpixel((10, 10)), (255, 0, 0, 255))

    def test_missing_corrupt_unsupported_and_large_image_have_clear_notices(self):
        self.assertIn("삭제", thumbnail(self.root / "missing.jpg").note)
        corrupt = self.root / "broken.jpg"
        corrupt.write_bytes(b"not a photo")
        self.assertIsNone(thumbnail(corrupt).image)
        unknown = self.root / "file.txt"
        unknown.write_text("file")
        self.assertIn("지원", thumbnail(unknown).note)
        with patch("xlikesaver.preview.MAX_PIXELS", 3):
            self.assertIn("큰 이미지", thumbnail(self.image()).note)

    def test_video_uses_bundled_decoder_bounded_filter_hidden_timeout_and_local_protocols(self):
        path = self.root / "video.mp4"
        path.write_bytes(b"offline-video")
        frame = BytesIO()
        Image.new("RGB", (220, 100), "green").save(frame, "BMP")
        completed = subprocess.CompletedProcess([], 0, stdout=frame.getvalue(), stderr=b"")
        with patch("xlikesaver.media.ffmpeg_path", return_value=Path("bundled-ffmpeg.exe")), \
                patch("xlikesaver.preview.subprocess.run", return_value=completed) as run:
            result = thumbnail(path)
        self.assertEqual(result.image.size, (220, 100))
        args = run.call_args.args[0]
        self.assertEqual(args[0], "bundled-ffmpeg.exe")
        self.assertEqual(args[args.index("-protocol_whitelist") + 1], "file,pipe")
        self.assertIn("scale=220:168:force_original_aspect_ratio=decrease", args)
        self.assertEqual(args[args.index("-vcodec") + 1], "bmp")
        self.assertEqual(run.call_args.kwargs["timeout"], 8)
        self.assertEqual(run.call_args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with patch("xlikesaver.media.ffmpeg_path", return_value=None):
            self.assertIn("도구", thumbnail(path).note)
        with patch("xlikesaver.media.ffmpeg_path", return_value=Path("bundled.exe")), \
                patch("xlikesaver.preview.subprocess.run", side_effect=subprocess.TimeoutExpired([], 8)):
            self.assertIsNone(thumbnail(path).image)

    def test_bundled_decoder_reads_real_local_video_without_network(self):
        from xlikesaver.media import ffmpeg_path
        executable = ffmpeg_path()
        if executable is None:
            self.skipTest("Bundled video decoder is not installed")
        path = self.root / "로컬 영상.mp4"
        generated = subprocess.run(
            [str(executable), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
             "testsrc2=size=320x180:rate=10", "-t", "0.1", "-an", "-c:v", "mpeg4", str(path)],
            capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(generated.returncode, 0, generated.stderr.decode("utf-8", errors="replace"))
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with patch("requests.Session.request") as network:
            result = thumbnail(path)
        network.assert_not_called()
        self.assertIsNotNone(result.image)
        self.assertLessEqual(result.image.width, PREVIEW_SIZE[0])
        self.assertLessEqual(result.image.height, PREVIEW_SIZE[1])
        self.assertIn("첫 프레임", result.note)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_loader_checks_permission_even_for_cache_and_invalidates_modified_file(self):
        path = self.image()
        results = queue.Queue(maxsize=2)
        with patch("xlikesaver.preview.thumbnail", wraps=thumbnail):
            loader = PreviewLoader(results, decoder=thumbnail)
        self.addCleanup(loader.close)
        with patch.object(loader, "decoder", wraps=thumbnail) as decode:
            loader.request(1, path, lambda _: True)
            self.assertEqual(results.get(timeout=3)[0], 1)
            loader.request(2, path, lambda _: True)
            self.assertEqual(results.get(timeout=3)[0], 2)
            self.assertEqual(decode.call_count, 1)
            loader.request(3, path, lambda _: False)
            _, result = results.get(timeout=3)
            self.assertIsNone(result.image)
            self.assertEqual(decode.call_count, 1)
            self.image(size=(120, 70))
            loader.request(4, path, lambda _: True)
            results.get(timeout=3)
            self.assertEqual(decode.call_count, 2)

    def test_loader_keeps_only_latest_pending_selection_and_closes_without_late_result(self):
        started, release = Event(), Event()
        decoded = []

        def decode(path):
            decoded.append(path.name)
            if len(decoded) == 1:
                started.set()
                release.wait(3)
            return Thumbnail(Image.new("RGB", (10, 10)), "ready")

        paths = [self.image(f"{index}.jpg") for index in range(4)]
        results = queue.Queue(maxsize=2)
        loader = PreviewLoader(results, decoder=decode, cache_size=1)
        self.addCleanup(loader.close)
        loader.request(1, paths[0], lambda _: True)
        self.assertTrue(started.wait(3))
        for index in range(1, 4):
            loader.request(index + 1, paths[index], lambda _: True)
        release.set()
        self.assertEqual(results.get(timeout=3)[0], 1)
        self.assertEqual(results.get(timeout=3)[0], 4)
        self.assertEqual(decoded, ["0.jpg", "3.jpg"])
        self.assertEqual(len(loader.cache), 1)
        loader.close()
        self.assertFalse(loader.cache)

        started.clear()
        release.clear()
        late = PreviewLoader(queue.Queue(maxsize=2), decoder=lambda _: (started.set(), release.wait(3), Thumbnail(None, "late"))[-1])
        self.addCleanup(late.close)
        late.request(1, paths[0], lambda _: True)
        self.assertTrue(started.wait(3))
        late.close()
        release.set()
        late.thread.join(3)
        self.assertTrue(late.results.empty())
        self.assertFalse(late.cache)


if __name__ == "__main__":
    unittest.main()
