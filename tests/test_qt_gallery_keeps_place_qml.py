"""A favorite in the duplicate view and a tag on a selection leave the gallery where it was.

Favoriting a photo decides which photo of its group is kept, and the strip above the
cards says so; the person does it from the preview, deep in a long list of groups. The
cards are redrawn in place, the preview stays on the photo just favorited, and the photos
picked in other groups stay picked.

Adding a tag to a selection does not change which photos the gallery lists, so the list
is not read again: the scroll, the selection and the confirmation stay. Only a tag named
like the AI category the list is filtered on can change it (the photo's AI label becomes
the person's own tag), and only then is the list read again.
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
PAIRS = 14
BATCH = 10


def _harness():
    """Qt and the window helpers both scenes use."""
    import faulthandler

    faulthandler.dump_traceback_later(50, exit=True)
    from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, Qt, qInstallMessageHandler
    from PySide6.QtGui import QKeyEvent, QMouseEvent
    from PySide6.QtWidgets import QApplication
    from PySide6.QtQuickControls2 import QQuickStyle

    QQuickStyle.setStyle("Basic")
    application = QApplication([])
    messages = []
    previous = qInstallMessageHandler(lambda _level, _context, message: messages.append(message))
    state = {"window": None}

    def settle(milliseconds=0):
        until = time.monotonic() + milliseconds / 1000
        while True:
            application.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if time.monotonic() >= until:
                return
            time.sleep(.003)

    def wait(predicate, label, seconds=8):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            settle()
            if predicate():
                return
            time.sleep(.003)
        raise AssertionError(label + ": timeout")

    def find(name):
        result = state["window"].findChild(QObject, name)
        if result is None:
            raise AssertionError("Missing product control: " + name)
        return result

    def top(item):
        return item.mapToScene(QPointF(0, 0)).y()

    def every(name):
        found = []
        stack = [state["window"].contentItem()]
        while stack:
            item = stack.pop()
            if item.objectName() == name and item.isVisible():
                found.append(item)
            stack.extend(item.childItems())
        return sorted(found, key=lambda item: (top(item), item.mapToScene(QPointF(0, 0)).x()))

    def press(item):
        # Posted events, delivered by processEvents, as in the duplicate-place scene: a synchronous
        # click keeps Python's global lock while Qt's image thread may need it for a thumbnail.
        settle(60)
        window = state["window"]
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        position = QPointF(window.mapToGlobal(point.toPoint()))
        for kind, held in ((QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
                           (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)):
            QCoreApplication.postEvent(window, QMouseEvent(kind, point, position, Qt.MouseButton.LeftButton, held,
                                                           Qt.KeyboardModifier.NoModifier))
        settle(30)

    def press_key(key):
        window = state["window"]
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QCoreApplication.postEvent(window, QKeyEvent(kind, key, Qt.KeyboardModifier.NoModifier))
        settle(20)

    def click(name):
        settle(30)
        item = find(name)
        assert item.property("visible") and item.property("enabled"), name
        press(item)

    return {"application": application, "messages": messages, "previous": previous, "state": state,
            "settle": settle, "wait": wait, "find": find, "top": top, "every": every, "press": press,
            "press_key": press_key, "click": click}


def _qml_errors(messages):
    return [m for m in messages if any(term in m for term in (
        "ReferenceError", "TypeError", "Binding loop", "Cannot assign", "Cannot read", "is not a type",
        "Unable to assign"))]


def _identity(key):
    """A gallery key without its list generation: the same photo in the next reading of the list."""
    return key.split("_", 1)[1] if key else ""


def favorite_scene():
    from contextlib import ExitStack, closing
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch

    from PIL import Image, ImageDraw
    from PySide6.QtCore import QMetaObject
    from PySide6.QtQml import QQmlApplicationEngine
    from luvbox.history import HistoryStore
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_gallery import GalleryModel
    from luvbox.qt_wheel import WheelInputFilter
    from luvbox.storage import resolve_storage
    from shiboken6 import delete

    tools = _harness()
    settle, wait, find, top, every, press, click = (tools[name] for name in (
        "settle", "wait", "find", "top", "every", "press", "click"))
    messages = tools["messages"]
    controller = engine = folder = None
    resources = ExitStack()
    try:
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-favorite-place-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        resources.enter_context(patch("luvbox.clip_model.model_file_present", return_value=False))
        resources.enter_context(patch(
            "luvbox.qt_library.GalleryModel",
            side_effect=lambda reader, **options: GalleryModel(reader, batch_size=BATCH, **options)))
        base = Path(folder)
        paths = resolve_storage(base / "사진 폴더", "offline")

        def picture(seed):
            image = Image.new("RGB", (200, 150), (30 + seed * 47 % 200, 60 + seed * 13 % 120, 90 + seed * 29 % 140))
            draw = ImageDraw.Draw(image)
            for index in range(5):
                draw.ellipse((10 + index * 36, 20 + (seed * 37 + index * 53) % 90, 40 + index * 36,
                              50 + (seed * 37 + index * 53) % 90), fill=(250 - index * 30, 40 + seed * 30 % 200, 20))
            return image

        files = {}
        with closing(HistoryStore.for_storage(paths)) as store:
            def add(tweet, source=None, image=None):
                path = paths.media_dir / f"user_{tweet}_1.jpg"
                if source is not None:
                    path.write_bytes(files[source].read_bytes())
                else:
                    image.save(path, "JPEG", quality=90)
                files[tweet] = path
                store.record_media("flat", {"tweet_id": str(tweet), "num": 1, "type": "photo",
                                            "author": {"name": "user"},
                                            "date": datetime(2026, 10, 1, 12, tzinfo=timezone(timedelta(hours=9)))}, path)

            for number in range(PAIRS):                 # fourteen pairs: 1101/1102 ... 1231/1232
                first = 1101 + number * 10
                add(first, image=picture(number))
                add(first + 1, source=first)
            add(2001, image=picture(40))                # and one group of four
            for copy in (2002, 2003, 2004):
                add(copy, source=2001)
        total = 2 * PAIRS + 4
        (base / "LuvBox").mkdir(exist_ok=True)
        (base / "LuvBox/settings.json").write_text(
            json.dumps({"account": "offline", "output_dir": str(paths.output_dir), "gallery_density": 0}),
            encoding="utf-8")

        controller = ApplicationController()
        wheel_filter = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", controller.library.provider)
        engine.addImageProvider("unsavedpreview", controller.records.preview_provider())
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel_filter})
        engine.load(ROOT / "assets/qt/MainWindow.qml")
        assert engine.rootObjects(), messages
        window = tools["state"]["window"] = engine.rootObjects()[0]
        window.setWidth(1000)
        window.setHeight(1000)
        window.setProperty("currentScreen", 1)
        library, model = controller.library, controller.library.model
        wait(lambda: not controller.busy and not model.busy and model.count >= BATCH, "gallery loaded")
        click("galleryDuplicateFilter")
        wait(lambda: library.filterDuplicateOnly and not model.busy, "duplicate view")
        click("galleryScanDuplicates")
        wait(lambda: not library.busy and library.duplicateCount == total and not model.busy and model.count >= BATCH,
             "scan finished")
        while model.count < total:
            before = model.count
            model.loadMore()
            wait(lambda: not model.busy and model.count > before, "the rest read")
        cards_model = library.duplicateGroupModel
        wait(lambda: cards_model.rowCount() == PAIRS + 1, "every group has its card")

        events = []
        cards_model.modelReset.connect(lambda: events.append("reset"))
        cards_model.rowsRemoved.connect(lambda _parent, first, last: events.append(("removed", last - first + 1)))
        cards_model.rowsInserted.connect(lambda _parent, first, last: events.append(("inserted", last - first + 1)))
        view = find("duplicateGroupList")
        preview = find("previewDialog")

        def scrolled():
            return view.property("contentY") - view.property("originY")

        def cards():
            return every("duplicateGroup")

        def view_top():
            return top(view)

        def headed():
            """Cards whose title row is on screen, from the top."""
            return [card for card in cards() if view_top() <= top(card) < view_top() + view.height() - 60]

        def places():
            return [(card.property("group"), round(top(card) - view_top())) for card in cards()
                    if top(card) + card.height() > view_top() + 1 and top(card) < view_top() + view.height() - 1]

        def inside(card, name):
            return [item for item in every(name) if top(card) <= top(item) < top(card) + card.height()]

        def members(group):
            return next(row for row in library.duplicateGroups if row["group"] == group)["items"]

        def calm():
            wait(lambda: not library.duplicatesSettling and not model.busy and not library.selectionBusy,
                 "the list has been read again")
            settle(200)

        # Deep in the list: one photo picked in a card, the copy of the next card opened large.
        wait(lambda: len(cards()) >= 2, "cards shown")
        step = top(cards()[1]) - top(cards()[0])
        view.setProperty("contentY", view.property("originY") + 7 * step + 40)
        settle(150)
        shown = headed()
        assert len(shown) >= 2, places()
        picked_card, opened_card = shown[0], shown[1]
        press(inside(picked_card, "duplicateTile")[1])
        wait(lambda: library.selectedCount == 1, "a photo picked")
        picked = _identity(library.selectedKeys[0])
        group = opened_card.property("group")
        copy = members(group)[1]
        assert not copy["keeper"] and not copy["favorite"], copy
        opened = _identity(copy["key"])
        library.selectItem(copy["key"])           # what a double-click on the tile does
        QMetaObject.invokeMethod(preview, "open")
        wait(lambda: preview.property("visible") and library.selectedKey == copy["key"] and not library.selectionBusy,
             "preview open on the copy")
        before, offset, generation = places(), scrolled(), library._generation
        assert offset > step, offset
        del events[:]

        # The copy is favorited from the preview: it becomes the photo the group keeps.
        click("previewFavoriteButton")
        calm()
        result = {"preview": preview.property("visible"), "opened": _identity(library.selectedKey),
                  "picked": [_identity(key) for key in library.selectedKeys], "offset": scrolled(),
                  "events": events[:], "favorite": library.selectedIsFavorite}
        assert library._generation > generation, "the duplicate list is read again for the new keeper"
        assert result["preview"], result
        assert result["opened"] == opened and result["favorite"], result
        assert library.selectedKey.startswith(f"g{library._generation}_"), library.selectedKey
        assert result["picked"] == [picked], result
        assert "reset" not in result["events"], result
        assert abs(result["offset"] - offset) < 1, (result, offset)
        assert places() == before, (places(), before)
        first = members(group)[0]
        assert _identity(first["key"]) == opened and first["keeper"] and first["favorite"], members(group)
        wait(lambda: find("previewFavoriteButton").property("text").startswith("★"), "the button shows the favorite")

        # And back: the larger file is kept again, the preview still on the same photo.
        del events[:]
        click("previewFavoriteButton")
        calm()
        assert preview.property("visible") and _identity(library.selectedKey) == opened, (
            preview.property("visible"), library.selectedKey)
        assert not library.selectedIsFavorite
        assert [_identity(key) for key in library.selectedKeys] == [picked], library.selectedKeys
        assert "reset" not in events and abs(scrolled() - offset) < 1, (events, scrolled(), offset)
        assert not members(group)[1]["favorite"] and not members(group)[1]["keeper"], members(group)

        # A photo that leaves the list when it is no longer a favorite closes its preview instead of loading forever.
        library.favoriteNotice(library.selectedKey)
        calm()
        assert library.selectedIsFavorite and preview.property("visible")
        QMetaObject.invokeMethod(preview, "close")
        wait(lambda: not preview.property("visible"), "preview closed")
        library.setFavoriteOnly(True)
        wait(lambda: not model.busy and model.count == 1 and cards_model.rowCount() == 1, "favorites among duplicates")
        only = model._rows[0]["key"]
        assert _identity(only) == opened, only
        library.selectItem(only)
        QMetaObject.invokeMethod(preview, "open")
        wait(lambda: preview.property("visible") and library.selectedKey == only and not library.selectionBusy,
             "preview open on the only favorite")
        click("previewFavoriteButton")
        wait(lambda: not library.duplicatesSettling and not model.busy, "the list read again")
        wait(lambda: not preview.property("visible"), "the preview of a photo that left the list closed")
        assert not library.selectionBusy and library.selectedKey == "" and model.count == 0, (
            library.selectionBusy, library.selectedKey, model.count)

        controller.requestClose()
        wait(lambda: controller.shutdownReady, "shutdown")
        window.close()
        wheel_filter.close()
        delete(engine)
        engine = None
        delete(controller)
        controller = None
        errors = _qml_errors(messages)
        assert not errors, errors
        print(json.dumps({"ok": True}))
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
        resources.close()


def tag_scene():
    from contextlib import ExitStack, closing
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch

    from PIL import Image
    from PySide6.QtCore import QMetaObject, Qt
    from PySide6.QtQml import QQmlApplicationEngine
    from luvbox.history import HistoryStore
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_wheel import WheelInputFilter
    from luvbox.storage import resolve_storage
    from shiboken6 import delete

    tools = _harness()
    settle, wait, find, press_key, click = (tools[name] for name in ("settle", "wait", "find", "press_key", "click"))
    messages = tools["messages"]
    controller = engine = folder = None
    resources = ExitStack()
    try:
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-tag-place-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        resources.enter_context(patch("luvbox.clip_model.model_file_present", return_value=False))
        base = Path(folder)
        paths = resolve_storage(base / "사진 폴더", "offline")
        count = 120
        with closing(HistoryStore.for_storage(paths)) as store:
            for index in range(count):
                path = paths.media_dir / f"alice_{1000 + index}_1.png"
                Image.new("RGB", (48, 32), (40 + index, 30, 50)).save(path)
                store.record_media("flat", {"tweet_id": str(1000 + index), "num": 1, "type": "photo",
                                            "author": {"name": "alice"},
                                            "date": datetime(2026, 10, 1, 12, tzinfo=timezone(timedelta(hours=9)))},
                                   path)
            # Ten photos the AI put among people.
            assert store.add_tags_batch([("flat", str(1000 + index), 1, "photo") for index in range(10)],
                                        "인물", source="ai") == 10
        (base / "LuvBox").mkdir(exist_ok=True)
        (base / "LuvBox/settings.json").write_text(
            json.dumps({"account": "offline", "output_dir": str(paths.output_dir), "gallery_density": 0}),
            encoding="utf-8")

        controller = ApplicationController()
        wheel_filter = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", controller.library.provider)
        engine.addImageProvider("unsavedpreview", controller.records.preview_provider())
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel_filter})
        engine.load(ROOT / "assets/qt/MainWindow.qml")
        assert engine.rootObjects(), messages
        window = tools["state"]["window"] = engine.rootObjects()[0]
        window.setWidth(680)
        window.setHeight(480)
        window.setProperty("currentScreen", 1)
        library, model = controller.library, controller.library.model
        wait(lambda: not controller.busy and not model.busy and model.count == count, "gallery loaded")
        grid = find("galleryGrid")
        resets = []
        model.modelReset.connect(lambda: resets.append(library._generation))

        def keys():
            return [row["key"] for row in model._rows]

        # Deep in the list with the keyboard, as a person does; that tile is the opened one.
        settle(150)
        QMetaObject.invokeMethod(grid, "forceActiveFocus")
        settle()
        columns = max(1, int(grid.width() // grid.property("cellWidth")))
        deep = 100
        for _ in range(200):
            current = grid.property("currentIndex")
            if current == deep:
                break
            press_key(Qt.Key.Key_Down if 0 <= current <= deep - columns else Qt.Key.Key_Right)
        assert grid.property("currentIndex") == deep, (grid.property("currentIndex"), columns)
        wait(lambda: library.selectedKey == keys()[deep] and not library.selectionBusy, "deep tile opened")
        wait(lambda: grid.property("contentY") - grid.property("originY") > grid.property("cellHeight"),
             "grid scrolled to the tile")
        click("gallerySelectionToggle")
        chosen = keys()[deep:deep + 4]
        for key in chosen:
            library.toggleSelection(key)
        assert library.selectedKeys == chosen, library.selectedKeys
        settle(150)
        offset = grid.property("contentY") - grid.property("originY")
        generation = library._generation
        del resets[:]

        # 태그 추가 from the selection bar.
        click("galleryAddTagSelected")
        popup = find("tagBatchPopup")
        wait(lambda: popup.property("visible"), "tag popup open")
        field = find("batchTagInput")
        wait(lambda: field.property("activeFocus"), "tag field focused")
        field.setProperty("text", "#여행")
        press_key(Qt.Key.Key_Return)
        wait(lambda: not popup.property("visible"), "tag popup closed after adding")
        message = "선택한 4개 사진에 #여행 태그를 추가했습니다."
        wait(lambda: any(tag.get("tag") == "여행" for tag in library.selectedTags), "the opened photo shows the tag")
        wait(lambda: library._user_tags_loaded, "my tags read again")
        settle(300)
        result = {"resets": resets[:], "generation": library._generation - generation,
                  "offset": grid.property("contentY") - grid.property("originY"), "selected": library.selectedKeys,
                  "status": library.status, "opened": library.selectedKey, "count": model.count}
        assert result["resets"] == [] and result["generation"] == 0, result
        assert abs(result["offset"] - offset) < 1, (result, offset)
        assert result["selected"] == chosen and library.selectionMode, result
        assert result["status"] == message, result
        assert result["opened"] == keys()[deep] and grid.property("currentIndex") == deep, result
        assert {"tag": "여행", "count": 4} in library.userTags, library.userTags
        assert find("galleryAddTagSelected").property("enabled"), "a second tag can go to the same photos"

        # A tag named like the AI category the list shows turns that label into the person's own tag:
        # those photos leave the list, so this one tag reads the list again.
        library.setAiFilter("인물", "")
        wait(lambda: not model.busy and model.count == 10, "photos the AI put among people")
        people = keys()[:2]
        for key in people:
            library.toggleSelection(key)
        assert library.addTagToSelection("인물") == 2
        wait(lambda: not model.busy and model.count == 8, "the two photos left the AI list")
        assert not {_identity(key) for key in people} & {_identity(key) for key in keys()}, keys()

        controller.requestClose()
        wait(lambda: controller.shutdownReady, "shutdown")
        window.close()
        wheel_filter.close()
        delete(engine)
        engine = None
        delete(controller)
        controller = None
        errors = _qml_errors(messages)
        assert not errors, errors
        print(json.dumps({"ok": True}))
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
        resources.close()


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class GalleryKeepsPlaceSceneTests(unittest.TestCase):
    def run_scene(self, name):
        try:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene", name],
                                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=70,
                                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        except subprocess.TimeoutExpired as error:
            self.fail("scene timed out: " + str(error.stdout or "") + str(error.stderr or ""))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout.splitlines()[-1])["ok"])

    def test_favoriting_in_the_duplicate_view_keeps_the_cards_the_preview_and_the_picks(self):
        self.run_scene("favorite")

    def test_tagging_a_selection_keeps_the_list_the_selection_and_the_confirmation(self):
        self.run_scene("tag")


if __name__ == "__main__":
    if "--scene" in sys.argv:
        {"favorite": favorite_scene, "tag": tag_scene}[sys.argv[sys.argv.index("--scene") + 1]]()
    else:
        unittest.main()
