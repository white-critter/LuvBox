import os
import subprocess
import threading
from pathlib import Path
from queue import Queue

from PySide6.QtCore import QObject, Property, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication

from .browser_discovery import find_default_browser, find_edge


_WORKING = {
    "manager": "확장 관리 화면을 여는 중…",
    "copy": "확장 폴더를 확인하는 중…",
    "folder": "확장 폴더를 여는 중…",
    "x": "X를 여는 중…",
    "install": "브라우저에 확장을 불러오는 중…",
}
_ERRORS = {
    "manager": "확장 관리를 열지 못했습니다. 주소창에서 chrome://extensions 또는 edge://extensions를 여세요.",
    "copy": "확장 폴더를 찾지 못했습니다. LuvBox의 assets 폴더를 확인해 주세요.",
    "folder": "확장 폴더를 열지 못했습니다. LuvBox의 assets 폴더를 확인해 주세요.",
    "x": "X를 열지 못했습니다. 브라우저에서 https://x.com/home을 여세요.",
    "install": "확장을 불러오지 못했습니다. 아래 단계로 폴더를 직접 선택해 주세요.",
}


def plan_extension_launch(browser, running, folder):
    """Load the unpacked extension into the user's browser for this launch.

    Chrome and Edge ignore ``--load-extension`` when that browser is already
    open, and closing the window can drop the extension. The manual load stays
    available for a copy that remains installed.
    """
    if browser is None:
        return {"status": "Chrome 또는 Edge를 찾지 못했습니다. 아래 단계로 폴더를 직접 불러 주세요."}
    if running:
        return {"status": f"{browser.name}가 이미 켜져 있습니다. 그 브라우저를 모두 닫은 뒤 다시 누르면 확장을 함께 엽니다. 계속 쓰려면 아래 단계로 한 번만 불러 두세요."}
    return {"status": "브라우저를 확장과 함께 열었습니다. 이 창을 닫으면 확장이 다시 빠질 수 있어, 계속 쓰려면 아래 단계로 한 번만 불러 두세요.",
            "command": [str(browser), "--load-extension=" + str(folder)]}


class BrowserSetupController(QObject):
    changed = Signal()
    shutdownFinished = Signal()
    _completed = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False
        self._status = ""
        self._error = ""
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

    @Property(bool, notify=changed)
    def busy(self):
        return self._busy

    @Property(str, notify=changed)
    def status(self):
        return self._status

    @Property(str, notify=changed)
    def error(self):
        return self._error

    def _start(self, operation):
        if self._closing or self._busy or self._thread is None:
            return
        self._busy = True
        self._error = ""
        self._status = _WORKING[operation]
        self.changed.emit()
        self._commands.put(operation)

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
    def installExtension(self):
        self._start("install")

    @staticmethod
    def _extension_folder():
        folder = Path(__file__).resolve().parents[1] / "assets" / "browser-extension" / "chromium"
        if not folder.is_dir() or not (folder / "manifest.json").is_file():
            raise OSError("missing extension assets")
        return folder

    @staticmethod
    def _chromium():
        default = find_default_browser()
        if default is not None and default.name.lower() in {"chrome.exe", "msedge.exe"}:
            return default
        edge = find_edge()
        if edge is not None:
            return edge
        candidates = []
        for variable, parts in (
            ("PROGRAMFILES", ("Google", "Chrome", "Application", "chrome.exe")),
            ("PROGRAMFILES(X86)", ("Google", "Chrome", "Application", "chrome.exe")),
            ("LOCALAPPDATA", ("Google", "Chrome", "Application", "chrome.exe")),
        ):
            base = os.environ.get(variable)
            if base:
                candidates.append(Path(base).joinpath(*parts))
        return next((path for path in candidates if path.is_file()), None)

    @staticmethod
    def _running(image):
        if os.name != "nt" or not image:
            return False
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return image.lower() in (result.stdout or "").lower()

    def _perform(self, operation):
        if operation == "install":
            folder = self._extension_folder()
            browser = self._chromium()
            if self._stop.is_set():
                return {}
            plan = plan_extension_launch(browser, self._running(browser.name) if browser else False, folder)
            command = plan.get("command")
            if command and not self._stop.is_set():
                subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True)
            return {"status": plan["status"]}
        if operation == "manager":
            browser = find_default_browser()
            if self._stop.is_set():
                return {}
            if browser is None:
                return {"status": "관리 화면을 직접 열어 주세요. Chrome: chrome://extensions · Edge: edge://extensions. Firefox 설치는 확장 폴더 상위의 README를 확인하세요."}
            manager = {"chrome.exe": "chrome://extensions", "msedge.exe": "edge://extensions"}.get(browser.name.lower())
            if manager is None:
                return {"status": "이 브라우저의 관리 화면은 직접 열어 주세요. Firefox 설치는 확장 폴더 상위의 README를 확인하세요."}
            subprocess.Popen([str(browser), manager], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
            return {"status": "확장 관리 화면에서 개발자 모드를 켜 주세요."}
        if operation == "x":
            if self._stop.is_set():
                return {}
            return {"status": "확장을 설치한 브라우저 프로필에서 X에 로그인해 주세요.",
                    "url": QUrl("https://x.com/home")}
        folder = self._extension_folder()
        if self._stop.is_set():
            return {}
        if operation == "copy":
            return {"status": "확장 폴더 경로를 복사했습니다.", "path": str(folder)}
        return {"status": "열린 chromium 폴더를 확장 로드 창에서 선택해 주세요.",
                "url": QUrl.fromLocalFile(str(folder))}

    def _work(self):
        while True:
            operation = self._commands.get()
            if operation is None or self._stop.is_set():
                return
            try:
                result = self._perform(operation)
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
        if "url" in result and not QDesktopServices.openUrl(result["url"]):
            self._status = ""
            self._error = _ERRORS[operation]
        if operation == "copy" and "path" in result:
            clipboard = QGuiApplication.clipboard()
            if clipboard is None:
                self._status = ""
                self._error = "폴더 경로를 복사하지 못했습니다. ‘폴더 열기’를 사용해 주세요."
            else:
                clipboard.setText(result["path"])
        self.changed.emit()

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
