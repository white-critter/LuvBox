"""Draw the LuvBox app icon: a heart outline on a dark rounded square.

Run once after changing the design, then tools/build_icon.py makes the ICO.
Drawn at 1024 px and reduced, so edges stay smooth at every Windows size.
"""

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "assets" / "icons" / "luvbox.png"
BACKGROUND = (18, 18, 18, 255)
HEART = (255, 77, 79, 255)
SIZE = 1024


def heart(center_x, center_y, scale):
    """A compact outline with gentle lobes and a pointed, rounded bottom."""
    points = [(12, 5.9)]

    def curve(control1, control2, end):
        start = points[-1]
        for index in range(1, 201):
            t = index / 200
            s = 1 - t
            points.append(tuple(s ** 3 * start[axis] + 3 * s ** 2 * t * control1[axis] +
                                3 * s * t ** 2 * control2[axis] + t ** 3 * end[axis]
                                for axis in (0, 1)))

    curve((10.2, 2.6), (6.8, 2.0), (4.2, 3.7))
    curve((1.5, 5.6), (1.5, 9.2), (3.8, 11.5))
    points.extend(((12, 19.9), (20.2, 11.5)))
    curve((22.5, 9.2), (22.5, 5.6), (19.8, 3.7))
    curve((17.2, 2.0), (13.8, 2.6), (12, 5.9))
    return [(center_x + (x - 12) * scale, center_y + (y - 11.3) * scale) for x, y in points]


def draw() -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas = ImageDraw.Draw(image)
    margin = SIZE * 0.06
    canvas.rounded_rectangle((margin, margin, SIZE - margin, SIZE - margin), radius=SIZE * 0.24, fill=BACKGROUND)
    # A fine red outline; the empty center and generous padding match the
    # reference. Round caps keep the notch and bottom soft at small sizes.
    radius = SIZE * 0.0125
    points = heart(SIZE / 2, SIZE / 2, SIZE * 0.0175)
    canvas.line(points, fill=HEART, width=round(radius * 2), joint="curve")
    for x, y in points:
        canvas.ellipse((x - radius, y - radius, x + radius, y + radius), fill=HEART)
    return image.resize((256, 256), Image.Resampling.LANCZOS)


if __name__ == "__main__":
    draw().save(TARGET)
    print(TARGET)
