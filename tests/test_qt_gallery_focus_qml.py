"""The gallery's toolbar leaves the keys with the pictures, and its lists find a name by typing.

A list or a chip used with the mouse must not keep the keyboard: the arrows, Space and
Esc that follow belong to the grid. A list the mouse opened still takes the keys while it
is open, and Tab still reaches every control. In the author, hashtag and my-tag lists,
letters typed quickly find a row by its name without the "@" or "#" in front, and a
letter typed on a closed list opens it on the row it found instead of applying it.
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
# Authors by how many photos they have; the author list is sorted the same way (then by name).
# The thirty with one photo each make the list longer than its popup, so "zed" is out of sight.
AUTHORS = ((("alice", 6), ("bob_b", 5), ("carol_photo_acc", 4), ("cat_lover", 3), ("dave", 2))
           + tuple((f"kim_{number:02d}", 1) for number in range(30)) + (("zed", 1),))


def scene(part, shots=None):
    """``part`` is "mouse", "typing" or "closed"; ``shots`` is a folder for window captures."""
    import faulthandler

    # A scene that stops responding prints every thread's stack and ends before the
    # parent test's own timeout, so the report says where it stopped.
    faulthandler.dump_traceback_later(50, exit=True)
    from contextlib import ExitStack, closing
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch

    from PIL import Image
    from PySide6.QtCore import (QCoreApplication, QEvent, QMetaObject, QObject, QPointF, Qt,
                                qInstallMessageHandler)
    from PySide6.QtGui import QFont, QFontDatabase, QKeyEvent, QMouseEvent
    from PySide6.QtQml import QQmlApplicationEngine, QQmlEngine, QQmlExpression
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
        if result is not None:
            return result
        # The calendar's days live in a popup, outside the window's object tree.
        picker = window.findChild(QObject, "galleryDatePickerPopup")
        stack = [picker.property("contentItem")] if picker is not None else []
        while stack:
            current = stack.pop()
            if current.objectName() == name:
                return current
            stack.extend(current.childItems())
        raise AssertionError("Missing product control: " + name)

    def qml(item, expression):
        evaluation = QQmlExpression(QQmlEngine.contextForObject(item), item, expression)
        value = evaluation.evaluate()
        assert not evaluation.hasError(), evaluation.error().toString()
        return value[0] if isinstance(value, tuple) else value

    def opened(combo):
        return bool(qml(combo, "popup.visible"))

    def rows(combo):
        view = qml(combo, "popup.contentItem")
        return sorted((row for row in view.property("contentItem").childItems() if row.property("index") is not None),
                      key=lambda row: row.property("index"))

    def label(combo, index):
        return combo.property("model")[index]["label"]

    def row_of(combo, name):
        """The row whose label starts with ``name`` ("@cat_lover", "#food")."""
        labels = [row["label"] for row in combo.property("model")]
        found = [index for index, text in enumerate(labels) if text.split(" ")[0] == name]
        assert len(found) == 1, (name, labels)
        return found[0]

    def in_sight(combo, index):
        """The open list shows that row whole, without scrolling."""
        view = qml(combo, "popup.contentItem")
        for row in rows(combo):
            if row.property("index") == index:
                top = row.property("y") - view.property("contentY")
                return top >= -0.5 and top + row.property("height") <= view.property("height") + 0.5
        return False

    def shot(name):
        if shots is not None:
            settle(150)
            window.grabWindow().save(str(Path(shots) / (name + ".png")))

    def click(item):
        # Posted, as the real event loop delivers them: QTest.mouseClick keeps Python's global
        # lock while Qt handles the click, and a click that reloads the gallery has Qt's image
        # thread call the thumbnail provider (Python) at the same moment. The events carry
        # their time: a list's row ignored a click whose press and release had none.
        # Clicks are kept apart, so two of them never make a double click.
        settle(450)
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        position = QPointF(window.mapToGlobal(point.toPoint()))
        for kind, held in ((QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
                           (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)):
            event = QMouseEvent(kind, point, position, Qt.MouseButton.LeftButton, held, Qt.KeyboardModifier.NoModifier)
            event.setTimestamp(int(time.monotonic() * 1000))
            QCoreApplication.postEvent(window, event)
        settle(30)

    def key(code, text="", modifiers=Qt.KeyboardModifier.NoModifier):
        # Posted keys reach the item with the keys as the event loop delivers them. They pass
        # the window's shortcuts by, so the letters below that are also shortcuts (D, B, S, F)
        # are typed with QTest, which goes through them as a real key press does.
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QCoreApplication.postEvent(window, QKeyEvent(kind, code, modifiers, text))
        settle()

    def type_text(text):
        for letter in text:
            if letter.upper() in "DBSF":
                QTest.keyClick(window, letter)
                settle()
            else:
                key(Qt.Key(ord(letter.upper())), letter)

    def focused():
        item = window.property("activeFocusItem")
        while item is not None and not item.objectName():
            item = item.parentItem()
        return item.objectName() if item is not None else None

    try:
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-gallery-focus-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        base = Path(folder)
        paths = resolve_storage(base / "사진 폴더", "offline")
        number = 0
        with closing(HistoryStore.for_storage(paths)) as store:
            for author, count in AUTHORS:
                for _ in range(count):
                    number += 1
                    tweet = str(1000 + number)
                    path = paths.media_dir / f"{author}_{tweet}_1.png"
                    Image.new("RGB", (48, 32), (40 + number * 9 % 200, 30, 50)).save(path)
                    store.record_media("flat", {"tweet_id": tweet, "num": 1, "type": "photo",
                                                "author": {"name": author},
                                                "date": datetime(2026, 10, 1, 12, number,
                                                                 tzinfo=timezone(timedelta(hours=9)))}, path)
                    # Hashtags: #travel on alice's six, #food on bob_b's five. My tags: trip x3, work x2.
                    if author in ("alice", "bob_b"):
                        tag = "travel" if author == "alice" else "food"
                        store.record_tweets([(tweet, "사진 #" + tag, [tag])])
                    if number <= 3:
                        store.add_tag("flat", tweet, 1, "photo", "trip")
                    elif number <= 5:
                        store.add_tag("flat", tweet, 1, "photo", "work")
            store.toggle_favorite("flat", "1001", 1, "photo")
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
        wait(lambda: not controller.busy and not model.busy and model.count == number
             and len(library.authors) == len(AUTHORS) and len(library.hashtags) == 2 and len(library.userTags) == 2,
             "gallery loaded")
        grid = find("galleryGrid")
        wait(lambda: grid.property("activeFocus"), "the grid has the keys")

        def reloaded(generation):
            wait(lambda: library._generation > generation and not model.busy and not library.busy, "list read again")

        if part == "mouse":
            # A sort picked with the mouse: the list has the keys only while it is open.
            order = find("galleryOrderChoice")
            click(order)
            wait(lambda: opened(order), "sort list open")
            assert order.property("activeFocus"), focused()
            generation = library._generation
            click(rows(order)[1])
            wait(lambda: not opened(order) and library._order == "posted", "newest posts chosen with the mouse")
            reloaded(generation)
            assert grid.property("activeFocus"), ("the sort list kept the keys", focused())
            assert window.property("letterKeysFree")
            shot("1-sort-picked")
            # Down moves between the pictures; it does not change the sort.
            generation = library._generation
            key(Qt.Key.Key_Down)
            settle(100)
            assert library._order == "posted" and order.property("currentIndex") == 1, library._order
            assert library._generation == generation, "Down reloaded the gallery"
            assert grid.property("currentIndex") >= 0, grid.property("currentIndex")

            # The thumbnail size: Up after a mouse pick neither shrinks it nor saves another size.
            size = find("thumbSizeChoice")
            click(size)
            wait(lambda: opened(size), "size list open")
            click(rows(size)[2])
            wait(lambda: not opened(size) and window.property("densityChoice") == 2
                 and controller.preferences["gallery_density"] == 2, "large thumbnails chosen")
            assert grid.property("activeFocus"), ("the size list kept the keys", focused())
            settle(150)
            key(Qt.Key.Key_Escape)
            key(Qt.Key.Key_Up)
            settle(100)
            assert window.property("densityChoice") == 2 and controller.preferences["gallery_density"] == 2, (
                window.property("densityChoice"), controller.preferences["gallery_density"])

            # The type: Down after "사진" stays on photos.
            kind = find("galleryMediaFilter")
            click(kind)
            wait(lambda: opened(kind), "type list open")
            generation = library._generation
            click(rows(kind)[1])
            wait(lambda: not opened(kind) and library._media_type == "photo", "photos chosen")
            reloaded(generation)
            assert grid.property("activeFocus"), ("the type list kept the keys", focused())
            generation = library._generation
            key(Qt.Key.Key_Down)
            settle(100)
            assert library._media_type == "photo" and library._generation == generation, library._media_type

            # A list the mouse opened takes the arrows and Enter while it is open, then gives the keys back.
            click(order)
            wait(lambda: opened(order), "sort list open again")
            before = grid.property("currentIndex")
            key(Qt.Key.Key_Down)
            assert order.property("highlightedIndex") == 2 and grid.property("currentIndex") == before, (
                order.property("highlightedIndex"), grid.property("currentIndex"), before)
            generation = library._generation
            key(Qt.Key.Key_Return)
            wait(lambda: not opened(order) and library._order == "posted_oldest", "oldest posts chosen by keys")
            reloaded(generation)
            assert grid.property("activeFocus"), ("the list opened with the mouse kept the keys", focused())

            # Chips: after a click, Space belongs to the grid, not to the chip.
            favorites = find("galleryFavoriteFilter")
            generation = library._generation
            click(favorites)
            wait(lambda: library.filterFavoriteOnly, "favorites on")
            reloaded(generation)
            assert grid.property("activeFocus"), ("the favorites chip kept the keys", focused())
            key(Qt.Key.Key_Space)
            settle(100)
            assert library.filterFavoriteOnly, "Space turned the favorites filter off again"
            click(favorites)
            wait(lambda: not library.filterFavoriteOnly and not model.busy, "favorites off")
            choose = find("gallerySelectionToggle")
            click(choose)
            wait(lambda: library.selectionMode, "choosing several")
            assert grid.property("activeFocus"), ("the selection button kept the keys", focused())
            key(Qt.Key.Key_Space)
            settle(100)
            assert library.selectionMode, "Space ended choosing several"
            click(choose)
            wait(lambda: not library.selectionMode, "choosing ended")

            # The AI menu gives the keys back to the grid when it closes.
            ai_menu = find("galleryAiMenu")
            click(find("galleryAiMenuButton"))
            wait(lambda: ai_menu.property("visible"), "AI menu open")
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait(lambda: not ai_menu.property("visible"), "AI menu closed")
            settle(50)
            assert grid.property("activeFocus"), ("the AI chip kept the keys", focused())

            # A date picked with the mouse: Space then does not open the calendar again.
            click(find("galleryFilterToggle"))
            date_from = find("galleryDateFrom")
            wait(lambda: date_from.property("visible"), "date range shown")
            assert grid.property("activeFocus"), ("the date chip kept the keys", focused())
            picker = find("galleryDatePickerPopup")
            click(date_from)
            wait(lambda: picker.property("visible"), "calendar open")
            picker.setProperty("viewYear", 2026)
            picker.setProperty("viewMonth", 9)
            settle(50)
            generation = library._generation
            click(find("datePickerDay_1"))
            wait(lambda: not picker.property("visible") and library.dateFrom == "2026-10-01", "start date chosen")
            reloaded(generation)
            assert grid.property("activeFocus"), ("the date field kept the keys", focused())
            key(Qt.Key.Key_Space)
            settle(100)
            assert not picker.property("visible"), "Space opened the calendar again"
            shot("2-date-picked")

            # Tab still reaches the lists, with the focus ring; Esc on a closed list goes back to the grid.
            search = find("searchBox")
            QMetaObject.invokeMethod(search, "forceActiveFocus")
            settle()
            key(Qt.Key.Key_Tab)
            wait(lambda: order.property("activeFocus"), "Tab reaches the sort list")
            assert order.property("visualFocus"), "the sort list shows that it has the keys"
            shot("3-tab-to-sort")
            key(Qt.Key.Key_Tab)
            wait(lambda: kind.property("activeFocus"), "Tab reaches the type list")
            key(Qt.Key.Key_Escape)
            wait(lambda: grid.property("activeFocus"), "Esc on the closed type list returns to the grid")
            # A list the keyboard reached keeps the keys after its own arrows change it.
            QMetaObject.invokeMethod(search, "forceActiveFocus")
            settle()
            key(Qt.Key.Key_Tab)
            wait(lambda: order.property("activeFocus"), "Tab reaches the sort list again")
            generation = library._generation
            key(Qt.Key.Key_Up)
            wait(lambda: library._order == "posted", "Up on the focused sort list")
            reloaded(generation)
            assert order.property("activeFocus"), ("a list the keyboard reached lost the keys", focused())
            # Opened and closed from the keyboard, it keeps them too; Esc then goes to the grid.
            key(Qt.Key.Key_Space)
            wait(lambda: opened(order), "sort list opened by Space")
            key(Qt.Key.Key_Escape)
            wait(lambda: not opened(order), "sort list closed by Esc")
            assert order.property("activeFocus") and library._order == "posted", (focused(), library._order)
            key(Qt.Key.Key_Escape)
            wait(lambda: grid.property("activeFocus"), "second Esc returns to the grid")
            find("gallerySelectionToggle").forceActiveFocus(Qt.FocusReason.TabFocusReason)
            key(Qt.Key.Key_Tab)
            assert grid.property("activeFocus"), ("Tab skipped the gallery grid", focused())

        elif part == "typing":
            author = find("galleryAuthorFilter")
            labels = [row["label"] for row in author.property("model")]
            assert [text.split(" ")[0] for text in labels[1:]] == ["@" + name for name, _count in AUTHORS], labels
            carol, cat, zed = row_of(author, "@carol_photo_acc"), row_of(author, "@cat_lover"), row_of(author, "@zed")
            # Opened with the mouse, letters typed one after another find a row together, past
            # the "@": "cat" is cat_lover, where one letter at a time would go from "c" (carol)
            # to "a" (alice) to "t" (nothing).
            click(author)
            wait(lambda: opened(author), "author list open")
            assert author.property("activeFocus"), focused()
            type_text("cat")
            assert author.property("highlightedIndex") == cat, (author.property("highlightedIndex"), labels)
            assert library.filterAuthor == "" and opened(author)
            shot("1-typed-cat")
            key(Qt.Key.Key_Escape)
            wait(lambda: not opened(author) and grid.property("activeFocus"), "closed by Esc, keys back to the grid")
            assert library.filterAuthor == "", "Esc applied the typed row"
            # A new word starts after the list closed: "c" alone finds the first name with c.
            click(author)
            wait(lambda: opened(author), "author list open again")
            type_text("c")
            assert author.property("highlightedIndex") == carol, (author.property("highlightedIndex"), labels)
            generation = library._generation
            key(Qt.Key.Key_Return)
            wait(lambda: library.filterAuthor == "carol_photo_acc" and not opened(author), "carol chosen by Enter")
            reloaded(generation)
            assert grid.property("activeFocus"), ("the author list kept the keys", focused())
            # The same letter again and again steps through the names that start with it.
            click(author)
            wait(lambda: opened(author) and author.property("highlightedIndex") == carol, "author list open on carol")
            type_text("c")
            assert author.property("highlightedIndex") == cat, author.property("highlightedIndex")
            type_text("c")
            assert author.property("highlightedIndex") == carol, author.property("highlightedIndex")
            key(Qt.Key.Key_Escape)
            wait(lambda: not opened(author), "closed again")
            # A name far down a long list: the list scrolls to it.
            click(author)
            wait(lambda: opened(author), "author list open a third time")
            assert not in_sight(author, zed), "zed was in sight before typing"
            type_text("ze")
            assert author.property("highlightedIndex") == zed, (author.property("highlightedIndex"), labels)
            wait(lambda: in_sight(author, zed), "the list scrolled to zed")
            shot("2-typed-ze")
            key(Qt.Key.Key_Escape)
            wait(lambda: not opened(author) and grid.property("activeFocus"), "closed a third time")
            assert library.filterAuthor == "carol_photo_acc", library.filterAuthor

            # Hashtags ("#food") and my tags ("#work") are found by the word after the "#".
            hashtag = find("galleryHashtagFilter")
            click(hashtag)
            wait(lambda: opened(hashtag), "hashtag list open")
            type_text("f")
            assert hashtag.property("highlightedIndex") == row_of(hashtag, "#food"), (
                hashtag.property("highlightedIndex"), [row["label"] for row in hashtag.property("model")])
            generation = library._generation
            key(Qt.Key.Key_Return)
            wait(lambda: library.filterHashtag == "#food" and not opened(hashtag), "#food chosen")
            reloaded(generation)
            mine = find("galleryUserTagFilter")
            click(mine)
            wait(lambda: opened(mine), "my-tag list open")
            type_text("w")
            assert mine.property("highlightedIndex") == row_of(mine, "#work"), (
                mine.property("highlightedIndex"), [row["label"] for row in mine.property("model")])
            key(Qt.Key.Key_Escape)
            wait(lambda: not opened(mine) and library.filterUserTag == "", "my-tag list closed")
            assert grid.property("activeFocus"), focused()

        elif part == "closed":
            # A closed list with the keys (reached with Tab): "@" alone changes nothing, and a
            # name opens the list on its row instead of applying it. Only Enter applies it.
            author = find("galleryAuthorFilter")
            labels = [row["label"] for row in author.property("model")]
            QMetaObject.invokeMethod(author, "forceActiveFocus")
            wait(lambda: author.property("activeFocus"), "the author list has the keys")
            generation = library._generation
            type_text("@")
            settle(100)
            assert not opened(author) and library.filterAuthor == "", ("@ applied an author", library.filterAuthor)
            # Nor does an "@" typed with AltGr, which Windows reports as Ctrl+Alt.
            key(Qt.Key.Key_At, "@", Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
            settle(100)
            assert not opened(author) and library.filterAuthor == "", ("AltGr+@ applied an author", library.filterAuthor)
            type_text("d")
            wait(lambda: opened(author), "a letter opens the closed list")
            assert author.property("highlightedIndex") == row_of(author, "@dave"), (
                author.property("highlightedIndex"), labels)
            settle(100)
            assert library.filterAuthor == "" and library._generation == generation, library.filterAuthor
            shot("1-typed-on-closed-list")
            key(Qt.Key.Key_Escape)
            wait(lambda: not opened(author), "closed without applying")
            assert library.filterAuthor == "" and author.property("activeFocus"), (library.filterAuthor, focused())
            key(Qt.Key.Key_Escape)
            wait(lambda: grid.property("activeFocus"), "Esc on the closed list returns to the grid")

            # The same with "#" on the hashtag list; Enter then applies the row the letters found.
            hashtag = find("galleryHashtagFilter")
            QMetaObject.invokeMethod(hashtag, "forceActiveFocus")
            wait(lambda: hashtag.property("activeFocus"), "the hashtag list has the keys")
            type_text("#")
            settle(100)
            assert not opened(hashtag) and library.filterHashtag == "", ("# applied a hashtag", library.filterHashtag)
            type_text("t")
            wait(lambda: opened(hashtag), "a letter opens the closed hashtag list")
            assert hashtag.property("highlightedIndex") == row_of(hashtag, "#travel"), hashtag.property("highlightedIndex")
            assert library.filterHashtag == "" and library._generation == generation, library.filterHashtag
            key(Qt.Key.Key_Return)
            wait(lambda: library.filterHashtag == "#travel" and not opened(hashtag), "#travel chosen by Enter")
            reloaded(generation)
            assert hashtag.property("activeFocus"), ("a list the keyboard reached lost the keys", focused())
        else:
            raise AssertionError("unknown part " + part)

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
class GalleryFocusSceneTests(unittest.TestCase):
    def run_scene(self, part):
        try:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene", part],
                                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
                                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        except subprocess.TimeoutExpired as error:
            self.fail("scene timed out: " + str(error.stdout or "") + str(error.stderr or ""))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout.splitlines()[-1])["ok"])

    def test_toolbar_lists_and_chips_used_with_the_mouse_leave_the_keys_with_the_grid(self):
        self.run_scene("mouse")

    def test_typing_finds_a_name_in_the_author_hashtag_and_tag_lists_past_the_at_or_hash(self):
        self.run_scene("typing")

    def test_a_key_on_a_closed_list_opens_it_on_the_row_and_applies_nothing(self):
        self.run_scene("closed")


if __name__ == "__main__":
    if "--scene" in sys.argv:
        folder = sys.argv[sys.argv.index("--screenshots") + 1] if "--screenshots" in sys.argv else None
        if folder is not None:
            Path(folder).mkdir(parents=True, exist_ok=True)
        scene(sys.argv[sys.argv.index("--scene") + 1], folder)
    else:
        unittest.main()
