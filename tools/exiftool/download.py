"""Prepare the pinned Windows bundle from the ExifTool Windows maintainer.

Called by setup.ps1 only. Runtime media operations never download software.
"""

import argparse
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


VERSION = "13.59"
URL = "https://oliverbetz.de/cms/files/Artikel/ExifTool-for-Windows/exiftool-13.59_64.zip"
SHA256 = "577257dd22baebe77157d905792d6ed2c5916cd03aa627f40b2175db12110ac6"
MAX_DOWNLOAD = 40 * 1024 * 1024
MAX_EXPANDED = 100 * 1024 * 1024


def extract_bundle(archive: Path, destination: Path) -> None:
    """Extract a bounded ZIP without links, absolute names, or parent paths."""
    root = destination.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if len(entries) > 10000 or sum(entry.file_size for entry in entries) > MAX_EXPANDED:
            raise ValueError("invalid archive size")
        for entry in entries:
            relative = PurePosixPath(entry.filename.replace("\\", "/"))
            if relative.is_absolute() or not relative.parts or any(part in {".", ".."} or ":" in part for part in relative.parts):
                raise ValueError("invalid archive path")
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("archive link")
            target = root.joinpath(*relative.parts).resolve()
            if not target.is_relative_to(root):
                raise ValueError("invalid archive path")
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(entry) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)


def _valid_runtime(runtime: Path) -> bool:
    executable = runtime / "exiftool.exe"
    if not executable.is_file() or not (runtime / "exiftool_files").is_dir():
        return False
    try:
        result = subprocess.run([str(executable), "-config", "", "-ver"], capture_output=True, text=True, timeout=20,
                                shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode == 0 and result.stdout.strip() == VERSION
    except (OSError, subprocess.SubprocessError):
        return False


def prepare(local_zip: Path | None = None) -> Path:
    root = Path(__file__).resolve().parent
    runtime = root / "runtime"
    if _valid_runtime(runtime):
        return runtime
    if runtime.exists():
        raise ValueError("existing invalid runtime")
    # A sibling stage ensures os.replace publishes the full bundle atomically.
    with tempfile.TemporaryDirectory(prefix=".exiftool-setup-", dir=root) as directory:
        stage = Path(directory).resolve()
        if not stage.is_relative_to(root):
            raise ValueError("invalid stage")
        archive = stage / "bundle.zip"
        if local_zip is not None:
            if local_zip.stat().st_size > MAX_DOWNLOAD:
                raise ValueError("invalid archive size")
            shutil.copyfile(local_zip, archive)
        else:
            with urllib.request.urlopen(URL, timeout=60) as response, archive.open("wb") as output:
                size = 0
                while chunk := response.read(256 * 1024):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        raise ValueError("invalid download size")
                    output.write(chunk)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
            raise ValueError("download integrity")
        bundle = stage / "runtime"
        extract_bundle(archive, bundle)
        # This maintainer's ZIP uses ExifTool.exe; Windows names are case-insensitive.
        if not (bundle / "exiftool.exe").exists():
            for name in ("ExifTool.exe", "exiftool(-k).exe"):
                if (bundle / name).is_file():
                    (bundle / name).rename(bundle / "exiftool.exe")
                    break
        if not _valid_runtime(bundle):
            raise ValueError("invalid bundle")
        os.replace(bundle, runtime)
    return runtime


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare ExifTool 13.59 for local source metadata.")
    parser.add_argument("--zip", type=Path, help="Use an already downloaded pinned official ZIP.")
    arguments = parser.parse_args()
    try:
        prepare(arguments.zip)
    except Exception:
        print("ExifTool 준비에 실패했습니다. 공식 배포 파일과 tools/exiftool 폴더 권한을 확인해 주세요.", file=sys.stderr)
        return 1
    print("ExifTool 13.59 준비 완료 (파일 메타데이터 기록용).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
