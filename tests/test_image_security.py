"""Reject hostile image headers without allowing large native allocations."""

from pathlib import Path
from io import BytesIO
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from PIL import Image, WebPImagePlugin, features

from luvbox import preview
from luvbox.preview import thumbnail


class ImageSecurityTests(unittest.TestCase):
    @unittest.skipUnless(features.check("webp"), "Pillow WebP decoder is unavailable")
    def test_large_encoded_raster_is_rejected_before_open_even_when_renamed(self):
        encoded = BytesIO()
        with Image.new("RGB", (16, 16), "red") as image:
            image.save(encoded, "WEBP")
        data = encoded.getvalue() + b"\0" * 4096
        with tempfile.TemporaryDirectory() as folder:
            for suffix in (".webp", ".jpg", ".png", ".gif"):
                with self.subTest(suffix=suffix):
                    path = Path(folder) / ("oversized" + suffix)
                    path.write_bytes(data)
                    before = path.stat().st_mtime_ns
                    with patch.object(preview, "MAX_RASTER_BYTES", 4096), \
                            patch.object(Image, "open", side_effect=AssertionError("unbounded parser")):
                        result = thumbnail(path)
                    self.assertIsNone(result.image)
                    self.assertIn("큰 이미지", result.note)
                    self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (data, before))

    @unittest.skipUnless(features.check("webp"), "Pillow WebP decoder is unavailable")
    def test_webp_growth_after_size_check_is_bounded_before_native_decoder(self):
        for operation in (thumbnail, preview.photo_info):
            with self.subTest(operation=operation.__name__), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "growing.jpg"
                with Image.new("RGB", (16, 16), "red") as image:
                    image.save(path, "WEBP")
                budget = path.stat().st_size + 32
                original_open = Image.open
                streams = []

                def growing_open(stream, *args, **kwargs):
                    streams.append(stream)
                    with path.open("ab") as output:
                        output.write(b"\0" * (budget * 4))
                    return original_open(stream, *args, **kwargs)

                with patch.object(preview, "MAX_RASTER_BYTES", budget), \
                        patch.object(Image, "open", side_effect=growing_open), \
                        patch.object(WebPImagePlugin._webp, "WebPAnimDecoder",
                                     side_effect=AssertionError("oversized native allocation")) as decoder:
                    result = operation(path)
                if operation is thumbnail:
                    self.assertIsNone(result.image)
                else:
                    self.assertEqual(result, {})
                decoder.assert_not_called()
                self.assertEqual(len(streams), 1)
                self.assertTrue(streams[0].closed)

    def test_apng_disposal_is_bounded_before_the_canvas_size_check(self):
        # GHSA-5xm2-6676-qcp9: a tiny first frame must not allocate the
        # oversized header canvas before Pillow checks its pixel count.
        # The guard also makes a regression safe to execute on an older wheel.
        def chunk(kind, data):
            return (struct.pack(">I", len(data)) + kind + data
                    + struct.pack(">I", zlib.crc32(kind + data)))

        image = (b"\x89PNG\r\n\x1a\n"
                 + chunk(b"IHDR", struct.pack(">IIBBBBB", 40000, 40000, 8, 6, 0, 0, 0))
                 + chunk(b"acTL", struct.pack(">II", 2, 0))
                 + chunk(b"fcTL", struct.pack(">IIIIIHHBB", 0, 1, 1, 0, 0, 1, 10, 1, 0))
                 + chunk(b"IDAT", zlib.compress(b"\0\0\0\0\0"))
                 + chunk(b"IEND", b""))
        original_fill = Image.core.fill
        allocations = []

        def bounded_fill(mode, size, *args):
            allocations.append(size)
            if size[0] * size[1] > 1_000_000:
                raise AssertionError("Oversized APNG allocation before validation")
            return original_fill(mode, size, *args)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "synthetic-apng.png"
            path.write_bytes(image)
            with patch.object(Image.core, "fill", side_effect=bounded_fill):
                result = thumbnail(path)
            self.assertIsNone(result.image)
            self.assertEqual(path.read_bytes(), image)
            self.assertTrue(all(width * height <= 1_000_000 for width, height in allocations))


if __name__ == "__main__":
    unittest.main()
