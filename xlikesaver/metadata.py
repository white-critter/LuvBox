"""Lossless local tweet source and author metadata through bundled ExifTool.

The original file is replaced only after the temporary copy's source tag has
been read back successfully. No account credentials or network calls are used.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path


SUPPORTED_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4"})
TIMEOUT_SECONDS = 20
_ID = re.compile(r"[0-9]{1,20}\Z", re.ASCII)
_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}\Z", re.ASCII)
_URL = re.compile(r"https://x\.com/i/web/status/[0-9]{1,20}\Z", re.ASCII)
_URL_CHARACTER = r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]"
_AUTHOR_MARKER = "xlikesaver:x-author:"
_AUTHOR_ID_MARKER = "xlikesaver:x-user-id:"
_OWNED_AUTHOR_MARKER = "xlikesaver:owned-author:v1:"
# Ownership is separate from identity: an existing matching value may have
# come from another program. Only values this app actually appends are owned.
_AUTHOR_ENTRY_ROLES = {
    "creator": ("XMP-dc:Creator", "author", "@{0}"),
    "profile": ("XMP-dc:Relation", "author", "https://x.com/{0}"),
    "user-url": ("XMP-dc:Relation", "author_id", "https://x.com/i/user/{0}"),
    "description-handle": ("XMP-dc:Description", "author", "X author: @{0} (https://x.com/{0})"),
    "description-id": ("XMP-dc:Description", "author_id", "X user ID: {0} (https://x.com/i/user/{0})"),
    "comment-handle": ("ItemList:Comment", "author", "X author: @{0} (https://x.com/{0})"),
    "comment-id": ("ItemList:Comment", "author_id", "X user ID: {0} (https://x.com/i/user/{0})"),
}
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


def _tool_command() -> tuple[list[str], dict]:
    """Run the bundled writer itself so a timeout also stops its file writes.

    The Windows executable is a launcher for this same bundled Perl script.
    Killing only the launcher can leave Perl holding our output pipes and
    temporary file. An ASCII script name relative to the bundle avoids Perl's
    command-line encoding limitations in Unicode installation directories.
    """
    executable = exiftool_path()
    bundle = executable.parent / "exiftool_files"
    perl = bundle / "perl.exe"
    if os.name == "nt" and perl.is_file() and (bundle / "exiftool.pl").is_file():
        environment = os.environ.copy()
        # Unix-oriented host locales (e.g. C.UTF-8) are absent on Windows Perl.
        # File names still use the explicit filename=UTF8 argument protocol.
        environment["PERL_BADLANG"] = "0"
        return [str(perl), "exiftool.pl"], {"cwd": str(bundle), "env": environment}
    return [str(executable)], {}


class _SessionUnavailable(RuntimeError):
    """The resident ExifTool cannot run a command; use a one-shot process."""


class _ExifToolSession:
    """One resident ExifTool (-stay_open) reading UTF-8 argument lines on stdin.

    Each command ends with -executeN, after which ExifTool prints {readyN} on
    stdout; -echo4 prints a marker with the command's exit status on stderr
    once processing is complete. Commands run one at a time. A timeout or any
    pipe failure kills the process, and later commands fall back to one-shot.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._counter = 0
        command, options = _tool_command()
        self._process = subprocess.Popen(
            [*command, "-config", "", "-stay_open", "True", "-@", "-",
             "-common_args", "-charset", "filename=UTF8"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **options)
        self._lines = {"stdout": queue.Queue(), "stderr": queue.Queue()}
        self._readers = []
        for name in self._lines:
            reader = threading.Thread(target=self._pump,
                                      args=(getattr(self._process, name), self._lines[name]), daemon=True)
            self._readers.append(reader)
            reader.start()

    @staticmethod
    def _pump(stream, lines):
        try:
            for line in iter(stream.readline, b""):
                lines.put(line.decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            # Another thread may be closing a failed process's pipe.
            pass
        finally:
            # A BufferedReader must be closed by its reader after readline
            # releases its internal mutex. Closing it from the shutdown thread
            # can wait forever when another process still holds the pipe.
            try:
                stream.close()
            except (OSError, ValueError):
                pass
            lines.put(None)

    def _collect(self, name: str, done, deadline: float) -> tuple[list[str], str]:
        collected = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("metadata timeout")
            try:
                line = self._lines[name].get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("metadata timeout") from None
            if line is None:
                raise _SessionUnavailable("exiftool exited")
            if done(line):
                return collected, line
            collected.append(line)

    @contextmanager
    def _command_lock(self, deadline: float):
        if not self._lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise TimeoutError("metadata timeout")
        try:
            yield
        finally:
            self._lock.release()

    def execute(self, arguments: list[str], deadline: float) -> subprocess.CompletedProcess:
        with self._command_lock(deadline):
            if self._process.poll() is not None:
                raise _SessionUnavailable("exiftool exited")
            if time.monotonic() >= deadline:
                self.close(force=True)
                raise TimeoutError("metadata timeout")
            self._counter += 1
            number = self._counter
            ready, finished = f"{{ready{number}}}", f"{{finished{number} "
            payload = "\n".join([*arguments, "-echo4", finished + "${status}}", f"-execute{number}"]) + "\n"
            try:
                self._process.stdin.write(payload.encode("utf-8"))
                self._process.stdin.flush()
                stdout, _ = self._collect("stdout", lambda line: line.rstrip("\r\n") == ready, deadline)
                stderr, marker = self._collect("stderr", lambda line: line.startswith(finished), deadline)
                status = int(marker[len(finished):].strip().rstrip("}"))
            except TimeoutError:
                # Stop a stuck command now; never leave it writing a temporary
                # copy. (TimeoutError is an OSError, so it is handled first.)
                self.close(force=True)
                raise
            except _SessionUnavailable:
                self.close(force=True)
                raise
            except (OSError, ValueError):
                self.close(force=True)
                raise _SessionUnavailable("exiftool pipe failed") from None
            return subprocess.CompletedProcess(arguments, status, "".join(stdout), "".join(stderr))

    def close(self, *, force: bool = False) -> None:
        with self._lock:
            process = self._process
            if process.poll() is None and not force:
                try:
                    process.stdin.write(b"-stay_open\nFalse\n")
                    process.stdin.flush()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            deadline = time.monotonic() + 0.5
            for reader in self._readers:
                reader.join(timeout=max(0, deadline - time.monotonic()))


_session: _ExifToolSession | None = None
_session_lock = threading.RLock()
_session_users = 0


def _borrow_session() -> _ExifToolSession | None:
    global _session_users
    with _session_lock:
        session = _session
        if session is not None:
            _session_users += 1
        return session


def _release_session(session: _ExifToolSession) -> None:
    global _session, _session_users
    close = False
    with _session_lock:
        if _session is session:
            _session_users -= 1
            if _session_users == 0:
                _session = None
                close = True
    if close:
        session.close()


@contextmanager
def persistent():
    """Reuse one ExifTool process for every metadata call inside this block.

    Starting the bundled Perl ExifTool costs about 0.2 s per call and a source
    write takes three calls. Outside this block, or if the resident process
    fails, each call starts its own process exactly as before.
    """
    global _session, _session_users
    # Overlapping callers share the process until the last context and active
    # command release it. An outer context must not close another thread's tool.
    with _session_lock:
        session = _borrow_session()
        if session is None and available():
            try:
                session = _ExifToolSession()
            except OSError:
                pass
            else:
                _session, _session_users = session, 1
    try:
        yield
    finally:
        if session is not None:
            _release_session(session)


def _run(arguments: list[str], deadline: float) -> subprocess.CompletedProcess:
    # A UTF-8 argument file avoids Windows ANSI command-line filename loss.
    # '-' reads that argument file from stdin, so no private sidecar is created.
    if any("\n" in arg or "\r" in arg or "\0" in arg for arg in arguments):
        raise ValueError("invalid argument")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("metadata timeout")
    session = _borrow_session()
    if session is not None:
        try:
            return session.execute(arguments, deadline)
        except _SessionUnavailable:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("metadata timeout") from None
        finally:
            _release_session(session)
    command, options = _tool_command()
    return subprocess.run(
        [*command, "-config", "", "-charset", "filename=UTF8", "-@", "-"],
        input="\n".join(arguments) + "\n", capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=remaining, shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **options,
    )


def _read_tags(path: Path, deadline: float) -> dict:
    result = _run(["-j", "-G1", "-XMP-dc:Source", "-XMP-dc:Description",
                   "-XMP-dc:Creator", "-XMP-dc:Relation", "-XMP-xmp:Identifier",
                   "-QuickTime:Comment", str(path)], deadline)
    if result.returncode != 0:
        raise ValueError("metadata read failed")
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("metadata read failed")
    return rows[0]


def _values(value) -> list[str]:
    """ExifTool JSON uses either a string or a list for list-valued tags."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return []


def _identity(author: str, author_id: str) -> dict[str, str]:
    if not isinstance(author, str) or not isinstance(author_id, str):
        raise ValueError("invalid author")
    supplied_author = author
    author = author.removeprefix("@")
    if supplied_author and not author:
        raise ValueError("invalid author")
    if author and not _HANDLE.fullmatch(author):
        raise ValueError("invalid author")
    if author_id and not _ID.fullmatch(author_id):
        raise ValueError("invalid author id")
    profile_url = f"https://x.com/{author}" if author else (
        f"https://x.com/i/user/{author_id}" if author_id else "")
    return {"author": author, "author_id": author_id, "profile_url": profile_url}


def author_identity(author, author_id) -> dict[str, str]:
    """Keep only a valid X handle and numeric user ID; anything else is unknown.

    Catalog text can come from an old folder name rather than a handle. Such a
    value must never stop the source URL from being recorded.
    """
    if isinstance(author_id, int) and not isinstance(author_id, bool):
        author_id = str(author_id)
    author = author.removeprefix("@") if isinstance(author, str) else ""
    author_id = author_id if isinstance(author_id, str) else ""
    return _identity(author if _HANDLE.fullmatch(author) else "",
                     author_id if _ID.fullmatch(author_id) else "")


def _owned_author_entry(marker: str) -> tuple[str, str] | None:
    """Recognize only bounded, valid ownership markers written by this app."""
    if not marker.startswith(_OWNED_AUTHOR_MARKER):
        return None
    role, separator, value = marker[len(_OWNED_AUTHOR_MARKER):].partition(":")
    specification = _AUTHOR_ENTRY_ROLES.get(role)
    if not separator or specification is None:
        return None
    tag, field, template = specification
    if not (_HANDLE if field == "author" else _ID).fullmatch(value):
        return None
    return tag, template.format(value)


def _identity_marker(marker: str) -> bool:
    return ((marker.startswith(_AUTHOR_MARKER) and
             bool(_HANDLE.fullmatch(marker[len(_AUTHOR_MARKER):]))) or
            (marker.startswith(_AUTHOR_ID_MARKER) and
             bool(_ID.fullmatch(marker[len(_AUTHOR_ID_MARKER):]))))


def _remove_owned_line(text: str, line: str) -> str:
    # An appended author line owns its preceding LF, not a newline belonging
    # to the original description. Keep all other separators byte-for-byte.
    # In particular, keep an original CR immediately before our appended LF.
    pattern = re.compile(r"\n" + re.escape(line) + r"(?=\r|\n|\Z)")
    matches = list(pattern.finditer(text))
    if not matches:
        return text
    match = matches[-1]
    return text[:match.start()] + text[match.end():]


def _author_from_tags(tags: dict) -> dict[str, str]:
    identities = _values(tags.get("XMP-xmp:Identifier"))
    authors = {value[len(_AUTHOR_MARKER):] for value in identities
               if value.startswith(_AUTHOR_MARKER) and _HANDLE.fullmatch(value[len(_AUTHOR_MARKER):])}
    ids = {value[len(_AUTHOR_ID_MARKER):] for value in identities
           if value.startswith(_AUTHOR_ID_MARKER) and _ID.fullmatch(value[len(_AUTHOR_ID_MARKER):])}
    # A photographer's unrelated Creator entry is not a tweet-author identity.
    # Conflicting markers are ambiguous; do not guess which author is current.
    if len(authors) > 1 or len(ids) > 1:
        return _identity("", "")
    author = next(iter(authors), "")
    author_id = next(iter(ids), "")
    creators, relations = _values(tags.get("XMP-dc:Creator")), _values(tags.get("XMP-dc:Relation"))
    if author and (f"@{author}" not in creators or f"https://x.com/{author}" not in relations):
        author = ""
    if author_id and f"https://x.com/i/user/{author_id}" not in relations:
        author_id = ""
    return _identity(author, author_id)


def read_author(path: Path | str) -> dict[str, str]:
    """Read the explicitly recorded tweet author; never infer it from a folder."""
    try:
        target = Path(path).resolve(strict=True)
        if not available() or not target.is_file() or target.suffix.lower() not in SUPPORTED_SUFFIXES:
            return _identity("", "")
        return _author_from_tags(_read_tags(target, time.monotonic() + TIMEOUT_SECONDS))
    except Exception:
        return _identity("", "")


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


def embed_source(path: Path | str, tweet_id: str, author: str = "", author_id: str = "") -> dict[str, str]:
    """Embed tweet source and optional author, preserving original on failure."""
    url = f"https://x.com/i/web/status/{tweet_id}" if isinstance(tweet_id, str) and _ID.fullmatch(tweet_id) else ""
    status = "failed"
    temporary = None
    text_files = []
    # An invalid handle or ID is treated as unknown: the author part is
    # skipped and the source URL is still recorded.
    identity = author_identity(author, author_id)
    has_author = bool(identity["author"] or identity["author_id"])
    author_status = "error" if has_author else "unknown"
    try:
        if not url:
            raise ValueError("invalid tweet id")
        target_input = Path(path)
        if target_input.suffix.lower() not in SUPPORTED_SUFFIXES:
            status = "unsupported"
            return {"status": status, "url": url, "message": _MESSAGES[status],
                    "author_status": author_status, **identity}
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
        identifiers = _values(previous_tags.get("XMP-xmp:Identifier"))
        previous_owned = {}
        wanted_entries = {}
        owned_markers = []
        if has_author:
            for value in identifiers:
                entry = _owned_author_entry(value)
                if entry:
                    tag, content = entry
                    previous_owned.setdefault(tag, set()).add(content)
            for role, (tag, field, template) in _AUTHOR_ENTRY_ROLES.items():
                if identity[field] and (tag != "ItemList:Comment" or target.suffix.lower() == ".mp4"):
                    content = template.format(identity[field])
                    marker = _OWNED_AUTHOR_MARKER + role + ":" + identity[field]
                    wanted_entries.setdefault(tag, []).append((content, marker))

        def stale_values(tag: str) -> set[str]:
            wanted = {content for content, _ in wanted_entries.get(tag, ())}
            return previous_owned.get(tag, set()) - wanted

        def text_argument(tag: str, old_value: str) -> tuple[str, str]:
            if not isinstance(old_value, str):
                old_value = ""
            ownership_tag = tag.removesuffix("-x-default")
            for line in sorted(stale_values(ownership_tag)):
                old_value = _remove_owned_line(old_value, line)
            value = old_value if source_token.search(old_value) else (f"{old_value}\nSource: {url}" if old_value else url)
            for line, marker in wanted_entries.get(ownership_tag, ()):
                if line not in value.splitlines():
                    value += "\n" + line
                    owned_markers.append(marker)
                elif line in previous_owned.get(ownership_tag, ()):
                    owned_markers.append(marker)
            fd, name = tempfile.mkstemp(prefix=".xlikesaver-source-", suffix=".txt", dir=target.parent)
            text_path = Path(name)
            text_files.append(text_path)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
                output.write(value)
            return f"-{tag}<={text_path}", value

        description_arg, description_value = text_argument("XMP-dc:Description-x-default", previous_tags.get("XMP-dc:Description", ""))
        arguments = ["-overwrite_original", "-P", f"-XMP-dc:Source={url}", description_arg]
        required_tags = {}
        removed_tags = {}
        if has_author:
            # Legacy identity markers are not evidence that a matching value
            # was appended by us. Unproven existing entries are preserved.
            for tag in ("XMP-dc:Creator", "XMP-dc:Relation"):
                present = _values(previous_tags.get(tag))
                removed_tags[tag] = [value for value in dict.fromkeys(present) if value in stale_values(tag)]
                for value in removed_tags[tag]:
                    arguments.append(f"-{tag}-={value}")
                old_values = [value for value in present if value not in removed_tags[tag]]
                required_tags[tag] = [*old_values]
                for value, marker in wanted_entries.get(tag, ()):
                    if value not in old_values:
                        arguments.append(f"-{tag}+={value}")
                        required_tags[tag].append(value)
                        owned_markers.append(marker)
                    elif value in previous_owned.get(tag, ()):
                        owned_markers.append(marker)
        if target.suffix.lower() == ".mp4":
            # ItemList belongs to QuickTime. Other Comment locations remain intact.
            comment_arg, comment_value = text_argument("ItemList:Comment", previous_tags.get("ItemList:Comment", ""))
            arguments.append(comment_arg)
        if has_author:
            wanted_identifiers = []
            if identity["author"]:
                wanted_identifiers.append(_AUTHOR_MARKER + identity["author"])
            if identity["author_id"]:
                wanted_identifiers.append(_AUTHOR_ID_MARKER + identity["author_id"])
            wanted_identifiers.extend(owned_markers)
            kept_identifiers = []
            for value in identifiers:
                if (_identity_marker(value) or _owned_author_entry(value)) and value not in wanted_identifiers:
                    arguments.append(f"-XMP-xmp:Identifier-={value}")
                else:
                    kept_identifiers.append(value)
            required_tags["XMP-xmp:Identifier"] = [*kept_identifiers]
            for value in wanted_identifiers:
                if value not in kept_identifiers:
                    arguments.append(f"-XMP-xmp:Identifier+={value}")
                    required_tags["XMP-xmp:Identifier"].append(value)
        result = _run(arguments + [str(temporary)], deadline)
        if result.returncode != 0:
            raise ValueError("metadata write failed")
        tags = _read_tags(temporary, deadline)
        if tags.get("XMP-dc:Source") != url or tags.get("XMP-dc:Description") != description_value:
            raise ValueError("metadata verification failed")
        if target.suffix.lower() == ".mp4" and tags.get("ItemList:Comment") != comment_value:
            raise ValueError("metadata verification failed")
        if has_author:
            for tag, expected in required_tags.items():
                actual = _values(tags.get(tag))
                if any(actual.count(value) < expected.count(value) for value in expected):
                    raise ValueError("author metadata verification failed")
            for tag, removed in removed_tags.items():
                if any(value in _values(tags.get(tag)) for value in removed):
                    raise ValueError("author metadata verification failed")
            if _author_from_tags(tags) != identity:
                raise ValueError("author metadata verification failed")
        # Do not overwrite a file another local process changed during the copy.
        current = target.stat()
        if (current.st_size, current.st_mtime_ns, current.st_ino) != (original.st_size, original.st_mtime_ns, original.st_ino):
            raise ValueError("media changed")
        os.utime(temporary, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(temporary, target)
        temporary = None
        status = "embedded"
        author_status = "embedded" if has_author else "unknown"
    except Exception:
        status = "failed"
        if has_author:
            author_status = "error"
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
    return {"status": status, "url": url, "message": _MESSAGES[status],
            "author_status": author_status, **identity}
