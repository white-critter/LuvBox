from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def check_wheel():
    from PySide6.QtCore import QCoreApplication, QPoint, QPointF, Qt, QUrl, qInstallMessageHandler
    from PySide6.QtGui import QGuiApplication, QInputDevice, QPointingDevice, QWheelEvent
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickItem
    from shiboken6 import delete
    sys.path.insert(0, str(ROOT))
    from luvbox.qt_wheel import WheelInputFilter

    application = QGuiApplication([])
    messages = []
    previous = qInstallMessageHandler(lambda kind, context, message: messages.append(message))
    engine = QQmlApplicationEngine()
    wheel_filter = WheelInputFilter()
    engine.setInitialProperties({"wheelInputFilter": wheel_filter})
    directory = QUrl.fromLocalFile(str(ROOT / "assets/qt") + "/")
    engine.loadData(b'''import QtQuick
import "."
Window {
    required property var wheelInputFilter
    width: 400; height: 400; visible: true
    property int fallbackWheels: 0
    Flickable {
        id: gallery; objectName: "gallery"
        anchors.fill: parent; contentWidth: width; contentHeight: 10000
        boundsBehavior: Flickable.StopAtBounds
        MouseArea {
            parent: gallery; anchors.fill: parent; acceptedButtons: Qt.NoButton
            onWheel: function(event) { fallbackWheels += 1; event.accepted = false }
        }
        GalleryWheel { id: wheel; objectName: "wheel"; view: gallery; wheelLines: 3; inputFilter: wheelInputFilter }
    }
}''', directory.resolved(QUrl("WheelTest.qml")))
    if not engine.rootObjects():
        raise AssertionError(f"wheel scene failed: {messages}")
    window = engine.rootObjects()[0]
    view = window.findChild(QQuickItem, "gallery")
    wheel = window.findChild(QQuickItem, "wheel")
    checks = []

    def settle(milliseconds=140):
        deadline = time.monotonic() + milliseconds / 1000
        while time.monotonic() < deadline:
            application.processEvents()
            time.sleep(.002)

    def send(angle=-120, *, pixels=0, modifiers=Qt.KeyboardModifier.NoModifier, device=None,
             phase=Qt.ScrollPhase.NoScrollPhase):
        event = QWheelEvent(QPointF(200, 200), QPointF(200, 200), QPoint(0, pixels),
                            QPoint(0, angle), Qt.MouseButton.NoButton, modifiers,
                            phase, False,
                            Qt.MouseEventSource.MouseEventNotSynthesized,
                            device or QPointingDevice.primaryPointingDevice())
        event.setTimestamp(int(time.monotonic() * 1000))
        QCoreApplication.sendEvent(window, event)
        application.processEvents()
        return event

    def pixel_sequence(pixels, device=None):
        send(0, device=device, phase=Qt.ScrollPhase.ScrollBegin)
        send(0, pixels=pixels, device=device, phase=Qt.ScrollPhase.ScrollUpdate)
        send(0, device=device, phase=Qt.ScrollPhase.ScrollEnd)

    def reset(position=3000):
        wheel.reset()
        view.cancelFlick()
        view.setProperty("contentY", position)
        settle(10)

    try:
        settle(30)
        reset()
        fallback_before = window.property("fallbackWheels")
        send()
        settle()
        first = view.property("contentY") - 3000
        if not 55 <= first <= 65:
            raise AssertionError(f"single click did not honor wheel lines: {first}")
        if window.property("fallbackWheels") != fallback_before:
            raise AssertionError("click wheel leaked into native fallback")
        before = view.property("contentY")
        send()
        settle()
        second = view.property("contentY") - before
        if second <= first or wheel.property("multiplier") > 3:
            raise AssertionError(f"burst did not accelerate within cap: {first}, {second}")
        checks.append("mouse-routing-and-acceleration")

        for _ in range(12):
            send()
            settle(10)
        if wheel.property("multiplier") != 3:
            raise AssertionError("continuous burst was not capped at three")
        checks.append("bounded-burst")

        before = view.property("contentY")
        send(120)
        if wheel.property("multiplier") != 1:
            raise AssertionError("reverse input retained acceleration")
        settle()
        if not 55 <= before - view.property("contentY") <= 65:
            raise AssertionError("reverse input retained forward residual motion")
        settle(100)
        if wheel.property("multiplier") != 1 or wheel.property("motionActive"):
            raise AssertionError("idle did not reset motion")
        checks.append("reverse-and-idle-reset")

        reset()
        send()
        settle(20)
        wheel.reset()
        stopped = view.property("contentY")
        settle()
        if abs(view.property("contentY") - stopped) > .01:
            raise AssertionError("external reset left residual motion")
        checks.append("external-reset")

        for position, angle in ((0, 120), (9600, -120)):
            reset(position)
            send(angle)
            settle()
            if abs(view.property("contentY") - position) > .01:
                raise AssertionError("boundary motion escaped content bounds")
        checks.append("boundaries")

        reset()
        wheel.setProperty("wheelLines", 0)
        send()
        settle()
        if view.property("contentY") != 3000:
            raise AssertionError("disabled OS wheel lines still scrolled")
        for page_lines in (-1, 2147483647):
            reset()
            wheel.setProperty("wheelLines", page_lines)
            send()
            settle()
            if not 395 <= view.property("contentY") - 3000 <= 405:
                raise AssertionError("page wheel mode was not limited to one viewport")
        wheel.setProperty("wheelLines", 3)
        checks.append("os-lines-zero-and-page")

        reset()
        send()
        settle(20)
        send(pixels=-15)
        if wheel.property("motionActive"):
            raise AssertionError("pixel wheel did not cancel click-wheel residual motion")
        reset()
        wheel.setProperty("enabled", False)
        fallback_before = window.property("fallbackWheels")
        pixel_sequence(-15)
        settle()
        native_pixels = view.property("contentY") - 3000
        native_pixel_events = window.property("fallbackWheels") - fallback_before
        if native_pixel_events <= 0:
            raise AssertionError("native pixel-wheel baseline did not reach fallback")
        wheel.setProperty("enabled", True)
        reset()
        fallback_before = window.property("fallbackWheels")
        pixel_sequence(-15)
        settle()
        if window.property("fallbackWheels") - fallback_before != native_pixel_events:
            raise AssertionError("pixel wheel was swallowed before native fallback")
        if abs(view.property("contentY") - 3000 - native_pixels) > .5:
            raise AssertionError(f"pixel wheel no longer matched the native baseline: {native_pixels}, {view.property('contentY') - 3000}")
        checks.append("pixel-pass-and-cancel")

        touchpad = QPointingDevice("fixture touchpad", 1, QInputDevice.DeviceType.TouchPad,
                                   QPointingDevice.PointerType.Finger,
                                   QInputDevice.Capability.Position | QInputDevice.Capability.Scroll,
                                   5, 0)
        reset()
        wheel.setProperty("enabled", False)
        fallback_before = window.property("fallbackWheels")
        pixel_sequence(-20, touchpad)
        settle()
        native_touchpad = view.property("contentY") - 3000
        native_touchpad_events = window.property("fallbackWheels") - fallback_before
        if native_touchpad_events <= 0:
            raise AssertionError("native touchpad baseline did not reach fallback")
        wheel.setProperty("enabled", True)
        reset()
        fallback_before = window.property("fallbackWheels")
        pixel_sequence(-20, touchpad)
        settle()
        if window.property("fallbackWheels") - fallback_before != native_touchpad_events:
            raise AssertionError("touchpad was swallowed before native fallback")
        if abs(view.property("contentY") - 3000 - native_touchpad) > .5:
            raise AssertionError(f"touchpad no longer matched the native baseline: {native_touchpad}, {view.property('contentY') - 3000}")
        reset()
        send()
        settle(20)
        send(pixels=-20, device=touchpad)
        if wheel.property("multiplier") != 1 or wheel.property("motionActive"):
            raise AssertionError("touchpad retained click-wheel residual motion")
        reset()
        send(modifiers=Qt.KeyboardModifier.ControlModifier)
        if wheel.property("motionActive"):
            raise AssertionError("modified wheel was captured")
        reset()
        fallback_before = window.property("fallbackWheels")
        send()
        settle()
        if not 55 <= view.property("contentY") - 3000 <= 65:
            raise AssertionError("click wheel after passthrough was lost or applied twice")
        if window.property("fallbackWheels") != fallback_before:
            raise AssertionError("click after passthrough leaked into native fallback")
        checks.append("touchpad-and-modifier-pass")

        reset()
        send()
        wheel.setProperty("enabled", False)
        stopped = view.property("contentY")
        settle()
        if wheel.property("motionActive") or abs(view.property("contentY") - stopped) > .01:
            raise AssertionError("disabled controller retained residual motion")
        checks.append("disable-reset")
        wheel.setProperty("enabled", True)
        requested = []
        wheel_filter.wheelRequested.connect(requested.append)
        wheel_filter.view = None
        send()
        if requested:
            raise AssertionError("detached filter still consumed the original window")
        orphan = QQuickItem()
        orphan.setWidth(400)
        orphan.setHeight(400)
        wheel_filter.view = orphan
        send()
        if requested:
            raise AssertionError("view without a window captured old-window input")
        orphan.setParentItem(window.contentItem())
        send()
        if requested != [-120]:
            raise AssertionError("windowChanged did not attach the new view")
        delete(orphan)
        send()
        if requested != [-120] or wheel_filter.view is not None:
            raise AssertionError("destroyed view retained its event filter")
        wheel_filter.view = view
        send()
        if requested != [-120, -120]:
            raise AssertionError("restored view did not receive click-wheel input")
        wheel_filter.close()
        wheel_filter.close()
        wheel_filter.enabled = True
        send()
        if requested != [-120, -120] or wheel_filter.enabled:
            raise AssertionError("closed filter consumed input or could be reopened")
        checks.append("view-window-lifecycle-and-idempotent-close")
        if messages:
            raise AssertionError(f"QML messages: {messages}")
        return {"checks": checks, "input_method": "offscreen QWheelEvent.sendEvent only"}
    finally:
        window.close()
        wheel_filter.close()
        delete(engine)
        application.processEvents()
        qInstallMessageHandler(previous)


@unittest.skipUnless(find_spec("PySide6") is not None, "optional Qt experiment dependencies are not installed")
class GalleryWheelTests(unittest.TestCase):
    def test_click_wheel_acceleration_routing_reset_and_native_passthrough(self):
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"],
                                cwd=ROOT, env=environment, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["checks"]), 10)


if __name__ == "__main__":
    print(json.dumps(check_wheel()))
