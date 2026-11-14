"""Require an isolated native-window startup from the exact LuvBox.exe that ships.

The check starts the single program the way a person does: its launcher
verifies and unpacks the application (into a private folder given here, never
the developer's own %LOCALAPPDATA%\\LuvBox), then runs it with `--qt-smoke`.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from luvbox import __version__

# The first start of a build also unpacks about 270 MB before the window opens.
TIMEOUT = 240


def validate_report(report, executable_sha256=None):
    required = ("ok", "frozen", "launcher", "isolated", "window_visible", "window_exposed",
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


def launcher_environment(app_data):
    """The launcher unpacks under LOCALAPPDATA; point it at a folder of the check's own.

    Qt does not read LOCALAPPDATA: it asks Windows for the folder of its compiled-QML
    cache, so that cache gets the check's folder through its own variable. Without it
    every checked build left .qmlc files in the developer's real LuvBox cache folder.
    """
    environment = dict(os.environ)
    environment["LOCALAPPDATA"] = str(app_data)
    environment["QML_DISK_CACHE_PATH"] = str(Path(app_data) / "qml-cache")
    environment["LUVBOX_LAUNCHER_SILENT"] = "1"
    for name in ("LUVBOX_LAUNCHER", "LUVBOX_READY_EVENT"):
        environment.pop(name, None)
    return environment


def check(executable, destination, app_data=None):
    executable = Path(executable).resolve(strict=True)
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="luvbox-startup-check-") as private:
        folder = Path(app_data).resolve() if app_data else Path(private)
        folder.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [str(executable), "--qt-smoke", str(destination)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=TIMEOUT, env=launcher_environment(folder),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    if not destination.is_file():
        raise RuntimeError(f"The frozen Qt startup did not write a report (exit code {result.returncode}).")
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
    print(f"Frozen Qt startup passed: {__version__}, through the launcher, native window, gallery/photo, "
          "dialogs, clean shutdown.")
    return report


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        raise SystemExit("Usage: check_qt_startup.py EXE REPORT [APP_DATA_FOLDER]")
    check(*sys.argv[1:])
