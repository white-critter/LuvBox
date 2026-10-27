"""Require an isolated native-window startup from the exact frozen executable."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from luvbox import __version__


def validate_report(report, executable_sha256=None):
    required = ("ok", "frozen", "isolated", "window_visible", "window_exposed",
                "gallery_thumbnail_ready", "photo_preview_ready", "settings_and_confirmation",
                "connection_dialog", "shutdown_ready", "temporary_data_removed")
    if (not isinstance(report, dict) or report.get("error")
            or report.get("version") != __version__
            or any(report.get(name) is not True for name in required)
            or report.get("qml_errors") != []
            or (os.name == "nt" and report.get("platform") != "windows")):
        raise ValueError("The frozen Qt startup report did not pass for this version")
    if executable_sha256 is not None and report.get("executable_sha256") != executable_sha256:
        raise ValueError("The executable changed after its Qt startup check")


def check(executable, destination):
    executable = Path(executable).resolve(strict=True)
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    result = subprocess.run(
        [str(executable), "--qt-smoke", str(destination)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if not destination.is_file():
        raise RuntimeError("The frozen Qt startup did not write a report.")
    report = json.loads(destination.read_text(encoding="utf-8"))
    if result.returncode != 0:
        raise RuntimeError("Frozen Qt startup failed: child process did not exit successfully")
    try:
        validate_report(report)
    except ValueError as error:
        raise RuntimeError("Frozen Qt startup failed: " + str(error)) from None
    with executable.open("rb") as stream:
        report["executable_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Frozen Qt startup passed: {__version__}, native window, gallery/photo, dialogs, clean shutdown.")
    return report


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: check_qt_startup.py EXE REPORT")
    check(sys.argv[1], sys.argv[2])
