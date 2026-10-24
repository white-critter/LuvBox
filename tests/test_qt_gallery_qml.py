from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def check_scene(scenario):
    from PySide6.QtCore import QCoreApplication, QMetaObject, QObject, QPoint, QPointF, Qt, QtMsgType, Signal, qInstallMessageHandler
    from PySide6.QtGui import QGuiApplication, QWheelEvent
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtQuickControls2 import QQuickStyle
    from shiboken6 import delete

    sys.path.insert(0, str(ROOT))
    from luvbox.qt_gallery import GalleryModel
    from luvbox.qt_wheel import WheelInputFilter

    class RecordingProvider(QObject):
        ready = Signal(str)

        def __init__(self):
            super().__init__()
            self.calls = []

        def schedule(self, keys, width, height):
            self.calls.append((set(keys), width, height))

        def reset_notifications(self):
            pass

    QQuickStyle.setStyle("Fusion")
    application = QGuiApplication([])
    messages = []

    def record_message(kind, _context, message):
        if kind in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
            messages.append(message)

    previous_handler = qInstallMessageHandler(record_message)
    provider = RecordingProvider()
    rows = [{"key": f"photo{index}", "caption": str(index)} for index in range(157)]
    model = GalleryModel(lambda query, offset, limit: (len(rows), rows[offset:offset + limit]),
                         provider=provider)
    wheel_input = WheelInputFilter()
    engine = QQmlApplicationEngine()
    engine.setInitialProperties({"galleryModel": model, "wheelInputFilter": wheel_input})
    window = None
    checks = []

    def settle(duration=0.18):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            application.processEvents()
            time.sleep(0.002)

    def visible_keys():
        return {child.property("key") for child in grid.property("contentItem").childItems()
                if child.objectName() == "galleryTile" and child.property("activeTile")
                and child.y() + child.height() > grid.property("contentY")
                and child.y() < grid.property("contentY") + grid.height()}

    def assert_visible_scheduled(label, since):
        settle()
        visible = visible_keys()
        if not visible:
            raise AssertionError(f"{label}: no visible delegates")
        if len(provider.calls) <= since:
            raise AssertionError(f"{label}: viewport did not schedule after geometry changed")
        missing = visible - provider.calls[-1][0]
        if missing:
            raise AssertionError(f"{label}: visible photos not scheduled: {sorted(missing)}; "
                                 f"scheduled={sorted(provider.calls[-1][0])}; "
                                 f"contentY={grid.property('contentY')}, originY={grid.property('originY')}")
        checks.append(label)
        return visible

    try:
        engine.load(ROOT / "assets/qt/GalleryPreview.qml")
        if not engine.rootObjects():
            raise AssertionError(f"scene failed to load: {messages}")
        window = engine.rootObjects()[0]
        grid = window.findChild(QQuickItem, "galleryGrid")
        settle()
        if window.property("currentScreen") != 0:
            raise AssertionError("save must remain the default screen")
        window.setProperty("currentScreen", 1)
        settle()
        if scenario == "scroll":
            for density in range(3):
                window.setProperty("densityRequest", density)
                settle(0.4)
                height = grid.property("cellHeight")
                grid.setProperty("contentY", grid.property("originY") + height * 12 + 7)
                settle()
                before = len(provider.calls)
                grid.setProperty("contentY", grid.property("originY") + height * 7 + 19)
                assert_visible_scheduled(f"upward-density-{density}", before)
                before = len(provider.calls)
                grid.setProperty("contentY", grid.property("originY") + grid.property("contentHeight") - grid.height())
                assert_visible_scheduled(f"partial-last-row-density-{density}", before)
        elif scenario == "resize":
            window.setProperty("densityRequest", 0)
            window.setWidth(680)
            window.setHeight(480)
            settle()
            before = len(provider.calls)
            window.setHeight(900)
            assert_visible_scheduled("height-expansion-without-scrolling", before)
            before = len(provider.calls)
            window.setWidth(1250)
            assert_visible_scheduled("width-expansion-without-scrolling", before)
        elif scenario == "anchor":
            window.setProperty("densityRequest", 0)
            settle(0.4)
            grid.setProperty("contentY", grid.property("originY") + grid.property("cellHeight") * 18 + 19)
            settle()
            for density in (1, 2, 1, 0):
                previous_top = min(int(key.removeprefix("photo")) for key in visible_keys())
                before = len(provider.calls)
                window.setProperty("densityRequest", density)
                settle(0.4)
                assert_visible_scheduled(f"deep-anchor-density-{density}", before)
                if window.property("heldAnchor") != previous_top or f"photo{previous_top}" not in visible_keys():
                    raise AssertionError(f"density {density}: previous top photo {previous_top} was lost")
        elif scenario == "wide":
            window.setProperty("densityRequest", 0)
            settle(0.4)
            for width, height in ((2560, 1440), (3840, 2160)):
                before = len(provider.calls)
                window.setWidth(width)
                window.setHeight(height)
                visible = assert_visible_scheduled(f"wide-viewport-{width}-{height}", before)
                if len(visible) <= 48:
                    raise AssertionError("large viewport did not exercise more than 48 visible photos")
        elif scenario == "wheel":
            wheel_motion = window.findChild(QQuickItem, "galleryWheel")
            wheel_motion.setProperty("wheelLines", 3)

            def send_wheel(pixels=0):
                position = grid.mapToScene(QPointF(grid.width() / 2, grid.height() / 2))
                event = QWheelEvent(position, QPointF(window.mapToGlobal(position.toPoint())),
                                    QPoint(0, pixels), QPoint(0, -120), Qt.MouseButton.NoButton,
                                    Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
                event.setTimestamp(int(time.monotonic() * 1000))
                QCoreApplication.sendEvent(window, event)

            start = grid.property("contentY")
            send_wheel()
            settle(0.15)
            if abs(grid.property("contentY") - start - 60) > 1:
                raise AssertionError("one mouse notch must be handled once above the tile MouseArea")
            checks.append("tile-wheel-routing")
            for _ in range(4):
                send_wheel()
                settle(0.025)
            if not wheel_motion.property("motionActive") or wheel_motion.property("multiplier") <= 1:
                raise AssertionError("rapid mouse notches did not accelerate")
            window.setProperty("densityRequest", 0)
            settle(0.4)
            if wheel_motion.property("motionActive") or wheel_motion.property("multiplier") != 1:
                raise AssertionError("density change did not cancel pending wheel motion")
            checks.append("density-cancels-wheel")
            native_distances = []
            for enabled in (False, True):
                wheel_motion.setProperty("enabled", enabled)
                QMetaObject.invokeMethod(grid, "cancelFlick")
                grid.setProperty("contentY", grid.property("originY") + 500)
                settle()
                start = grid.property("contentY")
                send_wheel(pixels=-15)
                settle(0.25)
                native_distances.append(grid.property("contentY") - start)
            if native_distances[0] < 0 or abs(native_distances[0] - native_distances[1]) > 1:
                raise AssertionError(f"tile pixel wheel changed native routing: {native_distances}")
            if wheel_motion.property("motionActive") or wheel_motion.property("multiplier") != 1:
                raise AssertionError("tile pixel wheel incorrectly gained click-wheel acceleration")
            checks.append("tile-pixel-native-baseline")
            send_wheel()
            settle(0.025)
            window.setProperty("currentScreen", 0)
            stopped_at = grid.property("contentY")
            settle()
            if wheel_motion.property("motionActive") or abs(grid.property("contentY") - stopped_at) > 1:
                raise AssertionError("hidden gallery continued scrolling")
            checks.append("tab-cancels-wheel")
        else:
            raise AssertionError(f"unknown scenario: {scenario}")
        if messages:
            raise AssertionError(f"QML messages: {messages}")
        return {"scenario": scenario, "checks": checks}
    finally:
        if window is not None:
            window.close()
        wheel_input.close()
        model.shutdown()
        delete(engine)
        application.processEvents()
        qInstallMessageHandler(previous_handler)


@unittest.skipUnless(find_spec("PySide6") is not None, "optional Qt experiment dependencies are not installed")
class GalleryQmlTests(unittest.TestCase):
    def run_scene(self, scenario):
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
                           QT_QPA_FONTDIR=str(ROOT / "assets/fonts/Pretendard"))
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), scenario],
                                cwd=ROOT, env=environment, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_upward_scroll_and_partial_last_row_schedule_every_visible_photo(self):
        result = self.run_scene("scroll")
        self.assertEqual(len(result["checks"]), 6)

    def test_resize_reschedules_new_visible_photos_without_scroll(self):
        result = self.run_scene("resize")
        self.assertEqual(len(result["checks"]), 2)

    def test_density_round_trip_keeps_deep_scroll_anchor(self):
        result = self.run_scene("anchor")
        self.assertEqual(len(result["checks"]), 4)

    def test_large_viewport_schedules_more_than_48_visible_photos(self):
        result = self.run_scene("wide")
        self.assertEqual(len(result["checks"]), 2)

    def test_mouse_wheel_routes_once_and_stops_on_density_or_tab_change(self):
        result = self.run_scene("wheel")
        self.assertEqual(len(result["checks"]), 4)


if __name__ == "__main__":
    print(json.dumps(check_scene(sys.argv[1])))
