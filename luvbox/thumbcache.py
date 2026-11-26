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
import struct
import tempfile
import threading
import time
import uuid
from array import array
from collections import OrderedDict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .file_mutex import replace_file


SIZE = (150, 112)
LIMIT_BYTES = 300 * 1024 * 1024
_ACCOUNT = re.compile(r"[a-z0-9_]{1,15}\Z")
_TWEET = re.compile(r"[0-9]{1,20}\Z")
_TYPES = frozenset({"photo", "video", "animated_gif"})
# FILE_ATTRIBUTE_OFFLINE, _RECALL_ON_OPEN and _RECALL_ON_DATA_ACCESS: the
# file's data is in the cloud (OneDrive "online-only" and similar).
_CLOUD_ATTRIBUTES = 0x1000 | 0x40000 | 0x400000
_PRUNE_EVERY = 200
_RECORDER_PENDING = 32
_RECORDER_RETRY_SECONDS = 1.0
# Encoded 150x112 previews are small. Reject malformed SQLite values before
# fetching them: bytes(integer) would allocate that many zero bytes, and a
# damaged BLOB should not be copied into memory just to inspect its header.
_MAX_IMAGE_BYTES = 256 * 1024
_VALID_IMAGE = "typeof(image)='blob' AND length(image) BETWEEN 1 AND ?"

# Identities already stored, per cache file: the gallery need not ask again.
# Bound the set so a long session does not retain every identity it has seen.
# Evicted keys are asked of the database again; stored previews are not lost.
_KNOWN_LIMIT = 8192
_USER_VERSION = 1
_known: dict[str, OrderedDict] = {}
_known_files: dict[str, tuple] = {}
_prune_writes: dict[str, int] = {}
_deadband_headroom: dict[str, tuple[int, int, tuple | None]] = {}
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
    """Reject redirected cache directories, databases and SQLite sidecars.

    Folders up to the app's own data folder are checked. Above it, a user may
    have moved AppData itself with a junction; records work there, so do
    previews.
    """
    boundary = directory().parent
    for current in (path, *path.parents):
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise OSError("linked preview cache")
        if current == boundary:
            break
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


