"""Acquire pinned Qt sources and retain their original attribution files for packaging."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
import tempfile
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / "tools/qt"
MANIFEST = DIRECTORY / "sources.json"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024


def _plain(path: Path) -> None:
    for current in (path, *path.parents):
        details = current.lstat()
        if (stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400 or
                getattr(current, "is_junction", lambda: False)() or
                (stat.S_ISREG(details.st_mode) and details.st_nlink > 1)):
            raise ValueError("Qt source materials must not contain linked paths")
        if not (stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)):
            raise ValueError("Qt source materials must be regular files or directories")
        if current == ROOT:
            return
    raise ValueError("Qt source materials must be inside the project")


def _archives() -> list[dict]:
    value = json.loads(MANIFEST.read_text(encoding="utf-8"))
    rows = value["archives"]
    for row in rows:
        if (Path(row["file"]).name != row["file"] or ":" in row["file"] or
                "/" in row["component"] or "\\" in row["component"] or
                row["component"] in {"", ".", ".."} or
                len(row["sha256"]) != 64 or not row["url"].startswith("https://")):
            raise ValueError("Invalid Qt source manifest")
    return rows


def _verify(path: Path, row: dict) -> None:
    _plain(path)
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != row["sha256"]:
        raise ValueError(f"Qt source checksum mismatch: {path.name}")
    if row.get("sha1"):
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha1").hexdigest()
        if digest != row["sha1"]:
            raise ValueError(f"Qt upstream source checksum mismatch: {path.name}")


def _download(path: Path, row: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".qt-source-", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        with urlopen(row["url"], timeout=90) as response, temporary.open("wb") as target:
            size = 0
            while block := response.read(1024 * 1024):
                size += len(block)
                if size > MAX_ARCHIVE_BYTES:
                    raise ValueError("Qt source archive exceeds the acquisition limit")
                target.write(block)
        _verify(temporary, row)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _notices(path: Path, component: str):
    with tarfile.open(path, "r:*") as archive:
        seen = set()
        for member in archive:
            if not member.isfile():
                continue
            parts = PurePosixPath(member.name).parts
            if (len(parts) < 2 or any(part in {".", ".."} or ":" in part or "\\" in part for part in parts) or
                    PurePosixPath(member.name).is_absolute()):
                raise ValueError("Unsafe Qt source archive member")
            relative = PurePosixPath(*parts[1:])
            basename = relative.name.casefold()
            if not ("licenses" in [part.casefold() for part in relative.parts] or
                    basename in {"qt_attribution.json", "reuse.toml"} or
                    basename.startswith(("license", "licence", "copying", "copyright", "notice"))):
                continue
            destination = PurePosixPath(component) / relative
            key = destination.as_posix().casefold()
            if key in seen or member.size > 8 * 1024 * 1024:
                raise ValueError("Invalid Qt attribution member")
            seen.add(key)
            with archive.extractfile(member) as stream:
                yield destination, stream.read()


def release_files() -> list[tuple[Path, str]]:
    """Return only pinned archives and notices matched against those archives."""
    output = []
    size = 0
    for row in _archives():
        path = DIRECTORY / "sources" / row["file"]
        if not path.is_file():
            raise FileNotFoundError("Prepare Qt redistribution sources with tools/qt/prepare_sources.py")
        _verify(path, row)
        size += path.stat().st_size
        if size > MAX_TOTAL_BYTES:
            raise ValueError("Qt source collection exceeds the acquisition limit")
        output.append((path, "source/tools/qt/sources/" + path.name))
        for relative, data in _notices(path, row["component"]):
            notice = DIRECTORY / "notices" / Path(relative)
            _plain(notice)
            if notice.read_bytes() != data:
                raise ValueError(f"Qt attribution differs from upstream source: {relative}")
            output.append((notice, "licenses/Qt-upstream/" + relative.as_posix()))
    return output


def prepare() -> None:
    sources = DIRECTORY / "sources"
    sources.mkdir(exist_ok=True)
    _plain(sources)
    rows = _archives()
    size = 0
    notice_count = 0
    for row in rows:
        path = sources / row["file"]
        if not path.is_file():
            print(f"Acquiring {row['file']}", flush=True)
            _download(path, row)
        _verify(path, row)
        size += path.stat().st_size
        if size > MAX_TOTAL_BYTES:
            raise ValueError("Qt source collection exceeds the acquisition limit")
        for relative, data in _notices(path, row["component"]):
            target = DIRECTORY / "notices" / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            _plain(target.parent)
            if target.exists():
                _plain(target)
            target.write_bytes(data)
            notice_count += 1
    print(f"Qt redistribution sources: {len(rows)} pinned archives, {size} bytes, {notice_count} original notice files")


if __name__ == "__main__":
    prepare()
