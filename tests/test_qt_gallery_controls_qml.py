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
        if result is not None:
            return result
        stack = [window.contentItem()]
        popup = window.findChild(QObject, "galleryDatePickerPopup")
        if popup is not None and popup.property("contentItem") is not None:
            stack.append(popup.property("contentItem"))
        while stack:
            current = stack.pop()
            if current.objectName() == name:
                return current
            stack.extend(current.childItems())
        raise AssertionError("Missing product control: " + name)

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
        wait(lambda: not controller.busy, "save screen ready")
        hint = find("saveReadinessHint")
        assert hint.property("visible") and "X 계정" in hint.property("text")
        assert not find("saveStartButton").property("enabled")
        # A saved token without a verified handle still needs a typed account.
        controller.accounts._connected = True
        controller.accounts.changed.emit()
        account_input = find("saveAccountInput")
        account_input.setProperty("text", "")
        settle()
        assert hint.property("visible") and "아이디" in hint.property("text")
        assert not find("saveStartButton").property("enabled")
        account_input.setProperty("text", "offline")
        settle()
        assert not hint.property("visible") and find("saveStartButton").property("enabled")
        controller.accounts._connected = False
        controller.accounts.changed.emit()
        window.setProperty("currentScreen", 1)
        wait(lambda: not controller.library.model.busy and controller.library.model.total == 5
             and len(controller.library.authors) == 3, "initial gallery")
        for width, height in ((680, 480), (1080, 760)):
            window.setWidth(width)
            window.setHeight(height)
            settle()
            click("galleryFilterToggle")
            author_combo = find("galleryAuthorFilter")
            assert len(author_combo.property("model")) >= 4
            if width == 680:
                date_from = find("galleryDateFrom")
                date_from.setProperty("text", "2026-10-15")
                invoke(date_from, "forceActiveFocus")
                settle()
                assert date_from.property("activeFocus")
                QTest.keyClick(window, Qt.Key.Key_Space)
                picker = find("galleryDatePickerPopup")
                wait(lambda: picker.property("visible"), "date picker open via keyboard")
                click("datePickerDay_1")
                wait(lambda: not picker.property("visible"), "date picker close")
                assert date_from.property("text") == "2026-10-01"
                invoke(date_from, "forceActiveFocus")
                settle()
                QTest.keyClick(window, Qt.Key.Key_Backspace)
                settle()
                assert date_from.property("text") == ""
                date_from.setProperty("text", "2026-10-01")
                find("galleryDateTo").setProperty("text", "2026-10-01")
                author_combo.setProperty("text", "@ALICE")
            else:
                author_combo.setProperty("text", "@ALICE")
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
            authors_queries = []
            original_authors = controller.library._repository.authors
            with patch.object(controller.library._repository, "authors",
                              side_effect=lambda gen: (authors_queries.append(gen), original_authors(gen))[1]):
                click("galleryFilterReset")
                wait(lambda: not controller.library.model.busy and controller.library.model.total == 5, "reset filter")
                assert authors_queries == [], authors_queries
            assert controller.library.selectedCount == 0
            assert controller.library.filterSummary == ""
        # A missing tile must not make selecting and recycling the other
        # visible files impossible. Only the disposable fixture is removed.
        files[-1].unlink()
        controller.library.refresh()
        wait(lambda: not controller.library.model.busy and controller.library.model.total == 5, "missing gallery item")
        QTest.qWait(40)
        grid_stack = list(find("galleryGrid").property("contentItem").childItems())
        placeholders = []
        while grid_stack:
            node = grid_stack.pop()
            if node.objectName() == "galleryTileMissingPlaceholder":
                placeholders.append(node)
            grid_stack.extend(node.childItems())
        assert any(item.property("visible") for item in placeholders), "missing tile placeholder should be visible"
        missing_key = next(row["key"] for row in controller.library.model._rows if row.get("missing"))
        controller.library.selectItem(missing_key)
        wait(lambda: not controller.library.busy and controller.library.selectedKey == missing_key, "select missing item")
        preview_dialog = find("previewDialog")
        invoke(preview_dialog, "open")
        wait(lambda: preview_dialog.property("visible"), "open missing preview dialog")
        assert find("previewMissingBox").property("visible"), "preview missing placeholder should be visible"
        invoke(preview_dialog, "close")
        wait(lambda: not preview_dialog.property("visible"), "close missing preview dialog")
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
