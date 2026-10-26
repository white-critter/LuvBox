"""One optional, static preview of the most recently saved local media.

The latest-request slot is bounded. Decoding never runs on the Qt thread,
contacts X, animates a GIF, plays a video, or writes the saved media.
"""

import base64
from io import BytesIO
import os
from pathlib import Path
import threading
import time

from .preview import thumbnail
from .recycling import file_signature
from .thumbcache import cloud_only


PREVIEW_SIZE = (384, 288)
_MAX_PNG_BYTES = 512 * 1024
_UNAVAILABLE = "저장한 파일의 미리보기를 표시할 수 없습니다."
_CHANGED = "파일이 이동되었거나 바뀌어 미리보기를 표시할 수 없습니다."
_RETRY_DELAYS = (0.1, 0.2, 0.4)
# Drive for desktop hardlinks a new file here while uploading. That second
# name is the sync client, not another copy the user can change independently.
_DRIVE_UPLOAD_DIR = ".tmp.driveupload"


def _volume_hard_links(path: Path) -> list[str] | None:
    """Return volume-relative hard link names, or None when they cannot be listed."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    find_first = kernel.FindFirstFileNameW
    find_first.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR)
    find_first.restype = wintypes.HANDLE
    find_next = kernel.FindNextFileNameW
    find_next.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR)
    find_next.restype = wintypes.BOOL
    close_handle = kernel.FindClose
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    invalid = wintypes.HANDLE(-1).value
    size = 32768
    length = wintypes.DWORD(size)
    buffer = ctypes.create_unicode_buffer(size)
    handle = find_first(os.fspath(path), 0, ctypes.byref(length), buffer)
    if handle in (invalid, None):
        return None
    found = []
    try:
        while len(found) < 4:
            found.append(buffer.value)
            length = wintypes.DWORD(size)
            if not find_next(handle, ctypes.byref(length), buffer):
                # ERROR_HANDLE_EOF means the name just read was the last one.
                return found if ctypes.get_last_error() == 38 else None
        return None
    finally:
        close_handle(handle)


def _foreign_hard_link(path: Path, status) -> bool:
    """Whether another name shares this file, ignoring Drive's upload link."""
    if getattr(status, "st_nlink", 0) <= 1:
        return False
    names = _volume_hard_links(path)
    if not names:
        return True
    own = os.path.normcase(os.path.splitdrive(os.path.abspath(path))[1])
    saw_own = False
    for name in names:
        if os.path.normcase(name) == own:
            saw_own = True
            continue
        upload = os.path.normcase(os.path.normpath(os.path.dirname(name)))
        root = os.path.dirname(upload)
        # A matching leaf name elsewhere on the volume is not an upload
        # alias for this saved file. Never trust a volume-wide exception.
        if (os.path.basename(upload) != _DRIVE_UPLOAD_DIR or not root.strip("\\/")
                or os.path.commonpath((own, root)) != root):
            return True
    return not saw_own


def _preview_changed(path: Path, before) -> bool:
    return (file_signature(path) != before or path.resolve() != path
            or _foreign_hard_link(path, path.lstat()))


def _decode_saved_preview(path, before, decoder):
    """Decode against the original identity; return the result and retry flag."""
    picture = None
    try:
        if _preview_changed(path, before):
            return {"preview_url": "", "preview_note": _CHANGED}, False
        status = path.lstat()
        if cloud_only(path, status=status):
            return {"preview_url": "", "preview_note": "파일이 클라우드에만 있어 미리보기를 만들지 않습니다."}, False
        result = decoder(path, PREVIEW_SIZE)
        picture = result.image
        if _preview_changed(path, before):
            return {"preview_url": "", "preview_note": _CHANGED}, False
        if picture is None:
            return {"preview_url": "", "preview_note": result.note or _UNAVAILABLE}, getattr(result, "retryable", False)
        if (picture.width < 1 or picture.height < 1 or picture.width > PREVIEW_SIZE[0]
                or picture.height > PREVIEW_SIZE[1]):
            return {"preview_url": "", "preview_note": _UNAVAILABLE}, False
        output = BytesIO()
        picture.save(output, format="PNG")
        data = output.getvalue()
        if len(data) > _MAX_PNG_BYTES:
            return {"preview_url": "", "preview_note": _UNAVAILABLE}, False
        if _preview_changed(path, before):
            return {"preview_url": "", "preview_note": _CHANGED}, False
        return {"preview_url": "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
                "preview_note": result.note}, False
    finally:
        if picture is not None:
            picture.close()


def decode_saved_preview(value, *, decoder=thumbnail):
    """Retry brief sharing locks, without accepting a replacement file."""
    try:
        path = Path(value)
        if not path.is_absolute() or path.resolve() != path:
            return {"preview_url": "", "preview_note": _CHANGED}
        before = file_signature(path)
        for delay in (0, *_RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                result, retryable = _decode_saved_preview(path, before, decoder)
            except OSError as error:
                if getattr(error, "winerror", None) not in {32, 33}:
                    raise
                result, retryable = {"preview_url": "", "preview_note": _UNAVAILABLE}, True
            if not retryable:
                return result
        return result
    except Exception:
        # Never disclose decoder exceptions or turn a preview failure into a
        # failed save. The card's post information is independent of decoding.
        return {"preview_url": "", "preview_note": _UNAVAILABLE}


class SavedPreviewLoader:
    """Lazily own one decoder thread and at most one pending request."""

    def __init__(self, deliver, *, decoder=decode_saved_preview):
        self._deliver = deliver
        self._decoder = decoder
        self._condition = threading.Condition()
        self._pending = None
        self._thread = None
        self._closed = False

    def request(self, generation, path):
        with self._condition:
            if self._closed:
                return False
            self._pending = (generation, path)
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, name="qt-saved-preview", daemon=True)
                try:
                    self._thread.start()
                except RuntimeError:
                    self._thread = None
                    self._pending = None
                    return False
            self._condition.notify()
        return True

    def cancel(self):
        with self._condition:
            self._pending = None

    def close(self):
        with self._condition:
            self._closed = True
            self._pending = None
            self._deliver = None
            self._condition.notify_all()

    def alive(self):
        with self._condition:
            return self._thread is not None and self._thread.is_alive()

    def _work(self):
        while True:
            with self._condition:
                while not self._closed and self._pending is None:
                    self._condition.wait()
                if self._closed:
                    return
                generation, path = self._pending
                self._pending = None
            try:
                result = self._decoder(path)
            except Exception:
                result = {"preview_url": "", "preview_note": _UNAVAILABLE}
            with self._condition:
                if self._closed:
                    return
                if self._pending is not None and self._pending[0] != generation:
                    continue
                deliver = self._deliver
            if deliver is not None:
                deliver(generation, result)
