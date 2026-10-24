"""Build deterministic control PNG bundles before PyInstaller collects data.

No Tk interpreter or display is needed. Pixel-rendering changes must increment
theme._RENDERER_REVISION; palette/spec parameters already invalidate the key.
"""

import argparse
import io
import json
from pathlib import Path
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from luvbox import theme  # noqa: E402


def _archive(assets, manifest):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        entries = {**assets, "manifest.json": json.dumps(manifest, sort_keys=True,
                                                        separators=(",", ":")).encode("utf-8")}
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return output.getvalue()


def build(directory=None, *, check=False):
    directory = Path(directory) if directory is not None else ROOT / "assets" / "theme"
    previous = theme._active_mode
    try:
        for mode, palette in (("light", theme.LIGHT), ("dark", theme.DARK)):
            theme._active_mode = mode
            theme.__dict__.update(palette)
            assets = {}
            theme._draw_assets(assets.__setitem__)
            data = _archive(assets, theme._asset_manifest(theme._asset_key()))
            location = directory / f"{mode}.zip"
            if check:
                if not location.is_file() or location.read_bytes() != data:
                    raise ValueError(f"Regenerate theme assets: {location}")
            else:
                directory.mkdir(parents=True, exist_ok=True)
                location.write_bytes(data)
    finally:
        theme._active_mode = previous
        theme.__dict__.update(theme.DARK if previous == "dark" else theme.LIGHT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check that the committed bundles match the renderer")
    build(check=parser.parse_args().check)
