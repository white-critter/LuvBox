"""Lossless local source URL metadata, written through a bundled ExifTool.

The original file is replaced only after the temporary copy's source tag has
been read back successfully. No account credentials or network calls are used.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


SUPPORTED_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".mp4"})
TIMEOUT_SECONDS = 20
_ID = re.compile(r"[0-9]{1,20}\Z", re.ASCII)
_URL = re.compile(r"https://x\.com/i/web/status/[0-9]{1,20}\Z", re.ASCII)
_URL_CHARACTER = r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]"
_MESSAGES = {
    "embedded": "파일 안에 원본 트윗 주소를 기록했습니다.",
    "failed": "원본 트윗 주소를 기록하지 못했습니다. 미디어 원본은 그대로 보존했습니다.",
    "unsupported": "이 파일 형식은 원본 트윗 주소 기록을 지원하지 않습니다.",
}


def exiftool_path() -> Path:
    """Resolve only our bundle, never an executable from the user's PATH."""
    if getattr(sys, "frozen", False):
        # A one-file build extracts its data into _MEIPASS. The worker shares
        # that directory while the parent app waits for it to finish.
        root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    else:
        root = Path(__file__).resolve().parent.parent
    return root / "tools" / "exiftool" / "runtime" / "exiftool.exe"


def available() -> bool:
    executable = exiftool_path()
    return executable.is_file() and (executable.parent / "exiftool_files").is_dir()


def _run(arguments: list[str], deadline: float) -> subprocess.CompletedProcess:
    # A UTF-8 argument file avoids Windows ANSI command-line filename loss.
    # '-' reads that argument file from stdin, so no private sidecar is created.
    if any("\n" in arg or "\r" in arg or "\0" in arg for arg in arguments):
        raise ValueError("invalid argument")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("metadata timeout")
    return subprocess.run(
        [str(exiftool_path()), "-config", "", "-charset", "filename=UTF8", "-@", "-"],
        input="\n".join(arguments) + "\n", capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=remaining, shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _read_tags(path: Path, deadline: float) -> dict:
    result = _run(["-j", "-G1", "-XMP-dc:Source", "-XMP-dc:Description", "-QuickTime:Comment", str(path)], deadline)
    if result.returncode != 0:
        raise ValueError("metadata read failed")
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("metadata read failed")
    return rows[0]


def read_source(path: Path | str) -> str:
    """Return a canonical embedded X status URL, or an empty string on failure."""
    try:
        target = Path(path).resolve(strict=True)
        if not available() or not target.is_file() or target.suffix.lower() not in SUPPORTED_SUFFIXES:
            return ""
        tags = _read_tags(target, time.monotonic() + TIMEOUT_SECONDS)
        value = tags.get("XMP-dc:Source", "")
        return value if isinstance(value, str) and _URL.fullmatch(value) else ""
    except Exception:
        return ""


def embed_source(path: Path | str, tweet_id: str) -> dict[str, str]:
    """Embed a canonical source URL, preserving original data on any failure."""
    url = f"https://x.com/i/web/status/{tweet_id}" if isinstance(tweet_id, str) and _ID.fullmatch(tweet_id) else ""
    status = "failed"
    temporary = None
    text_files = []
    try:
        if not url:
            raise ValueError("invalid tweet id")
        target_input = Path(path)
        if target_input.suffix.lower() not in SUPPORTED_SUFFIXES:
            status = "unsupported"
            return {"status": status, "url": url, "message": _MESSAGES[status]}
        if target_input.is_symlink() or getattr(target_input, "is_junction", lambda: False)():
            raise ValueError("linked media")
        target = target_input.resolve(strict=True)
        if not target.is_file() or not available():
            raise ValueError("media or helper missing")
        original = target.stat()
        deadline = time.monotonic() + TIMEOUT_SECONDS
        previous_tags = _read_tags(target, deadline)
        fd, name = tempfile.mkstemp(prefix=".xlikesaver-source-", suffix=target.suffix, dir=target.parent)
        os.close(fd)
        temporary = Path(name)
        shutil.copy2(target, temporary)
        source_token = re.compile(r"(?<!" + _URL_CHARACTER + r")" + re.escape(url) + r"(?!" + _URL_CHARACTER + r")")
        def text_argument(tag: str, old_value: str) -> tuple[str, str]:
            if not isinstance(old_value, str):
                old_value = ""
            value = old_value if source_token.search(old_value) else (f"{old_value}\nSource: {url}" if old_value else url)
            fd, name = tempfile.mkstemp(prefix=".xlikesaver-source-", suffix=".txt", dir=target.parent)
            text_path = Path(name)
            text_files.append(text_path)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
                output.write(value)
            return f"-{tag}<={text_path}", value

        description_arg, description_value = text_argument("XMP-dc:Description-x-default", previous_tags.get("XMP-dc:Description", ""))
        arguments = ["-overwrite_original", "-P", f"-XMP-dc:Source={url}", description_arg]
        if target.suffix.lower() == ".mp4":
            # ItemList belongs to QuickTime. Other Comment locations remain intact.
            comment_arg, comment_value = text_argument("ItemList:Comment", previous_tags.get("ItemList:Comment", ""))
            arguments.append(comment_arg)
        result = _run(arguments + [str(temporary)], deadline)
        if result.returncode != 0:
            raise ValueError("metadata write failed")
        tags = _read_tags(temporary, deadline)
        if tags.get("XMP-dc:Source") != url or tags.get("XMP-dc:Description") != description_value:
            raise ValueError("metadata verification failed")
        if target.suffix.lower() == ".mp4" and tags.get("ItemList:Comment") != comment_value:
            raise ValueError("metadata verification failed")
        # Do not overwrite a file another local process changed during the copy.
        current = target.stat()
        if (current.st_size, current.st_mtime_ns, current.st_ino) != (original.st_size, original.st_mtime_ns, original.st_ino):
            raise ValueError("media changed")
        os.utime(temporary, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(temporary, target)
        temporary = None
        status = "embedded"
    except Exception:
        status = "failed"
    finally:
        for text_path in text_files:
            try:
                text_path.unlink(missing_ok=True)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
                Path(str(temporary) + "_original").unlink(missing_ok=True)
                Path(str(temporary) + "_exiftool_tmp").unlink(missing_ok=True)
            except OSError:
                pass
    return {"status": status, "url": url, "message": _MESSAGES[status]}
