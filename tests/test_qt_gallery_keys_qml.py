"""Keys, focus and the window's height in the main window, as found by the 1.10.8 inspection.

Each part drives the real MainWindow.qml with real controllers on generated photos:

duplicates  the duplicate view's buttons do not keep the keys after a click, the hidden grid takes
            no keys while the duplicate view shows, and "사본 일괄 선택" does nothing outside it
escape      Esc that clears the highlight makes the library forget the photo too, so the next page
            does not bring the highlight and the scroll position back, and F stars nothing
titlebar    통계 opened with the mouse and closed leaves the keys with the photos
resize      a new thumbnail size does not highlight the first tile when none was highlighted
savecard    with "자세히 보기" on, the window only grows while files are saved and returns to its
            own height when the card goes; a height the person set meanwhile is kept
maximized   a card that goes away while the window is maximized leaves the restored window at
            the person's own height, and that is the height remembered
composing   a syllable an input method is still composing (the last one of a Korean word) is
            part of what is searched for
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
SCREEN = {"synchronousWindowSystemEvents": False, "windowFrameMargins": False,
          "screens": [{"name": "main", "x": 0, "y": 0, "width": 1920, "height": 1080,
                       "logicalDpi": 96, "logicalBaseDpi": 96, "dpr": 1}]}
PARTS = {"duplicates": dict(photos=20, pairs=4), "escape": dict(photos=450), "titlebar": dict(photos=20),
         "resize": dict(photos=60), "savecard": dict(photos=6, width=1280, height=860, screen=0),
         "maximized": dict(photos=6, width=760, height=560, screen=0), "composing": dict(photos=30)}


def scene(part):
    import faulthandler

    # A scene that stops responding prints every thread's stack and ends before the
    # parent test's own timeout, so the report says where it stopped.
    faulthandler.dump_traceback_later(100, exit=True)
    from contextlib import ExitStack, closing
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch

    from PIL import Image, ImageDraw
    from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, Qt, qInstallMessageHandler
    from PySide6.QtGui import QFont, QFontDatabase, QKeyEvent, QMouseEvent, QWindow
    from PySide6.QtQml import QQmlApplicationEngine, QQmlEngine, QQmlExpression
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from shiboken6 import delete

    sys.path.insert(0, str(ROOT))
    from luvbox.history import HistoryStore
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_download import _saved_item
    from luvbox.qt_wheel import WheelInputFilter
    from luvbox.storage import resolve_storage

    setup = dict(photos=20, pairs=0, width=1080, height=760, screen=1)
    setup.update(PARTS[part])
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

    def wait(predicate, label, seconds=10):
        until = time.monotonic() + seconds
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
        found, stack = [], [window.contentItem()]
        while stack:
            item = stack.pop()
            if item.objectName() == name and item.isVisible():
                found.append(item)
            stack.extend(item.childItems())
        return sorted(found, key=lambda item: (item.mapToScene(QPointF(0, 0)).y(), item.mapToScene(QPointF(0, 0)).x()))

    def qml(item, expression):
        evaluation = QQmlExpression(QQmlEngine.contextForObject(item), item, expression)
        value = evaluation.evaluate()
        assert not evaluation.hasError(), evaluation.error().toString()
        return value[0] if isinstance(value, tuple) else value

    def click(item):
        # A control that has just appeared is placed a moment later; posted events avoid the
        # stop inside QTest.mouseClick while thumbnails load.
        settle(450)
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        position = QPointF(window.mapToGlobal(point.toPoint()))
        start = int(time.monotonic() * 1000)
        for kind, held, after in ((QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton, 0),
                                  (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton, 5)):
            event = QMouseEvent(kind, point, position, Qt.MouseButton.LeftButton, held,
                                Qt.KeyboardModifier.NoModifier)
            event.setTimestamp(start + after)      # a tile tells a click from a press held by its time
            QCoreApplication.postEvent(window, event)
        settle(30)

    def key(code):
        """Posted: reaches the item that has the keys, past the window's shortcuts."""
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QCoreApplication.postEvent(window, QKeyEvent(kind, code, Qt.KeyboardModifier.NoModifier))
        settle(60)

    def shortcut(code):
        """Through the shortcut map first, as a pressed key goes."""
        settle(60)
        QTest.keyClick(window, code)
        settle(60)

    def focused():
        item = window.property("activeFocusItem")
        while item is not None and not item.objectName():
            item = item.parentItem()
        return item.objectName() if item is not None else ""

    try:
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-keys-scene-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        base = Path(folder)
        paths = resolve_storage(base / "photos", "offline")

        def picture(seed):
            image = Image.new("RGB", (96, 72), (30 + seed * 47 % 200, 60 + seed * 13 % 120, 90 + seed * 29 % 140))
            ImageDraw.Draw(image).text((4, 56), str(seed), fill=(255, 255, 255))
            return image

        number = 0
        with closing(HistoryStore.for_storage(paths)) as store:
            def add(author, copy_of=None):
                nonlocal number
                number += 1
                tweet = str(100000 + number)
                path = paths.media_dir / f"{author}_{tweet}_1.png"
                if copy_of is not None:
                    path.write_bytes(copy_of.read_bytes())
                else:
                    picture(number).save(path)
                store.record_media("flat", {"tweet_id": tweet, "num": 1, "type": "photo", "author": {"name": author},
                                            "date": datetime(2026, 10, 1, 12, tzinfo=timezone(timedelta(hours=9)))
                                            + timedelta(minutes=number)}, path)
                return path

            for _ in range(setup["pairs"]):
                add("carol", copy_of=add("carol"))
            for index in range(setup["photos"]):
                add(("alice", "bob", "dave")[index % 3])
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
        window.setWidth(setup["width"])
        window.setHeight(setup["height"])
        window.setProperty("currentScreen", setup["screen"])
        library, model, management = controller.library, controller.library.model, controller.management
        wait(lambda: not controller.busy and not model.busy and model.count >= min(number, 200), "gallery loaded", 20)
        settle(200)
        grid = find("galleryGrid")
        preview = find("previewDialog")
        report = {}

        if part == "duplicates":
            generation = library._generation
            click(find("galleryDuplicateFilter"))
            wait(lambda: library._generation > generation and not model.busy and not library.busy, "duplicate view")
            # Started without a click, so the keys are where they were on entering: with the grid.
            library.scanDuplicates(False)
            wait(lambda: not library.busy and library.duplicateCount == 8 and not model.busy and model.count == 8,
                 "scan finished", 30)
            wait(lambda: len(every("duplicateTile")) >= 2, "cards shown")
            settle(300)
            # The grid under the duplicate view takes no keys: nothing is highlighted, opened or sent.
            key(Qt.Key.Key_Right)
            key(Qt.Key.Key_Right)
            key(Qt.Key.Key_Return)
            shortcut(Qt.Key.Key_Delete)
            settle(300)
            report["hidden_grid"] = [grid.property("currentIndex"), library.selectedKey,
                                     bool(preview.property("visible")), management.deleteConfirmationPending]
            assert report["hidden_grid"] == [-1, "", False, False], report
            # A click on the button picks the copies and leaves the keys where they were.
            click(find("gallerySelectDuplicates"))
            wait(lambda: library.selectedCount == 4, "copies picked")
            assert focused() != "gallerySelectDuplicates", focused()
            # Sent from here, "다음 저장에서 다시 받지 않기" starts on: fetched again, a copy is a duplicate again.
            confirmation = find("deleteConfirmation")
            shortcut(Qt.Key.Key_Delete)
            wait(lambda: bool(confirmation.property("visible")), "confirmation from the duplicate view")
            settle(200)
            assert find("deleteKeepSwitch").property("checked") is True
            shortcut(Qt.Key.Key_Escape)
            wait(lambda: not confirmation.property("visible") and not management.deleteConfirmationPending,
                 "confirmation closed")
            shortcut(Qt.Key.Key_Escape)
            wait(lambda: library.selectedCount == 0, "selection cleared")
            generation = library._generation
            shortcut(Qt.Key.Key_D)
            wait(lambda: library._generation > generation and not model.busy and not library.busy
                 and model.count == number, "ordinary gallery again")
            settle(300)
            holder = window.property("activeFocusItem")
            assert holder is not None and holder.isVisible(), focused()
            shortcut(Qt.Key.Key_Space)
            settle(300)
            report["space_after_leaving"] = [library.selectedCount, library.selectionMode,
                                             bool(find("gallerySelectionBar").property("visible"))]
            assert report["space_after_leaving"] == [0, False, False], report
            # And asked directly outside the duplicate view, it picks nothing.
            assert library.selectDuplicateCopies() == 0 and library.selectedCount == 0
            # From the ordinary gallery the switch starts off, as it always did.
            click(every("galleryTile")[0])
            wait(lambda: library.selectedKey != "" and not library.selectionBusy, "photo opened")
            shortcut(Qt.Key.Key_Delete)
            wait(lambda: bool(confirmation.property("visible")), "confirmation from the gallery")
            settle(200)
            assert find("deleteKeepSwitch").property("checked") is False
            shortcut(Qt.Key.Key_Escape)
            wait(lambda: not confirmation.property("visible"), "confirmation closed again")

        elif part == "escape":
            tiles = every("galleryTile")
            click(tiles[2])
            wait(lambda: library.selectedKey != "" and not library.selectionBusy, "photo opened")
            assert grid.property("currentIndex") == 2
            shortcut(Qt.Key.Key_Escape)
            wait(lambda: library.selectedKey == "", "the library forgot the photo")
            assert grid.property("currentIndex") == -1 and library.selectedIndex == -1
            # F has no photo to star now.
            shortcut(Qt.Key.Key_F)
            settle(200)
            assert not any(row["favorite"] for row in model._rows)
            # Scrolling to the end of what is loaded reads the next page; the place stays.
            origin = grid.property("originY")
            loaded = model.count
            bottom = origin + max(0, grid.property("contentHeight") - grid.height())
            grid.setProperty("contentY", bottom)
            wait(lambda: model.count > loaded and not model.busy, "next page", 20)
            settle(400)
            report["after_next_page"] = [round(grid.property("contentY") - origin), grid.property("currentIndex")]
            assert report["after_next_page"][0] >= round(bottom - origin) - 2, report
            assert report["after_next_page"][1] == -1, report

        elif part == "titlebar":
            click(every("galleryTile")[0])
            wait(lambda: library.selectedKey != "" and not library.selectionBusy, "photo opened")
            statistics = find("statisticsDialog")
            click(find("galleryStatisticsButton"))
            wait(lambda: bool(statistics.property("visible")), "statistics opened")
            shortcut(Qt.Key.Key_Escape)
            wait(lambda: not statistics.property("visible"), "statistics closed")
            settle(200)
            assert focused() != "galleryStatisticsButton", focused()
            key(Qt.Key.Key_Right)
            assert grid.property("currentIndex") == 1, grid.property("currentIndex")
            shortcut(Qt.Key.Key_Space)
            wait(lambda: bool(preview.property("visible")), "preview opened by Space")
            assert not statistics.property("visible")
            shortcut(Qt.Key.Key_Escape)
            wait(lambda: not preview.property("visible"), "preview closed")

        elif part == "composing":
            from PySide6.QtGui import QInputMethodEvent

            box = find("searchBox")
            click(box)
            wait(lambda: focused() == "searchBox", "search box has the keys")

            def composed(preedit, commit=""):
                event = QInputMethodEvent(preedit, [])
                if commit:
                    event.setCommitString(commit)
                QCoreApplication.sendEvent(box, event)
                settle(20)

            # "alice" as Hangul arrives: a syllable is committed when the next begins, the last stays composed.
            composed("al")
            composed("ic", commit="al")
            composed("e", commit="ic")
            wait(lambda: library._query == "alice" and not model.busy, "searched with the composed syllable")
            report["composing"] = [box.property("text"), box.property("displayText"), model.total]
            assert report["composing"] == ["alic", "alice", 10], report

        elif part == "resize":
            assert grid.property("currentIndex") == -1
            qml(window, "setDensity(2)")
            wait(lambda: grid.property("cellHeight") > 200, "larger tiles")
            wait(lambda: bool(grid.property("highlightFollowsCurrentItem")), "position kept")
            settle(400)
            assert grid.property("currentIndex") == -1, grid.property("currentIndex")
            # A highlighted photo stays the highlighted one.
            click(every("galleryTile")[1])
            wait(lambda: library.selectedKey != "" and not library.selectionBusy, "photo opened")
            qml(window, "setDensity(0)")
            wait(lambda: grid.property("cellHeight") < 200, "smaller tiles")
            wait(lambda: bool(grid.property("highlightFollowsCurrentItem")), "position kept")
            settle(400)
            assert grid.property("currentIndex") == 1, grid.property("currentIndex")

        else:
            download = controller.download
            pictures = base / "cards"
            pictures.mkdir()

            def saved(serial, lines):
                target = pictures / f"p{serial}.png"
                Image.new("RGB", (600, 400), (20 * serial % 255, 90, 120)).save(target)
                download._running = True
                download._current_saved = _saved_item({
                    "path": str(target), "author": "example", "display_name": "Example",
                    "body": "\n".join("line %d" % index for index in range(lines)), "media_type": "photo"})
                download._invalidate_preview()
                download._request_preview()
                download.savedItemChanged.emit()
                wait(lambda: bool(download.currentSavedItem.get("preview_url")), "the card's picture")
                settle(500)
                return window.height()

            window.setGeometry(100, 40, setup["width"], setup["height"])
            settle(500)
            own = window.height()
            download._running = True
            download.setDetailsEnabled(True)
            settle(300)
            if part == "savecard":
                heights = [saved(serial, lines) for serial, lines in enumerate((12, 0, 3, 0, 8, 1))]
                report["heights"] = heights
                # Grown for the long text, and never smaller again while the card shows.
                assert heights[0] > own + 40, (own, heights)
                assert heights == sorted(heights), heights
                # A height the person sets meanwhile is kept for the files that follow.
                window.setHeight(own + 20)
                settle(300)
                assert saved(7, 12) == own + 20, (own, window.height())
                download.setDetailsEnabled(False)
                wait(lambda: window.height() == own, "own height again")
            else:
                grown = saved(0, 12)
                assert grown > own + 40, (own, grown)
                window.showMaximized()
                wait(lambda: window.visibility() == QWindow.Visibility.Maximized, "maximized")
                settle(600)
                download.setDetailsEnabled(False)
                settle(500)
                window.showNormal()
                wait(lambda: window.visibility() == QWindow.Visibility.Windowed, "normal again")
                wait(lambda: window.height() == own, "own height after the restore")
                settle(600)                    # the place to remember is taken a moment later
                report["remembered"] = round(window.property("normalPlace").height())
                assert report["remembered"] == own, (own, report)
            download._running = False

        controller.requestClose()
        wait(lambda: controller.shutdownReady, "shutdown", 15)
        window.close()
        wheel.close()
        delete(engine)
        engine = None
        delete(controller)
        controller = None
        errors = [message for message in messages if any(term in message for term in (
            "ReferenceError", "TypeError", "Binding loop", "Cannot assign", "Cannot read", "is not a type",
            "Unable to assign"))]
        assert not errors, errors
        print(json.dumps({"ok": True, "report": report}, ensure_ascii=True))
        sys.stdout.flush()
    except BaseException:
        import shutil
        import traceback

        traceback.print_exc()
        print("qt messages:", [message.encode("ascii", "backslashreplace").decode() for message in messages[-10:]],
              file=sys.stderr)
        sys.stdout.flush()
        sys.stderr.flush()
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        os._exit(1)
    finally:
        if controller is not None:
            controller.requestClose()
            wait(lambda: controller.shutdownReady, "cleanup", 15)
        if engine is not None:
            delete(engine)
        qInstallMessageHandler(previous)
        resources.close()


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class GalleryKeysSceneTests(unittest.TestCase):
    def run_scene(self, part):
        # A 1920x1080 screen, so a window of the default size has room to grow for a card. The
        # offscreen platform reads the file's name relative to the folder it starts in.
        with tempfile.TemporaryDirectory(prefix="luvbox-keys-screen-") as folder:
            Path(folder, "screens.json").write_text(json.dumps(SCREEN), encoding="utf-8")
            environment = dict(os.environ, QT_QPA_PLATFORM="offscreen:configfile=screens.json",
                               PYTHONIOENCODING="utf-8")
            try:
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene", part],
                                        cwd=folder, env=environment, capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=120)
            except subprocess.TimeoutExpired as error:
                self.fail("scene timed out: " + str(error.stdout or "") + str(error.stderr or ""))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout.strip().splitlines()[-1])["ok"])

    def test_the_duplicate_views_buttons_and_the_hidden_grid_take_no_stray_keys(self):
        self.run_scene("duplicates")

    def test_escape_makes_the_library_forget_the_photo_as_well(self):
        self.run_scene("escape")

    def test_a_title_bar_window_opened_with_the_mouse_leaves_the_keys_with_the_photos(self):
        self.run_scene("titlebar")

    def test_a_new_thumbnail_size_highlights_nothing_that_was_not_highlighted(self):
        self.run_scene("resize")

    def test_the_window_only_grows_for_saved_file_cards_and_keeps_a_height_the_person_set(self):
        self.run_scene("savecard")

    def test_a_card_gone_while_maximized_leaves_the_persons_own_height(self):
        self.run_scene("maximized")

    def test_the_syllable_still_being_composed_is_searched_for(self):
        self.run_scene("composing")


if __name__ == "__main__":
    if "--scene" in sys.argv:
        scene(sys.argv[sys.argv.index("--scene") + 1])
    else:
        unittest.main()
