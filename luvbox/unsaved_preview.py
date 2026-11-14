"""Small pictures for media a save could not store, or was told not to.

A file that failed, or a video over the size limit, never reached the disk, so
the unsaved list had nothing to show for it. The same goes for media the
person excluded: the save passes them by. While the save still has the
post in hand it can fetch the smallest rendition of the photo, or a video's
poster image, from X's image host and keep it in this PC's preview cache.

This is the only preview that makes a request. It is bounded on purpose: one
host, one small image per media, a byte limit, a per-run count, and it stops
after a few failures in a row. Nothing here can fail or delay a save.
"""

from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlsplit


HOST = "pbs.twimg.com"
MAX_BYTES = 256 * 1024
# One full check may meet every excluded media of a library (over a thousand in a real one).
MAX_PER_RUN = 1500
_FAILURES_IN_A_ROW = 3
_FORMATS = {"jpg": "jpg", "jpeg": "jpg", "png": "png", "webp": "webp"}
_TYPES = {"image/jpeg", "image/png", "image/webp"}


def preview_source(url, media) -> str:
    """The small rendition to fetch for this media, or '' when there is none to ask for.

    A photo is its own picture at ``name=small``. A video or GIF uses the
    poster image the post carries (``_luvbox_poster``). Only X's image host
    over HTTPS is ever returned.
    """
    if not isinstance(media, dict):
        return ""
    candidate = url if media.get("type", "photo") == "photo" else media.get("_luvbox_poster")
    if not isinstance(candidate, str) or len(candidate) > 2048:
        return ""
    try:
        parts = urlsplit(candidate)
        if parts.scheme != "https" or parts.hostname != HOST or parts.port not in (None, 443):
            return ""
        if parts.username or parts.password:
            return ""
        path = parts.path
        if not path.startswith("/") or ".." in path or any(ord(char) < 33 or ord(char) > 126 for char in path):
            return ""
        base, dot, extension = path.rpartition(".")
        if dot and "/" not in extension and extension.lower() in _FORMATS:
            kind = _FORMATS[extension.lower()]
        else:
            base = path
            chosen = parse_qs(parts.query).get("format", [""])[0].lower()
            if chosen not in _FORMATS:
                return ""
            kind = _FORMATS[chosen]
    except ValueError:
        return ""
    return f"https://{HOST}{base}?format={kind}&name=small"


def fetch(session, source, *, timeout=(5.0, 8.0)):
    """Bytes of one small image, or None. Refuses redirects, other types and large bodies."""
    response = session.get(source, timeout=timeout, stream=True, allow_redirects=False,
                           headers={"Accept": "image/jpeg,image/png,image/webp"})
    try:
        if response.status_code != 200:
            return None
        kind = str(response.headers.get("Content-Type", "")).split(";")[0].strip().lower()
        if kind not in _TYPES:
            return None
        length = response.headers.get("Content-Length")
        if length is not None and (not str(length).isdigit() or int(length) > MAX_BYTES):
            return None
        data = bytearray()
        for chunk in response.iter_content(16384):
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                return None
        return bytes(data) or None
    finally:
        response.close()


class UnsavedPreviews:
    """Fill missing previews of unsaved media through one save's own session."""

    def __init__(self, recorder, *, limit=MAX_PER_RUN, folder=None):
        self._recorder = recorder
        self._limit = limit
        self._folder = folder
        self.requested = 0
        self.stored = 0
        self._failures = 0

    def keep(self, session, url, media) -> bool:
        """True when this media has a preview afterwards. Never raises."""
        try:
            from .thumbcache import identity
            # A media the cache could not name is not worth a request.
            key = identity(media.get("tweet_id"), media.get("num"), media.get("type", "photo"))
            if self._recorder.has(key):
                return True
            if self.requested >= self._limit or self._failures >= _FAILURES_IN_A_ROW:
                return False
            source = preview_source(url, media)
            if not source:
                return False
            self.requested += 1
            data = fetch(session, source)
            if not data:
                self._failures += 1
                return False
            if not self._store(key, data):
                self._failures += 1
                return False
            self._failures = 0
            self.stored += 1
            return True
        except ValueError:
            return False
        except Exception:
            self._failures += 1
            return False

    def _store(self, key, data) -> bool:
        from . import thumbcache

        folder = Path(self._folder) if self._folder is not None else thumbcache.directory()
        folder.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(prefix="unsaved-preview-", suffix=".tmp", dir=folder, delete=False)
        path = Path(handle.name)
        try:
            with handle:
                handle.write(data)
            # The decoder decides the format from the bytes; the name only says "a still image".
            return bool(self._recorder.add(key, path, suffix=".jpg"))
        finally:
            path.unlink(missing_ok=True)
