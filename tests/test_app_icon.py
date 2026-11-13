"""Keep transparent PNG and multi-size ICO assets used by the Qt app."""

from pathlib import Path
import unittest

from PIL import Image
try:
    from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
except ModuleNotFoundError:      # run as a script: the tests folder is the path
    import app_data_isolation  # noqa: F401


ICONS = Path(__file__).resolve().parents[1] / "assets" / "icons"


class AppIconTests(unittest.TestCase):
    def test_transparency_is_preserved_at_every_windows_icon_size(self):
        with Image.open(ICONS / "luvbox.png") as image:
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getpixel((0, 0))[3], 0)
        with Image.open(ICONS / "luvbox.ico") as image:
            self.assertEqual(image.ico.sizes(), {(n, n) for n in (16, 24, 32, 48, 64, 128, 256)})
            for size in image.ico.sizes():
                with image.ico.getimage(size) as rendered:
                    self.assertEqual(rendered.getpixel((0, 0))[3], 0)


if __name__ == "__main__":
    unittest.main()