def _db_stamp(path: Path) -> tuple | None:
    try:
        main = path.stat()
        try:
            wal = path.with_name(path.name + "-wal").stat()
            wal_stamp = (wal.st_size, wal.st_mtime_ns)
        except FileNotFoundError:
            wal_stamp = None
        try:
            epoch = path.with_name(path.name + ".epoch").stat()
            epoch_stamp = (epoch.st_dev, epoch.st_ino, epoch.st_mtime_ns)
        except FileNotFoundError:
            epoch_stamp = None
        return (main.st_dev, main.st_ino, main.st_size, main.st_mtime_ns, wal_stamp, epoch_stamp)
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
        replace_file(staged, path.with_name(path.name + ".epoch"))
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
    # preview imports cloud_only from this module; keep this import at call
    # time so either public preview path can be the first one used.
    from .preview import _prepare_decoders

    try:
        _prepare_decoders(False)
        with Image.open(io.BytesIO(data), formats=("JPEG", "WEBP")) as picture:
            if picture.width > SIZE[0] * 2 or picture.height > SIZE[1] * 2:
                return None
            picture.load()
            return picture.convert("RGBA")
    except (OSError, ValueError, SyntaxError, TypeError, EOFError, struct.error,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None


def make(path: Path | str, *, suffix: str | None = None):
    """A preview image of a local media file, or None (never downloads)."""
    if cloud_only(path):
        return None
    from .preview import thumbnail
    return thumbnail(Path(path), SIZE, suffix=suffix).image


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
            db.execute("PRAGMA synchronous=NORMAL")
            if create:
                version = int(db.execute("PRAGMA user_version").fetchone()[0])
                if version != _USER_VERSION:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("CREATE TABLE IF NOT EXISTS previews (tweet_id TEXT NOT NULL, num INTEGER NOT NULL, "
                               "media_type TEXT NOT NULL, image BLOB NOT NULL, saved_at TEXT NOT NULL, "
                               "PRIMARY KEY(tweet_id,num,media_type))")
                    db.execute("CREATE INDEX IF NOT EXISTS previews_saved_at ON previews(saved_at)")
                    db.execute(f"PRAGMA user_version={_USER_VERSION}")
        except BaseException:
            db.close()
            raise
        self._db = db

    def stored(self, identities) -> set:
        """Which of these identities already have a preview."""
        if self._db is None:
            return set()
        items = [tuple(key) for key in identities]
        if not items:
            return set()
        if len(items) == 1:
            key = items[0]
            row = self._db.execute(
                "SELECT 1 FROM previews WHERE tweet_id=? AND num=? AND media_type=? AND " + _VALID_IMAGE,
                (*key, _MAX_IMAGE_BYTES)).fetchone()
            found = {key} if row else set()
        else:
            found = set()
            for start in range(0, len(items), 200):
                chunk = items[start:start + 200]
                clause = ",".join("(?,?,?)" for _ in chunk)
                params = [part for key in chunk for part in key]
                params.append(_MAX_IMAGE_BYTES)
                rows = self._db.execute(
                    f"SELECT tweet_id, num, media_type FROM previews "
                    f"WHERE (tweet_id, num, media_type) IN (VALUES {clause}) AND " + _VALID_IMAGE,
                    params,
                ).fetchall()
                for row in rows:
                    found.add((row[0], int(row[1]), row[2]))
        _note(self.path, found)
        return found

    def get(self, key) -> bytes | None:
        if self._db is None:
            return None
        row = self._db.execute("SELECT image FROM previews WHERE tweet_id=? AND num=? AND media_type=? AND "
                               + _VALID_IMAGE, (*identity(*key), _MAX_IMAGE_BYTES)).fetchone()
        if not row:
            return None
        return row[0]

    def get_many(self, keys) -> dict[tuple, bytes]:
        if self._db is None:
            return {}
        valid = []
        for key in keys:
            try:
                valid.append(identity(*key))
            except (TypeError, ValueError):
                continue
        if not valid:
            return {}
        found = {}
        for start in range(0, len(valid), 200):
            chunk = valid[start:start + 200]
            clause = ",".join("(?,?,?)" for _ in chunk)
            params = [part for key in chunk for part in key]
            params.append(_MAX_IMAGE_BYTES)
            rows = self._db.execute(
                f"SELECT tweet_id, num, media_type, image FROM previews "
                f"WHERE (tweet_id, num, media_type) IN (VALUES {clause}) AND " + _VALID_IMAGE,
                params,
            ).fetchall()
            for row in rows:
                found[(row[0], int(row[1]), row[2])] = row[3]
        return found

    def put(self, key, image) -> None:
        if self._db is None:
            raise OSError("preview cache unavailable")
        key = identity(*key)
        data = encode(image)
        stamp_before = _db_stamp(self.path)
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO previews(tweet_id,num,media_type,image,saved_at) VALUES (?,?,?,?,?)",
                             (*key, data, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        _note(self.path, {key})
        self._auto_prune(len(data), 1, stamp_before)

    def put_many(self, items) -> None:
        if self._db is None:
            raise OSError("preview cache unavailable")
        encoded = []
        keys = []
        stamp_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
        total_added = 0
        for raw_key, image in items:
            key = identity(*raw_key)
            data = encode(image)
            encoded.append((*key, data, stamp_time))
            keys.append(key)
            total_added += len(data)
        if not encoded:
            return
        stamp_before = _db_stamp(self.path)
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO previews(tweet_id,num,media_type,image,saved_at) VALUES (?,?,?,?,?)",
                encoded,
            )
        _note(self.path, keys)
        self._auto_prune(total_added, len(encoded), stamp_before)

    def _auto_prune(self, added_bytes: int, batch_count: int, stamp_before) -> None:
        # Gallery callbacks open one cache per image, and short save runs also
        # reopen it. Share the counter by file, checking an existing cache on
        # the first write in this process as well as every later batch.
        limit = LIMIT_BYTES
        path = str(self.path)
        stamp_after = _db_stamp(self.path)
        with _known_lock:
            prev_writes = _prune_writes.get(path, 0)
            writes = prev_writes + batch_count
            _prune_writes[path] = writes
            entry = _deadband_headroom.get(path)
            if entry is not None and entry[0] == limit and entry[2] == stamp_before:
                remaining = entry[1] - added_bytes
                _deadband_headroom[path] = (limit, remaining, stamp_after)
            else:
                remaining = None
                _deadband_headroom.pop(path, None)
        if not (prev_writes == 0 or (prev_writes // _PRUNE_EVERY != writes // _PRUNE_EVERY)):
            return
        if prev_writes > 0 and remaining is not None and remaining > 0:
            return
        self._prune_internal(limit, update_headroom=True)

    def total_bytes(self) -> int:
        if self._db is None:
            return 0
        return int(self._db.execute("SELECT COALESCE(SUM(length(image)),0) FROM previews").fetchone()[0])

    def _prune_internal(self, limit: int, *, update_headroom: bool) -> int:
        if self._db is None:
            return 0
        page_size = int(self._db.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(self._db.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(self._db.execute("PRAGMA freelist_count").fetchone()[0])
        active_bytes = max(0, page_count - freelist_count) * page_size
        if active_bytes <= limit:
            if update_headroom:
                stamp = _db_stamp(self.path)
                with _known_lock:
                    _deadband_headroom[str(self.path)] = (limit, limit - active_bytes, stamp)
            return 0
        total = self.total_bytes()
        if total <= limit:
            if update_headroom:
                stamp = _db_stamp(self.path)
                with _known_lock:
                    _deadband_headroom[str(self.path)] = (limit, limit - total, stamp)
            return 0
        target = int(limit * 0.9)
        # Read only until enough bytes can be reclaimed, retaining compact
        # row IDs rather than every (rowid, size) tuple in a large cache.
        # Finish the SELECT before deleting, even if a future index changes
        # its query plan from a materialized sort to a live table scan.
        pending = array("q")
        remaining_total = total
        with closing(self._db.execute(
                "SELECT rowid,length(image) FROM previews ORDER BY saved_at,rowid")) as rows:
            for rowid, size in rows:
                if remaining_total <= target:
                    break
                pending.append(rowid)
                remaining_total -= size
        with self._db:
            self._db.executemany("DELETE FROM previews WHERE rowid=?", ((rowid,) for rowid in pending))
        _mark_pruned(self.path)
        stamp = _file_identity(self.path)
        db_stamp = _db_stamp(self.path)
        with _known_lock:
            name = str(self.path)
            _known.pop(name, None)
            if stamp is not None:
                _known_files[name] = stamp
            if update_headroom:
                _deadband_headroom[name] = (limit, max(0, limit - remaining_total), db_stamp)
            else:
                _deadband_headroom.pop(name, None)
        return len(pending)

    def prune(self, limit: int | None = None) -> int:
        """Drop the oldest previews once the cache is larger than ``limit``."""
        if limit is None:
            limit = LIMIT_BYTES
        with _known_lock:
            _deadband_headroom.pop(str(self.path), None)
        return self._prune_internal(limit, update_headroom=False)

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None


_DEFAULT_CACHE_PUT = Cache.put


def _note(path: Path, keys) -> None:
    if keys:
        stamp = _file_identity(path)
        with _known_lock:
            name = str(path)
            if _known_files.get(name) != stamp:
                _known.pop(name, None)
                _prune_writes.pop(name, None)
                _deadband_headroom.pop(name, None)
                _known_files[name] = stamp
            bucket = _known.setdefault(name, OrderedDict())
            for key in keys:
                bucket[key] = None
                bucket.move_to_end(key)
            while len(bucket) > _KNOWN_LIMIT:
                bucket.popitem(last=False)


def known(account: str, key) -> bool:
    path = directory() / (account + ".sqlite3")
    stamp = _file_identity(path)
    with _known_lock:
        name = str(path)
        if stamp is None or _known_files.get(name) != stamp:
            _known.pop(name, None)
            _known_files.pop(name, None)
            _prune_writes.pop(name, None)
            _deadband_headroom.pop(name, None)
            return False
        bucket = _known.get(name)
        if not bucket or key not in bucket:
            return False
        bucket.move_to_end(key)
        return True


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


def kept(account: str, identities) -> set:
    """Which of these media have a preview kept on this PC.

    Only the local thumbnail cache is asked, and no picture is read: a list of any
    length learns which tiles can show one, and ``recall`` reads a picture when its
    tile is shown. Never raises.
    """
    try:
        if not isinstance(account, str) or not _ACCOUNT.fullmatch(account):
            return set()
        if not (directory() / (account + ".sqlite3")).is_file():
            return set()
        keys = []
        for key in identities:
            try:
                keys.append(identity(*key))
            except (TypeError, ValueError):
                continue
        if not keys:
            return set()
        with closing(Cache(account, create=False)) as cache:
            return cache.stored(keys)
    except Exception:
        return set()


class Recorder:
    """Keep downloaded/encountered previews, retrying a bounded failed batch."""

    def __init__(self, account: str):
        self.account = account
        self._cache = None
        self._pending = OrderedDict()
        self._retry_at = 0.0
        self._dropped = 0
        self._closed = False

    @property
    def failed(self) -> int:
        """Previews whose cache write still failed, excluding unreadable media."""
        return self._dropped + len(self._pending)

    def _cache_failed(self):
        if self._cache is not None:
            try:
                self._cache.close()
            except Exception:
                pass
            self._cache = None
        self._retry_at = time.monotonic() + _RECORDER_RETRY_SECONDS

    def _open_cache(self):
        if self._cache is None:
            self._cache = Cache(self.account)
        return self._cache

    def _flush(self, *, force=False):
        if not self._pending:
            return True
        if not force and time.monotonic() < self._retry_at:
            return False
        try:
            cache = self._open_cache()
            if type(cache).put is _DEFAULT_CACHE_PUT:
                cache.put_many(list(self._pending.items()))
                while self._pending:
                    _, image = self._pending.popitem(last=False)
                    image.close()
            else:
                for key in tuple(self._pending):
                    image = self._pending[key]
                    cache.put(key, image)
                    self._pending.pop(key).close()
        except Exception:
            # A busy cache must not disable every later preview in this run.
            # Back off, then retry the small batch on a later file or close.
            self._cache_failed()
            return False
        self._retry_at = 0.0
        return True

    def has(self, key) -> bool:
        """Whether a preview for this media is already kept or waiting to be written. Never raises."""
        if self._closed:
            return False
        try:
            key = identity(*key)
        except (TypeError, ValueError):
            return False
        try:
            if key in self._pending or known(self.account, key):
                return True
            if time.monotonic() < self._retry_at:
                return False
            return bool(self._open_cache().stored([key]))
        except Exception:
            self._cache_failed()
            return False

    def _should_skip_missing(self, key, *, path=None) -> bool | None:
        """Return True if already cached, False if cache probe succeeded (miss), None to skip."""
        if known(self.account, key):
            return True
        if time.monotonic() < self._retry_at or (path is not None and cloud_only(path)):
            return None
        try:
            return bool(self._open_cache().stored([key]))
        except Exception:
            self._cache_failed()
            return None

    def _enqueue(self, key, image) -> bool:
        if image is None:
            return False
        previous = self._pending.pop(key, None)
        if previous is not None:
            previous.close()
        self._pending[key] = image
        while len(self._pending) > _RECORDER_PENDING:
            _, dropped = self._pending.popitem(last=False)
            dropped.close()
            self._dropped += 1
        self._flush()
        return key not in self._pending

    def add(self, key, path: Path | str, *, suffix: str | None = None, only_missing=False) -> bool:
        if self._closed:
            return False
        try:
            key = identity(*key)
            if only_missing:
                status = self._should_skip_missing(key, path=path)
                if status is not False:
                    return bool(status)
            return self._enqueue(key, make(path, suffix=suffix))
        except Exception:
            return False

    def add_bytes(self, key, data: bytes, *, only_missing=False) -> bool:
        if self._closed or not isinstance(data, (bytes, bytearray, memoryview)):
            return False
        try:
            key = identity(*key)
            if only_missing:
                status = self._should_skip_missing(key)
                if status is not False:
                    return bool(status)
            from .preview import thumbnail_bytes
            image = thumbnail_bytes(bytes(data), SIZE)
            return self._enqueue(key, image)
        except Exception:
            return False

    def close(self) -> None:
        if self._closed:
            return
        self._flush(force=True)
        self._closed = True
        self._dropped += len(self._pending)
        while self._pending:
            _, image = self._pending.popitem(last=False)
            image.close()
        if self._cache is not None:
            try:
                self._cache.close()
            except Exception:
                pass
            self._cache = None
