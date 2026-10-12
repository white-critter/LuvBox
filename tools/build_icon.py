"""Convert the transparent source PNG to a Windows multi-resolution ICO."""

from pathlib import Path
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "assets" / "icons"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def build():
    with Image.open(DIRECTORY / "luvbox.png") as image:
        if image.mode != "RGBA" or image.width != image.height or image.getextrema()[3][0] != 0:
            raise ValueError("A square RGBA image with a transparent background is required")
        image.save(DIRECTORY / "luvbox.ico", sizes=[(size, size) for size in SIZES])


if __name__ == "__main__":
    build()
