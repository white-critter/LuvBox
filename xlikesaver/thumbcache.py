"""Small previews of saved files, kept on this PC for files that later disappear.

A file deleted or moved outside the app keeps its catalog record; a 150x112
preview kept here still shows what it was. Previews live in this PC's
application data, never in the (possibly synced) media folder, and are made
only from files whose data is already on this PC: reading a cloud-only
placeholder would download it. Each preview is about 1-4 KB; above
LIMIT_BYTES the oldest previews are dropped.
"""

from __future__ import annotations

import io
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


SIZE = (150, 112)
LIMIT_BYTES = 300 * 1024 * 1024
_ACCOUNT = re.compile(r"[a-z0-9_]{1,15}\Z")
_TWEET = re.compile(r"[0-9]{1,20}\Z")
_TYPES = frozenset({"photo", "video", "animated_gif"})
# FILE_ATTRIBUTE_OFFLINE, _RECALL_ON_OPEN and _RECALL_ON_DATA_ACCESS: the
# file's data is in the cloud (OneDrive "online-only" and similar).
_CLOUD_ATTRIBUTES = 0x1000 | 0x40000 | 0x400000
_PRUNE_EVERY = 200

# Identities already stored, per cache file: the gallery need not ask again.
_known: dict[str, set] = {}
_known_files: dict[str, tuple] = {}
_prune_writes: dict[str, int] = {}
_known_lock = threading.Lock()


def directory() -> Path:
    from .protection import state_directory
    return state_directory().parent / "thumbnails"


def identity(tweet_id, num, media_type) -> tuple[str, int, str]:
    tweet_id = str(tweet_id)
    if (not _TWEET.fullmatch(tweet_id) or isinstance(num, bool) or not isinstance(num, int)
            or not 1 <= num <= 10000 or media_type not in _TYPES):
        raise ValueError("invalid media identity")
    return tweet_id, num, media_type


def cloud_only(path: Path | str, *, status=None) -> bool:
    """Whether reading this file would first download it from a cloud service."""
    try:
        attributes = getattr(status if status is not None else Path(path).stat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & _CLOUD_ATTRIBUTES)


