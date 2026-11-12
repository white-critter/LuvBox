"""Qt controls for the update notice: check, fetch on a click, and restart into the new version."""

from datetime import datetime, timezone
from queue import Queue
from threading import Event, Lock, Thread
import time

from PySide6.QtCore import QObject, Property, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices

from . import __version__, updater
from .launcher import launcher_path


class UpdateController(QObject):
    changed = Signal()
    shutdownFinished = Signal()
    _delivered = Signal(object)

    def __init__(self, parent=None, *, settings=None, preferences=None, current=__version__,
                 source=updater.GITHUB, launcher=launcher_path, folder=None):
        super().__init__(parent)
        self._settings = settings
        self._current = current
        self._source = source
        self._launcher = launcher
        self._folder = folder
        preferences = preferences if isinstance(preferences, dict) else {}
        # A damaged saved value means the default: checking.
        self._auto = preferences["update_check"] if type(preferences.get("update_check")) is bool else True
        self._checked_at = preferences.get("update_checked_at") if isinstance(
            preferences.get("update_checked_at"), str) else ""
        self._found = self._remembered(preferences.get("update_found"))
        # idle, checking, current, available, downloading, ready, failed
        self._state = "idle"
        self._error = ""
        self._progress = 0.0
        self._dismissed = False
        self._manual = False
        self._restart = False
        self._closing = False
        self._shutdown_complete = False
        self._lock = Lock()
        self._swapping = False
        self._cancel = Event()
        self._jobs = Queue()
        self._worker = None
        self._delivered.connect(self._accept, Qt.ConnectionType.QueuedConnection)
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(50)
        self._shutdown_timer.timeout.connect(self._poll_shutdown)

    def _remembered(self, value):
        """A newer release found by an earlier check, if it is still newer than this program."""
        if not isinstance(value, dict):
            return None
        version = updater.parse_version(value.get("version"))
        installed = updater.parse_version(self._current)
        if version is None or installed is None or version <= installed or str(value.get("version")).startswith("v"):
            return None
        notes = value.get("notes") if isinstance(value.get("notes"), str) else ""
        size = value.get("size") if type(value.get("size")) is int and value.get("size") > 0 else 0
        return {"version": value["version"], "notes": notes[:4000],
                "installable": value.get("installable") is True, "size": size}

    @Property(str, constant=True)
    def currentVersion(self):
        return self._current

    @Property(str, notify=changed)
    def state(self):
        return self._state

    @Property(bool, notify=changed)
    def busy(self):
        return self._state in {"checking", "downloading"}

    @Property(str, notify=changed)
    def version(self):
        return self._found["version"] if self._found else ""

    @Property(str, notify=changed)
    def notes(self):
        return self._found["notes"] if self._found else ""

    @Property(bool, notify=changed)
    def canInstall(self):
        """This release can be fetched here: it has the files, and a launcher to replace is known."""
        return bool(self._found and self._found["installable"] and self._launcher() is not None)

    @Property(float, notify=changed)
    def progress(self):
        return self._progress

    @Property(bool, notify=changed)
    def autoCheck(self):
        return self._auto

    @Property(str, notify=changed)
    def error(self):
        return self._error

    @Property(bool, notify=changed)
    def noticeVisible(self):
        return bool(self._found and not self._dismissed and not self._closing
                    and self._state in {"available", "downloading", "ready", "failed"})

    @Property(str, notify=changed)
    def status(self):
        """One line for the settings dialog."""
        if self._state == "checking":
            return "새 버전을 확인하는 중"
        if self._state == "failed" or (self._error and self._state == "available"):
            # Also a check by hand that failed while an earlier find is still on offer.
            return self._error
        if self._state == "downloading":
            return f"{self.version} 받는 중 · {round(self._progress * 100)}%"
        if self._state == "ready":
            return f"{self.version} 준비 완료 · 다시 시작하면 바뀌어요"
        if self._found:
            return f"새 버전이 나왔어요 · {self.version}"
        if self._state == "current":
            return "최신 버전이에요"
        return "최신 버전이에요 · " + self._checked_text() if self._checked_at else "아직 확인하지 않았어요"

    def _checked_text(self):
        try:
            when = datetime.fromisoformat(self._checked_at).astimezone()
        except ValueError:
            return "확인함"
        return f"{when.month}월 {when.day}일 확인"

    def _start_worker(self):
        if self._worker is None or not self._worker.is_alive():
            self._worker = Thread(target=self._work, name="LuvBox update", daemon=True)
            self._worker.start()

    def _queue(self, kind, **details):
        if self._closing:
            return False
        try:
            self._start_worker()
        except RuntimeError:
            return False
        self._jobs.put((kind, details))
        return True

    @Slot()
    def startAutomaticCheck(self):
        """At application start: show what an earlier check found, and look again once a day."""
        if self._closing or self.busy:
            return
        if self._found and self._state == "idle":
            self._state = "available"
            self.changed.emit()
        if updater.check_due(self._auto, self._checked_at):
            self._manual = False
            if self._queue("check"):
                if self._state == "idle":
                    self._state = "checking"
                self.changed.emit()

    @Slot()
    def checkNow(self):
        if self._closing or self.busy or self._state == "ready":
            return
        self._manual = True
        self._error = ""
        if self._queue("check"):
            self._state = "checking"
            self.changed.emit()

    @Slot()
    def install(self):
        """The click: fetch the release's program, verify it and put it in the launcher's place."""
        if self._closing or self._state not in {"available", "failed"} or not self.canInstall:
            return
        launcher = self._launcher()
        self._cancel.clear()
        self._error = ""
        self._progress = 0.0
        self._dismissed = False
        if self._queue("install", version=self._found["version"], size=self._found["size"], launcher=launcher):
            self._state = "downloading"
            self.changed.emit()

    @Slot()
    def cancel(self):
        if self._state == "downloading":
            self._cancel.set()

    @Slot()
    def dismiss(self):
        """Hide the notice until the next start; settings keep showing the new version."""
        if not self._dismissed:
            self._dismissed = True
            self.changed.emit()

    @Slot(bool)
    def setAutoCheck(self, enabled):
        enabled = bool(enabled)
        if enabled == self._auto or self._closing:
            return
        self._auto = enabled
        self._queue("remember", changes={"update_check": enabled})
        self.changed.emit()

    @Slot()
    def openReleasePage(self):
        QDesktopServices.openUrl(QUrl(updater.RELEASES_PAGE))

    def confirm_restart(self):
        """The application is closing in order to start the launcher again."""
        self._restart = True

    @property
    def restart_pending(self):
        return self._restart and self._state == "ready"

    def _folder_path(self):
        if self._folder is not None:
            return self._folder
        from .settings import app_directory
        return app_directory() / "updates"

    def _remember(self, changes):
        if self._settings is None:
            return
        try:
            self._settings.update(changes)
        except Exception:
            pass  # the notice still works; the next start simply checks again

    def _work(self):
        while True:
            kind, details = self._jobs.get()
            if kind == "quit":
                return
            if kind == "remember":
                self._remember(details["changes"])
                continue
            try:
                if kind == "check":
                    found = updater.check(self._current, self._source)
                    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    remembered = ({"version": found["version"], "notes": found["notes"],
                                   "installable": found["installable"], "size": found["size"]}
                                  if found["newer"] else {})
                    self._remember({"update_checked_at": stamp, "update_found": remembered})
                    result = ("checked", found, stamp)
                else:
                    result = self._install(**details)
            except updater.UpdateError as error:
                result = ("failed", kind, error.kind, str(error))
            except Exception:
                result = ("failed", kind, "unexpected", updater.MESSAGES["unexpected"])
            self._delivered.emit(result)

    def _install(self, version, size, launcher):
        folder = self._folder_path()
        updater.clear_downloads(folder)
        last = [0.0, -1.0]

        def progress(received, total):
            fraction = received / total if total else 0.0
            now = time.monotonic()
            if fraction - last[1] >= 0.01 and now - last[0] >= 0.05:
                last[0], last[1] = now, fraction
                self._delivered.emit(("progress", fraction))

        path, digest = updater.download(version, folder, self._source, size=size, progress=progress,
                                        cancelled=self._cancel.is_set)
        with self._lock:
            if self._closing or self._cancel.is_set():
                raise updater.UpdateError("cancelled")
            self._swapping = True
        try:
            updater.install(path, launcher, digest)
        finally:
            with self._lock:
                self._swapping = False
            updater.clear_downloads(folder)
        return ("installed", version)

    @Slot(object)
    def _accept(self, result):
        if self._closing:
            return
        kind = result[0]
        if kind == "progress":
            if self._state == "downloading":
                self._progress = max(self._progress, min(1.0, result[1]))
                self.changed.emit()
            return
        if kind == "checked":
            found, self._checked_at = result[1], result[2]
            if self._state in {"downloading", "ready"}:
                return
            if found["newer"]:
                if not self._found or self._found["version"] != found["version"]:
                    self._dismissed = False
                self._found = {name: found[name] for name in ("version", "notes", "installable", "size")}
                self._state = "available"
            else:
                self._found = None
                self._state = "current"
            self._error = ""
        elif kind == "installed":
            self._progress = 1.0
            self._state = "ready"
            self._dismissed = False
            self._error = ""
        elif kind == "failed":
            stage, reason, message = result[1], result[2], result[3]
            if stage == "check":
                if self._manual:
                    self._error = message
                    self._state = "failed" if not self._found else "available"
                else:
                    # A check nobody asked for fails quietly; what was known stays.
                    self._state = "available" if self._found else "idle"
            elif reason == "cancelled":
                self._state = "available"
                self._progress = 0.0
            else:
                self._error = message
                self._state = "failed"
                self._dismissed = False
        self.changed.emit()

    @Slot(result=bool)
    def shutdown(self):
        if self._closing:
            return self._shutdown_complete
        self._closing = True
        self._cancel.set()
        self._jobs.put(("quit", {}))
        self.changed.emit()
        self._shutdown_timer.start()
        self._poll_shutdown()
        return self._shutdown_complete

    @Slot()
    def _poll_shutdown(self):
        if self._shutdown_complete:
            return
        with self._lock:
            swapping = self._swapping
        # Only the two renames of an install must not be cut off. A download
        # or a slow network read is simply left; its partial file is removed
        # before the next download.
        if swapping:
            return
        self._shutdown_timer.stop()
        self._shutdown_complete = True
        self.shutdownFinished.emit()
