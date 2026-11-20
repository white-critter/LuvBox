"""Regression checks for the 1.11.9 inspection fixes."""

from contextlib import ExitStack
from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from tests import app_data_isolation  # noqa: E402,F401
except ModuleNotFoundError:
    import app_data_isolation  # noqa: E402,F401


class DocumentationWordingRegressionTests(unittest.TestCase):
    def test_user_guides_do_not_expose_developer_internals_or_dev_doc_links(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertNotIn("changelog-detail.md", changelog)
        self.assertNotIn("exFAT", changelog)
        self.assertNotIn("확장 1.2.0", changelog)
        self.assertNotIn("릴리스의 서명", changelog)

        guide = (ROOT / "docs" / "사용안내.txt").read_text(encoding="utf-8")
        self.assertNotIn("auth_token", guide)

        ext_readme = (ROOT / "assets" / "browser-extension" / "README.md").read_text(encoding="utf-8")
        for forbidden in (
            "%LOCALAPPDATA%",
            "매니페스트 오류",
            "미설치 확정",
            "추정하거나 강제로",
            "1.1.5 이하",
            "암호화 저장 완료는 별도",
            "보안 정책을 우회",
        ):
            self.assertNotIn(forbidden, ext_readme)

        ext_privacy = (ROOT / "assets" / "browser-extension" / "PRIVACY.md").read_text(encoding="utf-8")
        for forbidden in (
            "게시 전 초안",
            "발행자",
            "127.0.0.1",
            "AES-GCM",
            "HMAC-SHA-256",
            "DPAPI",
            "개발자 대시보드",
            "심사 적합성",
        ):
            self.assertNotIn(forbidden, ext_privacy)


def qml_scene():
    from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QPointF, QUrl, qInstallMessageHandler
    from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtQuickControls2 import QQuickStyle
    from PIL import Image
    from shiboken6 import delete

    from luvbox.app_settings import default_settings
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_wheel import WheelInputFilter

    QQuickStyle.setStyle("Basic")
    app = QGuiApplication([])
    QFontDatabase.addApplicationFont(str(ROOT / "assets/fonts/Pretendard/Pretendard-Regular.ttf"))
    app.setFont(QFont("Pretendard"))
    messages = []
    old = qInstallMessageHandler(lambda _k, _c, text: messages.append(text))
    controller = wheel = engine = window = None

    def settle(dur=0.05):
        deadline = __import__("time").monotonic() + dur
        while __import__("time").monotonic() < deadline:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            __import__("time").sleep(0.002)

    def wait(cond, label, timeout=7):
        deadline = __import__("time").monotonic() + timeout
        while __import__("time").monotonic() < deadline:
            settle(0.015)
            if cond():
                return
        raise AssertionError(label)

    def find(name):
        item = window.findChild(QObject, name)
        assert item is not None, f"missing {name}"
        return item

    def walk_visual(root_item):
        stack = [root_item]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(node.childItems())

    def invoke(item, method):
        assert QMetaObject.invokeMethod(item, method), method
        settle()

    with tempfile.TemporaryDirectory(prefix="luvbox-qml-1119-") as tmp, ExitStack() as mocks:
        d = Path(tmp)
        mocks.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": str(d / "app"), "APPDATA": str(d / "roam")}))
        photo1 = d / "pic1.png"
        photo2 = d / "pic2.png"
        Image.new("RGB", (640, 480), "#336699").save(photo1)
        Image.new("RGB", (640, 480), "#996633").save(photo2)
        vals = {**default_settings(), "account": "alice", "output_dir": str(d / "media")}

        class Mem:
            def load(self):
                return dict(vals)

            def update(self, c):
                vals.update(c)
                return dict(vals)

        mocks.enter_context(patch("luvbox.qt_app.SettingsRepository", Mem))
        mocks.enter_context(patch("luvbox.qt_accounts.AccountController._read_state",
                                  staticmethod(lambda: {"accounts": [], "identity": {}, "connected": False})))
        mocks.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        mocks.enter_context(patch("luvbox.qt_schedule.register_task", return_value={"registered": False}))
        mocks.enter_context(patch("luvbox.qt_schedule.unregister_task", return_value={"registered": False}))
        mocks.enter_context(patch("luvbox.qt_browser_setup.available_browsers", return_value=[]))

        try:
            controller = ApplicationController()
            wheel = WheelInputFilter()
            engine = QQmlApplicationEngine()
            engine.addImageProvider("thumbs", controller.library.provider)
            engine.setInitialProperties({"galleryModel": controller.library.model, "wheelInputFilter": wheel,
                                         "appController": controller})
            engine.load(ROOT / "assets/qt/MainWindow.qml")
            assert engine.rootObjects(), messages
            window = engine.rootObjects()[0]
            wait(lambda: not controller.library.model.busy, "initial load")

            # 1) previewMetaScroll clamps contentWidth and resets contentY on photo switch & reopen.
            long_info = "2026-11-20 · " + ("매우 긴 파일 정보와 경로 설명 " * 12)
            item_k1 = {
                "key": "k1", "author": "alice", "body": "첫 번째 본문 " * 40,
                "source": "https://x.com/alice/status/1", "path": str(photo1), "missing": False,
                "missing_note": "", "preview": QUrl.fromLocalFile(str(photo1)).toString(),
                "media_type": "photo", "public_info": long_info,
                "hashtags": [f"#태그{i:02}" for i in range(25)],
            }
            item_k2 = {
                "key": "k2", "author": "bob", "body": "두 번째 본문 " * 40,
                "source": "https://x.com/bob/status/2", "path": str(photo2), "missing": False,
                "missing_note": "", "preview": QUrl.fromLocalFile(str(photo2)).toString(),
                "media_type": "photo", "public_info": long_info,
                "hashtags": [f"#다른태그{i:02}" for i in range(25)],
            }
            controller.library._selected = dict(item_k1)
            controller.library.changed.emit()
            window.resize(680, 480)
            preview = find("previewDialog")
            media = find("selectedMedia")
            invoke(preview, "open")
            wait(lambda: media.property("ready"), "preview ready k1")
            meta_scroll = find("previewMetaScroll")
            meta_flick = meta_scroll.property("contentItem")
            assert abs(meta_scroll.property("contentWidth") - meta_scroll.property("availableWidth")) <= 1.0
            wait(lambda: meta_flick.property("contentHeight") > meta_flick.property("height") + 10,
                 "meta_scroll overflows at 680x480")
            meta_flick.setProperty("contentY", 60.0)
            settle()
            assert meta_flick.property("contentY") > 10.0

            # Switch to k2 -> scroll must reset to 0.
            controller.library._selected = dict(item_k2)
            controller.library.changed.emit()
            wait(lambda: preview.property("openedKey") == "k2", "switched to k2")
            settle()
            assert abs(meta_flick.property("contentY")) < 0.5

            # Scroll k2 down, close previewDialog, reopen -> scroll must reset to 0.
            meta_flick.setProperty("contentY", 50.0)
            settle()
            assert meta_flick.property("contentY") > 10.0
            invoke(preview, "close")
            wait(lambda: not preview.property("visible"), "preview closed")
            invoke(preview, "open")
            wait(lambda: preview.property("visible"), "preview reopened")
            settle()
            assert abs(meta_flick.property("contentY")) < 0.5
            invoke(preview, "close")

            # 2) preferencesScroll clamps contentWidth.
            prefs = find("preferencesDialog")
            invoke(prefs, "open")
            wait(lambda: prefs.property("visible"), "prefs open")
            prefs_scroll = find("preferencesScroll")
            assert abs(prefs_scroll.property("contentWidth") - prefs_scroll.property("availableWidth")) <= 1.0
            invoke(prefs, "close")

            # 3) galleryDatePickerPopup repositions when window resizes while open.
            window.resize(1080, 760)
            window.setProperty("currentScreen", 1)
            settle()
            toggle = find("galleryFilterToggle")
            if not toggle.property("checked"):
                toggle.click()
            wait(lambda: find("galleryDateFrom").property("visible"), "dateFrom visible")
            date_from = find("galleryDateFrom")
            date_from.pickRequested.emit()
            popup = find("galleryDatePickerPopup")
            host = popup.property("parent")
            wait(lambda: popup.property("visible"), "datePicker visible")
            initial_y = popup.property("y")
            initial_field_pos = date_from.mapToScene(QPointF(0, 0))
            window.resize(680, 480)
            wait(lambda: abs(date_from.mapToScene(QPointF(0, 0)).x() - initial_field_pos.x()) > 10
                 or abs(date_from.mapToScene(QPointF(0, 0)).y() - initial_field_pos.y()) > 10,
                 "dateFrom moved after resize")

            def expected_popup_pos():
                pos = date_from.mapToItem(host, QPointF(0, date_from.property("height") + 6))
                popup_h = max(308, popup.property("height"), popup.property("implicitHeight"))
                ex = max(8, min(host.property("width") - popup.property("width") - 8, pos.x()))
                ey = max(8, min(host.property("height") - popup_h - 8, pos.y()))
                return ex, ey

            wait(lambda: (
                abs(popup.property("x") - expected_popup_pos()[0]) <= 1.0
                and abs(popup.property("y") - expected_popup_pos()[1]) <= 1.0
                and abs(popup.property("y") - initial_y) > 5
            ), "datePickerPopup followed dateFrom and stayed inside popupHost after resize")
            assert popup.property("x") >= 8 and popup.property("y") >= 8
            assert popup.property("x") + popup.property("width") <= host.property("width")
            invoke(popup, "close")

            # 4) DialogButtons inside DeleteConfirmation, AlikeConfirmation, SchedulePanel emit accepted once.
            del_dlg = find("deleteConfirmation")
            del_accepts = []
            del_dlg.accepted.connect(lambda: del_accepts.append(1))
            invoke(del_dlg, "open")
            wait(lambda: del_dlg.property("visible"), "deleteConfirmation open")
            find("confirmRecycle").click()
            wait(lambda: not del_dlg.property("visible"), "deleteConfirmation closed")
            assert len(del_accepts) == 1, f"expected 1 accept on deleteConfirmation, got {len(del_accepts)}"

            alike_dlg = find("alikeConfirmation")
            alike_accepts = []
            alike_dlg.accepted.connect(lambda: alike_accepts.append(1))
            invoke(alike_dlg, "open")
            wait(lambda: alike_dlg.property("visible"), "alikeConfirmation open")
            find("confirmAlikeRecycle").click()
            wait(lambda: not alike_dlg.property("visible"), "alikeConfirmation closed")
            assert len(alike_accepts) == 1, f"expected 1 accept on alikeConfirmation, got {len(alike_accepts)}"

            sched_dlg = find("scheduleDialog")
            invoke(sched_dlg, "open")
            wait(lambda: sched_dlg.property("visible"), "scheduleDialog open")
            sched_panel = find("dailySchedulePanel")
            sched_panel.setProperty("action", "create")
            sched_panel.setProperty("requestedTime", "09:00")
            sched_conf = find("scheduleConfirmation")
            sched_accepts = []
            sched_conf.accepted.connect(lambda: sched_accepts.append(1))
            invoke(sched_conf, "open")
            wait(lambda: sched_conf.property("visible"), "scheduleConfirmation open")
            confirm_btn = next(
                c for c in walk_visual(sched_conf.property("footer"))
                if isinstance(c, QQuickItem) and c.property("text") == "확인" and c.property("visible")
            )
            confirm_btn.click()
            wait(lambda: not sched_conf.property("visible"), "scheduleConfirmation closed")
            assert len(sched_accepts) == 1, f"expected 1 accept on scheduleConfirmation, got {len(sched_accepts)}"
            invoke(sched_dlg, "close")

            controller.requestClose()
            wait(lambda: controller.shutdownReady, "shutdown")
            window.close()
            wheel.close()
            delete(engine)
            engine = None
            delete(controller)
            controller = None
            errors = [m for m in messages if any(t in m for t in ("ReferenceError", "TypeError", "Binding loop", "Cannot assign", "Cannot read", "is not a type"))]
            assert not errors, errors
            print(json.dumps({"ok": True}))
        finally:
            if controller is not None:
                controller.requestClose()
                wait(lambda: controller.shutdownReady, "cleanup")
            if engine is not None:
                delete(engine)
            if wheel is not None:
                wheel.close()
            qInstallMessageHandler(old)


@unittest.skipUnless(find_spec("PySide6") is not None, "optional Qt dependencies are not installed")
class QmlRegressionTests(unittest.TestCase):
    def test_1119_qml_fixes_in_offscreen_window(self):
        res = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"], cwd=ROOT,
                             capture_output=True, text=True, encoding="utf-8", timeout=150,
                             env={**os.environ, "QT_QPA_PLATFORM": os.environ.get("QT_QPA_PLATFORM", "offscreen"),
                                  "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue(json.loads(res.stdout.splitlines()[-1])["ok"])


if __name__ == "__main__":
    if "--scene" in sys.argv:
        qml_scene()
    else:
        unittest.main()
