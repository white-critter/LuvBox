"""The unsaved-media page scrolls on like the gallery and picks tiles the same way."""

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
TOTAL = 30
PAGE = 12


def scene(shots=None):
    """``shots`` is a folder for window captures at each step, for a look at the result."""
    import faulthandler

    faulthandler.dump_traceback_later(50, exit=True)
    from contextlib import ExitStack, closing
    from unittest.mock import patch

    from PIL import Image
    from PySide6.QtCore import Q_ARG, QCoreApplication, QEvent, QMetaObject, QObject, QPoint, QPointF, Qt, qInstallMessageHandler
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from PySide6.QtQml import QQmlApplicationEngine, QQmlExpression, qmlContext
    from PySide6.QtQuickControls2 import QQuickStyle
    from shiboken6 import delete

    sys.path.insert(0, str(ROOT))
    from luvbox import thumbcache
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
            settle(200)
            window.grabWindow().save(str(Path(shots) / (name + ".png")))

    def click(name):
        settle(40)
        item = find(name)
        assert item.property("visible") and item.property("enabled"), name
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        settle(40)

    try:
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-unsaved-review-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        resources.enter_context(patch("luvbox.qt_records._UNSAVED_PAGE", PAGE))
        base = Path(folder)
        paths = resolve_storage(base / "사진 폴더", "offline")
        with closing(HistoryStore.for_storage(paths)) as store:
            for index in range(TOTAL):
                tweet = str(5000 + index)
                store.record_declined("flat", {"tweet_id": tweet, "num": 1, "type": "photo",
                                               "author": {"name": f"writer{index:02d}"}},
                                      paths.media_dir / f"writer_{tweet}_1.jpg")
        cache = thumbcache.Cache("offline")
        try:
            for index in range(0, TOTAL, 2):       # every other one has a picture kept on this PC
                cache.put((str(5000 + index), 1, "photo"),
                          Image.new("RGB", (96, 72), (40 + index * 7, 90, 200 - index * 5)))
        finally:
            cache.close()
        (base / "LuvBox").mkdir(exist_ok=True)
        (base / "LuvBox/settings.json").write_text(
            json.dumps({"account": "offline", "output_dir": str(paths.output_dir), "gallery_density": 0}),
            encoding="utf-8")

        controller = ApplicationController()
        wheel_filter = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", controller.library.provider)
        # Handed over without keeping it, as the app does: the engine owns the picture source.
        engine.addImageProvider("unsavedpreview", controller.records.preview_provider())
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel_filter})
        engine.load(ROOT / "assets/qt/MainWindow.qml")
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        window.setWidth(1000)
        window.setHeight(720)
        window.setProperty("currentScreen", 1)
        records = controller.records
        wait(lambda: not controller.busy and records.excludedCount == TOTAL, "excluded media counted")

        # The gallery's button opens the page on the list that has something.
        button = find("galleryUnsavedButton")
        assert button.property("visible") and button.property("text") == "저장 못 한 미디어", button.property("text")
        click("galleryUnsavedButton")
        grid = find("unsavedGrid")
        wait(lambda: window.property("currentScreen") == 2 and records.unsavedView == "excluded"
             and not records.unsavedBusy and grid.property("count") >= PAGE, "page opened")
        # The first page does not fill the window, so the next one follows at once; then it waits for scrolling.
        wait(lambda: not records.unsavedBusy and grid.property("count") == 2 * PAGE, "second page follows")
        settle(150)
        assert grid.property("count") == 2 * PAGE and records.unsavedMore
        assert find("unsavedLoadedCount").property("text") == f"{TOTAL}개 중 {2 * PAGE}개 불러옴"
        columns = grid.property("columns")
        assert columns >= 3, columns
        # A tile asks for its picture when it is shown: the rows carry none, and only tiles
        # near the view exist. Every other media has a picture kept on this PC.
        assert all(len(row["preview"]) < 80 for row in records.unsavedRows)
        with_picture = {row["token"] for row in records.unsavedRows if row["preview"]}
        assert len(with_picture) == PAGE, len(with_picture)

        def pictures():
            found = {}

            def walk(item):
                for child in item.childItems():
                    if child.objectName() == "unsavedPreview":
                        found[child.parentItem().parentItem().property("token")] = child
                    walk(child)

            walk(grid.property("contentItem"))
            return found

        # The picture is shown only once it is read (visible: status === Image.Ready).
        wait(lambda: any(item.property("visible") for item in pictures().values()), "a picture arrived")
        wait(lambda: all(bool(item.property("visible")) == (token in with_picture)
                         for token, item in pictures().items()), "pictures only where one is kept")
        assert 0 < len(pictures()) < 2 * PAGE + 1
        shown = next(item for token, item in pictures().items() if token in with_picture)
        assert shown.property("paintedWidth") > 0 and shown.property("visible")
        shot("1-opened")

        def tile_point(index):
            x = (index % columns + 0.5) * grid.property("cellWidth")
            y = (index // columns + 0.5) * grid.property("cellHeight") - (
                grid.property("contentY") - grid.property("originY"))
            return grid.mapToScene(QPointF(x, y)).toPoint()

        def picked():
            return sorted(records.unsavedRows.index(row) for row in records.unsavedRows
                          if row["token"] in set(records.unsavedSelection))

        # Scrolling to the end brings the rest, under what is shown: the view stays where it is.
        first_tokens = [row["token"] for row in records.unsavedRows]
        bottom = grid.property("originY") + max(0, grid.property("contentHeight") - grid.property("height"))
        assert bottom > grid.property("originY") + 10, "the list is taller than the view"
        grid.setProperty("contentY", bottom)
        wait(lambda: not records.unsavedBusy and grid.property("count") == TOTAL, "the rest arrived on scrolling")
        settle(100)
        assert grid.property("contentY") >= bottom - 1, (grid.property("contentY"), bottom)
        assert [row["token"] for row in records.unsavedRows][:2 * PAGE] == first_tokens
        assert not records.unsavedMore
        assert find("unsavedLoadedCount").property("text") == f"{TOTAL}개"
        grid.setProperty("contentY", grid.property("originY"))
        settle(80)

        # A click picks, Shift+click picks the range from it.
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, tile_point(1))
        wait(lambda: picked() == [1], "one tile picked")
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, tile_point(columns + 2))
        wait(lambda: picked() == list(range(1, columns + 3)), "shift range picked")
        assert find("unsavedRetry").property("text") == f"선택한 {columns + 2}개 다시 받기"
        assert find("unsavedSettle").property("text") == f"선택한 {columns + 2}개 제외 해제"
        shot("2-shift-range")
        # Esc lets go of what is picked and stays on the page.
        QTest.keyClick(window, Qt.Key.Key_Escape)
        wait(lambda: records.unsavedSelectedCount == 0, "esc cleared the selection")
        assert window.property("currentScreen") == 2

        # Without a mouse: the accessibility interface toggles or presses a tile, as a screen reader does.
        def tile_items():
            found = []

            def walk(item):
                for child in item.childItems():
                    if child.objectName() == "unsavedTile":
                        found.append(child)
                    walk(child)

            walk(grid.property("contentItem"))
            return found

        some = tile_items()[0]
        for action in ("toggleAction", "pressAction"):
            QQmlExpression(qmlContext(some), some, f"Accessible.{action}()").evaluate()
            wait(lambda: list(records.unsavedSelection) == [some.property("token")], action + " picks the tile")
            QQmlExpression(qmlContext(some), some, f"Accessible.{action}()").evaluate()
            wait(lambda: records.unsavedSelectedCount == 0, action + " releases the tile")

        # Dragging draws a rectangle and picks the tiles it touches.
        begin, finish = tile_point(0), tile_point(columns + 1)
        QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, begin)
        for step in range(1, 7):
            QTest.mouseMove(window, QPoint(begin.x() + (finish.x() - begin.x()) * step // 6,
                                           begin.y() + (finish.y() - begin.y()) * step // 6))
            settle(15)
        band = find("unsavedBand")
        wait(lambda: band.property("visible"), "selection rectangle shown")
        wait(lambda: picked() == [0, 1, columns, columns + 1], "rectangle picked two by two")
        shot("3-drag-band")
        QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, finish)
        settle(60)
        assert not band.property("visible")
        assert picked() == [0, 1, columns, columns + 1], picked()
        # With Ctrl held a second rectangle adds to the first.
        middle = tile_point(2)
        begin, finish = QPoint(middle.x() - 30, middle.y()), QPoint(middle.x() + 30, middle.y())
        QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, begin)
        for step in range(1, 7):
            QTest.mouseMove(window, QPoint(begin.x() + (finish.x() - begin.x()) * step // 6, begin.y()))
            settle(15)
        wait(lambda: picked() == [0, 1, 2, columns, columns + 1], "ctrl drag added")
        QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, finish)
        settle(60)

        # Ctrl+A picks every loaded tile.
        QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        wait(lambda: records.unsavedSelectedCount == TOTAL, "ctrl+a picked all loaded")
        assert find("unsavedSelectPage").property("text") == "선택 해제"
        click("unsavedSelectPage")
        wait(lambda: records.unsavedSelectedCount == 0, "selection cleared by its button")

        # Putting two away from further down reads the list again without jumping to the top.
        target = grid.property("originY") + 2 * grid.property("cellHeight")
        grid.setProperty("contentY", target)
        settle(120)
        row = round((grid.property("contentY") - grid.property("originY")) / grid.property("cellHeight")) + 1
        first, second = row * columns, row * columns + 1
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, tile_point(first))
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, tile_point(second))
        wait(lambda: picked() == [first, second], "two tiles further down picked")
        before = grid.property("contentY")
        click("unsavedSettle")
        wait(lambda: records.excludedCount == TOTAL - 2 and not records.unsavedBusy
             and grid.property("count") == TOTAL - 2, "two exclusions released")
        settle(150)
        assert abs(grid.property("contentY") - before) <= grid.property("cellHeight"), (grid.property("contentY"), before)
        assert grid.property("contentY") > grid.property("originY") + 10, "the view did not jump to the top"
        assert records.unsavedSelectedCount == 0
        shot("4-after-release")

        # Esc with nothing picked returns to the gallery.
        QTest.keyClick(window, Qt.Key.Key_Escape)
        wait(lambda: window.property("currentScreen") == 1, "esc returned to the gallery")

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
        print(json.dumps({"ok": True, "scrolled": True, "picked": True, "qml_errors": errors}))
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
class UnsavedReviewSceneTests(unittest.TestCase):
    def test_the_page_scrolls_on_and_picks_like_the_gallery(self):
        try:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"],
                                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
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
