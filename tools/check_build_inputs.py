"""Check pinned Windows build inputs without downloads or executing tools."""

import argparse
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
_LFS_HEADER = b"version https://git-lfs.github.com/spec/v1"
_FONT_FILES = ("Pretendard-Regular.ttf", "Pretendard-Bold.ttf", "OFL.txt")
_SECURE_PYTHON_PATCHES = {(3, 12): 16, (3, 13): 18, (3, 14): 10, (3, 15): 2}


def require_secure_python(version=None):
    """Reject runtimes older than the security baseline for their series."""
    current = tuple((sys.version_info if version is None else version)[:3])
    minimum = _SECURE_PYTHON_PATCHES.get(current[:2])
    if current[:2] < (3, 12) or (minimum is not None and current[2] < minimum):
        found = ".".join(map(str, current))
        baselines = ", ".join(f"{major}.{minor}.{patch}" for (major, minor), patch
                              in _SECURE_PYTHON_PATCHES.items())
        raise ValueError(f"Python {found} is below the security baseline. "
                         f"Minimum patch per supported series: {baselines}; newer series are allowed. "
                         "Update Python and recreate .venv before setup or building.")


def check_inputs(root=ROOT, *, assets_only=False):
    """Return all missing/changed input problems; never prepare those inputs.

    The early assets-only pass checks files obtained by Git LFS. The complete
    pass also checks the source archive and prepared ExifTool directory before
    PyInstaller or ZIP packaging. ExifTool execution/version is checked by its
    existing setup helper, not by this read-only check.
    """
    root = Path(root)
    problems = []

    def manifest(relative):
        try:
            value = json.loads((root / relative).read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("manifest must be an object")
            return value
        except (OSError, ValueError):
            problems.append(f"{relative}: missing or invalid manifest.")
            return None

    def pinned(relative, expected, size=None):
        path = root / relative
        try:
            with path.open("rb") as source:
                if source.read(len(_LFS_HEADER)) == _LFS_HEADER:
                    problems.append(f"{relative}: Git LFS pointer; run git lfs pull.")
                    return
                source.seek(0)
                actual = hashlib.file_digest(source, "sha256").hexdigest()
            if (size is not None and path.stat().st_size != size) or actual != expected:
                problems.append(f"{relative}: size or SHA-256 differs from the pinned manifest.")
        except OSError:
            problems.append(f"{relative}: missing or unreadable build input.")

    fonts = manifest("assets/fonts/Pretendard/manifest.json")
    if fonts is not None:
        try:
            for name in _FONT_FILES:
                entry = fonts["files"][name]
                pinned(f"assets/fonts/Pretendard/{name}", entry["sha256"], entry["bytes"])
        except (KeyError, TypeError):
            problems.append("assets/fonts/Pretendard/manifest.json: incomplete font metadata.")

    ffmpeg = manifest("tools/ffmpeg/manifest.json")
    if ffmpeg is not None:
        try:
            runtime = ffmpeg["runtime"]
            pinned("tools/ffmpeg/runtime/ffmpeg.exe", runtime["sha256"], runtime["size"])
            if not assets_only:
                pinned("tools/ffmpeg/ffmpeg-7.1.tar.xz", ffmpeg["source"]["sha256"])
            for name in ("build.py", "configure-flags.json"):
                pinned("tools/ffmpeg/" + name, ffmpeg["build_files"][name])
        except (KeyError, TypeError):
            problems.append("tools/ffmpeg/manifest.json: incomplete FFmpeg metadata.")

    if not assets_only:
        runtime = root / "tools/exiftool/runtime"
        try:
            for relative in ("exiftool.exe", "exiftool_files/perl.exe"):
                with (runtime / relative).open("rb") as executable:
                    if executable.read(2) != b"MZ":
                        raise ValueError("incomplete native runtime")
        except (OSError, ValueError):
            problems.append("tools/exiftool/runtime: missing or incomplete Windows bundle; complete setup.ps1 first.")
    return problems


def require_inputs(root=ROOT, *, assets_only=False):
    problems = check_inputs(root, assets_only=assets_only)
    if problems:
        raise ValueError("Build inputs are not ready:\n" + "\n".join(problems))


def main(argv=None, *, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--assets-only", action="store_true",
                      help="Check pinned Git assets before setup helpers prepare other inputs")
    mode.add_argument("--python-only", action="store_true",
                      help="Check the Python security baseline before creating or preparing a virtual environment")
    options = parser.parse_args(argv)
    try:
        require_secure_python()
        if not options.python_only:
            require_inputs(root, assets_only=options.assets_only)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    if options.python_only:
        print("Python security baseline verified.")
    else:
        print("Python security baseline and pinned build inputs verified (files only; no tools executed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
