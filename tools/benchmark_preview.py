"""Compare large PNG preview decoding using a generated temporary image.

Run from the project root: python tools/benchmark_preview.py --repeats 5
Measures the image pipeline only; no accounts, media libraries or X requests.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageOps, __version__ as pillow_version

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from luvbox.preview import oriented_thumbnail


def _baseline(original, size):
    original.draft("RGB", size)
    image = ImageOps.exif_transpose(original)
    try:
        image.thumbnail(size, Image.Resampling.LANCZOS)
        return image.convert("RGBA")
    finally:
        image.close()


def _median_ms(path, size, pipeline, repeats):
    samples = []
    for _ in range(repeats + 1):
        started = time.perf_counter()
        with Image.open(path) as original:
            image = pipeline(original, size)
        image.close()
        samples.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(samples[1:]), 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=5)
    options = parser.parse_args()
    if options.repeats < 1:
        parser.error("repeats must be positive")
    results = []
    with tempfile.TemporaryDirectory(prefix="luvbox-preview-benchmark-") as temporary:
        path = Path(temporary) / "synthetic.png"
        with Image.new("RGB", (6000, 4000), (120, 90, 160)) as image:
            image.save(path)
        for size in ((150, 112), (640, 400)):
            baseline = _median_ms(path, size, _baseline, options.repeats)
            optimized = _median_ms(path, size, oriented_thumbnail, options.repeats)
            results.append({"preview_size": size, "baseline_ms": baseline,
                            "optimized_ms": optimized, "speedup": round(baseline / optimized, 2)})
    print(json.dumps({"python": platform.python_version(), "pillow": pillow_version,
                      "image_size": [6000, 4000], "repeats": options.repeats,
                      "results": results}, indent=2))


if __name__ == "__main__":
    main()
