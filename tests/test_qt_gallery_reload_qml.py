"""After the gallery's list is read again, nothing acts on a photo the person did not choose.

Another sort, filter or search reads the list again from its top. The highlight must
not stay on the old row number, which names another photo in the new list (or none):
Enter, Space, F and Delete would act on it, and opening it would wipe the message the
reload had just shown. The empty grid says there are no favorites only when the
favorites filter is the only thing narrowing the list. In the duplicate view a
double-click shows a photo large without picking it.
"""

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
NO_FAVORITES = "아직 즐겨찾기가 없어요 · 사진의 ★이나 F 키로 담아 보세요"
NO_MATCH = "조건에 맞는 사진이 없어요"


def scene(part, shots=None):
    """``part`` is "reload", "empty", "duplicates" or "doubleclick"; ``shots`` is a folder for window captures."""
    import faulthandler

    faulthandler.dump_traceback_later(50, exit=True)
    from contextlib import ExitStack, closing
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch

    from PIL import Image, ImageDraw
    from PySide6.QtCore import (QCoreApplication, QEvent, QMetaObject, QObject, QPointF, Qt,
                                qInstallMessageHandler)
    from PySide6.QtGui import QFont, QFontDatabase, QKeyEvent, QMouseEvent
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
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

    def every(name):
        found = []
        stack = [window.contentItem()]
        while stack:
            item = stack.pop()
            if item.objectName() == name and item.isVisible():
                found.append(item)
            stack.extend(item.childItems())
        return sorted(found, key=lambda item: (item.mapToScene(QPointF(0, 0)).y(), item.mapToScene(QPointF(0, 0)).x()))

    def shot(name):
        if shots is not None:
            settle(150)
            window.grabWindow().save(str(Path(shots) / (name + ".png")))

    def post_mouse(item, steps):
        # Posted, as the real event loop delivers them, and with their time: QTest.mouseClick
        # keeps Python's global lock while Qt handles the click, and a click that reloads the
        # gallery has Qt's image thread call the thumbnail provider (Python) at the same moment.
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        position = QPointF(window.mapToGlobal(point.toPoint()))
        start = int(time.monotonic() * 1000)
        for kind, after in steps:
            held = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
            event = QMouseEvent(kind, point, position, Qt.MouseButton.LeftButton, held, Qt.KeyboardModifier.NoModifier)
            event.setTimestamp(start + after)
            QCoreApplication.postEvent(window, event)
        settle(30)

    def click(item):
        # Kept apart from the click before, so two clicks never make a double click.
        settle(450)
        post_mouse(item, ((QEvent.Type.MouseButtonPress, 0), (QEvent.Type.MouseButtonRelease, 5)))

    def double_click(item):
        # What the system sends for a double click: press, release, press with a double-click event, release.
        settle(450)
        post_mouse(item, ((QEvent.Type.MouseButtonPress, 0), (QEvent.Type.MouseButtonRelease, 5),
                          (QEvent.Type.MouseButtonPress, 80), (QEvent.Type.MouseButtonDblClick, 80),
                          (QEvent.Type.MouseButtonRelease, 85)))

    def key(code):
        # Posted keys pass the window's shortcuts by; F, a shortcut, is typed with QTest below.
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QCoreApplication.postEvent(window, QKeyEvent(kind, code, Qt.KeyboardModifier.NoModifier))
        settle()

    try:
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-gallery-reload-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        base = Path(folder)
        paths = resolve_storage(base / "사진 폴더", "offline")

        def picture(seed):
            image = Image.new("RGB", (120, 90), (30 + seed * 47 % 200, 60 + seed * 13 % 120, 90 + seed * 29 % 140))
            draw = ImageDraw.Draw(image)
            for index in range(4):
                draw.ellipse((8 + index * 26, 10 + (seed * 37 + index * 53) % 50, 30 + index * 26,
                              32 + (seed * 37 + index * 53) % 50), fill=(250 - index * 40, 40 + seed * 30 % 200, 20))
            return image

        number = 0
        with closing(HistoryStore.for_storage(paths)) as store:
            def add(author, copy_of=None):
                nonlocal number
                number += 1
                tweet = str(1000 + number)
                path = paths.media_dir / f"{author}_{tweet}_1.png"
                if copy_of is not None:
                    path.write_bytes(copy_of.read_bytes())
                else:
                    picture(number).save(path)
                store.record_media("flat", {"tweet_id": tweet, "num": 1, "type": "photo", "author": {"name": author},
                                            "date": datetime(2026, 10, 1, 12, tzinfo=timezone(timedelta(hours=9)))
                                            + timedelta(minutes=number * 7 % 60, hours=number % 5)}, path)
                return path

            if part in ("reload", "empty"):
                for _ in range(50):
                    add("alice")
                for _ in range(8):
                    add("bob")
                store.toggle_favorite("flat", "1001", 1, "photo")
            else:
                for _ in range(4):                  # four pairs of the same file, and four photos of their own
                    add("carol", copy_of=add("carol"))
                for _ in range(4):
                    add("dave")
        (base / "LuvBox").mkdir(exist_ok=True)
        (base / "LuvBox/settings.json").write_text(
            json.dumps({"account": "offline", "output_dir": str(paths.output_dir)}), encoding="utf-8")

        controller = ApplicationController()
        wheel = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", controller.library.provider)
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel})
        engine.load(ROOT / "assets/qt/MainWindow.qml")
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        window.setWidth(1080)
        window.setHeight(760)
        window.setProperty("currentScreen", 1)
        library, model = controller.library, controller.library.model
        wait(lambda: not controller.busy and not model.busy and model.count == number, "gallery loaded")
        grid = find("galleryGrid")
        preview = find("previewDialog")

        def keys():
            return [row["key"] for row in model._rows]

        def favorites():
            return sum(1 for row in model._rows if row["favorite"])

        def highlight(row):
            """Walk the highlight to a row with the arrow keys, as a person does."""
            QMetaObject.invokeMethod(grid, "forceActiveFocus")
            settle()
            columns = max(1, int(grid.width() // grid.property("cellWidth")))
            for _ in range(80):
                current = grid.property("currentIndex")
                if current == row:
                    break
                if current < 0:
                    key(Qt.Key.Key_Right)
                else:
                    key(Qt.Key.Key_Down if current + columns <= row else
                        (Qt.Key.Key_Right if current < row else Qt.Key.Key_Left))
            assert grid.property("currentIndex") == row, (grid.property("currentIndex"), row)
            wait(lambda: library.selectedKey == keys()[row] and not library.selectionBusy, f"row {row} selected")

        def reloaded(generation, count=None):
            wait(lambda: library._generation > generation and not model.busy and not library.busy
                 and (count is None or model.count == count), "list read again")
            settle(150)

        def nothing_chosen(label):
            assert grid.property("currentIndex") == -1, (label, grid.property("currentIndex"))
            assert library.selectedKey == "", (label, library.selectedKey)
            assert not find("galleryOpenPreview").property("visible"), label

        if part == "reload":
            # A photo deep in the list is highlighted; then the sort changes.
            highlight(24)
            wait(lambda: grid.property("contentY") - grid.property("originY") > grid.property("cellHeight"),
                 "grid scrolled to the photo")
            shot("1-deep-photo")
            generation = library._generation
            library.setFilters("all", "posted", "")
            reloaded(generation, number)
            nothing_chosen("after another sort")
            assert grid.property("contentY") - grid.property("originY") < 1, "the new list starts at its top"
            # Nothing is current, so F, Enter and Space have nothing to act on.
            QTest.keyClick(window, Qt.Key.Key_F)
            settle(150)
            assert favorites() == 1 and not window.property("transientNotice").startswith("즐겨찾기에"), (
                favorites(), window.property("transientNotice"))
            key(Qt.Key.Key_Return)
            key(Qt.Key.Key_Space)
            settle(150)
            assert not preview.property("visible"), "Enter or Space opened a photo nobody chose"
            shot("2-after-sort")

            # A search that leaves fewer photos than the old row number.
            highlight(24)
            generation = library._generation
            library.search("bob")
            reloaded(generation, 8)
            nothing_chosen("after a short search")
            key(Qt.Key.Key_Return)
            settle(150)
            assert not preview.property("visible"), "Enter opened a preview of a row past the end"

            # A reload that reports something keeps its message: no photo is opened by itself.
            highlight(3)
            generation = library._generation
            library._refresh(reload_authors=False, status="목록을 다시 읽었습니다.")
            reloaded(generation, 8)
            settle(200)
            nothing_chosen("after a reload with a message")
            assert library.status == "목록을 다시 읽었습니다.", library.status
            assert find("galleryWorkStatus").property("visible")
            shot("3-message-kept")

            # A photo is chosen again with the keys after a reload, as before.
            highlight(2)

        elif part == "empty":
            # The empty grid: favorites hidden by another condition are not "no favorites".
            empty = find("galleryEmptyState")
            generation = library._generation
            library.setFavoriteOnly(True)
            reloaded(generation, 1)
            generation = library._generation
            assert library.setAdvancedFilters("bob", "posted", "", "")
            reloaded(generation, 0)
            wait(lambda: empty.property("visible"), "empty grid")
            assert empty.property("text") == NO_MATCH, empty.property("text")
            shot("4-favorites-and-author")
            generation = library._generation
            assert library.setAdvancedFilters("", "posted", "", "")
            reloaded(generation, 1)
            search = find("searchBox")
            search.setProperty("text", "nothing-like-this")
            generation = library._generation
            library.search("nothing-like-this")
            reloaded(generation, 0)
            wait(lambda: empty.property("visible") and empty.property("text") == NO_MATCH,
                 "favorites and a search that finds nothing")
            search.setProperty("text", "")
            generation = library._generation
            library.search("")
            reloaded(generation, 1)
            kind = find("galleryMediaFilter")
            kind.setProperty("currentIndex", 2)
            generation = library._generation
            library.setFilters("video", "posted", "")
            reloaded(generation, 0)
            wait(lambda: empty.property("visible") and empty.property("text") == NO_MATCH, "favorites and videos")
            kind.setProperty("currentIndex", 0)
            generation = library._generation
            library.setFilters("all", "posted", "")
            reloaded(generation, 1)
            generation = library._generation
            assert library.setAdvancedFilters("", "posted", "2020-01-01", "2020-01-31")
            reloaded(generation, 0)
            wait(lambda: empty.property("visible") and empty.property("text") == NO_MATCH, "favorites and a date range")
            generation = library._generation
            assert library.setAdvancedFilters("", "posted", "", "")
            reloaded(generation, 1)
            # With favorites the only condition, an empty grid does mean there are none.
            library.toggleFavorite(keys()[0])
            generation = library._generation
            library.setFavoriteOnly(False)
            reloaded(generation, number)
            generation = library._generation
            library.setFavoriteOnly(True)
            reloaded(generation, 0)
            wait(lambda: empty.property("visible") and empty.property("text") == NO_FAVORITES, "no favorites at all")

        elif part in ("duplicates", "doubleclick"):
            generation = library._generation
            click(find("galleryDuplicateFilter"))
            reloaded(generation)
            assert library.filterDuplicateOnly
            click(find("galleryScanDuplicates"))
            wait(lambda: not library.busy and library.duplicateCount == 8 and not model.busy and model.count == 8,
                 "scan finished")
        else:
            raise AssertionError("unknown part " + part)

        if part == "duplicates":
            generation = library._generation
            click(find("galleryDuplicateFilter"))
            reloaded(generation, number)
            assert not library.filterDuplicateOnly
            kind = find("galleryMediaFilter")
            order = find("galleryOrderChoice")
            assert kind.property("enabled") and order.property("enabled")
            # Leaving "영상" selected must not blank duplicate photos or leave the media type control active.
            kind.setProperty("currentIndex", 2)
            generation = library._generation
            library.setFilters("video", "saved", "")
            reloaded(generation, 0)
            generation = library._generation
            click(find("galleryDuplicateFilter"))
            reloaded(generation, 8)
            assert library.filterDuplicateOnly
            assert not kind.property("enabled") and not order.property("enabled")
            generation = library._generation
            click(find("galleryDuplicateFilter"))
            reloaded(generation, 0)
            assert not library.filterDuplicateOnly
            assert kind.property("enabled") and kind.property("currentIndex") == 2 and library._media_type == "video"
            kind.setProperty("currentIndex", 0)
            generation = library._generation
            library.setFilters("all", "saved", "")
            reloaded(generation, number)
            # A photo of the flat grid is highlighted, then the duplicate view is opened. Its grid
            # is hidden; the old row number must not open one of the duplicates by itself.
            highlight(3)
            generation = library._generation
            click(find("galleryDuplicateFilter"))
            reloaded(generation, 8)
            assert library.filterDuplicateOnly
            nothing_chosen("in the duplicate view")
            QTest.keyClick(window, Qt.Key.Key_F)
            settle(200)
            wait(lambda: not library.duplicatesSettling and not model.busy, "duplicate view settled")
            assert favorites() == 0 and not window.property("transientNotice").startswith("즐겨찾기에"), (
                "F favorited a duplicate nobody chose", favorites(), window.property("transientNotice"))

        elif part == "doubleclick":
            wait(lambda: len(every("duplicateTile")) >= 4, "duplicate cards shown")
            tiles = every("duplicateTile")
            first, second = tiles[0], tiles[1]          # the first card's two photos
            first_key, second_key = first.property("modelData")["key"], second.property("modelData")["key"]
            # A double-click shows the photo large and leaves it unpicked.
            double_click(first)
            wait(lambda: preview.property("visible") and library.selectedKey == first_key, "preview of a duplicate")
            assert library.selectedCount == 0 and not library.selectionMode, (
                "the double-click picked the photo", library.selectedCount, library.selectionMode)
            shot("1-double-click")
            QMetaObject.invokeMethod(preview, "close")
            wait(lambda: not preview.property("visible"), "preview closed")
            assert not find("gallerySelectionBar").property("visible")
            # A single click still picks; a double-click elsewhere leaves that pick alone.
            click(first)
            wait(lambda: library.selectedCount == 1 and library.isSelected(first_key), "photo picked by a click")
            double_click(second)
            wait(lambda: preview.property("visible") and library.selectedKey == second_key, "preview of another")
            assert library.selectedCount == 1 and library.isSelected(first_key) and not library.isSelected(second_key), (
                library.selectedKeys)
            QMetaObject.invokeMethod(preview, "close")
            wait(lambda: not preview.property("visible"), "preview closed again")
            # A double-click on a picked photo leaves it picked.
            double_click(first)
            wait(lambda: preview.property("visible") and library.selectedKey == first_key, "preview of the picked one")
            assert library.selectedCount == 1 and library.isSelected(first_key), library.selectedKeys
            QMetaObject.invokeMethod(preview, "close")
            wait(lambda: not preview.property("visible"), "preview closed a third time")

        controller.requestClose()
        wait(lambda: controller.shutdownReady, "shutdown")
        window.close()
        wheel.close()
        delete(engine)
        engine = None
        delete(controller)
        controller = None
        errors = [m for m in messages if any(term in m for term in (
            "ReferenceError", "TypeError", "Binding loop", "Cannot assign", "Cannot read", "is not a type",
            "Unable to assign"))]
        assert not errors, errors
        print(json.dumps({"ok": True, "part": part, "qml_errors": errors}))
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
class GalleryReloadSceneTests(unittest.TestCase):
    def run_scene(self, part):
        try:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene", part],
                                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
                                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        except subprocess.TimeoutExpired as error:
            self.fail("scene timed out: " + str(error.stdout or "") + str(error.stderr or ""))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout.splitlines()[-1])["ok"])

    def test_a_list_read_again_highlights_nothing_and_keeps_its_message(self):
        self.run_scene("reload")

    def test_the_empty_grid_says_no_favorites_only_when_favorites_alone_narrow_it(self):
        self.run_scene("empty")

    def test_the_duplicate_view_opens_nothing_the_flat_grid_had_highlighted(self):
        self.run_scene("duplicates")

    def test_a_double_click_in_the_duplicate_view_shows_a_photo_without_picking_it(self):
        self.run_scene("doubleclick")


if __name__ == "__main__":
    if "--scene" in sys.argv:
        folder = sys.argv[sys.argv.index("--screenshots") + 1] if "--screenshots" in sys.argv else None
        if folder is not None:
            Path(folder).mkdir(parents=True, exist_ok=True)
        scene(sys.argv[sys.argv.index("--scene") + 1], folder)
    else:
        unittest.main()
