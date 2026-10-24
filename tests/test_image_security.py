"""Reject hostile image headers without allowing large native allocations."""

from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from PIL import Image

from luvbox.preview import thumbnail


class ImageSecurityTests(unittest.TestCase):
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
