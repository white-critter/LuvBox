"""Lossless local tweet source and author metadata through bundled ExifTool.

The original file is replaced only after the temporary copy's source tag has
been read back successfully. No account credentials or network calls are used.
"""

import hashlib
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from .file_access import is_read_locked


SUPPORTED_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4"})
TIMEOUT_SECONDS = 20
_SHUTDOWN_TIMEOUT = 5
# Bound decoded tool output as well as elapsed time. Compressed metadata can
# expand far beyond the input file's size, even for a one-pixel PNG.
_OUTPUT_LIMIT_BYTES = 2 * 1024 * 1024
_OUTPUT_LIMIT_LINES = 4096
_ID = re.compile(r"[0-9]{1,20}\Z", re.ASCII)
_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}\Z", re.ASCII)
_URL = re.compile(r"https://x\.com/i/web/status/[0-9]{1,20}\Z", re.ASCII)
_AUTHOR_MARKER = "luvbox:x-author:"
_AUTHOR_ID_MARKER = "luvbox:x-user-id:"
_OWNED_AUTHOR_MARKER = "luvbox:owned-author:v1:"
_OWNED_DESCRIPTION_MARKER = "luvbox:owned-description:v1:"
_TEXT_LIMIT_BYTES = 1024 * 1024
# These are persisted inside media from releases before the rename. Read and
# retire them too, while writing only the current protocol markers.
_AUTHOR_MARKERS = (_AUTHOR_MARKER, "xlikesaver:x-author:")
_AUTHOR_ID_MARKERS = (_AUTHOR_ID_MARKER, "xlikesaver:x-user-id:")
_OWNED_AUTHOR_MARKERS = (_OWNED_AUTHOR_MARKER, "xlikesaver:owned-author:v1:")
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
_FAILURE_MESSAGES = {
    "invalid": "파일 정보나 트윗 주소가 올바르지 않습니다.",
    "unavailable": "출처 기록 도구를 찾지 못했습니다. 프로그램 파일을 다시 확인해 주세요.",
    "missing": "파일이 사라졌거나 현재 위치에서 읽을 수 없습니다.",
    "permission": "파일이나 폴더에 접근할 권한이 없습니다. 권한과 읽기 전용 설정을 확인해 주세요.",
    "busy": "다른 프로그램이 파일을 사용 중입니다. 잠시 뒤 다시 기록해 주세요.",
    "changed": "기록하는 동안 파일이 바뀌어 교체를 중단했습니다.",
    "copy": "기록용 임시 복사본을 만들지 못했습니다. 폴더 권한과 남은 공간을 확인해 주세요.",
    "read": "파일 안의 기존 기록을 읽지 못했습니다. 파일 상태를 확인해 주세요.",
    "write": "파일 형식이나 기존 메타데이터 때문에 출처를 기록하지 못했습니다.",
    "replace": "검증한 파일을 원본 위치로 옮기지 못했습니다. 저장 폴더 상태와 남은 공간을 확인해 주세요.",
    "verification": "새로 기록한 출처·작성자를 확인하지 못해 원본 교체를 중단했습니다.",
    "timeout": "출처 기록 도구의 응답을 기다리다 중단했습니다. 잠시 뒤 다시 기록해 주세요.",
    "unknown": "출처 기록을 완료하지 못했습니다. 잠시 뒤 다시 기록해 주세요.",
    "unsupported": _MESSAGES["unsupported"],
}
_REPLACE_WAIT_SECONDS = 1.0
_REPLACE_RETRY_SECONDS = 0.1


def failure_message(reason) -> str:
    """Translate only fixed reason codes; never display external tool errors."""
    return _FAILURE_MESSAGES.get(reason, _FAILURE_MESSAGES["unknown"]) if isinstance(reason, str) else _FAILURE_MESSAGES["unknown"]


class _MediaChanged(ValueError):
    """The original is no longer the file whose copy was verified."""


class _MediaBusy(OSError):
    """A verified copy cannot replace a file still held by another program."""


