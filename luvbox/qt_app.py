"""Product Qt shell over the existing downloader, accounts and local library."""

from dataclasses import replace
from pathlib import Path
from queue import Queue
import threading

from PySide6.QtCore import QObject, Property, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontDatabase, QIcon, QPalette

from . import __version__
from .app_settings import SettingsRepository, default_settings
from .core import JobOptions, _normal_account
from .qt_accounts import AccountController
from .qt_browser_setup import BrowserSetupController
from .qt_download import DownloadController
from .qt_library import LibraryController
from .qt_schedule import ScheduleController
from .qt_wheel import WheelInputFilter


_ROOT = Path(__file__).resolve().parents[1]
_CHOICES = {
    "folder_mode": {"flat", "author", "date"},
    "scan_mode": {"quick", "full"},
    "gif_format": {"gif", "mp4"},
    "gif_size": {"original", "720", "480"},
}
_BOOLEANS = {"include_videos", "include_gifs", "separate_accounts", "confirm_each"}
_PUBLIC_PREFERENCES = {*_CHOICES, *_BOOLEANS, "max_video_mb"}
_SETTINGS_ERROR = "설정을 저장하지 못했습니다. 설정 파일과 저장 권한을 확인해 주세요."


class ApplicationController(QObject):
    changed = Signal()
    preferencesChanged = Signal()
    loginRequested = Signal()
    storageConfirmationRequested = Signal()
    reviewReady = Signal()
    reviewCleared = Signal()
    shutdownFinished = Signal()
    _completed = Signal(str, bool, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings = SettingsRepository()
        self._preferences = self._settings.load()
        defaults = default_settings()
        for name, choices in _CHOICES.items():
            if self._preferences.get(name) not in choices:
                self._preferences[name] = defaults[name]
        for name in _BOOLEANS:
            if type(self._preferences.get(name)) is not bool:
                self._preferences[name] = defaults[name]
        limit = self._preferences.get("max_video_mb")
        if type(limit) is not int or not 0 <= limit <= 1_000_000:
            self._preferences["max_video_mb"] = defaults["max_video_mb"]
        self._account = str(self._preferences.get("account") or "")
        self._output = str(self._preferences.get("output_dir") or defaults["output_dir"])
        locations = self._preferences.get("record_locations", {})
        locations = dict(locations) if isinstance(locations, dict) else {}
        try:
            if self._output:
                locations.setdefault(_normal_account(self._account), self._output)
        except ValueError:
            pass
        self._preferences["record_locations"] = locations
        self._committed_preferences = dict(self._preferences)
        self._message = ""
        self._error = ""
        self._preparing = False
        self._settings_revision = 0
        self._pending_settings = {}
        self._settings_versions = {}
        self._pending_download = None
        self._save_after_settings = False
        self._closing = False
        self._shutdown_ready = False
        self._pending_start = None
        self._confirmation = ""
        self._review = {}
        self._gallery_dirty = False
        self._gallery_visible = False
        self._shutdown_parts = set()
        self._accounts = AccountController(self, settings=self._settings)
        self._download = DownloadController(self)
        self._library = LibraryController(self)
        self._browser_setup = BrowserSetupController(self)
        self._schedule = ScheduleController(self)
        self._accounts.changed.connect(self._state_changed)
        self._accounts.accountChanged.connect(self._account_changed)
        self._download.changed.connect(self._state_changed)
        self._schedule.changed.connect(self._state_changed)
        self._download.eventReceived.connect(self._download_event)
        self._download.reviewRequested.connect(self._prepare_review)
        self._download.reviewDismissed.connect(self._dismiss_review)
        self._accounts.shutdownFinished.connect(lambda: self._part_closed("accounts"))
        self._download.shutdownFinished.connect(lambda: self._part_closed("download"))
        self._library.shutdownFinished.connect(lambda: self._part_closed("library"))
        self._browser_setup.shutdownFinished.connect(lambda: self._part_closed("browser_setup"))
        self._schedule.shutdownFinished.connect(lambda: self._part_closed("schedule"))
        self._completed.connect(self._accept, Qt.ConnectionType.QueuedConnection)
        self._commands = Queue()
        self._io_thread = threading.Thread(target=self._work, name="qt-app-storage", daemon=True)
        self._io_thread.start()
        self._close_timer = QTimer(self)
        self._close_timer.setInterval(50)
        self._close_timer.timeout.connect(self._finish_close)
        identity = self._accounts.identity
        if identity:
            self._account = identity["handle"]
            locations = self._preferences.get("record_locations", {})
            if isinstance(locations, dict) and isinstance(locations.get(self._account), str):
                self._output = locations[self._account]
        self._load_library()

    @Property(str, constant=True)
    def version(self):
        return __version__

    @Property(QObject, constant=True)
    def accounts(self):
        return self._accounts

    @Property(QObject, constant=True)
    def download(self):
        return self._download

    @Property(QObject, constant=True)
    def library(self):
        return self._library

    @Property(QObject, constant=True)
    def browserSetup(self):
        return self._browser_setup

    @Property(QObject, constant=True)
    def schedule(self):
        return self._schedule

    @Property("QVariantMap", notify=preferencesChanged)
    def preferences(self):
        return {name: self._preferences[name] for name in _PUBLIC_PREFERENCES}

    @Property(str, notify=changed)
    def account(self):
        return self._account

    @Property(str, notify=changed)
    def outputFolder(self):
        return self._output

    @Property(QUrl, notify=changed)
    def outputUrl(self):
        return QUrl.fromLocalFile(self._output)

    @Property(bool, notify=changed)
    def busy(self):
        return bool(self._closing or self._preparing or self._pending_start is not None
                    or self._pending_settings or self._accounts.busy or self._download.running
                    or self._schedule.mutating)

    @Property(bool, notify=changed)
    def closing(self):
        return self._closing

    @Property(bool, notify=changed)
    def shutdownReady(self):
        return self._shutdown_ready

    @Property(bool, notify=changed)
    def canStart(self):
        return not self.busy and self._accounts.connected and bool(self._account and self._output)

    @Property(str, notify=changed)
    def status(self):
        if self._closing:
            return "작업을 정리하고 닫습니다"
        if self._preparing:
            return "저장 폴더를 준비합니다"
        if self._pending_settings:
            return "설정을 저장합니다"
        if self._accounts.busy:
            return self._accounts.status
        if self._schedule.mutating:
            return self._schedule.status
        return self._message or self._download.status

    @Property(str, notify=changed)
    def error(self):
        return self._error or self._download.error or self._accounts.error

    @Property(str, notify=changed)
    def confirmation(self):
        return self._confirmation

    @Property("QVariantMap", notify=changed)
    def reviewItem(self):
        return dict(self._review)

    @Property(bool, notify=changed)
    def galleryChanged(self):
        return self._gallery_dirty

    def _state_changed(self):
        locked = (self._closing or self._preparing or self._pending_start is not None
                  or bool(self._pending_settings) or self._download.running)
        account_locked = locked or self._schedule.mutating
        if self._accounts.locked != account_locked:
            self._accounts.setLocked(account_locked)
        schedule_locked = locked or self._accounts.busy
        if self._schedule.locked != schedule_locked:
            self._schedule.setLocked(schedule_locked)
        self.changed.emit()

    def _persist(self, changes, *, rollback=None):
        self._settings_revision += 1
        revision = self._settings_revision
        self._pending_settings[revision] = (dict(changes), rollback or {})
        self._settings_versions.update({name: revision for name in changes})
        self._preferences.update(changes)
        self._commands.put(("settings", (revision, dict(changes))))
        return revision

    def _settings_finished(self, revision, success):
        pending = self._pending_settings.pop(revision, None)
        if pending is None:
            return
        changes, fields = pending
        if success:
            self._committed_preferences.update(changes)
            if self._error == _SETTINGS_ERROR:
                self._error = ""
        else:
            self._save_after_settings = False
            for name in changes:
                if self._settings_versions.get(name) != revision:
                    continue
                value = self._committed_preferences.get(name)
                if name in self._committed_preferences:
                    self._preferences[name] = value
                else:
                    self._preferences.pop(name, None)
                if name == "confirm_each":
                    self._download.setReviewEnabled(bool(value))
            for field, (name, value) in fields.items():
                if self._settings_versions.get(name) == revision:
                    setattr(self, field, value)
            self._load_library()
        self.preferencesChanged.emit()
        pending_download = self._pending_download
        if pending_download is not None and pending_download[0] == revision:
            self._pending_download = None
            if success and not self._closing:
                self._download.start(pending_download[1])
        if success and not self._pending_settings and self._save_after_settings:
            self._save_after_settings = False
            self.startSave()

    @Slot(str)
    def setAccount(self, value):
        if self.busy or self._accounts.identity:
            return
        account = value.strip().lstrip("@")[:15]
        if account == self._account:
            return
        previous = self._account
        self._account = account
        self._persist({"account": self._account}, rollback={"_account": ("account", previous)})
        self._load_library()
        self._state_changed()

    @Slot(str)
    def setOutputFolder(self, value):
        if self.busy:
            return
        url = QUrl(value)
        if url.isLocalFile():
            value = url.toLocalFile()
        if not value.strip() or "\x00" in value:
            return
        previous = self._output
        self._output = value.strip()
        self._persist({"output_dir": self._output}, rollback={"_output": ("output_dir", previous)})
        self._load_library()
        self._state_changed()

    @Slot(str, "QVariant")
    def setPreference(self, name, value):
        if self._closing or (self.busy and name != "confirm_each"):
            return
        if name in _CHOICES:
            if not isinstance(value, str) or value not in _CHOICES[name]:
                return
        elif name in _BOOLEANS:
            if type(value) is not bool:
                return
        elif name == "max_video_mb":
            if type(value) not in (int, float) or not 0 <= value <= 1_000_000 or value != int(value):
                return
            value = int(value)
        else:
            return
        self._persist({name: value})
        if name == "confirm_each":
            self._download.setReviewEnabled(value)
        if name == "separate_accounts":
            self._load_library()
        self.preferencesChanged.emit()
        self._state_changed()

    def _account_changed(self):
        if self._closing:
            return
        identity = self._accounts.identity
        if identity:
            self._account = identity["handle"]
            locations = self._preferences.get("record_locations", {})
            if isinstance(locations, dict) and isinstance(locations.get(self._account), str):
                self._output = locations[self._account]
            self._persist({"account": self._account, "output_dir": self._output, "auth_mode": "managed"})
        self._load_library()
        self._state_changed()

    def _load_library(self):
        try:
            account = _normal_account(self._account)
        except ValueError:
            account = ""
        self._library.setLibrary(account, self._output, self._preferences["separate_accounts"])

    @Slot(bool)
    def galleryVisible(self, visible):
        self._gallery_visible = visible
        if visible and self._gallery_dirty:
            self.refreshGallery()

    @Slot()
    def refreshGallery(self):
        if self._closing:
            return
        self._gallery_dirty = False
        self._library.refresh()
        self.changed.emit()

    @Slot()
    def startSave(self):
        if self.busy:
            # The Save button commits a typed account immediately before this
            # call. Continue that click after the queued settings commit.
            if self._pending_settings and not (self._closing or self._preparing
                    or self._pending_start is not None or self._accounts.busy
                    or self._download.running or self._schedule.mutating):
                self._save_after_settings = True
            return
        if not self._accounts.connected:
            self.loginRequested.emit()
            return
        identity = self._accounts.identity
        if identity and self._account.lower() != identity["handle"].lower():
            self._error = "연결한 X 계정을 다시 선택해 주세요."
            self.changed.emit()
            return
        snapshot = {**self._preferences, "account": self._account, "output_dir": self._output}
        self._preparing = True
        self._message = self._error = ""
        self._commands.put(("prepare", (snapshot, frozenset())))
        self._state_changed()

    @Slot(bool)
    def confirmStorage(self, approved):
        pending, self._pending_start = self._pending_start, None
        self._confirmation = ""
        if pending is None or self._closing:
            return
        if approved:
            snapshot, consents, choice = pending
            self._preparing = True
            self._commands.put(("prepare", (snapshot, consents | {choice})))
        else:
            self._message = "저장 폴더를 유지했습니다"
        self._state_changed()

    @staticmethod
    def _prepare_save(snapshot, consents):
        from .storage import initialize_storage, migrate_records, resolve_storage, saved_media_missing

        options = JobOptions(**{name: snapshot[name] for name in (
            "account", "output_dir", "folder_mode", "scan_mode", "include_videos", "include_gifs",
            "max_video_mb", "separate_accounts", "gif_format", "gif_size", "confirm_each")}).validate()
        destination = resolve_storage(options.output_dir, options.account, options.separate_accounts)
        locations = snapshot.get("record_locations", {})
        previous = locations.get(options.account) if isinstance(locations, dict) else None
        if isinstance(previous, str) and previous.strip():
            source = resolve_storage(previous, options.account, options.separate_accounts)
            if source.output_dir != destination.output_dir:
                if not source.output_dir.is_dir():
                    if "relocate" not in consents:
                        return {"choice": "relocate", "message": "이전 폴더가 연결되지 않았습니다. 새 위치에서 시작할까요? 기존 파일은 그대로 둡니다."}
                    initialize_storage(destination)
                else:
                    migrate_records(source, destination)
        if saved_media_missing(destination):
            if destination.media_dir.anchor and not Path(destination.media_dir.anchor).exists():
                raise ValueError("저장 드라이브를 연결해 주세요.")
            if "recreate" not in consents:
                return {"choice": "recreate", "message": "저장 폴더를 찾을 수 없습니다. 이 위치에 새 폴더를 만들까요?"}
            options = replace(options, allow_recreate_media=True)
        return {"options": options}

    def _work(self):
        while True:
            kind, payload = self._commands.get()
            if kind == "quit":
                return
            try:
                if kind == "settings":
                    revision, changes = payload
                    self._settings.update(changes)
                    result = {"revision": revision}
                elif kind == "prepare":
                    if self._closing:
                        continue
                    result = self._prepare_save(*payload)
                    result["request"] = payload
                elif kind == "review":
                    from .recycling import file_signature
                    from .thumbcache import cloud_only
                    path = Path(payload["path"])
                    if (not path.is_absolute() or path.is_symlink() or path.resolve() != path
                            or tuple(payload.get("signature", ())) != file_signature(path) or cloud_only(path)):
                        raise ValueError("저장 확인 중 파일이 바뀌었습니다.")
                    result = {"id": str(payload["id"]), "url": QUrl.fromLocalFile(str(path)).toString(),
                              "job_id": payload.get("job_id"),
                              "media_type": str(payload.get("media_type") or "photo"),
                              "author": str(payload.get("author") or ""), "filename": path.name}
                elif kind == "open":
                    path = payload() if callable(payload) else Path(payload)
                    if not path.is_dir():
                        raise ValueError("폴더가 아직 없습니다. 먼저 사진을 저장해 주세요.")
                    result = QUrl.fromLocalFile(str(path))
                else:
                    continue
                self._completed.emit(kind, True, result)
            except Exception as error:
                if kind == "settings":
                    message = _SETTINGS_ERROR
                elif kind == "review":
                    message = "저장 확인 중 파일이 바뀌었습니다."
                elif isinstance(error, ValueError) and kind in {"prepare", "open"}:
                    message = str(error)
                else:
                    message = "작업을 마치지 못했습니다. 저장 위치와 권한을 확인해 주세요."
                result = {"revision": payload[0], "message": message} if kind == "settings" else message
                self._completed.emit(kind, False, result)

    @Slot(str, bool, object)
    def _accept(self, kind, success, value):
        if self._closing:
            return
        if not success:
            if kind == "settings":
                self._settings_finished(value["revision"], False)
                self._error = value["message"]
            else:
                self._error = str(value)
            if kind == "prepare":
                self._preparing = False
            if kind == "review":
                self._download.stop()
            self._state_changed()
            return
        if kind == "settings":
            self._settings_finished(value["revision"], True)
        elif kind == "prepare":
            self._preparing = False
            if "choice" in value:
                snapshot, consents = value["request"]
                self._pending_start = (snapshot, consents, value["choice"])
                self._confirmation = value["message"]
                self._state_changed()
                self.storageConfirmationRequested.emit()
                return
            options = value["options"]
            locations = self._preferences.get("record_locations", {})
            locations = dict(locations) if isinstance(locations, dict) else {}
            locations[options.account] = options.output_dir
            self._account, self._output = options.account, options.output_dir
            revision = self._persist({"account": options.account, "output_dir": options.output_dir,
                                      "record_locations": locations, "auth_mode": "managed"})
            self._message = self._error = ""
            self._pending_download = (revision, options)
            self._load_library()
        elif kind == "review":
            if (self._download.running and not self._download.stopping
                    and value.get("job_id") == self._download.jobId):
                self._review = value
                self.changed.emit()
                self.reviewReady.emit()
        elif kind == "open":
            if not QDesktopServices.openUrl(value):
                self._error = "폴더를 열지 못했습니다."
        self._state_changed()

    def _download_event(self, event):
        if event.get("type") == "saved":
            self._gallery_dirty = True
            self.changed.emit()
        elif event.get("type") == "process_exit" and not self._gallery_visible and not self._closing:
            self.refreshGallery()

    def _prepare_review(self, candidate):
        self._commands.put(("review", dict(candidate)))

    def _dismiss_review(self):
        self._review = {}
        self.changed.emit()
        self.reviewCleared.emit()

    @Slot(str)
    def respondReview(self, action):
        if not self._review:
            return
        ident = self._review["id"]
        if action == "all":
            self.setPreference("confirm_each", False)
            action = "save"
        if action not in {"save", "skip", "never", "stop"}:
            return
        self._dismiss_review()
        self._download.resolveReview(ident, action)

    @Slot()
    def openOutputFolder(self):
        if not self._closing:
            self._commands.put(("open", self._output))

    @Slot()
    def openExtensionFolder(self):
        if not self._closing:
            self._commands.put(("open", _ROOT / "assets/browser-extension/chromium"))

    @Slot()
    def openRecordsFolder(self):
        from .storage import records_root
        if not self._closing:
            self._commands.put(("open", records_root))

    @Slot()
    def requestClose(self):
        if self._closing:
            return
        self._closing = True
        self._pending_start = None
        self._pending_download = None
        self._save_after_settings = False
        self._accounts.setLocked(True)
        self._dismiss_review()
        self._commands.put(("quit", None))
        self._accounts.shutdown()
        self._download.shutdown()
        self._library.shutdown()
        self._browser_setup.shutdown()
        self._schedule.shutdown()
        self._close_timer.start()
        self.changed.emit()

    def _part_closed(self, name):
        self._shutdown_parts.add(name)

    def _finish_close(self):
        if self._shutdown_parts >= {"accounts", "download", "library", "browser_setup", "schedule"} and not self._io_thread.is_alive():
            self._close_timer.stop()
            self._shutdown_ready = True
            self.changed.emit()
            self.shutdownFinished.emit()


def launch(*, startup_probe=None):
    import sys
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtWidgets import QApplication, QMessageBox
    from shiboken6 import delete

    QQuickStyle.setStyle("Basic")
    application = QApplication(sys.argv)
    palette = QPalette()
    for role, color in {
        QPalette.ColorRole.Window: "#101010",
        QPalette.ColorRole.WindowText: "#f4f1f1",
        QPalette.ColorRole.Base: "#121212",
        QPalette.ColorRole.AlternateBase: "#191919",
        QPalette.ColorRole.ToolTipBase: "#191919",
        QPalette.ColorRole.ToolTipText: "#f4f1f1",
        QPalette.ColorRole.Text: "#f4f1f1",
        QPalette.ColorRole.Button: "#252525",
        QPalette.ColorRole.ButtonText: "#f4f1f1",
        QPalette.ColorRole.BrightText: "#ff8791",
        QPalette.ColorRole.Highlight: "#d93645",
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.Link: "#d93645",
        QPalette.ColorRole.LinkVisited: "#d93645",
        QPalette.ColorRole.Light: "#353535",
        QPalette.ColorRole.Midlight: "#252525",
        QPalette.ColorRole.Mid: "#353535",
        QPalette.ColorRole.Dark: "#101010",
        QPalette.ColorRole.Shadow: "#000000",
        QPalette.ColorRole.PlaceholderText: "#b9afb1",
    }.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText, QPalette.ColorRole.PlaceholderText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#b9afb1"))
    application.setPalette(palette)
    application.setApplicationName("LuvBox")
    application.setApplicationVersion(__version__)
    application.setOrganizationName("LuvBox")
    application.setWindowIcon(QIcon(str(_ROOT / "assets/icons/luvbox.ico")))
    family = "Pretendard"
    registered = False
    for filename in ("Pretendard-Regular.ttf", "Pretendard-Bold.ttf"):
        font_id = QFontDatabase.addApplicationFont(str(_ROOT / "assets/fonts/Pretendard" / filename))
        registered = registered or (
            font_id >= 0 and family in QFontDatabase.applicationFontFamilies(font_id))
    if not registered:
        family = "Malgun Gothic"
    ui_font = QFont(family)
    ui_font.setPixelSize(14)
    ui_font.setWeight(QFont.Weight.Normal)
    ui_font.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
    application.setFont(ui_font)
    try:
        controller = ApplicationController()
    except Exception:
        if startup_probe is not None:
            raise
        QMessageBox.critical(None, "LuvBox", "기존 앱 데이터를 열지 못했습니다. 다른 LuvBox 창을 닫고 다시 실행해 주세요.")
        return 1
    wheel_input = WheelInputFilter()
    engine = QQmlApplicationEngine()
    engine.addImageProvider("thumbs", controller._library.provider)
    engine.setInitialProperties({"galleryModel": controller._library.model,
                                 "wheelInputFilter": wheel_input, "appController": controller,
                                 "uiFontFamily": family})
    engine.load(_ROOT / "assets/qt/MainWindow.qml")
    if not engine.rootObjects():
        controller.shutdownFinished.connect(application.quit)
        controller.requestClose()
        application.exec()
        wheel_input.close()
        delete(engine)
        if startup_probe is None:
            QMessageBox.critical(None, "LuvBox", "화면을 열지 못했습니다. Qt 패키지와 앱 파일을 확인해 주세요.")
        return 1
    window = engine.rootObjects()[0]
    controller.shutdownFinished.connect(window.close)
    available = window.screen().availableGeometry()
    window.setWidth(min(window.width(), max(window.minimumWidth(), available.width() - 24)))
    window.setHeight(min(window.height(), max(window.minimumHeight(), available.height() - 48)))
    if startup_probe is not None:
        startup_probe.attach(application, controller, window)
    result = application.exec()
    wheel_input.close()
    delete(engine)
    return result
