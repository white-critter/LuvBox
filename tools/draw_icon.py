"""Draw the LuvBox app icon: a heart outline on a dark rounded square.

Run once after changing the design, then tools/build_icon.py makes the ICO.
Drawn at 1024 px and reduced, so edges stay smooth at every Windows size.
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "assets" / "icons" / "luvbox.png"
BACKGROUND = (38, 38, 43, 255)      # near-black slate
HEART = (255, 122, 138, 255)        # soft coral pink
SIZE = 1024


def heart(center_x, center_y, scale, steps=720):
    points = []
    for index in range(steps):
        t = 2 * math.pi * index / steps
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        points.append((center_x + x * scale, center_y - y * scale))
    return points


def draw() -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas = ImageDraw.Draw(image)
    margin = SIZE * 0.06
    canvas.rounded_rectangle((margin, margin, SIZE - margin, SIZE - margin), radius=SIZE * 0.22, fill=BACKGROUND)
    # A round brush along the curve: an even stroke with smooth joins.
    radius = SIZE * 0.036
    for x, y in heart(SIZE / 2, SIZE * 0.525, SIZE * 0.0158, steps=2400):
        canvas.ellipse((x - radius, y - radius, x + radius, y + radius), fill=HEART)
    return image.resize((256, 256), Image.Resampling.LANCZOS)


if __name__ == "__main__":
    draw().save(TARGET)
    print(TARGET)
