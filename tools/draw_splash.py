"""Draw the startup splash layers: a dark card, a red glow and the LuvBox mark.

The launcher (tools/launcher/Launcher.cs) stacks the three PNGs, lets the glow
breathe, and draws the changing lines and the progress bar itself. Layout and
wording come from tools/launcher/splash.ini so both sides agree.

Run after changing the design: .venv/Scripts/python.exe tools/draw_splash.py
Add --preview FILE to also save one composed picture with sample text.
"""

from pathlib import Path
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.draw_icon import HEART, heart  # noqa: E402

TARGET = ROOT / "assets" / "splash"
SETTINGS = ROOT / "tools" / "launcher" / "splash.ini"
FONTS = ROOT / "assets" / "fonts" / "Pretendard"
SCALE = 2          # the saved layers, for sharp text up to 200% display scaling
OVERSAMPLE = 2     # shapes are drawn larger again and reduced, like the icon
TEXT = (244, 241, 241, 255)
MUTED = (185, 175, 177, 255)
FAINT = (125, 116, 118, 255)
LINE = (44, 44, 44, 255)
ACCENT = (217, 54, 69, 255)


def read_settings(path=SETTINGS):
    """key=value lines; a repeated key keeps every value in order."""
    values = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values.setdefault(key.strip(), []).append(value.strip())
    return values


def numbers(settings, key):
    return tuple(int(part) for part in settings[key][0].split(","))


