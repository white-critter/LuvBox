"""Exercise product author/date filters, selection and recycle cancellation."""

from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def scene():
    from contextlib import ExitStack, closing
    from datetime import datetime, timezone, timedelta
    from unittest.mock import patch

    from PIL import Image
    from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QPointF, Qt, qInstallMessageHandler
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from shiboken6 import delete

    sys.path.insert(0, str(ROOT))
    from luvbox.history import HistoryStore
    from luvbox.storage import resolve_storage
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_wheel import WheelInputFilter

    QQuickStyle.setStyle("Basic")
    application = QApplication([])
    messages = []
    previous = qInstallMessageHandler(lambda _level, _context, message: messages.append(message))
    controller = engine = window = None
    resources = ExitStack()

    def settle():
        application.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        time.sleep(.003)

    def wait(predicate, label):
        until = time.monotonic() + 8
        while time.monotonic() < until:
            settle()
            if predicate():
                return
        raise AssertionError(label + ": timeout")

    def find(name):
        result = window.findChild(QObject, name)
        if result is None:
            raise AssertionError("Missing product control: " + name)
        return result

    def click(name):
        # QQuick Layouts are polished on a frame boundary.  After opening or
        # closing the filters the button may be visible while mapToScene still
        # reports its previous position; let the new layout reach the window.
        QTest.qWait(30)
        item = find(name)
        assert item.property("visible") and item.property("enabled"), name
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        settle()

    def invoke(item, method):
        assert QMetaObject.invokeMethod(item, method), method

    try:
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-gallery-controls-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        base = Path(folder)
        paths = resolve_storage(base / "사진 폴더", "offline")
        files = []
        with closing(HistoryStore.for_storage(paths)) as store:
            for index, author, day in ((100, "alice", 1), (101, "alice", 1),
                                        (102, "alice2", 1), (103, "bob", 1), (104, "alice", 2)):
                path = paths.media_dir / f"{author}_{index}_1.png"
                Image.new("RGB", (48, 32), (200, 30, 50)).save(path)
                store.record_media("flat", {"tweet_id": str(index), "num": 1, "type": "photo",
                                            "author": {"name": author},
                                            "date": datetime(2026, 10, day, 12, tzinfo=timezone(timedelta(hours=9)))}, path)
                files.append(path)
        preferences = base / "LuvBox/settings.json"
        preferences.write_text(json.dumps({"account": "offline", "output_dir": str(paths.output_dir)}), encoding="utf-8")
        originals = {path: path.read_bytes() for path in files}
        controller = ApplicationController()
        wheel = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", controller.library.provider)
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel})
        engine.load(ROOT / "assets/qt/MainWindow.qml")
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        window.setProperty("currentScreen", 1)
        wait(lambda: not controller.library.model.busy and controller.library.model.total == 5, "initial gallery")
        for width, height in ((680, 480), (1080, 760)):
            window.setWidth(width)
            window.setHeight(height)
            settle()
            click("galleryFilterToggle")
            find("galleryAuthorFilter").setProperty("text", "@ALICE")
            find("galleryDateFrom").setProperty("text", "2026-10-01")
            find("galleryDateTo").setProperty("text", "2026-10-01")
            click("galleryFilterApply")
            wait(lambda: not controller.library.model.busy and controller.library.model.total == 2, "exact author/date")
            assert "작성자 @alice" in find("galleryActiveFilters").property("text")
            assert find("galleryGrid").property("height") > 40
            click("gallerySelectionToggle")
            click("gallerySelectLoaded")
            assert controller.library.selectedCount == 2
            if width == 680:
                with patch.object(controller.library, "deletionManifest", side_effect=ValueError("changed")):
                    click("galleryRecycleSelected")
                    wait(lambda: bool(controller.management.error) and not controller.management.mutating,
                         "failed deletion preparation")
                notice = find("galleryManagementMessage")
                assert notice.property("visible") and notice.property("text") == controller.management.error
            click("galleryRecycleSelected")
            confirmation = find("deleteConfirmation")
            wait(lambda: confirmation.property("visible"), "delete confirmation")
            assert controller.management.deleteCount == 2
            if width == 680:
                QTest.keyClick(window, Qt.Key.Key_Escape)
            else:
                invoke(confirmation, "reject")
            wait(lambda: not controller.management.deleteConfirmationPending, "cancel deletion")
            assert {path: path.read_bytes() for path in files} == originals
            click("gallerySelectionToggle")
            click("galleryFilterReset")
            wait(lambda: not controller.library.model.busy and controller.library.model.total == 5, "reset filter")
            assert controller.library.selectedCount == 0
            assert controller.library.filterSummary == ""
        # A missing tile must not make selecting and recycling the other
        # visible files impossible. Only the disposable fixture is removed.
        files[-1].unlink()
        controller.library.refresh()
        wait(lambda: not controller.library.model.busy and controller.library.model.total == 5, "missing gallery item")
        click("gallerySelectionToggle")
        click("gallerySelectLoaded")
        assert controller.library.selectedCount == 4
        assert "제외" in controller.library.status
        click("galleryRecycleSelected")
        confirmation = find("deleteConfirmation")
        wait(lambda: confirmation.property("visible"), "available files confirmation")
        assert controller.management.deleteCount == 4
        invoke(confirmation, "reject")
        wait(lambda: not controller.management.deleteConfirmationPending, "cancel available files")
        click("managementNavigation")
        assert window.property("currentScreen") == 2
        controller.requestClose()
        wait(lambda: controller.shutdownReady, "shutdown")
        window.close()
        wheel.close()
        delete(engine)
        engine = None
        delete(controller)
        controller = None
        errors = [m for m in messages if any(term in m for term in (
            "ReferenceError", "TypeError", "Binding loop", "Cannot assign", "Cannot read", "is not a type"))]
        assert not errors, errors
        print(json.dumps({"ok": True, "sizes": ["680x480", "1080x760"],
                          "filters": True, "selection": True, "cancel_preserves_files": True,
                          "qml_errors": errors}))
    finally:
        if controller is not None:
            controller.requestClose()
            wait(lambda: controller.shutdownReady, "cleanup")
        if engine is not None:
            delete(engine)
        qInstallMessageHandler(previous)
        resources.close()


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class GalleryControlsSceneTests(unittest.TestCase):
    def test_real_product_filters_selection_and_recycle_cancel(self):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=55,
                                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout.splitlines()[-1])["ok"])


if __name__ == "__main__":
    if "--scene" in sys.argv:
        scene()
    else:
        unittest.main()
