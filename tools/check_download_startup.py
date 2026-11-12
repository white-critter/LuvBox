"""Bind the isolated frozen downloader initialization check to the LuvBox.exe that ships.

Like the Qt check, this goes through the launcher: the program is verified and
unpacked into a private folder and then run with `--download-smoke`.
"""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from luvbox import __version__
from tools.check_qt_startup import launcher_environment

# Unpacking, when this is the build's first start, comes before the check itself.
TIMEOUT = 240


def validate_report(report, executable_sha256=None):
    if (not isinstance(report, dict) or report.get("error") or report.get("initialization_error")
            or report.get("version") != __version__
            or any(report.get(name) is not True for name in
                   ("ok", "frozen", "launcher", "isolated", "managed_cookies"))
            or report.get("authentication_or_network") is not False
            or type(report.get("prepared_requests")) is not int
            or report.get("prepared_requests") != 1
            or report.get("extractor") != "TwitterLikesExtractor"):
        raise ValueError("The frozen download startup report did not pass for this version")
    if executable_sha256 is not None and report.get("executable_sha256") != executable_sha256:
        raise ValueError("The executable changed after its download startup check")


def check(executable, destination, app_data=None):
    executable = Path(executable).resolve(strict=True)
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="luvbox-startup-check-") as private:
        folder = Path(app_data).resolve() if app_data else Path(private)
        folder.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [str(executable), "--download-smoke", str(destination)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=TIMEOUT, env=launcher_environment(folder),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    if not destination.is_file():
        raise RuntimeError(f"The frozen download startup did not write a report (exit code {result.returncode})")
    report = json.loads(destination.read_text(encoding="utf-8"))
    if result.returncode:
        raise RuntimeError("The frozen download startup process failed")
    validate_report(report)
    with executable.open("rb") as stream:
        report["executable_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Frozen download startup passed: {__version__}, through the launcher, isolated, "
          "first request prepared without network.")
    return report


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        raise SystemExit("Usage: check_download_startup.py EXE REPORT [APP_DATA_FOLDER]")
    check(*sys.argv[1:])