def _rounded_mask(size, box, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    return mask


def card_layer(settings):
    width, height = numbers(settings, "width")[0], numbers(settings, "height")[0]
    left, top, card_width, card_height, radius = numbers(settings, "card")
    factor = SCALE * OVERSAMPLE
    size = (width * factor, height * factor)
    box = (left * factor, top * factor, (left + card_width) * factor, (top + card_height) * factor)
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    # A soft shadow a little below the card, so it lifts off any wallpaper.
    shadow = _rounded_mask(size, (box[0], box[1] + 8 * factor, box[2], box[3] + 8 * factor), radius * factor)
    shadow = shadow.filter(ImageFilter.GaussianBlur(11 * factor)).point(lambda value: int(value * 0.62))
    image.putalpha(shadow)
    # The body: nearly black, a touch lighter at the top.
    body = Image.new("RGBA", size, (0, 0, 0, 0))
    gradient = Image.new("RGBA", (1, size[1]), (0, 0, 0, 0))
    for y in range(size[1]):
        t = min(1.0, max(0.0, (y - box[1]) / max(1, box[3] - box[1])))
        shade = round(26 - 12 * t)
        gradient.putpixel((0, y), (shade, shade, shade, 255))
    body.paste(gradient.resize(size), (0, 0))
    mask = _rounded_mask(size, box, radius * factor)
    image = Image.composite(body, image, mask)
    # A hairline border and a faint lit edge along the top.
    edge = ImageDraw.Draw(image)
    edge.rounded_rectangle(box, radius=radius * factor, outline=LINE, width=factor)
    highlight = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(highlight).rounded_rectangle(
        (box[0] + factor, box[1] + factor, box[2] - factor, box[1] + 60 * factor),
        radius=(radius - 1) * factor, outline=(255, 255, 255, 26), width=factor)
    fade = Image.new("L", size, 0)
    for y in range(box[1], box[1] + 30 * factor):
        ImageDraw.Draw(fade).line(((0, y), (size[0], y)), fill=round(255 * (1 - (y - box[1]) / (30 * factor))))
    highlight.putalpha(ImageChops.multiply(highlight.getchannel("A"), fade))
    image = Image.alpha_composite(image, highlight)
    return image.resize((width * SCALE, height * SCALE), Image.Resampling.LANCZOS)


def glow_layer(settings):
    """Red light behind the heart; the launcher varies its strength."""
    width, height = numbers(settings, "width")[0], numbers(settings, "height")[0]
    left, top, card_width, card_height, radius = numbers(settings, "card")
    center_x, center_y = numbers(settings, "heart_center")
    size = (width * SCALE, height * SCALE)
    alpha = Image.new("L", size, 0)
    draw = ImageDraw.Draw(alpha)
    reach = 190 * SCALE
    for step in range(reach, 0, -2):
        t = step / reach
        strength = (1 - t) ** 2.2
        draw.ellipse((center_x * SCALE - step * 1.25, center_y * SCALE - step,
                      center_x * SCALE + step * 1.25, center_y * SCALE + step), fill=round(150 * strength))
    alpha = alpha.filter(ImageFilter.GaussianBlur(6 * SCALE))
    inside = _rounded_mask(size, ((left + 1) * SCALE, (top + 1) * SCALE,
                                  (left + card_width - 1) * SCALE, (top + card_height - 1) * SCALE),
                           (radius - 1) * SCALE)
    alpha = ImageChops.multiply(alpha, inside)
    image = Image.new("RGBA", size, ACCENT[:3] + (0,))
    image.putalpha(alpha)
    return image


def _centered(draw, text, font, center_x, center_y, fill):
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text((center_x - (left + right) / 2, center_y - (top + bottom) / 2), text, font=font, fill=fill)


def mark_layer(settings):
    width, height = numbers(settings, "width")[0], numbers(settings, "height")[0]
    center_x, center_y = numbers(settings, "heart_center")
    heart_width = numbers(settings, "heart_width")[0]
    factor = SCALE * OVERSAMPLE
    size = (width * factor, height * factor)
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    # The icon's heart outline: the same curve, a slightly finer line.
    unit = heart_width * factor / 21.0
    stroke = unit * 1.22
    points = heart(center_x * factor, center_y * factor, unit)
    draw.line(points, fill=HEART, width=round(stroke), joint="curve")
    for x, y in points:
        draw.ellipse((x - stroke / 2, y - stroke / 2, x + stroke / 2, y + stroke / 2), fill=HEART)
    image = image.resize((width * SCALE, height * SCALE), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(image)
    wordmark = ImageFont.truetype(str(FONTS / "Pretendard-Bold.ttf"), numbers(settings, "wordmark_size")[0] * SCALE)
    tagline = ImageFont.truetype(str(FONTS / "Pretendard-Regular.ttf"), numbers(settings, "tagline_size")[0] * SCALE)
    middle = width * SCALE / 2
    _centered(draw, settings["wordmark"][0], wordmark, middle, numbers(settings, "wordmark_y")[0] * SCALE, TEXT)
    _centered(draw, settings["tagline"][0], tagline, middle, numbers(settings, "tagline_y")[0] * SCALE, MUTED)
    return image


def layers(settings=None):
    settings = settings or read_settings()
    return {"card.png": card_layer(settings), "glow.png": glow_layer(settings), "mark.png": mark_layer(settings)}


def preview(settings, drawn, status_key="status.first", tip_index=0, progress=0.42, glow=0.8):
    """What the launcher shows, composed here to judge the design."""
    image = drawn["card.png"].copy()
    light = drawn["glow.png"].copy()
    light.putalpha(light.getchannel("A").point(lambda value: int(value * glow)))
    image = Image.alpha_composite(image, light)
    image = Image.alpha_composite(image, drawn["mark.png"])
    draw = ImageDraw.Draw(image)
    middle = image.width / 2
    regular = FONTS / "Pretendard-Regular.ttf"
    status = ImageFont.truetype(str(regular), numbers(settings, "status_size")[0] * SCALE)
    tip = ImageFont.truetype(str(regular), numbers(settings, "tip_size")[0] * SCALE)
    small = ImageFont.truetype(str(regular), numbers(settings, "version_size")[0] * SCALE)
    _centered(draw, settings[status_key][0], status, middle, numbers(settings, "status_y")[0] * SCALE, TEXT)
    tips = settings["tip.open" if status_key == "status.open" else "tip.unpack"]
    _centered(draw, tips[tip_index % len(tips)], tip, middle, numbers(settings, "tip_y")[0] * SCALE, FAINT)
    left, top, bar_width, bar_height = (value * SCALE for value in numbers(settings, "bar"))
    draw.rounded_rectangle((left, top, left + bar_width, top + bar_height), radius=bar_height / 2, fill=(42, 42, 42, 255))
    draw.rounded_rectangle((left, top, left + bar_width * progress, top + bar_height), radius=bar_height / 2, fill=HEART)
    version = "1.8.0"
    box = draw.textbbox((0, 0), version, font=small)
    draw.text((numbers(settings, "version_right")[0] * SCALE - box[2],
               numbers(settings, "version_y")[0] * SCALE - (box[1] + box[3]) / 2), version, font=small, fill=FAINT)
    return image


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    settings = read_settings()
    drawn = layers(settings)
    TARGET.mkdir(parents=True, exist_ok=True)
    for name, image in drawn.items():
        image.save(TARGET / name, optimize=True)
        print(TARGET / name)
    if "--preview" in arguments:
        destination = Path(arguments[arguments.index("--preview") + 1])
        destination.parent.mkdir(parents=True, exist_ok=True)
        backdrop = Image.new("RGBA", drawn["card.png"].size, (58, 76, 96, 255))
        Image.alpha_composite(backdrop, preview(settings, drawn)).save(destination)
        opened = Image.new("RGBA", drawn["card.png"].size, (236, 236, 236, 255))
        Image.alpha_composite(opened, preview(settings, drawn, "status.open", 2, 0.3, 0.45)).save(
            destination.with_name(destination.stem + "-open" + destination.suffix))
        print(destination)


if __name__ == "__main__":
    main()
