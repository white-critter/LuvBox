"""Find a newer LuvBox release, fetch its program and put it in the launcher's place.

Nothing about this PC or its user is sent: one anonymous read of the public
release description, then (only after a click) the release's `LuvBox.exe` and
checksum file. The names and the checksum format below are a contract between
releases: `tools/package_release.py` writes them from here, and tests pin them,
so a later release cannot ship something an installed version fails to read.

A running LuvBox never replaces itself. It runs from its own unpacked folder
(see luvbox/launcher.py); the update swaps the launcher file, and the next
start unpacks and runs the new version.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .launcher import read_footer


REPOSITORY = "white-critter/LuvBox"
EXE_ASSET = "LuvBox.exe"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases/latest"
# A fixed, generic name: no version, no machine or account detail.
CLIENT_NAME = "LuvBox-Updater"
CHECK_INTERVAL = timedelta(hours=24)
_VERSION = re.compile(r"v?([0-9]{1,5})\.([0-9]{1,5})\.([0-9]{1,5})\Z")
_CHECKSUM_LINE = re.compile(r"([0-9a-f]{64})  ([^\s/\\][^/\\\r\n]{0,199})\Z")
_DESCRIPTION_LIMIT = 1024 * 1024
_CHECKSUM_LIMIT = 64 * 1024
_PROGRAM_MINIMUM = 1024 * 1024
_PROGRAM_LIMIT = 2 * 1024 * 1024 * 1024
_NOTE_LINES = 8
_NOTE_LENGTH = 220

MESSAGES = {
    "network": "새 버전을 확인하지 못했어요. 인터넷 연결을 확인하고 다시 시도해 주세요.",
    "limited": "지금은 확인할 수 없어요. 잠시 뒤 다시 시도해 주세요.",
    "unexpected": "릴리스 정보를 읽지 못했어요. 릴리스 페이지에서 직접 확인해 주세요.",
    "download": "새 버전을 받지 못했어요. 인터넷 연결을 확인하고 다시 시도해 주세요.",
    "checksum": "받은 파일이 릴리스의 체크섬과 달라 지웠어요. 다시 시도해 주세요.",
    "program": "받은 파일이 LuvBox 실행 파일이 아니에요. 릴리스 페이지에서 직접 받아 주세요.",
    "space": "받은 파일을 저장하지 못했어요. 디스크 여유 공간을 확인해 주세요.",
    "replace": "LuvBox.exe를 바꾸지 못했어요. 파일이 있는 폴더에 쓸 수 있는지 확인하거나, 릴리스 페이지에서 직접 받아 주세요.",
    "cancelled": "업데이트 받기를 멈췄어요.",
}


class UpdateError(Exception):
    """A failure the person can act on; `kind` names it, the text is fixed."""

    def __init__(self, kind: str):
        super().__init__(MESSAGES[kind])
        self.kind = kind


def checksum_asset(version: str) -> str:
    return f"SHA256-{version}.txt"


def source_asset(version: str) -> str:
    return f"LuvBox-{version}-source-licenses.zip"


def release_assets(version: str) -> tuple[str, str, str]:
    """The files every release carries, by the names an installed version looks for."""
    return EXE_ASSET, checksum_asset(version), source_asset(version)


def parse_version(text) -> tuple[int, int, int] | None:
    match = _VERSION.match(text) if isinstance(text, str) else None
    return tuple(int(part) for part in match.groups()) if match else None


def format_checksums(rows) -> str:
    """`<64 lowercase hex>  <file name>` per line: what sha256sum prints and checks."""
    lines = []
    for digest, name in rows:
        line = f"{digest}  {name}"
        if not _CHECKSUM_LINE.match(line):
            raise ValueError("A checksum row must be a lowercase SHA-256 and a plain file name")
        lines.append(line)
    return "\n".join(lines) + "\n"


def parse_checksums(text: str) -> dict[str, str]:
    """File name -> SHA-256 for every well-formed row; other lines are ignored."""
    found = {}
    for line in text.splitlines():
        match = _CHECKSUM_LINE.match(line.strip("\r\n"))
        if match:
            found.setdefault(match[2], match[1])
    return found


def summarize(description) -> str:
    """The release's own list of changes as a few plain lines for the notice."""
    if not isinstance(description, str):
        return ""
    lines = description.splitlines()
    heading = next((index for index, line in enumerate(lines)
                    if line.lstrip().startswith("#") and "변경" in line), None)
    if heading is None:
        section = lines
    else:
        # The list under the "changes" heading, up to the next heading.
        section = []
        for line in lines[heading + 1:]:
            if line.lstrip().startswith("#"):
                break
            section.append(line)
    # Top-level bullets only: nested detail does not fit a notice.
    items = [match[1] for line in section if (match := re.match(r"[-*+]\s+(.+)", line))]
    if not items:
        items = []
        for line in section:
            if line.lstrip().startswith("```"):
                break
            if line.strip() and not line.lstrip().startswith("#"):
                items.append(line.strip())
        items = items[:3]
    cleaned = []
    for item in items:
        item = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", item)   # [text](link) -> text
        item = re.sub(r"[*_`]+", "", item).strip()
        if not item:
            continue
        if len(item) > _NOTE_LENGTH:
            item = item[:_NOTE_LENGTH - 1].rstrip() + "…"
        cleaned.append("· " + item)
        if len(cleaned) == _NOTE_LINES:
            break
    return "\n".join(cleaned)


