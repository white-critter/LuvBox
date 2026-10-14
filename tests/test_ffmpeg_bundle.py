"""The small redistributable FFmpeg bundle works without an installed FFmpeg."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from PIL import Image

from luvbox.media import convert_mp4_to_gif, ffmpeg_path


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "tools" / "ffmpeg"


class FFmpegBundleTests(unittest.TestCase):
    def test_corresponding_source_runtime_and_build_recipe_are_pinned(self):
        manifest = json.loads((BUNDLE / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["license"], "LGPL-2.1-or-later")
        self.assertFalse(manifest["source_modified"])
        for item in (manifest["source"], manifest["runtime"]):
            path = BUNDLE / item["filename"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"])
        for filename, digest in manifest["build_files"].items():
            self.assertEqual(hashlib.sha256((BUNDLE / filename).read_bytes()).hexdigest(), digest)
        flags = manifest["configure"]
        self.assertIn("--disable-network", flags)
        self.assertIn("--disable-autodetect", flags)
        self.assertNotIn("--enable-gpl", flags)
        self.assertNotIn("--enable-nonfree", flags)
        self.assertNotIn("--enable-version3", flags)

    @unittest.skipUnless(os.name == "nt", "Windows native FFmpeg bundle")
    def test_runtime_has_local_protocols_and_expected_license(self):
        executable = ffmpeg_path()
        self.assertEqual(executable, BUNDLE / "runtime" / "ffmpeg.exe")
        result = self.run_ffmpeg("-hide_banner", "-protocols")
        protocols = {line.strip() for line in result.stdout.decode().splitlines() if line.startswith("  ")}
        self.assertEqual(protocols, {"file", "pipe"})
        license_text = self.run_ffmpeg("-L").stdout.decode()
        self.assertIn("GNU Lesser General Public", license_text)
        self.assertIn("version 2.1", license_text)

    @unittest.skipUnless(os.name == "nt", "Windows native FFmpeg bundle")
    def test_real_conversion_and_thumbnail_from_native_synthetic_mp4(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-영상-") as directory:
            source = Path(directory) / "테스트 동영상.mp4"
            self.run_ffmpeg("-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            "testsrc2=size=96x64:rate=10", "-t", "0.8", "-c:v", "mpeg4",
                            "-pix_fmt", "yuv420p", str(source))
            gif = convert_mp4_to_gif(source)
            with Image.open(gif) as image:
                self.assertEqual(image.format, "GIF")
                self.assertEqual(image.size, (96, 64))
                self.assertEqual(image.n_frames, 8)
            result = self.run_ffmpeg("-hide_banner", "-loglevel", "error", "-i", str(source),
                                     "-frames:v", "1", "-f", "image2pipe", "-c:v", "bmp", "pipe:1")
            with Image.open(io.BytesIO(result.stdout)) as thumbnail:
                self.assertEqual(thumbnail.format, "BMP")
                self.assertEqual(thumbnail.size, (96, 64))
            self.assertTrue(source.is_file())

    @staticmethod
    def run_ffmpeg(*arguments):
        return subprocess.run([str(ffmpeg_path()), *arguments], check=True, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30)


if __name__ == "__main__":
    unittest.main()
