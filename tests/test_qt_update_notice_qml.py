"""The update notice in the product window: shown on both screens, installed by one click.

The real MainWindow.qml and application controller run against a local release
server and a stand-in LuvBox.exe in a temporary folder. `--screenshots DIR`
keeps pictures of each step; without QT_QPA_PLATFORM it opens a real window.
"""

from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


def scene(screenshot_dir=None):
    from contextlib import ExitStack
    import tempfile
    import time
    from unittest.mock import patch

    from PySide6.QtCore import QObject, QPoint, QPointF, Qt, qInstallMessageHandler
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    sys.path.insert(0, str(ROOT))
    from luvbox import updater
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_updater import UpdateController
    from luvbox.qt_wheel import WheelInputFilter
    from tests.release_server import ReleaseServer, make_program

    QQuickStyle.setStyle("Basic")
    application = QApplication([])
    for filename in ("Pretendard-Regular.ttf", "Pretendard-Bold.ttf"):
        QFontDatabase.addApplicationFont(str(ROOT / "assets/fonts/Pretendard" / filename))
    font = QFont("Pretendard")
    font.setPixelSize(14)
    application.setFont(font)
    messages = []
    previous = qInstallMessageHandler(lambda _kind, _context, message: messages.append(message))
    resources = ExitStack()
    server = ReleaseServer()
    resources.callback(server.close)
    report = {"platform": application.platformName()}

    def wait(predicate, what, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            application.processEvents()
            if predicate():
                return
            time.sleep(.003)
        raise AssertionError("The scene did not reach: " + what)

    with resources:
        folder = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-update-notice-")))
        launcher = folder / "place" / "LuvBox.exe"
        launcher.parent.mkdir()
        launcher.write_bytes(make_program("1.8.0"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": str(folder)}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        resources.enter_context(patch(
            "luvbox.qt_app.UpdateController",
            side_effect=lambda parent, **options: UpdateController(
                parent, current="1.8.0", source=server.source, launcher=lambda: launcher,
                folder=folder / "updates", **options)))
        controller = ApplicationController()
        updates = controller.updates
        wheel = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", controller.library.provider)
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel})
        engine.load(ROOT / "assets/qt" / "MainWindow.qml")
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        controller.shutdownFinished.connect(window.close)   # as luvbox.qt_app.launch does
        window.setWidth(1080)
        window.setHeight(760)
        window.requestActivate()
        wait(lambda: not controller.busy, "idle application")

        def find(name):
            value = window.findChild(QObject, name)
            assert value is not None, name
            return value

        def bounds(item):
            origin = item.mapToScene(QPointF(0, 0))
            return [round(origin.x()), round(origin.y()), round(item.width()), round(item.height())]

        def click(name):
            item = find(name)
            assert item.property("visible") and item.property("enabled"), name
            left, top, width, height = bounds(item)
            QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                             QPoint(left + width // 2, top + height // 2))
            QTest.qWait(40)

        def screenshot(name):
            if screenshot_dir:
                destination = Path(screenshot_dir)
                destination.mkdir(parents=True, exist_ok=True)
                assert window.grabWindow().save(str(destination / (name + ".png")))

        notice = find("updateNotice")
        title = find("updateNoticeTitle")
        # Nothing was asked of the server by opening the window, and nothing is shown.
        assert not notice.property("visible") and notice.height() == 0
        assert server.requests == []

        server.publish("1.9.0", notes="### 주요 변경 사항\n\n- **새 기능**: 첫 번째 변경\n- 두 번째 변경\n")
        updates.checkNow()
        wait(lambda: updates.state == "available", "a found release")
        QTest.qWait(80)
        assert notice.property("visible") and window.property("currentScreen") == 0
        assert title.property("text") == "새 버전이 나왔어요 · 1.9.0"
        assert "지금 버전 1.8.0" in find("updateNoticeHint").property("text")
        for name in ("updateInstallButton", "updateLaterButton", "updateNotesToggle"):
            assert find(name).property("visible"), name
        assert not find("updateRestartButton").property("visible")
        assert not find("updateReleasePageButton").property("visible")
        report["notice_save"] = bounds(notice)
        screenshot("1-notice-save")
        # The save form starts below the notice instead of under it.
        assert bounds(find("saveScroll"))[1] >= report["notice_save"][1] + report["notice_save"][3]
        click("updateNotesToggle")
        notes = find("updateNotes")
        assert notes.property("visible") and notes.height() > 20
        assert updates.notes == "· 새 기능: 첫 번째 변경\n· 두 번째 변경"
        screenshot("2-notice-changes")
        click("updateNotesToggle")
        assert not notes.property("visible")

        # The same notice sits above the gallery, also in the smallest window.
        window.setProperty("currentScreen", 1)
        wait(lambda: not controller.library.model.busy, "gallery")
        QTest.qWait(80)
        assert notice.property("visible")
        screenshot("3-notice-gallery")
        window.setWidth(680)
        window.setHeight(480)
        QTest.qWait(120)
        small = bounds(notice)
        report["notice_small"] = small
        assert small[0] >= 0 and small[0] + small[2] <= 680
        install = bounds(find("updateInstallButton"))
        assert install[0] + install[2] <= small[0] + small[2] and install[1] + install[3] <= small[1] + small[3]
        assert title.height() >= title.property("implicitHeight") - 1
        screenshot("4-notice-small-window")
        window.setWidth(1080)
        window.setHeight(760)
        QTest.qWait(80)

        # "나중에" hides it for this session; a different release brings it back.
        click("updateLaterButton")
        assert not notice.property("visible")
        assert "새 버전이 나왔어요 · 1.9.0" in updates.status
        program = server.publish("1.9.1", make_program("1.9.1", os.urandom(1024 * 1024)),
                                 notes="### 주요 변경 사항\n\n- 다음 변경\n")
        updates.checkNow()
        wait(lambda: updates.version == "1.9.1" and updates.state == "available", "the next release")
        QTest.qWait(60)
        assert notice.property("visible") and title.property("text") == "새 버전이 나왔어요 · 1.9.1"

        # One click: fetch, verify, replace. The window stays usable meanwhile.
        server.slow.clear()
        before = len(server.requests)
        click("updateInstallButton")
        wait(lambda: len(server.requests) >= before + 2, "the download request")
        assert updates.state == "downloading"
        assert title.property("text").startswith("1.9.1 받는 중")
        assert find("updateProgress").property("visible") and find("updateCancelButton").property("visible")
        assert not find("updateInstallButton").property("visible")
        screenshot("5-downloading")
        window.setProperty("currentScreen", 0)
        QTest.qWait(40)
        assert notice.property("visible")
        server.slow.set()
        wait(lambda: updates.state == "ready", "the installed release")
        QTest.qWait(80)
        assert launcher.read_bytes() == program
        assert sorted(path.name for path in launcher.parent.iterdir()) == ["LuvBox.exe"]
        assert title.property("text") == "1.9.1 준비 완료 · 다시 시작하면 바뀌어요"
        restart = find("updateRestartButton")
        assert restart.property("visible") and restart.property("enabled")
        assert not find("updateCancelButton").property("visible")
        screenshot("6-ready")

        # Settings show the same state, the switch and the manual button.
        find("preferencesDialog").open()
        QTest.qWait(120)
        status = find("updateStatusLabel")
        assert "지금 버전 1.8.0" in status.property("text") and "1.9.1 준비 완료" in status.property("text")
        switch = find("updateAutoCheckSwitch")
        assert switch.property("checked") is True
        assert not find("updateCheckButton").property("enabled")
        click("updateAutoCheckSwitch")
        wait(lambda: updates.autoCheck is False, "the switch")
        assert switch.property("checked") is False
        screenshot("7-settings")
        find("preferencesDialog").close()
        QTest.qWait(80)

        # Restart: the window closes as usual, and only then is the launcher due to start.
        assert not updates.restart_pending
        click("updateRestartButton")
        wait(lambda: controller.shutdownReady and not window.isVisible(), "the closed window")
        assert updates.restart_pending
        report["requests"] = [path for path, _headers in server.requests]
        warnings = [message for message in messages if any(
            mark in message for mark in (".qml", "QQml", "ReferenceError", "TypeError", "Binding loop"))]
        assert not warnings, warnings
        wheel.close()
        controller._library.provider = None
        report.update(installed=True, restart_requested=True)
    qInstallMessageHandler(previous)
    return report


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class UpdateNoticeQmlTests(unittest.TestCase):
    def test_notice_appears_on_both_screens_and_one_click_installs(self):
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
                           PYTHONIOENCODING="utf-8")
        run = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"], cwd=ROOT,
                             env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=120)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertTrue(report["installed"] and report["restart_requested"])
        self.assertEqual(report["requests"], [
            "/latest", "/latest", "/download/v1.9.1/SHA256-1.9.1.txt", "/download/v1.9.1/LuvBox.exe"])


if __name__ == "__main__":
    if "--scene" in sys.argv:
        shots = sys.argv[sys.argv.index("--screenshots") + 1] if "--screenshots" in sys.argv else None
        print(json.dumps(scene(shots), ensure_ascii=False))
    else:
        unittest.main()
