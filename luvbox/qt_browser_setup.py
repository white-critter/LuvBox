import os
import subprocess
import tempfile
import threading
from pathlib import Path
from queue import Queue

from PySide6.QtCore import QObject, Property, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication

from .app_settings import SettingsRepository
from .browser_discovery import available_browsers, open_browser_url
from .file_mutex import replace_file
from .settings import app_directory


_WORKING = {
    "discover": "사용할 브라우저를 확인하는 중…",
    "select": "브라우저 선택을 저장하는 중…",
    "manager": "확장 관리 화면을 여는 중…",
    "copy": "확장 폴더를 확인하는 중…",
    "folder": "확장 폴더를 여는 중…",
    "x": "X를 여는 중…",
    "install": "확장 설치를 준비하는 중…",
    "prepare": "확장 폴더를 준비하는 중…",
}
_ERRORS = {
    "discover": "브라우저를 확인하지 못했습니다. Chrome·Edge·Firefox 설치 상태를 확인해 주세요.",
    "select": "브라우저 선택을 저장하지 못했습니다. 이전 선택을 유지합니다. 사용자 폴더 권한을 확인해 주세요.",
    "manager": "확장 관리를 열지 못했습니다. 주소창에서 chrome://extensions 또는 edge://extensions를 여세요.",
    "copy": "확장 폴더를 준비하지 못했습니다. LuvBox 파일과 사용자 폴더 권한을 확인해 주세요.",
    "folder": "확장 폴더를 열지 못했습니다. LuvBox 파일과 사용자 폴더 권한을 확인해 주세요.",
    "x": "X를 열지 못했습니다. 브라우저에서 https://x.com/home을 여세요.",
    "install": "확장 설치를 준비하지 못했습니다. 아래 단계로 폴더를 직접 선택해 주세요.",
    "prepare": "확장 폴더를 준비하지 못했습니다. LuvBox 파일과 사용자 폴더 권한을 확인해 주세요.",
}
_COPY_ERRORS = {
    "manager": "주소를 복사하지 못했습니다. 주소창에 chrome://extensions 또는 edge://extensions를 입력해 주세요.",
    "copy": "폴더 경로를 복사하지 못했습니다. ‘폴더 열기’를 사용해 주세요.",
    "install": "폴더 경로를 복사하지 못했습니다. 표시된 경로를 직접 복사해 주세요.",
}
_MANAGERS = {"chrome": "chrome://extensions", "edge": "edge://extensions",
             "firefox": "about:debugging#/runtime/this-firefox"}
_EXTENSION_FILES = ("manifest.json", "background.js", "profile.js", "connect.js", "connect.html",
                    "connect.css", "extension-page.js")


def plan_extension_launch(browser, running, folder):
    """Describe manual installation; never claim that a launch installed it."""
    if browser is None:
        return {"status": "지원하는 브라우저를 찾지 못했습니다. Chrome·Edge·Firefox 설치 상태를 확인해 주세요."}
    if browser.name.lower() == "firefox.exe":
        return {"status": "Firefox에서 ‘임시 부가 기능 로드’를 누른 뒤 표시된 폴더의 manifest.json을 선택하세요. "
                "임시 확장은 Firefox를 다시 시작하면 제거됩니다."}
    return {"status": "확장은 자동으로 설치되지 않습니다. 확장 관리에서 개발자 모드를 켜고 한 번만 직접 불러와 주세요. "
            "폴더 창이 ‘문서’에서 열려도 그 폴더를 선택하지 마세요. 복사된 경로를 주소창에 붙여 넣고, "
            "manifest.json이 있는 폴더를 선택하세요."}