def _safe_path(path: Path) -> None:
    """Reject redirected cache directories, databases and SQLite sidecars."""
    for current in (path, *path.parents):
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise OSError("linked preview cache")
    for current in (path, *(path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal", ".epoch"))):
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise OSError("linked preview cache")
        try:
            if current.stat().st_nlink > 1:
                raise OSError("linked preview cache")
        except FileNotFoundError:
            pass


def _file_identity(path: Path) -> tuple | None:
    try:
        status = path.stat()
        try:
            epoch = path.with_name(path.name + ".epoch").stat()
            changed = (epoch.st_dev, epoch.st_ino, epoch.st_mtime_ns)
        except FileNotFoundError:
            changed = None
        return status.st_dev, status.st_ino, changed
    except OSError:
        return None


def _mark_pruned(path: Path) -> None:
    """Invalidate known rows in other app/worker processes after eviction."""
    _safe_path(path)
    descriptor, filename = tempfile.mkstemp(prefix=".preview-", suffix=".epoch", dir=path.parent)
    staged = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(uuid.uuid4().bytes)
        os.replace(staged, path.with_name(path.name + ".epoch"))
    finally:
        staged.unlink(missing_ok=True)


def encode(image) -> bytes:
    from PIL import Image

    picture = image.copy()
    picture.thumbnail(SIZE, Image.Resampling.LANCZOS)
    if picture.mode not in {"RGB", "RGBA"}:
        picture = picture.convert("RGBA" if "A" in picture.getbands() else "RGB")
    buffer = io.BytesIO()
    try:
        picture.save(buffer, "WEBP", quality=70, method=4)
    except (OSError, KeyError, ValueError):
        buffer = io.BytesIO()
        picture.convert("RGB").save(buffer, "JPEG", quality=75)
    return buffer.getvalue()


def decode(data: bytes):
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as picture:
            if picture.width > SIZE[0] * 2 or picture.height > SIZE[1] * 2:
                return None
            picture.load()
            return picture.convert("RGBA")
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def make(path: Path | str):
    """A preview image of a local media file, or None (never downloads)."""
    if cloud_only(path):
        return None
    from .preview import thumbnail
    return thumbnail(Path(path), SIZE).image


class Cache:
    """One account's previews. Use each instance from a single thread."""

    def __init__(self, account: str, *, create: bool = True):
        if not isinstance(account, str) or not _ACCOUNT.fullmatch(account):
            raise ValueError("invalid account")
        self.account = account
        self.path = directory() / (account + ".sqlite3")
        self._db = None
        _safe_path(self.path)
        if not create and not self.path.is_file():
            return
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        _safe_path(self.path)
        db = (sqlite3.connect(self.path, timeout=2) if create else
              sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=2))
        try:
            db.execute("PRAGMA busy_timeout=2000")
            if create:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE IF NOT EXISTS previews (tweet_id TEXT NOT NULL, num INTEGER NOT NULL, "
                           "media_type TEXT NOT NULL, image BLOB NOT NULL, saved_at TEXT NOT NULL, "
                           "PRIMARY KEY(tweet_id,num,media_type))")
        except BaseException:
            db.close()
            raise
        self._db = db

    def stored(self, identities) -> set:
        """Which of these identities already have a preview."""
        if self._db is None:
            return set()
        found = {key for key in identities if self._db.execute(
            "SELECT 1 FROM previews WHERE tweet_id=? AND num=? AND media_type=?", key).fetchone()}
        _note(self.path, found)
        return found

    def get(self, key) -> bytes | None:
        if self._db is None:
            return None
        row = self._db.execute("SELECT image FROM previews WHERE tweet_id=? AND num=? AND media_type=?",
                               identity(*key)).fetchone()
        return bytes(row[0]) if row else None

    def put(self, key, image) -> None:
        if self._db is None:
            raise OSError("preview cache unavailable")
        key = identity(*key)
        data = encode(image)
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO previews(tweet_id,num,media_type,image,saved_at) VALUES (?,?,?,?,?)",
                             (*key, data, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        _note(self.path, {key})
        # Gallery callbacks open one cache per image, and short save runs also
        # reopen it. Share the counter by file, checking an existing cache on
        # the first write in this process as well as every later batch.
        with _known_lock:
            path = str(self.path)
            writes = _prune_writes.get(path, 0) + 1
            _prune_writes[path] = writes
        if writes == 1 or writes % _PRUNE_EVERY == 0:
            self.prune()

    def total_bytes(self) -> int:
        if self._db is None:
            return 0
        return int(self._db.execute("SELECT COALESCE(SUM(length(image)),0) FROM previews").fetchone()[0])

    def prune(self, limit: int | None = None) -> int:
        """Drop the oldest previews once the cache is larger than ``limit``."""
        if limit is None:
            limit = LIMIT_BYTES
        total = self.total_bytes()
        if total <= limit:
            return 0
        removed = 0
        target = int(limit * 0.9)
        with self._db:
            for rowid, size in self._db.execute(
                    "SELECT rowid,length(image) FROM previews ORDER BY saved_at,rowid").fetchall():
                if total <= target:
                    break
                self._db.execute("DELETE FROM previews WHERE rowid=?", (rowid,))
                total -= size
                removed += 1
        _mark_pruned(self.path)
        with _known_lock:
            _known.pop(str(self.path), None)
        return removed

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None


def _note(path: Path, keys) -> None:
    if keys:
        stamp = _file_identity(path)
        with _known_lock:
            name = str(path)
            if _known_files.get(name) != stamp:
                _known.pop(name, None)
                _prune_writes.pop(name, None)
                _known_files[name] = stamp
            _known.setdefault(name, set()).update(keys)


def known(account: str, key) -> bool:
    path = directory() / (account + ".sqlite3")
    stamp = _file_identity(path)
    with _known_lock:
        name = str(path)
        if stamp is None or _known_files.get(name) != stamp:
            _known.pop(name, None)
            _known_files.pop(name, None)
            _prune_writes.pop(name, None)
            return False
        return key in _known.get(name, ())


def remember(account: str, key, image) -> None:
    """Keep a preview the library has just decoded; never raises."""
    try:
        key = identity(*key)
        if image is None or known(account, key):
            return
        with closing(Cache(account)) as cache:
            if not cache.stored([key]):
                cache.put(key, image)
    except Exception:
        pass


def recall(account: str, key):
    """The kept preview image for one media, or None; never raises."""
    try:
        with closing(Cache(account, create=False)) as cache:
            data = cache.get(key)
        return decode(data) if data else None
    except Exception:
        return None


class Recorder:
    """Keep previews of files a save run has just written (worker thread)."""

    def __init__(self, account: str):
        self.account = account
        self._cache = None
        self._disabled = False

    def add(self, key, path: Path | str) -> None:
        if self._disabled:
            return
        try:
            key = identity(*key)
            image = make(path)
            if image is None:
                return
            if self._cache is None:
                self._cache = Cache(self.account)
            self._cache.put(key, image)
        except (OSError, sqlite3.Error):
            # The cache is optional; never slow a save with repeated failures.
            self._disabled = True
        except Exception:
            pass

    def close(self) -> None:
        if self._cache is not None:
            try:
                self._cache.close()
            except Exception:
                pass
            self._cache = None