@dataclass(frozen=True)
class Source:
    """Where releases are read from. Only tests point this anywhere but GitHub."""

    latest: str = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
    downloads: str = f"https://github.com/{REPOSITORY}/releases/download"
    # GitHub serves release files from its own storage hosts after a redirect.
    hosts: tuple = ("api.github.com", "github.com", ".githubusercontent.com")
    secure: bool = True

    def asset(self, version: str, name: str) -> str:
        return f"{self.downloads}/v{version}/{urllib.parse.quote(name)}"

    def allows(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        if not self.secure:
            return parts.scheme in {"http", "https"} and parts.hostname in {"127.0.0.1", "localhost"}
        host = (parts.hostname or "").lower()
        return (parts.scheme == "https" and not parts.username and not parts.password
                and parts.port in (None, 443)
                and any(host == allowed or (allowed.startswith(".") and host.endswith(allowed))
                        for allowed in self.hosts))


GITHUB = Source()


class _Refused(OSError):
    """The server answered with an error status."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, source):
        self._source = source

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self._source.allows(newurl):
            fp.close()
            raise urllib.error.URLError("redirect outside the release hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(source: Source, url: str, accept: str, timeout: float):
    if not source.allows(url):
        raise UpdateError("unexpected")
    # No cookies and no identifying headers: only what an anonymous read needs.
    opener = urllib.request.build_opener(_Redirects(source))
    request = urllib.request.Request(url, headers={"User-Agent": CLIENT_NAME, "Accept": accept})
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        # The error object holds the connection; keep only its status.
        status = error.code
        error.close()
        raise _Refused(status) from None


def _read_limited(response, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("unexpected")
    return data


def check(current: str, source: Source = GITHUB, *, timeout: float = 15) -> dict:
    """Describe the newest release relative to `current`.

    {"version", "newer", "installable", "notes", "size"}: `installable` is
    False for a release without the program and checksum files this version
    knows how to fetch; the person is then sent to the release page instead.
    """
    installed = parse_version(current)
    if installed is None:
        raise UpdateError("unexpected")
    try:
        with _open(source, source.latest, "application/vnd.github+json", timeout) as response:
            description = json.loads(_read_limited(response, _DESCRIPTION_LIMIT).decode("utf-8"))
    except _Refused as error:
        if error.status == 404:
            # A repository without a published release has nothing newer.
            return {"version": current, "newer": False, "installable": False, "notes": "", "size": 0}
        raise UpdateError("limited" if error.status in (403, 429) else "network") from None
    except UpdateError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise UpdateError("network") from None
    if not isinstance(description, dict):
        raise UpdateError("unexpected")
    found = parse_version(description.get("tag_name"))
    if found is None or description.get("draft") is True or description.get("prerelease") is True:
        raise UpdateError("unexpected")
    version = ".".join(str(part) for part in found)
    result = {"version": version, "newer": found > installed, "installable": False, "notes": "", "size": 0}
    if not result["newer"]:
        return result
    assets = {}
    listed = description.get("assets")
    for asset in listed if isinstance(listed, list) else ():
        if isinstance(asset, dict) and isinstance(asset.get("name"), str) and asset.get("state", "uploaded") == "uploaded":
            assets.setdefault(asset["name"], asset)
    program = assets.get(EXE_ASSET)
    size = program.get("size") if program else None
    if (program and checksum_asset(version) in assets and type(size) is int
            and _PROGRAM_MINIMUM <= size <= _PROGRAM_LIMIT):
        result.update(installable=True, size=size)
    result["notes"] = summarize(description.get("body"))
    return result


def check_due(enabled, checked_at, now: datetime | None = None) -> bool:
    """Whether the once-a-day check at start should run now."""
    if enabled is not True:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        last = datetime.fromisoformat(checked_at)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    # A clock set back must not silence the check for ever.
    return last > now or now - last >= CHECK_INTERVAL


def download(version: str, folder, source: Source = GITHUB, *, size: int = 0, progress=None,
             cancelled=None, timeout: float = 30) -> tuple[Path, str]:
    """Fetch and verify one release's program; returns (file, sha256).

    The checksum file is read first, so a release without a usable row costs
    no large download. The program is kept only when its SHA-256 matches that
    row and its own footer names the same version.
    """
    if parse_version(version) is None or version.startswith("v"):
        raise UpdateError("unexpected")
    folder = Path(folder)
    stopped = cancelled or (lambda: False)
    try:
        with _open(source, source.asset(version, checksum_asset(version)), "application/octet-stream",
                   timeout) as response:
            rows = parse_checksums(_read_limited(response, _CHECKSUM_LIMIT).decode("utf-8", errors="replace"))
    except UpdateError:
        raise
    except (OSError, ValueError):
        raise UpdateError("download") from None
    expected = rows.get(EXE_ASSET)
    if expected is None:
        raise UpdateError("unexpected")
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise UpdateError("space") from None
    partial = folder / f"LuvBox-{version}.exe.part"
    target = folder / f"LuvBox-{version}.exe"
    digest = hashlib.sha256()
    received = 0
    try:
        try:
            response = _open(source, source.asset(version, EXE_ASSET), "application/octet-stream", timeout)
        except UpdateError:
            raise
        except (OSError, ValueError):
            raise UpdateError("download") from None
        with response:
            announced = response.headers.get("Content-Length", "")
            total = int(announced) if announced.isdigit() else size
            if total and not _PROGRAM_MINIMUM <= total <= _PROGRAM_LIMIT:
                raise UpdateError("unexpected")
            try:
                stream = partial.open("wb")
            except OSError:
                raise UpdateError("space") from None
            with stream:
                while True:
                    if stopped():
                        raise UpdateError("cancelled")
                    try:
                        block = response.read(256 * 1024)
                    except (OSError, ValueError):
                        raise UpdateError("download") from None
                    if not block:
                        break
                    received += len(block)
                    if received > (total or _PROGRAM_LIMIT):
                        raise UpdateError("unexpected")
                    digest.update(block)
                    try:
                        stream.write(block)
                    except OSError:
                        raise UpdateError("space") from None
                    if progress is not None:
                        progress(received, total)
                try:
                    stream.flush()
                    os.fsync(stream.fileno())
                except OSError:
                    raise UpdateError("space") from None
        if total and received != total:
            raise UpdateError("download")
        if digest.hexdigest() != expected:
            raise UpdateError("checksum")
        try:
            footer = read_footer(partial)
        except (OSError, ValueError):
            footer = None
        if footer is None or footer["version"] != version:
            raise UpdateError("program")
        try:
            os.replace(partial, target)
        except OSError:
            raise UpdateError("space") from None
    finally:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
    return target, expected


def _digest(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def replaced_name(launcher: Path, index: int = 0) -> Path:
    """Where the program an update replaces waits to be deleted (the launcher looks for these)."""
    return launcher.with_name(launcher.name + ".old" + (f".{index}" if index else ""))


def install(downloaded, launcher, expected: str) -> None:
    """Put a verified download where the launcher is; the old one is set aside.

    Windows lets a program that is running be renamed but not overwritten, so
    the old file is renamed first and removed by the next start. Either both
    renames happen or the old program is back under its name.
    """
    downloaded, launcher = Path(downloaded), Path(launcher)
    staged = launcher.with_name(launcher.name + ".new")
    try:
        if not launcher.is_file() or launcher.is_symlink():
            raise UpdateError("replace")
        try:
            # Beside the launcher, so the final step is a rename on one volume.
            with downloaded.open("rb") as source, staged.open("wb") as target:
                while block := source.read(1024 * 1024):
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
        except OSError:
            raise UpdateError("replace") from None
        if _digest(staged) != expected:
            raise UpdateError("checksum")
        aside = None
        for index in range(1000):
            candidate = replaced_name(launcher, index)
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                continue  # a scheduled run still holds that earlier one open
            aside = candidate
            break
        if aside is None:
            raise UpdateError("replace")
        try:
            os.replace(launcher, aside)
        except OSError:
            raise UpdateError("replace") from None
        try:
            os.replace(staged, launcher)
        except OSError:
            try:
                os.replace(aside, launcher)
            except OSError:
                pass
            raise UpdateError("replace") from None
        try:
            aside.unlink()
        except OSError:
            pass  # in use: the new launcher removes it when it starts
    finally:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass


def clear_downloads(folder, *, older_than: float = 0) -> None:
    """Remove fetched programs once they are installed or abandoned.

    With `older_than` (seconds) only files left untouched for that long go.
    That is for the sweep at application start, where another LuvBox may be
    in the middle of its own download: a file being written cannot be
    removed anyway, and one just finished is still waiting to be installed.
    """
    try:
        entries = list(Path(folder).iterdir())
    except OSError:
        return
    newest = time.time() - older_than if older_than > 0 else None
    for path in entries:
        if re.fullmatch(r"LuvBox-[0-9.]+\.exe(\.part)?", path.name) and path.is_file() and not path.is_symlink():
            try:
                if newest is not None and path.stat().st_mtime > newest:
                    continue
                path.unlink()
            except OSError:
                pass
