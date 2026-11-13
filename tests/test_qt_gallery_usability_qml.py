"""Statistics show real numbers, the wheel moves a useful distance, and tiles select by range and by drag."""

from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
try:
    from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
except ModuleNotFoundError:      # run as a script: the tests folder is the path
    import app_data_isolation  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]


def scene(shots=None):
    """``shots`` is a folder for window captures at each step, for a look at the result."""
    import faulthandler

    faulthandler.dump_traceback_later(45, exit=True)
    from contextlib import ExitStack, closing
    from datetime import datetime, timezone, timedelta
    from unittest.mock import patch

    from PIL import Image
    from PySide6.QtCore import (QCoreApplication, QEvent, QMetaObject, QObject, QPoint, QPointF, Qt,
                                qInstallMessageHandler)
    from PySide6.QtGui import QFont, QFontDatabase, QPointingDevice, QWheelEvent
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
    for filename in ("Pretendard-Regular.ttf", "Pretendard-Bold.ttf"):
        QFontDatabase.addApplicationFont(str(ROOT / "assets/fonts/Pretendard" / filename))
    font = QFont("Pretendard")
    font.setPixelSize(14)
    application.setFont(font)
    messages = []
    previous = qInstallMessageHandler(lambda _level, _context, message: messages.append(message))
    controller = engine = window = folder = None
    resources = ExitStack()

    def settle(milliseconds=0):
        until = time.monotonic() + milliseconds / 1000
        while True:
            application.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if time.monotonic() >= until:
                return
            time.sleep(.003)

    def wait(predicate, label):
        until = time.monotonic() + 8
        while time.monotonic() < until:
            settle()
            if predicate():
                return
            time.sleep(.003)
        raise AssertionError(label + ": timeout")

    def find(name):
        result = window.findChild(QObject, name)
        if result is None:
            raise AssertionError("Missing product control: " + name)
        return result

    def shot(name):
        if shots is not None:
            settle(150)
            window.grabWindow().save(str(Path(shots) / (name + ".png")))

    def center(item):
        return item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()

    def click(name):
        settle(30)
        item = find(name)
        assert item.property("visible") and item.property("enabled"), name
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center(item))
        settle()

    def wheel(point, angle=-120):
        event = QWheelEvent(QPointF(point), QPointF(point), QPoint(0, 0), QPoint(0, angle), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False,
                            Qt.MouseEventSource.MouseEventNotSynthesized, QPointingDevice.primaryPointingDevice())
        event.setTimestamp(int(time.monotonic() * 1000))
        QCoreApplication.sendEvent(window, event)
        settle()

    try:
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-gallery-usability-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        base = Path(folder)
        paths = resolve_storage(base / "사진 폴더", "offline")
        with closing(HistoryStore.for_storage(paths)) as store:
            for index in range(60):
                author = ("alice", "bob", "carol")[index % 3]
                path = paths.media_dir / f"{author}_{2000 + index}_1.png"
                Image.new("RGB", (48, 32), (40 + index * 3, 30, 50)).save(path)
                store.record_media("flat", {"tweet_id": str(2000 + index), "num": 1, "type": "photo",
                                            "author": {"name": author},
                                            "date": datetime(2026, 8 + index % 3, 1 + index % 20, 12,
                                                             tzinfo=timezone(timedelta(hours=9)))}, path)
            store.add_tag("flat", "2000", 1, "photo", "풍경")
        (base / "LuvBox").mkdir(exist_ok=True)
        (base / "LuvBox/settings.json").write_text(
            json.dumps({"account": "offline", "output_dir": str(paths.output_dir), "gallery_density": 0}),
            encoding="utf-8")

        controller = ApplicationController()
        wheel_filter = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", controller.library.provider)
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel_filter})
        engine.load(ROOT / "assets/qt/MainWindow.qml")
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        # The window opens larger than before; the scene then uses a fixed size.
        assert (window.width(), window.height()) == (1280, 860), (window.width(), window.height())
        window.setWidth(1000)
        window.setHeight(700)
        window.setProperty("currentScreen", 1)
        library, model = controller.library, controller.library.model
        wait(lambda: not controller.busy and not model.busy and model.count == 60, "gallery loaded")
        grid = find("galleryGrid")
        settle(150)

        # --- Statistics: numbers arrive in the window, and the default size needs no scrollbar.
        stats = find("statisticsDialog")
        QMetaObject.invokeMethod(stats, "open")
        wait(lambda: stats.property("visible") and stats.property("loaded"), "statistics loaded in QML")
        assert find("statsFilesCard").property("value") == "60개", find("statsFilesCard").property("value")
        assert find("statsPostsCard").property("value") == "60건"
        assert find("statsKindsCard").property("value").startswith("사진 60")
        assert find("statsBytesCard").property("value") not in ("", "…")
        assert find("statsAuthorList").property("count") == 3
        assert find("statsMonthlyList").property("count") == 3
        assert find("statsUserTagsCard").property("visible") and not find("statsAiCategoriesCard").property("visible")
        assert not find("statsFlick").property("needsScroll"), "the default statistics window must not scroll"
        assert not find("statsScrollBar").property("visible") or find("statsScrollBar").property("size") >= 1
        shot("1-statistics")
        # A list inside the window moves 120 pixels a notch when it has more rows than fit.
        QMetaObject.invokeMethod(stats, "close")
        wait(lambda: not stats.property("visible"), "statistics closed")
        # A small window scrolls instead of cutting the lists off.
        window.setHeight(480)
        QMetaObject.invokeMethod(stats, "open")
        wait(lambda: stats.property("visible") and stats.property("loaded"), "statistics in a small window")
        assert find("statsFlick").property("needsScroll")
        flick = find("statsFlick")
        wheel(center(find("statsFilesCard")))
        settle(220)
        assert flick.property("contentY") > 0, flick.property("contentY")
        QMetaObject.invokeMethod(stats, "close")
        wait(lambda: not stats.property("visible"), "statistics closed again")
        window.setHeight(700)
        settle(120)

        # --- Wheel: about two notches for a row of tiles, faster while it keeps turning.
        QMetaObject.invokeMethod(grid, "forceActiveFocus")
        settle(60)
        inside = grid.mapToScene(QPointF(grid.width() / 2, grid.height() / 2)).toPoint()
        # The distance follows Windows' "lines per notch" setting; measure at its default of three.
        find("galleryWheel").setProperty("wheelLines", 3)
        cell = grid.property("cellHeight")
        expected = max(120, round(cell * 0.6))
        start = grid.property("contentY")
        wheel(inside)
        settle(260)
        first = grid.property("contentY") - start
        assert abs(first - expected) <= 2, (first, expected, cell)
        start = grid.property("contentY")
        for _ in range(3):
            wheel(inside)
            settle(20)
        settle(260)
        burst = grid.property("contentY") - start
        limit = grid.property("contentHeight") - grid.property("height") - start
        assert burst > expected * 3 or abs(burst - limit) < 2, (burst, expected, limit)
        wheel(inside, 120)
        settle(260)
        grid.setProperty("contentY", grid.property("originY"))
        settle(120)

        # --- Shift+click selects a range from the last clicked tile.
        def tile_point(index):
            columns = max(1, int(grid.width() // grid.property("cellWidth")))
            x = (index % columns + 0.5) * grid.property("cellWidth")
            y = (index // columns + 0.5) * grid.property("cellHeight") - (
                grid.property("contentY") - grid.property("originY"))
            return grid.mapToScene(QPointF(x, y)).toPoint(), columns

        keys = [row["key"] for row in model._rows]
        point, columns = tile_point(1)
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        settle(60)
        assert library.selectedCount == 0 and not library.selectionMode
        point, _ = tile_point(columns + 2)
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, point)
        wait(lambda: library.selectedCount == columns + 2, "shift range selected")
        assert library.selectedKeys == keys[1:columns + 3], library.selectedKeys
        assert library.selectionMode
        shot("2-shift-range")
        click("gallerySelectionClear")
        assert library.selectedCount == 0

        # --- Dragging draws a rectangle and selects the tiles it touches.
        begin, _ = tile_point(0)
        finish, _ = tile_point(columns + 1)
        QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, begin)
        steps = 6
        for step in range(1, steps + 1):
            QTest.mouseMove(window, QPoint(begin.x() + (finish.x() - begin.x()) * step // steps,
                                           begin.y() + (finish.y() - begin.y()) * step // steps))
            settle(15)
        wait(lambda: find("galleryBand").property("visible"), "selection rectangle shown")
        shot("3-drag-band")
        wait(lambda: library.selectedCount == 4, "dragged rectangle selected two by two")
        QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, finish)
        settle(60)
        assert not find("galleryBand").property("visible")
        assert set(library.selectedKeys) == {keys[0], keys[1], keys[columns], keys[columns + 1]}, library.selectedKeys
        # With Ctrl held a second rectangle adds to the first.
        begin, _ = tile_point(3)
        finish, _ = tile_point(4)
        QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, begin)
        for step in range(1, steps + 1):
            QTest.mouseMove(window, QPoint(begin.x() + (finish.x() - begin.x()) * step // steps, begin.y()))
            settle(15)
        wait(lambda: library.selectedCount == 6, "ctrl drag added two tiles")
        QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, finish)
        settle(60)
        # A plain click after a drag still works as a click.
        click("gallerySelectionClear")
        click("gallerySelectionToggle")
        assert not library.selectionMode and library.selectedCount == 0

        # --- The same drag after scrolling two rows down. The rectangle is under the pointer,
        # the tiles under it are the ones chosen, and the grid stays put while the pointer is
        # in the middle of the view. (Positions from the handler already include the scroll
        # offset; adding it again chose tiles further down and kept the grid scrolling.)
        grid.setProperty("contentY", grid.property("originY") + grid.property("cellHeight") * 2)
        settle(150)
        scrolled = grid.property("contentY")
        first = columns * 2
        begin, _ = tile_point(first)
        finish, _ = tile_point(first + columns + 1)
        QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, begin)
        for step in range(1, steps + 1):
            QTest.mouseMove(window, QPoint(begin.x() + (finish.x() - begin.x()) * step // steps,
                                           begin.y() + (finish.y() - begin.y()) * step // steps))
            settle(15)
        band = find("galleryBand")
        wait(lambda: band.property("visible"), "selection rectangle shown after scrolling")
        wait(lambda: library.selectedCount == 4, "dragged rectangle selected two by two after scrolling")
        corner = band.mapToItem(grid, QPointF(0, 0))
        pressed = grid.mapFromScene(QPointF(begin))
        assert abs(corner.x() - pressed.x()) <= 2 and abs(corner.y() - pressed.y()) <= 2, (corner, pressed)
        settle(150)
        assert abs(grid.property("contentY") - scrolled) < 1, (grid.property("contentY"), scrolled)
        shot("4-drag-band-scrolled")
        QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, finish)
        settle(60)
        assert set(library.selectedKeys) == {keys[first], keys[first + 1], keys[first + columns],
                                             keys[first + columns + 1]}, library.selectedKeys
        click("gallerySelectionClear")
        click("gallerySelectionToggle")
        assert not library.selectionMode and library.selectedCount == 0

        controller.requestClose()
        wait(lambda: controller.shutdownReady, "shutdown")
        window.close()
        wheel_filter.close()
        delete(engine)
        engine = None
        delete(controller)
        controller = None
        errors = [m for m in messages if any(term in m for term in (
            "ReferenceError", "TypeError", "Binding loop", "Cannot assign", "Cannot read", "is not a type",
            "Unable to assign"))]
        assert not errors, errors
        print(json.dumps({"ok": True, "statistics": True, "wheel": True, "selection": True, "qml_errors": errors}))
    except BaseException:
        import shutil
        import traceback
        traceback.print_exc()
        print("qt messages:", messages[-10:], file=sys.stderr)
        sys.stdout.flush()
        sys.stderr.flush()
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        os._exit(1)
    finally:
        if controller is not None:
            controller.requestClose()
            wait(lambda: controller.shutdownReady, "cleanup")
        if engine is not None:
            delete(engine)
        qInstallMessageHandler(previous)
        resources.close()


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class GalleryUsabilitySceneTests(unittest.TestCase):
    def test_statistics_wheel_and_selection_in_the_real_window(self):
        try:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"],
                                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=55,
                                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        except subprocess.TimeoutExpired as error:
            self.fail("scene timed out: " + str(error.stdout or "") + str(error.stderr or ""))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout.splitlines()[-1])["ok"])


if __name__ == "__main__":
    if "--scene" in sys.argv:
        folder = sys.argv[sys.argv.index("--screenshots") + 1] if "--screenshots" in sys.argv else None
        if folder is not None:
            Path(folder).mkdir(parents=True, exist_ok=True)
        scene(folder)
    else:
        unittest.main()
