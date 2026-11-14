"""A recycled file leaves the real gallery in place: scroll, highlight and preview carry on."""

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

    # A scene that stops responding prints every thread's stack and ends before the
    # parent test's own timeout, so the report says where it stopped.
    faulthandler.dump_traceback_later(45, exit=True)
    from contextlib import ExitStack, closing
    from datetime import datetime, timezone, timedelta
    from unittest.mock import patch

    from PIL import Image
    from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QPointF, Qt, qInstallMessageHandler
    from PySide6.QtGui import QFont, QFontDatabase, QKeyEvent
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

    def settle():
        application.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        time.sleep(.003)

    def pause(milliseconds):
        # Let timers and events run for a while. QTest.qWait did this inside one call that kept
        # Python's global lock, and 1 of 360 cold starts stopped in the pause after the thumbnail
        # size changed (the grid asks for every picture again then). processEvents lets the
        # other threads run, as in press_key.
        until = time.monotonic() + milliseconds / 1000
        while time.monotonic() < until:
            settle()

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

    def press_key(key):
        # QTest.keyClick keeps Python's global lock for as long as Qt handles the key. Changing
        # the thumbnail size makes Qt's image thread call the provider (Python) at the same
        # moment, and the two stopped each other in 9 of 200 cold starts. Posted events are
        # delivered by processEvents, which lets other threads run, as the real event loop
        # does: 0 of 200 stopped that way.
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QCoreApplication.postEvent(window, QKeyEvent(kind, key, Qt.KeyboardModifier.NoModifier))
        settle()

    def shot(name):
        if shots is not None:
            pause(150)
            settle()
            window.grabWindow().save(str(Path(shots) / (name + ".png")))

    def click(name):
        pause(30)
        item = find(name)
        assert item.property("visible") and item.property("enabled"), name
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        settle()

    try:
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-gallery-recycle-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        base = Path(folder)
        kept = base / "simulated-recycle"
        kept.mkdir()
        paths = resolve_storage(base / "사진 폴더", "offline")
        with closing(HistoryStore.for_storage(paths)) as store:
            for index in range(60):
                path = paths.media_dir / f"alice_{1000 + index}_1.png"
                Image.new("RGB", (48, 32), (40 + index * 3, 30, 50)).save(path)
                store.record_media("flat", {"tweet_id": str(1000 + index), "num": 1, "type": "photo",
                                            "author": {"name": "alice"},
                                            "date": datetime(2026, 10, 1, 12, tzinfo=timezone(timedelta(hours=9)))},
                                   path)
        (base / "LuvBox").mkdir(exist_ok=True)
        (base / "LuvBox/settings.json").write_text(
            json.dumps({"account": "offline", "output_dir": str(paths.output_dir), "gallery_density": 2}),
            encoding="utf-8")

        busy_moves = []

        def move(path, expected):
            # A person confirms after the thumbnail and preview readers have let go of the
            # file. This scene confirms within milliseconds, so wait out a reader instead of
            # reporting its sharing violation as a failed move.
            path = Path(path)
            target = kept / path.name
            until = time.monotonic() + 3
            while True:
                try:
                    path.rename(target)
                    return str(target)
                except PermissionError:
                    if time.monotonic() >= until:
                        raise
                    busy_moves.append(path.name)
                    time.sleep(.02)

        resources.enter_context(patch("luvbox.deletion.recycle_file", side_effect=move))
        controller = ApplicationController()
        wheel = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", controller.library.provider)
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel})
        engine.load(ROOT / "assets/qt/MainWindow.qml")
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        window.setWidth(680)
        window.setHeight(480)
        # The thumbnail size from the last session is applied, and a new choice is written back.
        assert window.property("densityChoice") == 2, window.property("densityChoice")
        window.setProperty("currentScreen", 1)
        library, model = controller.library, controller.library.model
        wait(lambda: not controller.busy and not model.busy and model.count == 60, "gallery loaded")
        grid = find("galleryGrid")
        large = grid.property("cellHeight")
        size_choice = find("thumbSizeChoice")
        QMetaObject.invokeMethod(size_choice, "forceActiveFocus")
        settle()
        press_key(Qt.Key.Key_Up)
        press_key(Qt.Key.Key_Up)
        wait(lambda: window.property("densityChoice") == 0 and controller.preferences["gallery_density"] == 0
             and not controller.busy, "small thumbnails chosen")
        assert grid.property("cellHeight") < large
        assert json.loads((base / "LuvBox/settings.json").read_text(encoding="utf-8"))["gallery_density"] == 0
        # Changing the size re-anchors the grid on a short timer; let it finish before moving the highlight.
        pause(120)
        wait(lambda: grid.property("highlightFollowsCurrentItem"), "grid re-anchored after the size change")
        generation = library._generation

        def keys():
            return [row["key"] for row in model._rows]

        # Go deep into the list and highlight a tile there, with the keyboard, as a person does.
        # Writing currentIndex from here moved the grid fifty tiles inside one call that keeps
        # Python's global lock; 2 of 120 cold starts stopped in that call. Keys are posted and
        # delivered by processEvents (see press_key), which lets the other threads run.
        QMetaObject.invokeMethod(grid, "forceActiveFocus")
        settle()
        columns = max(1, int(grid.width() // grid.property("cellWidth")))
        for _ in range(90):
            current = grid.property("currentIndex")
            if current == 50:
                break
            press_key(Qt.Key.Key_Down if 0 <= current <= 50 - columns else Qt.Key.Key_Right)
        assert grid.property("currentIndex") == 50, (grid.property("currentIndex"), columns)
        wait(lambda: library.selectedKey == keys()[50] and not library.selectionBusy, "deep tile selected")
        wait(lambda: grid.property("contentY") - grid.property("originY") > grid.property("cellHeight"),
             "grid scrolled to the tile")
        scrolled = grid.property("contentY") - grid.property("originY")
        shot("1-deep-tile")
        doomed, successor = keys()[50], keys()[51]

        QTest.keyClick(window, Qt.Key.Key_Delete)
        confirmation = find("deleteConfirmation")
        wait(lambda: confirmation.property("visible"), "delete confirmation")
        click("confirmRecycle")
        wait(lambda: not controller.management.mutating and model.count == 59, "tile recycled in place")
        assert doomed not in keys() and keys()[50] == successor
        assert library._generation == generation, "the list was not reloaded"
        assert controller.management.error == "", controller.management.error
        after = grid.property("contentY") - grid.property("originY")
        assert after > grid.property("cellHeight") and abs(after - scrolled) <= grid.property("cellHeight") * 1.5, (
            scrolled, after)
        # The highlight moved to the tile that took the place, and it is the opened item.
        wait(lambda: grid.property("currentIndex") == 50 and library.selectedKey == successor
             and not library.selectionBusy, "next tile selected")
        assert find("galleryCount").property("text").startswith("59")
        shot("2-after-recycle")

        # Cleaning up from the preview: Delete, confirm, and the next photo is ready to open.
        QMetaObject.invokeMethod(grid, "forceActiveFocus")
        settle()
        QTest.keyClick(window, Qt.Key.Key_Return)
        preview = find("previewDialog")
        wait(lambda: preview.property("visible"), "preview open")
        following = keys()[51]
        QTest.keyClick(window, Qt.Key.Key_Delete)
        wait(lambda: confirmation.property("visible") and not preview.property("visible"),
             "preview closed for the confirmation")
        click("confirmRecycle")
        wait(lambda: not controller.management.mutating and model.count == 58, "second tile recycled")
        wait(lambda: library.selectedKey == following and not library.selectionBusy, "following tile selected")
        assert library._generation == generation
        assert grid.property("contentY") - grid.property("originY") > grid.property("cellHeight")
        # The move started in the preview, so the preview comes back on the next photo.
        wait(lambda: preview.property("visible") and library.selectedKey == following, "preview of the next photo")
        assert find("selectedMedia").property("sourceUrl").toString().endswith(
            following.split("_")[2] + "_1.png"), find("selectedMedia").property("sourceUrl")
        shot("3-preview-on-next")
        # A cancelled move returns to the same photo.
        QTest.keyClick(window, Qt.Key.Key_Delete)
        wait(lambda: confirmation.property("visible") and not preview.property("visible"), "second confirmation")
        QTest.keyClick(window, Qt.Key.Key_Escape)
        wait(lambda: not controller.management.deleteConfirmationPending and preview.property("visible")
             and library.selectedKey == following, "preview restored after a cancel")
        assert model.count == 58
        QMetaObject.invokeMethod(preview, "close")
        wait(lambda: not preview.property("visible"), "preview closed")
        # A move that starts in the grid does not open the preview.
        QMetaObject.invokeMethod(grid, "forceActiveFocus")
        settle()
        QTest.keyClick(window, Qt.Key.Key_Delete)
        wait(lambda: confirmation.property("visible"), "grid confirmation")
        QTest.keyClick(window, Qt.Key.Key_Escape)
        wait(lambda: not controller.management.deleteConfirmationPending, "grid move cancelled")
        pause(60)
        assert not preview.property("visible") and model.count == 58

        # A multi-selection leaves together and the remaining tiles keep their order.
        remaining = keys()
        click("gallerySelectionToggle")
        for key in (remaining[10], remaining[11], remaining[30]):
            library.toggleSelection(key)
        assert library.selectedCount == 3
        click("galleryRecycleSelected")
        wait(lambda: confirmation.property("visible"), "batch confirmation")
        click("confirmRecycle")
        wait(lambda: not controller.management.mutating and model.count == 55, "batch recycled in place")
        expected = [key for key in remaining if key not in {remaining[10], remaining[11], remaining[30]}]
        assert keys() == expected and library.selectedCount == 0 and library._generation == generation
        shot("4-after-batch")
        assert len(list(kept.iterdir())) == 5

        # The shortcut list opens from the title bar and with F1, fits the window, and
        # blocks the single-letter shortcuts behind it.
        click("gallerySelectionToggle")
        help_dialog = find("shortcutHelp")
        QMetaObject.invokeMethod(grid, "forceActiveFocus")
        settle()
        QTest.keyClick(window, Qt.Key.Key_F1)
        wait(lambda: help_dialog.property("visible"), "shortcut help by F1")
        assert 0 <= help_dialog.property("x") and help_dialog.property("x") + help_dialog.property("width") <= 680
        assert 0 <= help_dialog.property("y") and help_dialog.property("y") + help_dialog.property("height") <= 480
        shot("5-shortcut-help")
        QTest.keyClick(window, Qt.Key.Key_B)
        settle()
        assert not library.filterFavoriteOnly, "letters must not reach the gallery behind the list"
        QTest.keyClick(window, Qt.Key.Key_Escape)
        wait(lambda: not help_dialog.property("visible"), "shortcut help closed by Escape")
        # A focused list keeps letters for itself; the grid gets them back afterwards.
        QMetaObject.invokeMethod(size_choice, "forceActiveFocus")
        settle()
        assert window.property("listEngaged") and not window.property("letterKeysFree")
        QTest.keyClick(window, Qt.Key.Key_B)
        settle()
        assert not library.filterFavoriteOnly, "B typed into a list must not toggle the favorites filter"
        QMetaObject.invokeMethod(grid, "forceActiveFocus")
        settle()
        assert window.property("letterKeysFree")
        QTest.keyClick(window, Qt.Key.Key_B)
        wait(lambda: library.filterFavoriteOnly and not model.busy, "B toggles favorites from the grid")
        QTest.keyClick(window, Qt.Key.Key_B)
        wait(lambda: not library.filterFavoriteOnly and not model.busy and model.count == 55, "B toggles back")
        # While a gallery menu is open a letter is not a gallery shortcut either.
        ai_menu = find("galleryAiMenu")
        click("galleryAiMenuButton")
        wait(lambda: ai_menu.property("visible"), "AI menu open")
        assert window.property("menuOpen") and not window.property("letterKeysFree")
        QTest.keyClick(window, Qt.Key.Key_B)
        settle()
        assert not library.filterFavoriteOnly, "B typed over an open menu must not toggle the favorites filter"
        QTest.keyClick(window, Qt.Key.Key_Escape)
        wait(lambda: not ai_menu.property("visible") and window.property("letterKeysFree"), "AI menu closed")
        # The list is one click away in the title bar, next to the statistics.
        assert find("galleryStatisticsButton").property("visible")
        click("shortcutHelpButton")
        wait(lambda: help_dialog.property("visible"), "shortcut help by button")
        click("shortcutHelpClose")
        wait(lambda: not help_dialog.property("visible"), "shortcut help closed by button")

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
        print(json.dumps({"ok": True, "in_place": True, "qml_errors": errors, "moves_retried": len(busy_moves)}))
    except BaseException:
        # A failed step has to name itself. Tearing Qt down after a half-finished
        # scene can block on a thumbnail request, and the parent test then saw
        # only a timeout with no message.
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
class GalleryRecycleSceneTests(unittest.TestCase):
    def test_recycling_keeps_the_scroll_position_and_selects_the_next_tile(self):
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
