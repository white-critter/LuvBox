"""Bind the isolated frozen downloader initialization check to its executable."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from luvbox import __version__


def validate_report(report, executable_sha256=None):
    if (not isinstance(report, dict) or report.get("error") or report.get("initialization_error")
            or report.get("version") != __version__
            or any(report.get(name) is not True for name in
                   ("ok", "frozen", "isolated", "managed_cookies"))
            or report.get("authentication_or_network") is not False
            or type(report.get("prepared_requests")) is not int
            or report.get("prepared_requests") != 1
            or report.get("extractor") != "TwitterLikesExtractor"):
        raise ValueError("The frozen download startup report did not pass for this version")
    if executable_sha256 is not None and report.get("executable_sha256") != executable_sha256:
        raise ValueError("The executable changed after its download startup check")


def check(executable, destination):
    executable = Path(executable).resolve(strict=True)
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    result = subprocess.run(
        [str(executable), "--download-smoke", str(destination)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if not destination.is_file():
        raise RuntimeError("The frozen download startup did not write a report")
    report = json.loads(destination.read_text(encoding="utf-8"))
    if result.returncode:
        raise RuntimeError("The frozen download startup process failed")
    validate_report(report)
    with executable.open("rb") as stream:
        report["executable_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Frozen download startup passed: {__version__}, isolated, first request prepared without network.")
    return report


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: check_download_startup.py EXE REPORT")
    check(sys.argv[1], sys.argv[2])
