"""Off-thread, explicitly confirmed moves of gallery files to the Windows Recycle Bin."""

from collections import deque
import os
from pathlib import Path
from threading import Condition, Event, Thread

from PySide6.QtCore import QObject, Property, Qt, QTimer, Signal, Slot

from .qt_library import ManifestError
from .recycling import RecyclingError


# Seconds without a new job before the records copy and backup of a deletion are written.
_SETTLE_AFTER = 20.0


def _recycled_keys(rows, paths):
    """Displayed keys whose file is now in the Recycle Bin, by the path that moved."""
    if not isinstance(paths, (list, tuple)):
        return None
    moved = {os.path.normcase(os.path.abspath(path)) for path in paths if isinstance(path, str) and path}
    keys = []
    for row in rows:
        key, path = row.get("_key"), row.get("path")
        if not isinstance(key, str) or not isinstance(path, str) or not path:
            return None
        if os.path.normcase(os.path.abspath(path)) in moved:
            keys.append(key)
    return keys


class ManagementController(QObject):
    changed = Signal()
    shutdownFinished = Signal()
    deleteConfirmationRequested = Signal()
    libraryChanged = Signal()
    _delivered = Signal(object)

    def __init__(self, library, parent=None):
        super().__init__(parent)
        self._library = library
        self._context = ("", "", False)
        self._generation = 0
        self._mutating = self._locked = self._closing = False
        self._error = self._status = ""
        self._delete = None
        self._recycled_keys = None
        self._stop = Event()
        self._condition = Condition()
        self._pending = deque()
        self._delivered.connect(self._accept, Qt.ConnectionType.QueuedConnection)
        self._worker = Thread(target=self._work, name="LuvBox management", daemon=True)
        self._worker.start()
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(50)
        self._shutdown_timer.timeout.connect(self._poll_shutdown)

    @Property(bool, notify=changed)
    def mutating(self): return self._mutating
    @Property(bool, notify=changed)
    def locked(self): return self._locked or self._closing
    @Property(str, notify=changed)
    def error(self): return self._error
    @Property(str, notify=changed)
    def status(self): return self._status
    @Property(bool, notify=changed)
    def deleteConfirmationPending(self): return self._delete is not None
    @Property(int, notify=changed)
    def deleteCount(self): return len(self._delete["rows"]) if self._delete else 0
    @Property("QVariantMap", notify=changed)
    def deleteAlike(self):
        """The two photos a '비슷함' move is compared on ({alike, keeper}: url, pixels, bytes, ...), or empty."""
        return dict(self._delete.get("alike") or {}) if self._delete else {}
    @Property(str, notify=changed)
    def deleteSummary(self):
        if not self._delete: return ""
        if self._delete.get("alike"):
            # One '비슷함' photo compared with the photo its group keeps: never more than this one.
            return "오른쪽 ‘비슷함’ 사진 한 장만 Windows 휴지통으로 이동합니다."
        names = [row.get("filename") or Path(row["path"]).name for row in self._delete["rows"][:5]]
        summary = f"선택한 파일 {self.deleteCount:,}개를 Windows 휴지통으로 이동합니다.\n"
        if any("_copy_keepers" in row for row in self._delete["rows"]):
            # From the duplicate view: say what protects the last file of a group.
            summary += "같은 파일이 남는 경우에만 이동합니다.\n"
            # Photos shown as only alike ('비슷함'): no file before them has their bytes, so they
            # usually stay. Said before the move, not only in the result.
            similar = sum(1 for row in self._delete["rows"] if row.get("_copy_near"))
            if similar:
                summary += f"‘비슷함’ {similar:,}장은 ‘비교’에서 한 장씩 이동할 수 있습니다.\n"
        kept = self._delete.get("kept") or 0
        if kept:
            # A whole group was chosen in the duplicate view: its first photo stays.
            summary += f"묶음마다 한 장씩 남깁니다 · 총 {kept:,}장\n"
        if self.deleteCount > len(names):
            # Only the first names are listed; say that the count above is not this list.
            names.append(f"외 {self.deleteCount - len(names):,}개")
        return summary + "\n".join(names)

    def takeRecycledKeys(self):
        """Gallery keys of the files the last move sent to the Recycle Bin.

        None means the outcome is not known file by file, so the gallery
        reloads its list instead of removing single tiles.
        """
        keys, self._recycled_keys = self._recycled_keys, None
        return keys

    @Slot(bool)
    def setLocked(self, locked):
        if self._locked != bool(locked):
            self._locked = bool(locked)
            self.changed.emit()

    def setLibrary(self, account, output_dir, separate_accounts):
        """A different account or folder invalidates every pending selection."""
        context = (account, output_dir, bool(separate_accounts))
        if context == self._context:
            return
        self._context = context
        self._generation += 1
        confirming = self._delete is not None
        self._delete = None
        if confirming:
            self._mutating = False
        elif self._mutating:
            self._stop.set()
        self._error = self._status = ""
        self.changed.emit()

    @Slot("QVariantList")
    def prepareDelete(self, keys):
        if self.locked or self._mutating or self._closing or not keys: return
        if getattr(self._library, "filesLocked", False):
            self._error = self._library.holdReason or "진행 중인 작업이 끝난 뒤 휴지통으로 이동할 수 있습니다."
            self._status = ""
            self.changed.emit()
            return
        # A '비슷함' photo goes only alone, compared with the photo its group keeps. Picked with its
        # check box and sent like a copy, it always stayed and nothing moved: one picked alone opens
        # the comparison, several picked alone are told so before anything is checked. Mixed with
        # copies, the copies go and the confirmation says the '비슷함' ones stay.
        keys = list(keys)
        alike = getattr(self._library, "alikeKeys", None)
        alike = alike(keys) if callable(alike) else []
        if alike and len(alike) == len(keys):
            if len(keys) == 1:
                self.prepareAlikeDelete(keys[0])
                return
            self._error = ("‘비슷함’ 사진은 한 장씩 휴지통으로 보낼 수 있습니다. "
                           "한 장만 골라 남길 사진과 비교한 뒤 보내 주세요.")
            self._status = ""
            self.changed.emit()
            return
        self._mutating = True
        self._stop.clear()
        self._error = ""
        self._status = "휴지통으로 이동할 파일을 확인합니다"
        self._enqueue("prepare_delete", tuple(keys))

    @Slot(str)
    def prepareAlikeDelete(self, key):
        """One '비슷함' photo of the duplicate view, to be compared with the photo its group keeps."""
        if self.locked or self._mutating or self._closing or not key: return
        if getattr(self._library, "filesLocked", False):
            self._error = self._library.holdReason or "진행 중인 작업이 끝난 뒤 휴지통으로 이동할 수 있습니다."
            self._status = ""
            self.changed.emit()
            return
        self._mutating = True
        self._stop.clear()
        self._error = ""
        self._status = "남길 사진과 비교할 준비를 합니다"
        self._enqueue("prepare_alike", str(key))

    @Slot()
    def cancelDelete(self):
        if self._delete is not None:
            self._delete = None
            self._mutating = False
            self._status = "파일을 유지했습니다"
            self.changed.emit()

    @Slot(bool)
    def confirmDelete(self, keep_deleted):
        manifest, self._delete = self._delete, None
        if manifest is None: return
        if self.locked or manifest["generation"] != self._library._generation:
            self._mutating = False
            self._error = "계정이나 목록이 바뀌었습니다. 파일을 다시 선택해 주세요."
            self.changed.emit()
            return
        self._stop.clear()
        self._status = "Windows 휴지통으로 이동합니다"
        self._enqueue("delete", (manifest, bool(keep_deleted)))

    @Slot()
    def stop(self):
        if self._delete is not None:
            self.cancelDelete()
            return
        self._stop.set()
        self._status = "휴지통 이동을 중단하는 중입니다"
        self.changed.emit()

    def _enqueue(self, kind, payload):
        with self._condition:
            self._pending.append((kind, self._generation, payload))
            self._condition.notify()
        self.changed.emit()

    def _work(self):
        # Libraries whose records copy (in the media folder) and catalog backup wait to be written.
        # A deletion used to write both itself: two copies of the whole catalog for every photo
        # put away, and in a synced folder an upload of it each time. They are written once,
        # after the last deletion has been quiet for a while, and before the application ends.
        unsettled = {}
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closing or self._pending,
                                         timeout=_SETTLE_AFTER if unsettled else None)
                closing = self._closing
                job = None if closing or not self._pending else self._pending.popleft()
            if job is None:
                if unsettled:
                    from .deletion import settle_records
                    for location in unsettled.values():
                        settle_records(location)
                    unsettled.clear()
                if closing: return
                continue
            kind, generation, payload = job
            try:
                if generation != self._generation:
                    self._delivered.emit((kind, generation, {}, "계정이나 목록이 바뀌었습니다."))
                    continue
                if kind == "prepare_delete":
                    result = self._library.deletionManifest(payload)
                    if self._stop.is_set(): result = {"rows": [], "cancelled": True}
                elif kind == "prepare_alike":
                    result = self._library.alikeManifest(payload)
                    if self._stop.is_set(): result = {"rows": [], "cancelled": True}
                else:
                    manifest, keep = payload
                    result = self._recycle(manifest, keep, generation)
                    if result.get("unsettled"):
                        location = manifest["location"]
                        unsettled[str(getattr(location, "record_dir", location))] = location
                    result = dict(result, keys=_recycled_keys(manifest["rows"], result.get("paths")),
                                  alike=bool(manifest.get("alike")))
                self._delivered.emit((kind, generation, result, ""))
            except (ManifestError, RecyclingError) as exc:
                fallback = ("사진을 비교하지 못했습니다." if kind == "prepare_alike"
                            else "작업을 마치지 못했습니다. 계정·폴더·파일 상태를 확인해 주세요.")
                self._delivered.emit((kind, generation, {}, str(exc) or fallback))
            except Exception:
                self._delivered.emit((kind, generation, {}, "작업을 마치지 못했습니다. 계정·폴더·파일 상태를 확인해 주세요."))

    def _recycle(self, manifest, keep_deleted, generation):
        """Move the confirmed files (on the worker). The 폴더 정리 tab replaces this with its own checks."""
        from .deletion import run_deletion

        return run_deletion(manifest["location"], manifest["rows"], keep_deleted, self._stop,
                            lambda event: self._delivered.emit(("progress", generation, event, "")),
                            settle=False)

    @Slot(object)
    def _accept(self, event):
        kind, generation, value, error = event
        if self._closing: return
        if generation != self._generation:
            if kind != "progress":
                self._delete = None
                self._mutating = False
                self._error = "계정이나 목록이 바뀌었습니다. 작업 결과를 다시 확인해 주세요."
                self.changed.emit()
            return
        if kind == "progress":
            self._status = str(value.get("message", ""))
        elif kind in ("prepare_delete", "prepare_alike"):
            self._error = error
            if self._stop.is_set() or value.get("cancelled"):
                self._mutating = False
                self._status = "파일 이동을 취소했습니다"
            elif error or not value.get("rows"):
                self._mutating = False
                # A refused comparison says why in the error; "nothing to move" would read as a result.
                self._status = "" if kind == "prepare_alike" and error else "이동할 파일이 없습니다"
            else:
                self._delete = value
                self._status = ("남길 사진과 비교해 주세요" if kind == "prepare_alike"
                                else "휴지통으로 이동할지 확인해 주세요")
                self.deleteConfirmationRequested.emit()
        else:
            self._mutating = False
            self._error = error
            self._status = str(value.get("message") or ("작업을 마쳤습니다" if not error else ""))
            if not error and value.get("alike") and value.get("protected"):
                # The one compared photo stayed: what the person asked for did not happen, and why
                # reads as an error, not as a result.
                self._error, self._status = self._status, ""
            # A failed or unprocessed file may have changed on disk, so only a
            # clean result removes single tiles; anything else reloads the list.
            keys = value.get("keys")
            clean = (not error and not value.get("failed") and not value.get("cancelled")
                     and not value.get("protected"))
            self._recycled_keys = list(keys) if clean and isinstance(keys, (list, tuple)) else None
            self.libraryChanged.emit()
        self.changed.emit()

    @Slot()
    def shutdown(self):
        self._closing = True
        self._stop.set()
        with self._condition:
            self._pending.clear()
            self._condition.notify_all()
        self._shutdown_timer.start()

    def _poll_shutdown(self):
        if not self._worker.is_alive():
            self._shutdown_timer.stop()
            self._worker.join(timeout=0)
            self.shutdownFinished.emit()
