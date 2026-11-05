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

    def show_october():
        # The calendar opens on the current month; the fixture posts are in October 2026.
        picker = find("galleryDatePickerPopup")
        picker.setProperty("viewYear", 2026)
        picker.setProperty("viewMonth", 9)
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
        def toolbar_separated(label):
            QTest.qWait(30)
            settle()
            bar = find("galleryToolBar")
            kids = [child for child in bar.childItems()
                    if child.property("visible") and child.property("width") >= 1 and child.property("height") >= 1]
            rects = []
            for child in kids:
                origin = child.mapToScene(QPointF(0, 0))
                rect = (origin.x(), origin.y(), child.property("width"), child.property("height"))
                assert rect[0] >= 8 and rect[0] + rect[2] <= width - 8, (label, rect, width)
                rects.append((child.objectName(), rect))
            for index, (left_name, left) in enumerate(rects):
                for right_name, right in rects[index + 1:]:
                    apart = (left[0] + left[2] <= right[0] + 0.5 or right[0] + right[2] <= left[0] + 0.5
                             or left[1] + left[3] <= right[1] + 0.5 or right[1] + right[3] <= left[1] + 0.5)
                    assert apart, (label, left_name, left, right_name, right)
            assert len(rects) >= 4, (label, [name for name, _rect in rects])

        for width, height in ((680, 480), (1080, 760)):
            window.setWidth(width)
            window.setHeight(height)
            settle()
            QTest.qWait(40)
            model = controller.library.model
            count = find("galleryCount")
            narrow = count.property("width")
            model._total = 1234567
            model._busy = True
            model.changed.emit()
            wait(lambda: find("galleryBusy").property("visible") and count.property("width") > narrow + 20,
                 "toolbar count grew while loading")
            toolbar_separated(f"busy {width}")
            model._busy = False
            model._total = 5
            model.changed.emit()
            wait(lambda: not find("galleryBusy").property("visible") and model.total == 5, "toolbar idle")
            toolbar_separated(f"idle {width}")
            if not find("galleryFilterToggle").property("checked"):
                click("galleryFilterToggle")
            author_combo = find("galleryAuthorFilter")
            labels = [row["label"] for row in author_combo.property("model")]
            assert labels == ["전체 작성자", "@alice · 미디어 3개", "@alice2 · 미디어 1개", "@bob · 미디어 1개"], labels
            def inside(name, bar):
                node = find(name)
                while node is not None:
                    if node.objectName() == bar:
                        return True
                    node = node.parent()
                return False

            assert inside("galleryFilterToggle", "galleryFilterBar")
            assert inside("galleryFilterReset", "galleryFilterBar")
            names = ("galleryAuthorFilter", "galleryDateField", "galleryDateFrom", "galleryDateTo",
                     "galleryFilterToggle", "galleryFilterReset")
            placed = []
            for name in names:
                item = find(name)
                origin = item.mapToScene(QPointF(0, 0))
                rect = (origin.x(), origin.y(), item.property("width"), item.property("height"))
                assert item.property("visible") and rect[2] >= 36 and rect[3] >= 36, (name, rect)
                assert rect[0] >= 16 and rect[0] + rect[2] <= width - 16, (name, rect, width)
                placed.append(rect)
            for index, left in enumerate(placed):
                for right in placed[index + 1:]:
                    apart = (left[0] + left[2] <= right[0] + 0.5 or right[0] + right[2] <= left[0] + 0.5
                             or left[1] + left[3] <= right[1] + 0.5 or right[1] + right[3] <= left[1] + 0.5)
                    assert apart, (left, right)
            bar = find("galleryFilterBar")
            kids = [child for child in bar.childItems() if child.property("visible")]
            total = sum(child.width() for child in kids) + 8 * max(0, len(kids) - 1)
            bands = [child.mapToScene(QPointF(0, 0)).y() for child in kids]
            if total <= bar.property("width") + 1:
                assert max(bands) - min(bands) <= 2, (width, total, bar.property("width"), bands)
            else:
                assert max(bands) - min(bands) >= 30, (width, total, bar.property("width"), bands)
            assert author_combo.property("width") <= 280 and find("galleryDateFrom").property("width") >= 136
            open_grid = find("galleryGrid").property("height")
            open_bar = find("galleryFilterBar").property("height")
            click("galleryFilterToggle")
            wait(lambda: not find("galleryAuthorFilter").property("visible")
                 and find("galleryFilterToggle").property("visible"), "filter inputs hidden")
            if open_bar > 56:
                wait(lambda: find("galleryFilterBar").property("height") <= open_bar - 20, "filter row collapsed")
                assert find("galleryGrid").property("height") > open_grid + 20, (
                    open_grid, find("galleryGrid").property("height"), open_bar)
            click("galleryFilterToggle")
            wait(lambda: find("galleryAuthorFilter").property("visible"), "filter reopened")
            date_from, date_to = find("galleryDateFrom"), find("galleryDateTo")
            picker = find("galleryDatePickerPopup")
            # A date is applied the moment it is chosen: Space opens the calendar, a day closes it.
            invoke(date_from, "forceActiveFocus")
            settle()
            assert date_from.property("activeFocus")
            QTest.keyClick(window, Qt.Key.Key_Space)
            wait(lambda: picker.property("visible"), "date picker open via keyboard")
            show_october()
            click("datePickerDay_1")
            wait(lambda: not picker.property("visible"), "date picker close")
            wait(lambda: controller.library.dateFrom == "2026-10-01" and not controller.library.model.busy, "start date applied")
            assert date_from.property("text") == "2026-10-01"
            assert controller.library.model.total == 5
            # Backspace clears it again.
            invoke(date_from, "forceActiveFocus")
            settle()
            QTest.keyClick(window, Qt.Key.Key_Backspace)
            wait(lambda: controller.library.dateFrom == "" and not controller.library.model.busy, "start date cleared")
            assert date_from.property("text") == ""
            if width == 1080:
                invoke(date_from, "forceActiveFocus")
                settle()
                QTest.keyClick(window, Qt.Key.Key_Space)
                wait(lambda: picker.property("visible"), "date picker for a keyboard day")
                show_october()
                invoke(find("datePickerDay_1"), "forceActiveFocus")
                settle()
                QTest.keyClick(window, Qt.Key.Key_Right)
                QTest.keyClick(window, Qt.Key.Key_Return)
                wait(lambda: not picker.property("visible") and controller.library.dateFrom == "2026-10-02"
                     and not controller.library.model.busy, "keyboard chose the next day")
                invoke(date_from, "forceActiveFocus")
                settle()
                QTest.keyClick(window, Qt.Key.Key_Backspace)
                wait(lambda: controller.library.dateFrom == "" and not controller.library.model.busy,
                     "keyboard date cleared")
            # Range: 2026-10-01 .. 2026-10-01.
            for field, name in ((date_from, "galleryDateFrom"), (date_to, "galleryDateTo")):
                click(name)
                wait(lambda: picker.property("visible"), "date picker open by mouse")
                show_october()
                click("datePickerDay_1")
                wait(lambda: not picker.property("visible"), "date picker close by mouse")
            wait(lambda: (controller.library.dateFrom, controller.library.dateTo) == ("2026-10-01", "2026-10-01")
                 and not controller.library.model.busy and controller.library.model.total == 4, "date range applied")
            # Choosing an author applies at once and keeps the dates.
            invoke(author_combo, "forceActiveFocus")
            settle()
            QTest.keyClick(window, Qt.Key.Key_Down)
            wait(lambda: controller.library.filterAuthor == "alice" and not controller.library.model.busy
                 and controller.library.model.total == 2, "author applied without a search step")
            assert (controller.library.dateFrom, controller.library.dateTo) == ("2026-10-01", "2026-10-01")
            assert author_combo.property("currentIndex") == 1
            assert find("galleryFilterToggle").property("checked"), "the filter panel stays open"
            # A start date after the end moves the end along instead of failing.
            click("galleryDateFrom")
            wait(lambda: picker.property("visible"), "date picker for an impossible range")
            show_october()
            click("datePickerDay_2")
            wait(lambda: controller.library.dateFrom == "2026-10-02" and not controller.library.model.busy, "range kept in order")
            assert controller.library.dateTo == "2026-10-02" and controller.library.model.total == 1
            assert controller.library.filterAuthor == "alice"
            # Back to the first day for the selection checks below.
            for name in ("galleryDateFrom", "galleryDateTo"):
                click(name)
                wait(lambda: picker.property("visible"), "date picker again")
                show_october()
                click("datePickerDay_1")
                wait(lambda: not picker.property("visible"), "date picker closed again")
            wait(lambda: (controller.library.dateFrom, controller.library.dateTo) == ("2026-10-01", "2026-10-01")
                 and not controller.library.model.busy and controller.library.model.total == 2, "exact author/date")
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
        # Search field clear button and Escape key
        search_field = find("searchBox")
        search_field.setProperty("text", "alice")
        settle()
        wait(lambda: find("searchClearButton").property("visible"), "clear button visible")
        click("searchClearButton")
        settle()
        wait(lambda: search_field.property("text") == "", "clear button empties search field")
        wait(lambda: not find("searchClearButton").property("visible"), "clear button hidden")
        search_field.setProperty("text", "bob")
        invoke(search_field, "forceActiveFocus")
        settle()
        QTest.keyClick(window, Qt.Key.Key_Escape)
        settle()
        wait(lambda: search_field.property("text") == "", "Escape empties search field")
        invoke(search_field, "forceActiveFocus")
        settle()
        assert search_field.property("activeFocus")
        QTest.keyClick(window, Qt.Key.Key_Escape)
        settle()
        assert not search_field.property("activeFocus"), "Escape on empty search removes focus" 
        # Choosing files is screen state: it stays available while a save runs, by button,
        # Ctrl+click and Space alike. Only the move to the Recycle Bin waits for the save.
        window.setWidth(1080)
        window.setHeight(760)
        controller._download._running = True
        controller._download.changed.emit()
        wait(lambda: controller.busy, "a save is running")
        assert find("gallerySelectionToggle").property("enabled")
        click("gallerySelectionToggle")
        assert controller.library.selectionMode
        click("gallerySelectLoaded")
        assert controller.library.selectedCount == 5
        assert not find("galleryRecycleSelected").property("enabled")
        assert "작업이 끝나면" in find("gallerySelectionCount").property("text")
        click("gallerySelectionClear")
        click("gallerySelectionToggle")
        assert not controller.library.selectionMode and controller.library.selectedCount == 0
        tiles = []
        stack = list(find("galleryGrid").property("contentItem").childItems())
        while stack:
            node = stack.pop()
            if node.objectName() == "galleryTile" and node.property("visible"):
                tiles.append(node)
            stack.extend(node.childItems())
        first_tile = min(tiles, key=lambda node: (node.y(), node.x()))
        point = first_tile.mapToScene(QPointF(first_tile.width() / 2, first_tile.height() / 2)).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, point)
        settle()
        assert controller.library.selectedCount == 1, controller.library.selectedCount
        QTest.keyClick(window, Qt.Key.Key_Delete)
        settle()
        notice = find("galleryManagementMessage")
        assert notice.property("visible") and "작업이 끝난 뒤" in notice.property("text")
        assert controller.management.deleteConfirmationPending is False
        assert {path: path.read_bytes() for path in files} == originals
        controller._download._running = False
        controller._download.changed.emit()
        wait(lambda: not controller.busy, "the save ended")
        assert controller.library.selectedCount == 1, "the choice survives the end of the save"
        assert find("galleryRecycleSelected").property("enabled")
        click("gallerySelectionClear")
        click("gallerySelectionToggle")
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
        assert window.findChild(QObject, "managementNavigation") is None
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