class BrowserSetupController(QObject):
    changed = Signal()
    shutdownFinished = Signal()
    _completed = Signal(str, object)

    def __init__(self, parent=None, *, settings=None):
        super().__init__(parent)
        self._busy = False
        self._status = ""
        self._error = ""
        self._extension_path = ""
        self._settings = settings if settings is not None else SettingsRepository()
        self._browsers = []
        self._selected_browser = ""
        self._prepare_pending = False
        self._closing = False
        self._shutdown_emitted = False
        self._stop = threading.Event()
        self._commands = Queue()
        self._completed.connect(self._receive, Qt.ConnectionType.QueuedConnection)
        self._close_timer = QTimer(self)
        self._close_timer.setInterval(25)
        self._close_timer.timeout.connect(self._finish_shutdown)
        self._thread = threading.Thread(target=self._work, name="qt-browser-setup", daemon=True)
        try:
            self._thread.start()
        except RuntimeError:
            self._thread = None
            self._error = "설정 도우미를 시작하지 못했습니다. LuvBox를 다시 열어 주세요."
        else:
            self._start("discover")

    @Property(bool, notify=changed)
    def busy(self):
        return self._busy

    @Property(str, notify=changed)
    def status(self):
        return self._status

    @Property(str, notify=changed)
    def error(self):
        return self._error

    @Property(str, notify=changed)
    def extensionPath(self):
        return self._extension_path

    @Property("QVariantList", notify=changed)
    def browsers(self):
        return [dict(item) for item in self._browsers]

    @Property(str, notify=changed)
    def selectedBrowser(self):
        return self._selected_browser

    @Property(str, notify=changed)
    def browserLabel(self):
        return next((item["label"] for item in self._browsers
                     if item["id"] == self._selected_browser), "브라우저")

    @Property(str, notify=changed)
    def managerAddress(self):
        return _MANAGERS.get(self._selected_browser, "")

    @Property(str, notify=changed)
    def extensionKind(self):
        return "firefox" if self._selected_browser == "firefox" else "chromium"

    @Slot(str, result=bool)
    def setBrowser(self, identifier):
        if identifier == self._selected_browser:
            return False
        if not any(item["id"] == identifier for item in self._browsers):
            return False
        return self._start("select", identifier)

    def _start(self, operation, identifier=None):
        if self._closing or self._busy or self._thread is None:
            return False
        self._busy = True
        self._error = ""
        self._status = _WORKING[operation]
        self.changed.emit()
        self._commands.put((operation, self._selected_browser if identifier is None else identifier))
        return True

    @Slot()
    def openManager(self):
        self._start("manager")

    @Slot()
    def copyExtensionPath(self):
        self._start("copy")

    @Slot()
    def openExtensionFolder(self):
        self._start("folder")

    @Slot()
    def openX(self):
        self._start("x")

    @Slot()
    def prepareExtension(self):
        if not self._extension_path:
            if self._busy:
                self._prepare_pending = True
            else:
                self._start("prepare")

    @staticmethod
    def _extension_folder(kind="chromium"):
        """The unpacked extension, copied outside the versioned app folder.

        Chrome and Edge keep reading an unpacked extension from the folder it
        was loaded from, while each LuvBox version has a folder of its own.
        Loading this copy keeps the extension after an old version is deleted;
        every version refreshes its files.
        """
        if kind not in {"chromium", "firefox"}:
            raise ValueError("unsupported extension variant")
        bundled = Path(__file__).resolve().parents[1] / "assets" / "browser-extension" / kind
        if not (bundled / "manifest.json").is_file():
            raise OSError("missing extension assets")
        target = app_directory() / "browser-extension" / kind
        for folder in (target.parent, target):
            if folder.is_symlink() or getattr(folder, "is_junction", lambda: False)():
                raise OSError("linked extension folder")
        target.mkdir(parents=True, exist_ok=True)
        for name in _EXTENSION_FILES:
            content = (bundled / name).read_bytes()
            destination = target / name
            if destination.is_symlink():
                raise OSError("linked extension file")
            try:
                if destination.read_bytes() == content:
                    continue
            except FileNotFoundError:
                pass
            descriptor, staged = tempfile.mkstemp(prefix=".extension-", dir=target)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                replace_file(staged, destination)
            finally:
                Path(staged).unlink(missing_ok=True)
        return target

    @staticmethod
    def _reveal(folder):
        """Show manifest.json itself so the load dialog is not left in Documents."""
        manifest = Path(folder) / "manifest.json"
        target = manifest if manifest.is_file() else Path(folder)
        subprocess.Popen(["explorer.exe", "/select," + str(target)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)

    def _perform(self, operation, identifier=None):
        if identifier is None:
            identifier = self._selected_browser
        if operation == "discover":
            choices = available_browsers()
            saved = self._settings.load().get("connection_browser")
            selected = saved if any(item["id"] == saved for item in choices) else (
                choices[0]["id"] if choices else "")
            return {"browsers": choices, "selected": selected,
                    "status": "연결할 브라우저를 선택하세요. 확장을 설치한 일반 프로필을 사용합니다." if selected else
                              "지원하는 브라우저를 찾지 못했습니다. Chrome·Edge·Firefox를 설치한 뒤 앱을 다시 열어 주세요."}
        if identifier not in _MANAGERS:
            raise ValueError("no supported browser selected")
        kind = "firefox" if identifier == "firefox" else "chromium"
        if operation == "select":
            if not any(item["id"] == identifier for item in available_browsers()):
                raise ValueError("selected browser unavailable")
            folder = self._extension_folder(kind)
            if self._stop.is_set():
                return {}
            self._settings.update({"connection_browser": identifier})
            return {"selected": identifier, "path": str(folder),
                    "status": "브라우저 선택을 저장했습니다. 확장을 설치한 일반 프로필에서 연결하세요."}
        if operation == "manager":
            if self._stop.is_set():
                return {}
            manager = _MANAGERS[identifier]
            # Browser-internal pages do not reliably open from a command line.
            # Open this selected browser, then let the person paste its address.
            if not open_browser_url(identifier):
                raise OSError("selected browser unavailable")
            return {"status": f"선택한 브라우저를 열고 {manager} 주소를 복사했습니다. 주소창에 붙여 넣어 확장 관리 화면을 여세요.",
                    "clipboard": manager}
        if operation == "x":
            if self._stop.is_set():
                return {}
            if not open_browser_url(identifier, "https://x.com/home"):
                raise OSError("selected browser unavailable")
            return {"status": "선택한 브라우저에서 X를 열었습니다. 확장을 설치한 일반 프로필에서 로그인하세요."}
        folder = self._extension_folder(kind)
        if self._stop.is_set():
            return {}
        if operation == "install":
            filename = {"chrome": "chrome.exe", "edge": "msedge.exe", "firefox": "firefox.exe"}[identifier]
            plan = plan_extension_launch(Path(filename), False, folder)
            return {"status": plan["status"], "path": str(folder), "clipboard": str(folder),
                    "reveal": str(folder)}
        if operation == "prepare":
            return {"status": "Firefox에서는 이 폴더의 manifest.json을 선택하세요." if kind == "firefox" else
                              "아래 경로만 선택하세요. ‘문서’ 폴더를 고르면 매니페스트 오류가 납니다.",
                    "path": str(folder)}
        if operation == "copy":
            return {"status": "확장 폴더 경로를 복사했습니다. 폴더 창 주소창에 붙여 넣고 Enter를 누르세요.",
                    "clipboard": str(folder), "path": str(folder)}
        return {"status": "manifest.json을 선택한 폴더를 열었습니다. 확장 로드 창에서는 ‘문서’가 아니라 표시된 폴더를 사용하세요.",
                "reveal": str(folder), "path": str(folder)}

    def _work(self):
        while True:
            command = self._commands.get()
            if command is None or self._stop.is_set():
                return
            operation, identifier = command
            try:
                result = self._perform(operation, identifier)
            except Exception:
                result = {"error": _ERRORS[operation]}
            self._completed.emit(operation, result)

    @Slot(str, object)
    def _receive(self, operation, result):
        if self._closing:
            return
        self._busy = False
        self._status = result.get("status", "")
        self._error = result.get("error", "")
        if "browsers" in result:
            self._browsers = result["browsers"]
        if "selected" in result:
            self._selected_browser = result["selected"]
        if result.get("path"):
            self._extension_path = result["path"]
        reveal = result.get("reveal")
        if reveal:
            try:
                self._reveal(reveal)
            except OSError:
                self._status = ""
                self._error = _ERRORS[operation]
        if "clipboard" in result:
            clipboard = QGuiApplication.clipboard()
            if clipboard is None:
                self._status = ""
                self._error = _COPY_ERRORS.get(operation, _ERRORS[operation])
            else:
                clipboard.setText(result["clipboard"])
        self.changed.emit()
        if self._prepare_pending:
            self._prepare_pending = False
            if self._selected_browser and not self._extension_path:
                self._start("prepare")

    @Slot()
    def shutdown(self):
        if self._closing:
            return
        self._closing = True
        self._stop.set()
        self.changed.emit()
        self._commands.put(None)
        self._finish_shutdown()
        if not self._shutdown_emitted:
            self._close_timer.start()

    def _finish_shutdown(self):
        if self._shutdown_emitted or (self._thread is not None and self._thread.is_alive()):
            return
        if self._thread is not None:
            self._thread.join(timeout=0)
            self._thread = None
        self._busy = False
        self._shutdown_emitted = True
        self._close_timer.stop()
        self.changed.emit()
        self.shutdownFinished.emit()