class _TagReadError(ValueError):
    """The tool could not read the original; native probing may identify a lock."""


def _sharing_violation(error: OSError, target: Path) -> bool:
    if getattr(error, "winerror", None) in {32, 33}:
        return True
    if os.name != "nt" or getattr(error, "winerror", None) != 5:
        return False
    # Windows rename/replace reports access denied when an existing handle
    # omitted FILE_SHARE_DELETE. Read-only attributes and a non-mutating
    # DELETE-access open distinguish ordinary permission failures. Handles
    # opened only for attributes may permit this open yet still block rename.
    if getattr(target.lstat(), "st_file_attributes", 0) & 1:
        return False
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                  ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.CreateFileW(str(target), 0x10000, 1 | 2 | 4, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        return ctypes.get_last_error() in {32, 33}
    kernel.CloseHandle(handle)
    return True


def _original_unchanged(target: Path, original) -> None:
    current = target.lstat()
    if (not stat.S_ISREG(current.st_mode) or
            getattr(current, "st_reparse_tag", 0) & 0x20000000 or
            (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) !=
            (original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns)):
        raise _MediaChanged("media changed")


def _copy_for_metadata(source: Path, temporary: Path) -> None:
    """Preserve Windows named streams on the private staging copy."""
    if os.name != "nt":
        shutil.copy2(source, temporary)
        return
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    copy = kernel.CopyFileW
    copy.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int)
    copy.restype = ctypes.c_int
    # mkstemp already reserved this path. CopyFile preserves the original's
    # NTFS streams; shutil.copy2 copies only the unnamed data stream.
    if not copy(str(source), str(temporary), False):
        raise ctypes.WinError(ctypes.get_last_error())


