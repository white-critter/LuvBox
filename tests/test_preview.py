from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops, ImageOps, ImageStat

from luvbox.preview import PREVIEW_SIZE, thumbnail


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

    def fresh_decoder(self, script, *paths):
        # Saving a fixture imports Pillow plugins. A new interpreter is needed
        # to exercise the application's first decode rather than a warm one.
        completed = subprocess.run(
            [sys.executable, "-c", script, *(str(path) for path in paths)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True,
            text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_first_raster_decode_avoids_global_plugin_initialization_even_when_renamed(self):
        from PIL import features
        for file_format, suffix in (("JPEG", ".jpg"), ("PNG", ".png"),
                                    ("WEBP", ".webp"), ("GIF", ".gif"),
                                    ("PNG", ".jpg")):
            if file_format == "WEBP" and not features.check("webp"):
                continue
            with self.subTest(file_format=file_format, suffix=suffix):
                path = self.root / (file_format + suffix)
                with Image.new("RGB", (320, 160), "red") as image:
                    image.save(path, format=file_format)
                self.fresh_decoder("""
import sys
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from luvbox.preview import thumbnail
assert not Image.OPEN, tuple(Image.OPEN)
with patch.object(Image, 'init', side_effect=AssertionError('global plugin initialization')):
    result = thumbnail(Path(sys.argv[1]))
assert result.image is not None, result.note
assert result.image.size == (220, 110), result.image.size
assert result.image.mode == 'RGBA'
assert 'PIL.EpsImagePlugin' not in sys.modules
assert 'PIL.PdfImagePlugin' not in sys.modules
result.image.close()
""", path)

    def test_first_disguised_eps_decode_rejects_without_loading_external_renderer(self):
        path = self.root / "disguised.png"
        path.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 16 16\nshowpage\n")
        self.fresh_decoder("""
import sys
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from luvbox.preview import thumbnail
assert not Image.OPEN, tuple(Image.OPEN)
with patch.object(Image, 'init', side_effect=AssertionError('global plugin initialization')):
    result = thumbnail(Path(sys.argv[1]))
assert result.image is None
assert 'PIL.EpsImagePlugin' not in sys.modules
assert 'PIL.PdfImagePlugin' not in sys.modules
""", path)

    def test_first_video_frame_decode_avoids_global_plugin_initialization(self):
        path = self.root / "video.mp4"
        path.write_bytes(b"offline-video")
        frame = self.root / "frame.bmp"
        with Image.new("RGB", (220, 100), "green") as image:
            image.save(frame)
        self.fresh_decoder("""
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from luvbox.preview import thumbnail
assert not Image.OPEN, tuple(Image.OPEN)
completed = subprocess.CompletedProcess([], 0, stdout=Path(sys.argv[2]).read_bytes(), stderr=b'')
with patch.object(Image, 'init', side_effect=AssertionError('global plugin initialization')), \\
        patch('luvbox.media.ffmpeg_path', return_value=Path('bundled-ffmpeg.exe')), \\
        patch('luvbox.preview.subprocess.run', return_value=completed):
    result = thumbnail(Path(sys.argv[1]))
assert result.image is not None, result.note
assert result.image.size == (220, 100), result.image.size
assert 'PIL.EpsImagePlugin' not in sys.modules
assert 'PIL.PdfImagePlugin' not in sys.modules
result.image.close()
""", path, frame)

    def test_disguised_eps_never_dispatches_an_external_renderer(self):
        path = self.root / "photo.jpg"
        path.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 16 16\nshowpage\n")
        with patch("PIL.EpsImagePlugin.Ghostscript", side_effect=AssertionError("external renderer")) as renderer:
            self.assertIsNone(thumbnail(path).image)
        renderer.assert_not_called()

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

    def test_all_exif_orientations_preserve_pixels_aspect_and_source(self):
        # A non-square image with four different corners catches mirrored
        # rotations as well as accidentally using the unswapped size bounds.
        image = Image.new("RGB", (400, 600), "red")
        image.paste("green", (200, 0, 400, 300))
        image.paste("blue", (0, 300, 200, 600))
        image.paste("yellow", (200, 300, 400, 600))
        self.addCleanup(image.close)
        for orientation in range(1, 9):
            with self.subTest(orientation=orientation):
                path = self.root / f"orientation-{orientation}.png"
                exif = Image.Exif()
                exif[274] = orientation
                image.save(path, exif=exif)
                before = path.read_bytes(), path.stat().st_mtime_ns
                with Image.open(path) as original:
                    expected = ImageOps.exif_transpose(original)
                    expected.thumbnail((101, 73), Image.Resampling.LANCZOS)
                    expected = expected.convert("RGBA")
                result = thumbnail(path, (101, 73))
                self.assertEqual(result.image.size, expected.size)
                difference = ImageChops.difference(result.image, expected)
                # Swapping Pillow's horizontal/vertical resampling passes can
                # round a few boundary pixels differently; the displayed
                # orientation and image remain equivalent.
                self.assertLess(max(ImageStat.Stat(difference).mean), .02)
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
                result.image.close()
                expected.close()
                difference.close()

    def test_symlink_and_junction_previews_do_not_open_the_target(self):
        path = self.image()
        real_symlink = Path.is_symlink

        def linked(candidate):
            return candidate == path or real_symlink(candidate)

        with patch.object(Path, "is_symlink", autospec=True, side_effect=linked), \
                patch("luvbox.preview.Image.open", side_effect=AssertionError("linked preview was opened")):
            result = thumbnail(path)
        self.assertIsNone(result.image)
        self.assertIn("삭제", result.note)
        with patch.object(Path, "is_symlink", autospec=True, return_value=False), \
                patch.object(Path, "is_junction", autospec=True,
                             side_effect=lambda candidate: candidate == path, create=True), \
                patch("luvbox.preview.Image.open", side_effect=AssertionError("junction preview was opened")):
            result = thumbnail(path)
        self.assertIsNone(result.image)

    def test_missing_corrupt_unsupported_and_large_image_have_clear_notices(self):
        self.assertIn("삭제", thumbnail(self.root / "missing.jpg").note)
        corrupt = self.root / "broken.jpg"
        corrupt.write_bytes(b"not a photo")
        self.assertIsNone(thumbnail(corrupt).image)
        unknown = self.root / "file.txt"
        unknown.write_text("file")
        self.assertIn("지원", thumbnail(unknown).note)
        with patch("luvbox.preview.MAX_PIXELS", 3):
            self.assertIn("큰 이미지", thumbnail(self.image()).note)

    def test_video_uses_bundled_decoder_bounded_filter_hidden_timeout_and_local_protocols(self):
        path = self.root / "video.mp4"
        path.write_bytes(b"offline-video")
        frame = BytesIO()
        Image.new("RGB", (220, 100), "green").save(frame, "BMP")
        completed = subprocess.CompletedProcess([], 0, stdout=frame.getvalue(), stderr=b"")
        with patch("luvbox.media.ffmpeg_path", return_value=Path("bundled-ffmpeg.exe")), \
                patch("luvbox.preview.subprocess.run", return_value=completed) as run:
            result = thumbnail(path)
        self.assertEqual(result.image.size, (220, 100))
        args = run.call_args.args[0]
        self.assertEqual(args[0], "bundled-ffmpeg.exe")
        self.assertEqual(args[args.index("-protocol_whitelist") + 1], "file,pipe")
        self.assertEqual(args[args.index("-i") - 2:args.index("-i")], ["-f", "mov"])
        self.assertIn("scale=220:168:force_original_aspect_ratio=decrease", args)
        self.assertEqual(args[args.index("-vcodec") + 1], "bmp")
        self.assertEqual(run.call_args.kwargs["timeout"], 8)
        self.assertEqual(run.call_args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with patch("luvbox.media.ffmpeg_path", return_value=None):
            self.assertIn("도구", thumbnail(path).note)
        with patch("luvbox.media.ffmpeg_path", return_value=Path("bundled.exe")), \
                patch("luvbox.preview.subprocess.run", side_effect=subprocess.TimeoutExpired([], 8)):
            self.assertIsNone(thumbnail(path).image)

    @unittest.skipUnless(os.name == "nt", "Windows bundled FFmpeg execution")
    def test_bundled_decoder_reads_real_local_video_without_network(self):
        from luvbox.media import ffmpeg_path
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







if __name__ == "__main__":
    unittest.main()
