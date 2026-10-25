"""One optional, static preview of the most recently saved local media.

The latest-request slot is bounded. Decoding never runs on the Qt thread,
contacts X, animates a GIF, plays a video, or writes the saved media.
"""

import base64
from io import BytesIO
from pathlib import Path
import threading

from .preview import thumbnail
from .recycling import file_signature
from .thumbcache import cloud_only


PREVIEW_SIZE = (384, 288)
_MAX_PNG_BYTES = 512 * 1024
_UNAVAILABLE = "저장한 파일의 미리보기를 표시할 수 없습니다."
_CHANGED = "파일이 이동되었거나 바뀌어 미리보기를 표시할 수 없습니다."


def decode_saved_preview(value, *, decoder=thumbnail):
    """Return a bounded PNG data URL from one verified local file/frame."""
    picture = None
    try:
        path = Path(value)
        if not path.is_absolute() or path.resolve() != path:
            return {"preview_url": "", "preview_note": _CHANGED}
        before = file_signature(path)
        status = path.lstat()
        if status.st_nlink > 1:
            return {"preview_url": "", "preview_note": _CHANGED}
        if cloud_only(path, status=status):
            return {"preview_url": "", "preview_note": "파일이 클라우드에만 있어 미리보기를 만들지 않습니다."}
        result = decoder(path, PREVIEW_SIZE)
        picture = result.image
        if file_signature(path) != before or path.resolve() != path:
            return {"preview_url": "", "preview_note": _CHANGED}
        if picture is None:
            return {"preview_url": "", "preview_note": result.note or _UNAVAILABLE}
        if (picture.width < 1 or picture.height < 1 or picture.width > PREVIEW_SIZE[0]
                or picture.height > PREVIEW_SIZE[1]):
            return {"preview_url": "", "preview_note": _UNAVAILABLE}
        output = BytesIO()
        picture.save(output, format="PNG")
        data = output.getvalue()
        if len(data) > _MAX_PNG_BYTES:
            return {"preview_url": "", "preview_note": _UNAVAILABLE}
        if file_signature(path) != before or path.resolve() != path:
            return {"preview_url": "", "preview_note": _CHANGED}
        return {"preview_url": "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
                "preview_note": result.note}
    except Exception:
        # A preview failure must never replace the save's status or disclose a
        # decoder exception containing file contents, paths or credentials.
        return {"preview_url": "", "preview_note": _UNAVAILABLE}
    finally:
        if picture is not None:
            picture.close()


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