def _preserve_creation_time(temporary: Path, original) -> None:
    """Restore Windows creation time exactly after the writer's time rounding."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    # Python 3.10/3.11 expose Windows creation time as st_ctime_ns; 3.12
    # added its explicit name. Both supported interpreter versions work.
    created_ns = getattr(original, "st_birthtime_ns", None)
    if created_ns is None:
        created_ns = original.st_ctime_ns
    created = created_ns // 100 + 116444736000000000
    birth = wintypes.FILETIME(created & 0xffffffff, created >> 32)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    set_time = kernel.SetFileTime
    set_time.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                         ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME))
    set_time.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    handle = create(str(temporary), 0x100, 7, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not set_time(handle, ctypes.byref(birth), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close(handle)


def _preserve_permissions(source: Path, temporary: Path) -> None:
    """Retain the original Windows DACL rather than inheriting the folder's."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    get = security.GetNamedSecurityInfoW
    get.argtypes = (wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                    ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer),
                    ctypes.POINTER(pointer), ctypes.POINTER(pointer))
    get.restype = wintypes.DWORD
    set_security = security.SetNamedSecurityInfoW
    set_security.argtypes = (wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                             pointer, pointer, pointer, pointer)
    set_security.restype = wintypes.DWORD
    control = security.GetSecurityDescriptorControl
    control.argtypes = (pointer, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD))
    control.restype = wintypes.BOOL
    free = kernel.LocalFree
    free.argtypes = (pointer,)
    free.restype = pointer
    descriptor, dacl = pointer(), pointer()
    result = get(str(source), 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if result:
        raise ctypes.WinError(result)
    try:
        flags, revision = wintypes.WORD(), wintypes.DWORD()
        if not control(descriptor, ctypes.byref(flags), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Preserve whether the file inherits permissions from its folder.
        protection = 0x80000000 if flags.value & 0x1000 else 0x20000000
        result = set_security(str(temporary), 1, 4 | protection, None, None, dacl, None)
        if result:
            raise ctypes.WinError(result)
    finally:
        free(descriptor)


def _replace_verified(temporary: Path, target: Path, original, deadline: float) -> None:
    """Wait briefly for a Windows sharing lock, rechecking the original each time."""
    expires = min(deadline, time.monotonic() + _REPLACE_WAIT_SECONDS)
    while True:
        _original_unchanged(target, original)
        if time.monotonic() >= deadline:
            raise TimeoutError("metadata timeout")
        try:
            os.replace(temporary, target)
            return
        except OSError as exc:
            if not _sharing_violation(exc, target):
                raise
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise _MediaBusy("media busy") from None
            time.sleep(min(_REPLACE_RETRY_SECONDS, remaining))


def _retry_original_io(target: Path, original, operation, deadline: float, cancelled=None):
    """Retry only proven sharing locks while retaining the original identity."""
    expires = min(deadline, time.monotonic() + _REPLACE_WAIT_SECONDS)
    retrying = False
    while True:
        _original_unchanged(target, original)
        if time.monotonic() >= deadline:
            raise TimeoutError("metadata timeout")
        if retrying and cancelled is not None and cancelled():
            raise _MediaBusy("media busy")
        try:
            return operation()
        except Exception as exc:
            locked = (isinstance(exc, OSError) and getattr(exc, "winerror", None) in {32, 33})
            if isinstance(exc, _TagReadError):
                # ExifTool's error text is private and cannot reliably tell a
                # temporary Windows read lock from a malformed file.
                locked = is_read_locked(target)
            if not locked:
                raise
            remaining = expires - time.monotonic()
            if remaining <= 0 or (cancelled is not None and cancelled()):
                raise _MediaBusy("media busy") from None
            retrying = True
            time.sleep(min(_REPLACE_RETRY_SECONDS, remaining))


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


class _OutputLimitExceeded(ValueError):
    """Reject excessive output without retrying the same input in a new tool."""


class _OutputBudget:
    def __init__(self):
        self.lock = threading.Lock()
        self.failed = False
        self.size = self.lines = 0

    def reset(self):
        with self.lock:
            if self.failed:
                raise _OutputLimitExceeded("metadata output limit")
            self.size = self.lines = 0

    def add(self, size):
        with self.lock:
            self.size += size
            self.lines += 1
            self.failed |= self.size > _OUTPUT_LIMIT_BYTES or self.lines > _OUTPUT_LIMIT_LINES
            if self.failed:
                raise _OutputLimitExceeded("metadata output limit")


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
        from .process_limits import MetadataJob
        try:
            self._job = MetadataJob(self._process)
        except BaseException:
            self._process.kill()
            self._process.wait(timeout=5)
            for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
                stream.close()
            raise
        # A stuck tool can stop reading while a large metadata argument list
        # fills stdin. The writer owns this buffered stream, including close,
        # so neither sending nor retiring the process can block the caller.
        self._writes = queue.Queue()
        self._writer = threading.Thread(target=self._send,
                                        args=(self._process.stdin, self._writes), daemon=True)
        self._writer.start()
        self._lines = {"stdout": queue.Queue(), "stderr": queue.Queue()}
        self._budgets = {name: _OutputBudget() for name in self._lines}
        self._readers = []
        for name in self._lines:
            reader = threading.Thread(target=self._pump,
                                      args=(getattr(self._process, name), self._lines[name], self._budgets[name],
                                            self._process.kill), daemon=True)
            self._readers.append(reader)
            reader.start()

    @staticmethod
    def _send(stream, writes):
        try:
            while True:
                command = writes.get()
                if command is None:
                    break
                payload, completed, errors = command
                try:
                    stream.write(payload)
                    stream.flush()
                except (OSError, ValueError):
                    errors.append(True)
                finally:
                    completed.set()
                if errors:
                    break
        finally:
            # Like a BufferedReader, a BufferedWriter must be closed by its
            # owning thread after the pending operation releases its mutex.
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _write(self, payload: bytes, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("metadata timeout")
        completed, errors = threading.Event(), []
        self._writes.put((payload, completed, errors))
        if not completed.wait(timeout=max(0, deadline - time.monotonic())):
            raise TimeoutError("metadata timeout")
        if errors:
            raise _SessionUnavailable("exiftool pipe failed")

    @staticmethod
    def _pump(stream, lines, budget=None, on_limit=None):
        budget = budget or _OutputBudget()
        try:
            while line := stream.readline(_OUTPUT_LIMIT_BYTES + 1):
                budget.add(len(line))
                lines.put(line.decode("utf-8", errors="replace"))
        except _OutputLimitExceeded as exc:
            lines.put(exc)
            if on_limit is not None:
                try:
                    on_limit()
                except OSError:
                    pass
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
            if any(budget.failed for budget in getattr(self, "_budgets", {}).values()):
                raise _OutputLimitExceeded("metadata output limit")
            if line is None:
                raise _SessionUnavailable("exiftool exited")
            if isinstance(line, _OutputLimitExceeded):
                raise line
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
                for budget in getattr(self, "_budgets", {}).values():
                    budget.reset()
                self._write(payload.encode("utf-8"), deadline)
                stdout, _ = self._collect("stdout", lambda line: line.rstrip("\r\n") == ready, deadline)
                stderr, marker = self._collect("stderr", lambda line: line.startswith(finished), deadline)
                status = int(marker[len(finished):].strip().rstrip("}"))
            except (TimeoutError, _OutputLimitExceeded):
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
                    deadline = time.monotonic() + _SHUTDOWN_TIMEOUT
                    self._write(b"-stay_open\nFalse\n", deadline)
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except (OSError, _SessionUnavailable, subprocess.TimeoutExpired):
                    pass
            if process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            job = getattr(self, "_job", None)
            if job is not None:
                job.close()
            self._writes.put(None)
            deadline = time.monotonic() + 0.5
            for reader in [self._writer, *self._readers]:
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
    # A standalone call needs the same bounded readers as a resident writer.
    # communicate()/capture_output would collect arbitrary expanded metadata.
    if time.monotonic() >= deadline:
        raise TimeoutError("metadata timeout")
    session = _ExifToolSession()
    try:
        return session.execute(arguments, deadline)
    finally:
        session.close()


def _read_tags(path: Path, deadline: float) -> dict:
    result = _run(["-j", "-G1", "-XMP-dc:Source", "-XMP-dc:Description",
                   "-XMP-dc:Creator", "-XMP-dc:Relation", "-XMP-xmp:Identifier",
                   "-QuickTime:Comment", str(path)], deadline)
    if result.returncode != 0:
        raise _TagReadError("metadata read failed")
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
    prefix = next((prefix for prefix in _OWNED_AUTHOR_MARKERS if marker.startswith(prefix)), None)
    if prefix is None:
        return None
    role, separator, value = marker[len(prefix):].partition(":")
    specification = _AUTHOR_ENTRY_ROLES.get(role)
    if not separator or specification is None:
        return None
    tag, field, template = specification
    if not (_HANDLE if field == "author" else _ID).fullmatch(value):
        return None
    return tag, template.format(value)


def _marker_value(marker: str, prefixes: tuple[str, ...], pattern) -> str:
    for prefix in prefixes:
        if marker.startswith(prefix):
            value = marker[len(prefix):]
            return value if pattern.fullmatch(value) else ""
    return ""


def _identity_marker(marker: str) -> bool:
    return bool(_marker_value(marker, _AUTHOR_MARKERS, _HANDLE) or
                _marker_value(marker, _AUTHOR_ID_MARKERS, _ID))


def _author_from_tags(tags: dict) -> dict[str, str]:
    identities = _values(tags.get("XMP-xmp:Identifier"))
    authors = {author for value in identities
               if (author := _marker_value(value, _AUTHOR_MARKERS, _HANDLE))}
    ids = {author_id for value in identities
           if (author_id := _marker_value(value, _AUTHOR_ID_MARKERS, _ID))}
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


def embed_source(path: Path | str, tweet_id: str, author: str = "", author_id: str = "",
                 *, text: str | None = None, cancelled=None) -> dict[str, str]:
    """Write separate post fields transactionally; None leaves descriptions intact.

    The optional cancellation predicate only skips new IO retries. The caller
    still finalizes its already saved media through the normal result path.
    """
    url = f"https://x.com/i/web/status/{tweet_id}" if isinstance(tweet_id, str) and _ID.fullmatch(tweet_id) else ""
    status = "failed"
    reason = "invalid"
    temporary = None
    text_files = []
    structured = text is not None
    text_status = "error" if structured else "not_requested"
    # An invalid handle or ID is treated as unknown: the author part is
    # skipped and the source URL is still recorded.
    identity = author_identity(author, author_id)
    has_author = bool(identity["author"] or identity["author_id"])
    author_status = "error" if has_author else "unknown"
    try:
        if not url:
            raise ValueError("invalid tweet id")
        if structured and (not isinstance(text, str) or len(text) > _TEXT_LIMIT_BYTES or "\0" in text or
                           len(text.encode("utf-8")) > _TEXT_LIMIT_BYTES):
            raise ValueError("invalid post text")
        target_input = Path(path)
        if target_input.suffix.lower() not in SUPPORTED_SUFFIXES:
            status = "unsupported"
            return {"status": status, "url": url, "message": _MESSAGES[status],
                    "reason": "unsupported", "reason_message": failure_message("unsupported"),
                    "author_status": author_status, "text_status": text_status, **identity}
        if target_input.is_symlink() or getattr(target_input, "is_junction", lambda: False)():
            raise _MediaChanged("linked media")
        reason = "missing"
        target = target_input.resolve(strict=True)
        if not target.is_file():
            raise ValueError("media missing")
        reason = "unavailable"
        if not available():
            raise ValueError("helper missing")
        reason = "read"
        original = target.stat()
        if os.name == "nt" and getattr(original, "st_file_attributes", 0) & 1:
            # copy2 also copies the read-only flag. Refuse before making a
            # temporary which neither the writer nor cleanup could change.
            raise PermissionError("read-only media")
        deadline = time.monotonic() + TIMEOUT_SECONDS
        previous_tags = _retry_original_io(
            target, original, lambda: _read_tags(target, deadline), deadline, cancelled)
        reason = "copy"
        fd, name = tempfile.mkstemp(prefix=".luvbox-source-", suffix=target.suffix, dir=target.parent)
        os.close(fd)
        temporary = Path(name)
        _retry_original_io(target, original, lambda: _copy_for_metadata(target, temporary), deadline, cancelled)
        identifiers = _values(previous_tags.get("XMP-xmp:Identifier"))
        description_markers = []
        update_description = False
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
                if tag in {"XMP-dc:Description", "ItemList:Comment"}:
                    continue
                if identity[field]:
                    content = template.format(identity[field])
                    marker = _OWNED_AUTHOR_MARKER + role + ":" + identity[field]
                    wanted_entries.setdefault(tag, []).append((content, marker))

        def stale_values(tag: str) -> set[str]:
            wanted = {content for content, _ in wanted_entries.get(tag, ())}
            return previous_owned.get(tag, set()) - wanted

        def text_argument(tag: str, old_value: str) -> tuple[str, str]:
            nonlocal text_status, update_description
            if not isinstance(old_value, str):
                old_value = ""
            ownership_tag = tag.removesuffix("-x-default")
            value = old_value
            if structured and ownership_tag == "XMP-dc:Description":
                marker = _OWNED_DESCRIPTION_MARKER + hashlib.sha256(old_value.encode("utf-8")).hexdigest()
                update_description = not old_value or marker in identifiers
                if update_description:
                    value = text
                    if value:
                        description_markers.append(_OWNED_DESCRIPTION_MARKER + hashlib.sha256(value.encode("utf-8")).hexdigest())
                text_status = "embedded" if value == text else "preserved"
            fd, name = tempfile.mkstemp(prefix=".luvbox-source-", suffix=".txt", dir=target.parent)
            text_path = Path(name)
            text_files.append(text_path)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
                output.write(value)
            return f"-{tag}<={text_path}", value

        description_arg, description_value = text_argument("XMP-dc:Description-x-default", previous_tags.get("XMP-dc:Description", ""))
        # Only the private copy is updated in place. The faster rename mode
        # would discard the named streams just copied by Windows.
        arguments = ["-overwrite_original_in_place", "-P", f"-XMP-dc:Source={url}", description_arg]
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
        if has_author or update_description:
            wanted_identifiers = []
            if has_author and identity["author"]:
                wanted_identifiers.append(_AUTHOR_MARKER + identity["author"])
            if identity["author_id"]:
                wanted_identifiers.append(_AUTHOR_ID_MARKER + identity["author_id"])
            wanted_identifiers.extend(owned_markers)
            wanted_identifiers.extend(description_markers)
            kept_identifiers = []
            for value in identifiers:
                replace_identity = has_author and (_identity_marker(value) or _owned_author_entry(value))
                replace_description = (update_description and value.startswith(_OWNED_DESCRIPTION_MARKER) and
                                       re.fullmatch(r"[0-9a-f]{64}", value[len(_OWNED_DESCRIPTION_MARKER):]))
                if (replace_identity or replace_description) and value not in wanted_identifiers:
                    arguments.append(f"-XMP-xmp:Identifier-={value}")
                else:
                    kept_identifiers.append(value)
            required_tags["XMP-xmp:Identifier"] = [*kept_identifiers]
            for value in wanted_identifiers:
                if value not in kept_identifiers:
                    arguments.append(f"-XMP-xmp:Identifier+={value}")
                    required_tags["XMP-xmp:Identifier"].append(value)
        reason = "write"
        result = _run(arguments + [str(temporary)], deadline)
        if result.returncode != 0:
            raise ValueError("metadata write failed")
        reason = "verification"
        tags = _read_tags(temporary, deadline)
        if tags.get("XMP-dc:Source") != url or tags.get("XMP-dc:Description", "") != description_value:
            raise ValueError("metadata verification failed")
        if target.suffix.lower() == ".mp4" and tags.get("ItemList:Comment", "") != comment_value:
            raise ValueError("metadata verification failed")
        for tag, expected in required_tags.items():
            actual = _values(tags.get(tag))
            # Existing third-party lists can contain many values, including
            # duplicates. Check their multiplicities once instead of scanning
            # both entire lists again for every value.
            if not Counter(actual) >= Counter(expected):
                raise ValueError("metadata verification failed")
        if has_author:
            for tag, removed in removed_tags.items():
                if any(value in _values(tags.get(tag)) for value in removed):
                    raise ValueError("author metadata verification failed")
            if _author_from_tags(tags) != identity:
                raise ValueError("author metadata verification failed")
        # Do not overwrite a file another local process changed during the copy.
        _original_unchanged(target, original)
        os.utime(temporary, ns=(original.st_atime_ns, original.st_mtime_ns))
        _preserve_creation_time(temporary, original)
        _preserve_permissions(target, temporary)
        reason = "replace"
        _replace_verified(temporary, target, original, deadline)
        temporary = None
        status = "embedded"
        reason = ""
        author_status = "embedded" if has_author else "unknown"
    except Exception as exc:
        status = "failed"
        if structured:
            text_status = "error"
        if isinstance(exc, _MediaChanged):
            reason = "changed"
        elif isinstance(exc, _MediaBusy):
            reason = "busy"
        elif isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
            reason = "timeout"
        elif isinstance(exc, FileNotFoundError):
            reason = "missing"
        elif getattr(exc, "winerror", None) in {32, 33}:
            reason = "busy"
        elif isinstance(exc, PermissionError):
            reason = "permission"
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
            "reason": reason, "reason_message": failure_message(reason) if reason else "",
            "author_status": author_status, "text_status": text_status, **identity}
