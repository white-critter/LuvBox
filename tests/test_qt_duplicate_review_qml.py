"""The duplicate view shows which photos are the same, which one stays, and clears the copies."""

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

    from PIL import Image, ImageDraw
    from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QPointF, Qt, qInstallMessageHandler
    from PySide6.QtGui import QFont, QFontDatabase, QMouseEvent
    from PySide6.QtWidgets import QApplication
    from PySide6.QtQml import QQmlApplicationEngine, QQmlExpression, qmlContext
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
            settle(200)
            window.grabWindow().save(str(Path(shots) / (name + ".png")))

    def press(item):
        # QTest.mouseClick keeps Python's global lock for as long as Qt handles the click. A click
        # that makes the gallery ask for its thumbnails again has Qt's image thread call the
        # provider (Python) at the same moment, and the two stopped each other in 3 of 240 cold
        # starts of this scene. Posted events are delivered by processEvents, which lets other
        # threads run, as the real event loop does.
        # A control that has just appeared is placed by its layout a moment later; reading its
        # position at once gave the row's left edge, and the click missed (1 in 20 cold starts).
        settle(60)
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        position = QPointF(window.mapToGlobal(point.toPoint()))
        for kind, held in ((QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
                           (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)):
            QCoreApplication.postEvent(window, QMouseEvent(kind, point, position, Qt.MouseButton.LeftButton, held,
                                                           Qt.KeyboardModifier.NoModifier))
        settle(30)

    def click(name):
        settle(30)
        item = find(name)
        assert item.property("visible") and item.property("enabled"), name
        press(item)

    try:
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-duplicate-review-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        base = Path(folder)
        kept = base / "simulated-recycle"
        kept.mkdir()
        paths = resolve_storage(base / "사진 폴더", "offline")

        def picture(seed):
            image = Image.new("RGB", (640, 480), (30 + seed * 40 % 200, 60, 90))
            draw = ImageDraw.Draw(image)
            for index in range(8):
                draw.ellipse((40 + index * 70, 60 + (seed * 37 + index * 53) % 300, 110 + index * 70,
                              130 + (seed * 37 + index * 53) % 300), fill=(250 - index * 20, 40 + seed * 30 % 200, 20))
            return image

        files = {}
        with closing(HistoryStore.for_storage(paths)) as store:
            def add(tweet, source=None, image=None, **options):
                path = paths.media_dir / f"user_{tweet}_1.jpg"
                if source is not None:
                    path.write_bytes(files[source].read_bytes())
                else:
                    image.save(path, "JPEG", **options)
                files[tweet] = path
                store.record_media("flat", {"tweet_id": str(tweet), "num": 1, "type": "photo",
                                            "author": {"name": "user"},
                                            "date": datetime(2026, 10, 1, 12, tzinfo=timezone(timedelta(hours=9)))}, path)

            add(101, image=picture(1), quality=92)
            add(102, source=101)
            add(103, source=101)
            add(201, image=picture(2), quality=92)
            add(202, source=201)
            add(301, image=picture(3), quality=95)
            add(302, image=picture(3).resize((320, 240), Image.Resampling.LANCZOS), quality=80)
            add(401, image=picture(4), quality=92)
            store.toggle_favorite("flat", "202", 1, "photo")
        (base / "LuvBox").mkdir(exist_ok=True)
        (base / "LuvBox/settings.json").write_text(
            json.dumps({"account": "offline", "output_dir": str(paths.output_dir), "gallery_density": 0}),
            encoding="utf-8")

        def move(path, expected):
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
                    time.sleep(.02)

        resources.enter_context(patch("luvbox.deletion.recycle_file", side_effect=move))
        controller = ApplicationController()
        wheel_filter = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", controller.library.provider)
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel_filter})
        engine.load(ROOT / "assets/qt/MainWindow.qml")
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        window.setWidth(1000)
        window.setHeight(720)
        window.setProperty("currentScreen", 1)
        library, model = controller.library, controller.library.model
        wait(lambda: not controller.busy and not model.busy and model.count == 8, "gallery loaded")

        # The grid gives way to the grouped view; nothing is found until a scan runs.
        click("galleryDuplicateFilter")
        wait(lambda: library.filterDuplicateOnly and not model.busy, "duplicate view")
        assert not find("galleryGrid").property("visible") and find("duplicateReview").property("visible")
        assert find("duplicateEmptyState").property("visible") and find("duplicateGuide").property("visible")

        # Default scan: identical files only.
        click("galleryScanDuplicates")
        wait(lambda: not library.busy and library.duplicateCount == 5 and not model.busy and model.count == 5,
             "exact scan finished")
        groups = library.duplicateGroups
        assert [(group["size"], [item["keeper"] for item in group["items"]]) for group in groups] == [
            (3, [True, False, False]), (2, [True, False])], groups
        # In the second group the favorite is the one to keep.
        assert groups[1]["items"][0]["key"].split("_")[2] == "202" and groups[1]["items"][0]["favorite"]
        wait(lambda: len(every("duplicateGroup")) == 2 and len(every("duplicateTile")) == 5, "group cards shown")
        roles = [item.findChild(QObject, "roleLabel") for item in every("duplicateTile")]
        titles = [item.property("text") for item in every("duplicateGroupTitle")]
        assert titles == ["묶음 1 · 같은 사진 3장", "묶음 2 · 같은 사진 2장"], titles
        assert "완전히 같은 사진 5장" in library.status, library.status
        shot("1-exact-groups")

        # One photo of a group can be taken out alone: pick it, and its card offers to do so.
        assert not every("duplicateGroupPhotosDistinct")
        press(every("duplicateTile")[2])
        wait(lambda: library.selectedCount == 1 and len(every("duplicateGroupPhotosDistinct")) == 1,
             "the card offers to take the picked photo out")
        offer = every("duplicateGroupPhotosDistinct")[0]
        assert offer.property("text") == "선택한 1장만 중복 아님", offer.property("text")
        shot("1a-photo-picked")
        press(offer)
        wait(lambda: not model.busy and not library.duplicatesSettling and model.count == 4
             and library.duplicateCount == 4 and library.distinctDuplicateCount == 1, "one photo taken out of its group")
        assert [group["size"] for group in library.duplicateGroups] == [2, 2]
        assert "묶음에서 뺐습니다" in library.status, library.status
        assert library.selectedCount == 0 and not every("duplicateGroupPhotosDistinct")
        # The bar that came with the pick left with it: nothing is picked, nothing to act on.
        assert not library.selectionMode and not find("gallerySelectionBar").property("visible")
        wait(lambda: [item.property("text") for item in every("duplicateGroupTitle")]
             == ["묶음 1 · 같은 사진 2장", "묶음 2 · 같은 사진 2장"], "the card shows the two that stay")
        assert find("galleryClearDistinct").property("text") == "중복 아님 1개 초기화"
        assert all(path.exists() for path in files.values()), "no file was touched"
        shot("1b-photo-taken-out")
        click("galleryUndoDistinct")
        wait(lambda: not model.busy and not library.duplicatesSettling and model.count == 5
             and library.duplicateCount == 5 and library.distinctDuplicateCount == 0, "the photo is back in its group")
        wait(lambda: len(every("duplicateTile")) == 5 and len(every("duplicateGroupSelect")) == 2, "five tiles again")
        groups = library.duplicateGroups

        # One button picks a group's copies; the photo to keep stays unselected.
        buttons = every("duplicateGroupSelect")
        assert buttons[0].property("text") == "사본 2장 선택"
        press(buttons[0])
        wait(lambda: library.selectedCount == 2 and library.selectionMode, "copies of the first group selected")
        assert not library.isSelected(groups[0]["items"][0]["key"])
        assert buttons[0].property("text") == "선택 해제"
        shot("2-copies-selected")
        click("galleryRecycleSelected")
        confirmation = find("deleteConfirmation")
        wait(lambda: confirmation.property("visible"), "delete confirmation")
        # The question says what protects the last file of a group, and the long
        # sentence wraps inside the dialog (it used to run out of it, cut off).
        summary = find("deleteSummaryText")
        assert "같은 내용의 다른 파일이 남는지" in summary.property("text"), summary.property("text")
        wait(lambda: 0 < summary.property("width") <= confirmation.property("width")
             and summary.property("lineCount") > summary.property("text").count("\n") + 1, "the summary wraps")
        click("confirmRecycle")
        wait(lambda: not controller.management.mutating and not model.busy and model.count == 2
             and library.duplicateCount == 2, "copies recycled, the kept photo leaves the list")
        # Same size and no favorite: the newest post's file is the one kept.
        assert files[103].exists() and not files[101].exists() and not files[102].exists()
        assert [group["size"] for group in library.duplicateGroups] == [2]

        # A tile can be picked by hand as well, and unpicked.
        wait(lambda: len(every("duplicateTile")) == 2, "remaining group shown")
        tiles = every("duplicateTile")
        press(tiles[1])
        wait(lambda: library.selectedCount == 1, "one copy picked by hand")
        assert library.selectionMode and find("gallerySelectionBar").property("visible")
        press(tiles[1])
        wait(lambda: library.selectedCount == 0, "picked copy released")
        wait(lambda: not library.selectionMode and not find("gallerySelectionBar").property("visible"),
             "the bar goes when the last picked photo is let go")
        # Someone who switched selecting on with its button keeps the bar, also with nothing picked.
        click("gallerySelectionToggle")
        wait(lambda: library.selectionMode and find("gallerySelectionBar").property("visible"), "selecting switched on")
        press(tiles[1])
        wait(lambda: library.selectedCount == 1, "a copy picked while selecting is on")
        press(tiles[1])
        wait(lambda: library.selectedCount == 0, "and let go")
        settle(60)
        assert library.selectionMode and find("gallerySelectionBar").property("visible")
        click("gallerySelectionToggle")
        wait(lambda: not library.selectionMode, "selecting switched off")
        # Without a mouse: the accessibility interface toggles or presses a tile, as a screen reader does.
        for action in ("toggleAction", "pressAction"):
            QQmlExpression(qmlContext(tiles[1]), tiles[1], f"Accessible.{action}()").evaluate()
            wait(lambda: library.selectedCount == 1, action + " picks the tile")
            QQmlExpression(qmlContext(tiles[1]), tiles[1], f"Accessible.{action}()").evaluate()
            wait(lambda: library.selectedCount == 0, action + " releases the tile")

        # Asked to, the scan also finds the same photo saved smaller.
        click("galleryDuplicateNear")
        assert window.property("duplicateNear")
        click("galleryScanDuplicates")
        wait(lambda: not library.busy and library.duplicateCount == 4 and not model.busy and model.count == 4,
             "near scan finished")
        sizes = [group["size"] for group in library.duplicateGroups]
        assert sizes == [2, 2], sizes
        resized = [group for group in library.duplicateGroups
                   if {item["key"].split("_")[2] for item in group["items"]} == {"301", "302"}]
        assert resized and resized[0]["items"][0]["key"].split("_")[2] == "301", "the larger file is kept"
        assert "크기·화질만 다른 것 포함" in library.status
        shot("3-near-groups")

        # "중복 아님": the group leaves the view, stays out after another scan, and can be taken back.
        assert not find("galleryUndoDistinct").property("visible")
        assert not find("galleryClearDistinct").property("visible")
        wait(lambda: len(every("duplicateGroupDistinct")) == 2, "a 'not duplicates' button on each group")
        marked = {item["key"].split("_")[2] for item in library.duplicateGroups[0]["items"]}
        press(every("duplicateGroupDistinct")[0])
        wait(lambda: not model.busy and model.count == 2 and library.duplicateCount == 2
             and library.distinctDuplicateCount == 1, "group marked 'not duplicates'")
        left = {item["key"].split("_")[2] for group in library.duplicateGroups for item in group["items"]}
        assert len(left) == 2 and not marked & left, (marked, left)
        assert "중복 아님으로 표시" in library.status, library.status
        assert find("galleryUndoDistinct").property("visible")
        assert find("galleryClearDistinct").property("text") == "중복 아님 1개 초기화"
        assert all(path.exists() for path in files.values() if path.name not in {"user_101_1.jpg", "user_102_1.jpg"})
        shot("4-marked-not-duplicates")
        click("galleryScanDuplicates")
        wait(lambda: not library.busy and "같은 사진" in library.status and not model.busy and model.count == 2
             and library.duplicateCount == 2, "another scan leaves the marked group out")
        assert {item["key"].split("_")[2] for group in library.duplicateGroups for item in group["items"]} == left
        click("galleryUndoDistinct")
        wait(lambda: not model.busy and model.count == 4 and library.duplicateCount == 4
             and library.distinctDuplicateCount == 0, "the mark taken back without a scan")
        assert not find("galleryUndoDistinct").property("visible")
        assert not find("galleryClearDistinct").property("visible")
        # Both groups marked: the empty view says how to get them back, and "초기화" returns both.
        wait(lambda: len(every("duplicateGroupDistinct")) == 2, "both groups shown again")
        press(every("duplicateGroupDistinct")[0])
        wait(lambda: not model.busy and model.count == 2 and library.distinctDuplicateCount == 1, "first group marked")
        wait(lambda: len(every("duplicateGroupDistinct")) == 1, "one group left")
        press(every("duplicateGroupDistinct")[0])
        wait(lambda: not model.busy and model.count == 0 and library.duplicateCount == 0
             and library.distinctDuplicateCount == 2, "second group marked")
        empty = find("duplicateEmptyState")
        assert empty.property("visible") and "‘중복 아님’ 표시 2개는 위의 ‘초기화’로" in empty.property("text"), empty.property("text")
        shot("5-all-marked")
        click("galleryClearDistinct")
        wait(lambda: not model.busy and model.count == 4 and library.duplicateCount == 4
             and library.distinctDuplicateCount == 0, "every mark cleared")
        assert "초기화했습니다" in library.status, library.status
        assert not find("galleryUndoDistinct").property("visible")

        # Leaving the view brings the grid back.
        click("galleryDuplicateFilter")
        wait(lambda: not library.filterDuplicateOnly and not model.busy and model.count == 6, "grid restored")
        assert find("galleryGrid").property("visible") and not find("duplicateReview").property("visible")
        assert library.duplicateGroups == []

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
        del roles
        print(json.dumps({"ok": True, "groups": True, "near": True, "qml_errors": errors}))
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
class DuplicateReviewSceneTests(unittest.TestCase):
    def test_groups_keep_one_photo_and_clear_the_copies(self):
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
