"""Qt gallery model and thumbnail provider over a caller-supplied catalog reader."""

from collections import OrderedDict
import heapq
import math
from pathlib import Path
import re
from threading import Condition, Thread
import time
from urllib.parse import quote, unquote

from PySide6.QtCore import QAbstractListModel, QModelIndex, Property, Qt, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickImageProvider

from .preview import thumbnail
from .thumbcache import cloud_only


_KEY = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_READ_ERROR = "사진 목록을 불러오지 못했습니다."
_LOOKAHEAD_CAP = 48
_TIMING_CAP = 8192


def _timing_summary(values):
    ordered = sorted(values)
    if not ordered:
        return {"samples": 0, "p95_ms": None, "max_ms": None}
    index = min(len(ordered) - 1, math.ceil(len(ordered) * .95) - 1)
    return {"samples": len(ordered), "p95_ms": round(ordered[index], 3),
            "max_ms": round(ordered[-1], 3)}


class SafeThumbnailProvider(QQuickImageProvider):
    """Decode registered files on a small priority queue, off the GUI thread.

    ``requestImage`` still serves QML, but a scroll focus schedules the
    visible keys first. Queued work that leaves that window is cancelled
    before decode instead of being hidden by a larger delegate cache.
    """

    ready = Signal(str)

    def __init__(self, *, cache_size=240, decoder=thumbnail, workers=2):
        if type(cache_size) is not int or not 1 <= cache_size <= 240:
            raise ValueError("invalid thumbnail cache size")
        if type(workers) is not int or not 1 <= workers <= 4:
            raise ValueError("invalid thumbnail worker count")
        super().__init__(QQuickImageProvider.ImageType.Image,
                         QQuickImageProvider.Flag.ForceAsynchronousImageLoading)
        self._lock = Condition()
        self._registry = {}
        self._registry_generation = 0
        self._cache = OrderedDict()
        self._fresh = {}
        self._cache_size = cache_size
        self._decoder = decoder
        self._worker_count = workers
        self._workers = []
        self._closed = False
        self._requests = 0
        self._decoded = 0
        self._cancelled = 0
        self._active = 0
        self._wanted = None
        self._target_size = (0, 0)
        self._heap = []
        self._live = {}
        self._priority = {}
        self._queued_size = {}
        self._sequence = 0
        self._inflight = set()
        self._failures = set()
        self._published = set()
        self._notified = {}
        self._timing_truncated = False
        self._samples = {name: [] for name in
                         ("queue_wait_ms", "decode_ms", "delivery_ms", "upload_request_ms")}

    def register(self, key, path):
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ValueError("invalid thumbnail key")
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("thumbnail path must be absolute")
        with self._lock:
            if self._closed:
                raise RuntimeError("thumbnail provider is closed")
            if key in self._registry and self._registry[key] != path:
                raise ValueError("thumbnail key is already registered")
            self._registry[key] = path

    def clear_registry(self):
        with self._lock:
            if self._closed:
                return
            self._registry_generation += 1
            self._registry.clear()
            self._cache.clear()
            self._fresh.clear()
            self._live.clear()
            self._priority.clear()
            self._queued_size.clear()
            self._heap.clear()
            self._wanted = set()
            self._failures.clear()
            self._published.clear()
            self._notified.clear()
            self._lock.notify_all()

    def close(self):
        with self._lock:
            self._closed = True
            self._cache.clear()
            self._fresh.clear()
            self._registry.clear()
            self._live.clear()
            self._priority.clear()
            self._queued_size.clear()
            self._heap.clear()
            self._wanted = set()
            self._failures.clear()
            self._published.clear()
            self._notified.clear()
            self._inflight.clear()
            self._lock.notify_all()

    def snapshot(self):
        with self._lock:
            return {"requests": self._requests, "decoded": self._decoded,
                    "cancelled": self._cancelled,
                    "cache_count": len(self._cache),
                    "cache_bytes": sum(image.sizeInBytes() for image in self._cache.values()),
                    "registered": len(self._registry), "active": self._active,
                    "queue_depth": len(self._live), "inflight": len(self._inflight),
                    "decode_workers": len(self._workers), "focus_count": len(self._wanted or ()),
                    "closed": self._closed, "timing_truncated": self._timing_truncated,
                    "queue_wait_ms": _timing_summary(self._samples["queue_wait_ms"]),
                    "decode_ms": _timing_summary(self._samples["decode_ms"]),
                    "delivery_ms": _timing_summary(self._samples["delivery_ms"]),
                    "upload_request_ms": _timing_summary(self._samples["upload_request_ms"])}

    def wait_for_idle(self, timeout=2.0):
        """Wait during tool cleanup, never during a live GUI close handler."""
        with self._lock:
            return self._lock.wait_for(self._idle_locked,
                                       timeout=max(0, min(float(timeout), 10.0)))

    def _idle_locked(self):
        return self._active == 0 and not self._live and not self._inflight

    def schedule(self, keys, width, height):
        """Decode ``keys`` in order and drop queued work that is no longer listed."""
        if type(width) is not int or type(height) is not int or not 1 <= width <= 384 or not 1 <= height <= 384:
            return
        wanted, seen = [], set()
        for key in keys:
            if isinstance(key, str) and key not in seen and _KEY.fullmatch(key):
                wanted.append(key)
                seen.add(key)
        publish = []
        with self._lock:
            if self._closed:
                return
            self._wanted = set(wanted)
            generation = self._registry_generation
            self._target_size = (width, height)
            self._failures = {item for item in self._failures
                              if item[0] in self._wanted and item[1] == width and item[2] == height}
            for key in list(self._live):
                if key not in self._wanted:
                    self._live.pop(key, None)
                    self._priority.pop(key, None)
                    self._queued_size.pop(key, None)
                    self._cancelled += 1
            if len(self._heap) > max(256, len(self._live) * 4):
                self._compact_heap_locked()
            for index, key in enumerate(wanted):
                token = (key, width, height)
                cache_key = self._fresh.get(token)
                if cache_key in self._cache:
                    self._cache.move_to_end(cache_key)
                    if token not in self._published:
                        self._published.add(token)
                        publish.append(key)
                    continue
                if token in self._failures or token in self._inflight:
                    continue
                self._enqueue_locked(key, width, height, index)
            self._start_workers_locked()
        for key in publish:
            self._publish(key, generation)

    def reset_notifications(self):
        with self._lock:
            self._published.clear()

    def join_workers(self, timeout=2.0):
        deadline = time.monotonic() + max(0.0, min(float(timeout), 5.0))
        for thread in list(self._workers):
            remaining = deadline - time.monotonic()
            thread.join(0 if remaining <= 0 else remaining)
        return all(not thread.is_alive() for thread in self._workers)

    def requestImage(self, identifier, size, requestedSize):
        key = unquote(identifier.partition("?")[0])
        width = min(384, max(1, requestedSize.width())) if requestedSize.width() > 0 else 220
        height = min(384, max(1, requestedSize.height())) if requestedSize.height() > 0 else 168
        with self._lock:
            self._requests += 1
            notified = self._notified.pop(key, None)
            if notified is not None:
                self._record("upload_request_ms", (time.perf_counter() - notified) * 1000)
            path = self._registry.get(key) if _KEY.fullmatch(key) else None
            scheduled = self._wanted is not None
            if self._closed or path is None:
                return self._output(QImage(), size)
        image, hit = self._read(key, path, width, height, decode=False)
        if hit:
            return self._output(image, size)
        if not scheduled:
            return self._decode_for_request(key, path, width, height, size)
        with self._lock:
            if self._closed:
                return self._output(QImage(), size)
            priority = 0 if key in self._wanted else 1_000_000
            if (key, width, height) not in self._inflight:
                self._enqueue_locked(key, width, height, priority)
            self._start_workers_locked()
        waited = self._wait_for_cached(key, path, width, height)
        if waited is not None:
            return self._output(waited, size)
        return self._decode_for_request(key, path, width, height, size)

    def _decode_for_request(self, key, path, width, height, size):
        token = (key, width, height)
        with self._lock:
            # A slow queued decode can outlive requestImage's cache wait.
            # Join that work instead of decoding the same image/video again.
            while token in self._inflight and not self._closed and self._registry.get(key) == path:
                self._lock.wait()
            if self._closed or self._registry.get(key) != path:
                return self._output(QImage(), size)
            # Retire the queued copy before taking ownership of the decode;
            # otherwise a scheduler worker can start it at the same time.
            if self._queued_size.get(key) == (width, height):
                self._live.pop(key, None)
                self._priority.pop(key, None)
                self._queued_size.pop(key, None)
            self._inflight.add(token)
            self._active += 1
        try:
            image, _hit = self._read(key, path, width, height, decode=True)
            return self._output(image, size)
        except Exception:
            return self._output(QImage(), size)
        finally:
            with self._lock:
                self._inflight.discard(token)
                self._active -= 1
                self._lock.notify_all()

    @staticmethod
    def _output(image, size):
        if size is not None:
            size.setWidth(image.width())
            size.setHeight(image.height())
        return image

    def _record(self, name, elapsed):
        values = self._samples[name]
        values.append(elapsed)
        if len(values) > _TIMING_CAP:
            del values[:len(values) - _TIMING_CAP]
            self._timing_truncated = True

    def _publish(self, key, generation):
        with self._lock:
            if self._closed or generation != self._registry_generation:
                return
            self._notified[key] = time.perf_counter()
        self.ready.emit(key)

    def _enqueue_locked(self, key, width, height, priority):
        token = (key, width, height)
        if token in self._inflight or token in self._failures or key not in self._registry:
            return
        current = self._priority.get(key)
        if key in self._live and current is not None and current <= priority and self._queued_size.get(key) == token[1:]:
            return
        sequence = self._sequence
        self._sequence += 1
        self._live[key] = sequence
        self._priority[key] = priority
        self._queued_size[key] = (width, height)
        heapq.heappush(self._heap, (priority, sequence, key, width, height, time.perf_counter()))
        self._lock.notify()

    def _compact_heap_locked(self):
        self._heap = [item for item in self._heap if self._live.get(item[2]) == item[1]]
        heapq.heapify(self._heap)

    def _start_workers_locked(self):
        if self._workers or self._closed:
            return
        for index in range(self._worker_count):
            thread = Thread(target=self._decode_loop, name=f"qt-gallery-thumb-{index}", daemon=True)
            thread.start()
            self._workers.append(thread)

    def _wait_for_cached(self, key, path, width, height):
        token = (key, width, height)
        deadline = time.monotonic() + 1
        while True:
            image, hit = self._read(key, path, width, height, decode=False)
            if hit:
                return image
            with self._lock:
                if self._closed or (key not in self._live and token not in self._inflight):
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._lock.wait(remaining)

    def _decode_loop(self):
        while True:
            with self._lock:
                while not self._closed and not self._live:
                    self._lock.wait()
                if self._closed:
                    return
                if not self._heap:
                    self._live.clear()
                    self._priority.clear()
                    self._queued_size.clear()
                    continue
                priority, sequence, key, width, height, enqueued = heapq.heappop(self._heap)
                if self._live.get(key) != sequence:
                    continue
                self._live.pop(key, None)
                self._priority.pop(key, None)
                self._queued_size.pop(key, None)
                path = self._registry.get(key)
                generation = self._registry_generation
                token = (key, width, height)
                self._inflight.add(token)
                self._active += 1
                self._record("queue_wait_ms", (time.perf_counter() - enqueued) * 1000)
            publish = False
            try:
                image, _hit = self._read(key, path, width, height, decode=True, deliver=False) if path is not None else (QImage(), False)
            except Exception:
                image = QImage()
            with self._lock:
                self._inflight.discard(token)
                self._active -= 1
                closed = self._closed
                stale = generation != self._registry_generation
                if not closed and not stale and image.isNull():
                    self._failures.add(token)
                elif (not closed and not stale and not image.isNull() and key in self._wanted
                      and self._target_size == (width, height) and token not in self._published):
                    self._published.add(token)
                    publish = True
                self._lock.notify_all()
            if publish:
                self._publish(key, generation)
            if closed:
                return

    def _read(self, key, path, width, height, *, decode=True, deliver=True):
        with self._lock:
            if self._closed or self._registry.get(key) != path:
                return QImage(), False
            generation = self._registry_generation
        if path is None or path.is_symlink() or path.resolve() != path:
            return QImage(), False
        try:
            status = path.stat()
        except OSError:
            return QImage(), False
        if cloud_only(path, status=status):
            return QImage(), False
        stamp = (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)
        cache_key = key, stamp, width, height
        with self._lock:
            if self._closed or generation != self._registry_generation:
                return QImage(), False
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._cache.move_to_end(cache_key)
                started = time.perf_counter()
                image = QImage(cached)
                if deliver:
                    self._record("delivery_ms", (time.perf_counter() - started) * 1000)
                return image, True
            if not decode:
                return QImage(), False
        started = time.perf_counter()
        result = self._decoder(path, (width, height))
        decode_ms = (time.perf_counter() - started) * 1000
        picture = result.image
        if picture is None:
            with self._lock:
                self._record("decode_ms", decode_ms)
            return QImage(), False
        try:
            if picture.mode != "RGBA":
                converted = picture.convert("RGBA")
                picture.close()
                picture = converted
            pixels = picture.tobytes("raw", "RGBA")
            image = QImage(pixels, picture.width, picture.height, picture.width * 4,
                           QImage.Format.Format_RGBA8888).copy()
        finally:
            picture.close()
        try:
            current = path.stat()
        except OSError:
            return QImage(), False
        if ((current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != stamp
                or path.is_symlink() or path.resolve() != path or cloud_only(path, status=current)):
            return QImage(), False
        with self._lock:
            self._record("decode_ms", decode_ms)
            if self._closed or generation != self._registry_generation:
                return QImage(), False
            self._decoded += 1
            self._cache[cache_key] = image
            self._fresh[(key, width, height)] = cache_key
            self._cache.move_to_end(cache_key)
            while len(self._cache) > self._cache_size:
                old_key, _old = self._cache.popitem(last=False)
                fresh_key = (old_key[0], old_key[2], old_key[3])
                if self._fresh.get(fresh_key) == old_key:
                    self._fresh.pop(fresh_key, None)
            started = time.perf_counter()
            delivered = QImage(image)
            if deliver:
                self._record("delivery_ms", (time.perf_counter() - started) * 1000)
        return delivered, False


class GalleryModel(QAbstractListModel):
    """Append small reader batches; only model signals touch the UI thread."""

    changed = Signal()
    _delivered = Signal(object)
    KeyRole = Qt.ItemDataRole.UserRole + 1
    CaptionRole = KeyRole + 1
    ThumbUrlRole = KeyRole + 2
    ThumbReadyRole = KeyRole + 3

    def __init__(self, reader, *, provider=None, batch_size=200, parent=None):
        super().__init__(parent)
        if not callable(reader) or type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise ValueError("invalid gallery reader")
        self._reader = reader
        self._provider = provider
        self._batch_size = batch_size
        self._rows = []
        self._keys = set()
        self._positions = {}
        self._ready = set()
        self._thumb_size = (0, 0)
        ready = getattr(provider, "ready", None)
        if ready is not None:
            ready.connect(self._thumb_ready)
        self._total = 0
        self._busy = False
        self._error = ""
        self._query = ""
        self._generation = 0
        self._exhausted = False
        self._condition = Condition()
        self._closed = False
        self._pending = None
        self._delivered.connect(self._accept, Qt.ConnectionType.QueuedConnection)
        self._worker = Thread(target=self._work, name="qt-gallery-catalog", daemon=True)
        self._worker.start()
        self.loadMore()

    @Property(int, notify=changed)
    def count(self):
        return len(self._rows)

    @Property(int, notify=changed)
    def total(self):
        return self._total

    @Property(bool, notify=changed)
    def busy(self):
        return self._busy

    @Property(str, notify=changed)
    def error(self):
        return self._error

    def roleNames(self):
        return {self.KeyRole: b"key", self.CaptionRole: b"caption",
                self.ThumbUrlRole: b"thumbUrl", self.ThumbReadyRole: b"thumbReady"}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        if role == self.KeyRole:
            return row["key"]
        if role in (self.CaptionRole, Qt.ItemDataRole.DisplayRole):
            return row["caption"]
        if role == self.ThumbUrlRole:
            return f"image://thumbs/{quote(row['key'], safe='')}?generation={self._generation}"
        if role == self.ThumbReadyRole:
            return row["key"] in self._ready
        return None

    def canFetchMore(self, parent=QModelIndex()):
        return not parent.isValid() and not (self._closed or self._busy or self._exhausted)

    def fetchMore(self, parent=QModelIndex()):
        if not parent.isValid():
            self.loadMore()

    @Slot()
    def loadMore(self):
        if not self.canFetchMore():
            return
        self._busy = True
        self.changed.emit()
        with self._condition:
            self._pending = (self._generation, self._query, len(self._rows), self._batch_size)
            self._condition.notify()

    @Slot(str)
    def search(self, query):
        if self._closed or not isinstance(query, str) or query == self._query:
            return
        self._generation += 1
        self.beginResetModel()
        self._rows.clear()
        self._keys.clear()
        self._positions.clear()
        self._ready.clear()
        self._total = 0
        reset = getattr(self._provider, "reset_notifications", None)
        if reset is not None:
            reset()
        self._query = query[:500]
        self._busy = False
        self._exhausted = False
        self._error = ""
        self.endResetModel()
        self.loadMore()

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                if self._closed:
                    return
                generation, query, offset, limit = self._pending
                self._pending = None
            try:
                total, rows = self._reader(query, offset, limit)
                if type(total) is not int or total < 0 or not isinstance(rows, (tuple, list)) or len(rows) > limit:
                    raise ValueError("invalid gallery batch")
                cleaned = []
                for row in rows:
                    key = row.get("key")
                    caption = row.get("caption", "")
                    if not isinstance(key, str) or not _KEY.fullmatch(key) or not isinstance(caption, str):
                        raise ValueError("invalid gallery row")
                    cleaned.append({"key": key, "caption": caption[:500]})
                result = generation, offset, total, cleaned, ""
            except Exception:
                result = generation, offset, 0, [], _READ_ERROR
            with self._condition:
                if self._closed:
                    return
                if generation != self._generation:
                    continue
            self._delivered.emit(result)

    @Slot(object)
    def _accept(self, result):
        generation, offset, total, rows, error = result
        if self._closed or generation != self._generation or offset != len(self._rows):
            return
        keys = [row["key"] for row in rows]
        if (len(set(keys)) != len(keys) or self._keys.intersection(keys) or
                total < offset + len(rows) or (not rows and total > offset)):
            error = _READ_ERROR
        if error:
            self._busy = False
            self._error = error
            self._exhausted = True
            self.changed.emit()
            return
        self._total = total
        if rows:
            self.beginInsertRows(QModelIndex(), offset, offset + len(rows) - 1)
            start = len(self._rows)
            self._rows.extend(rows)
            self._keys.update(keys)
            for index, row in enumerate(rows):
                self._positions[row["key"]] = start + index
            self.endInsertRows()
        self._exhausted = len(self._rows) >= total
        self._busy = False
        self.changed.emit()

    @Slot(int, int, int, int, int)
    def focusViewport(self, first, last, lead, width, height):
        """Ask the thumbnail queue for the visible rows plus a short lookahead."""
        if (self._closed or self._provider is None or not self._rows or
                type(width) is not int or type(height) is not int or
                not 1 <= width <= 384 or not 1 <= height <= 384):
            return
        count = len(self._rows)
        first = max(0, min(count - 1, first))
        last = max(0, min(count - 1, last))
        if first > last:
            first, last = last, first
        lead = max(-_LOOKAHEAD_CAP, min(_LOOKAHEAD_CAP, lead))
        visible = list(range(first, last + 1))
        if lead > 0:
            ahead = list(range(last + 1, min(count, last + 1 + lead)))
        elif lead < 0:
            ahead = list(range(first - 1, max(-1, first + lead - 1), -1))
        else:
            ahead = []
        keys = [self._rows[index]["key"] for index in visible + ahead]
        size = (width, height)
        if size != self._thumb_size:
            reset = getattr(self._provider, "reset_notifications", None)
            if reset is not None:
                reset()
            if self._ready:
                self._ready.clear()
                self.dataChanged.emit(self.index(0), self.index(count - 1), [self.ThumbReadyRole])
        self._thumb_size = size
        schedule = getattr(self._provider, "schedule", None)
        if schedule is not None:
            schedule(keys, width, height)

    @Slot(str)
    def _thumb_ready(self, key):
        if self._closed or not isinstance(key, str) or key in self._ready:
            return
        row = self._positions.get(key)
        if row is None or row >= len(self._rows) or self._rows[row]["key"] != key:
            return
        self._ready.add(key)
        model_index = self.index(row)
        self.dataChanged.emit(model_index, model_index, [self.ThumbReadyRole])

    def shutdown(self, timeout=2.0):
        with self._condition:
            self._closed = True
            self._pending = None
            self._condition.notify()
        bounded = max(0, min(float(timeout), 5.0))
        self._worker.join(bounded)
        self._busy = False
        stopped = not self._worker.is_alive()
        if self._provider is not None:
            close = getattr(self._provider, "close", None)
            if close is not None:
                close()
            join = getattr(self._provider, "join_workers", None)
            if join is not None:
                stopped = bool(join(bounded)) and stopped
        return stopped
