"""The duplicate view stays where the person was looking.

Marking a group "not duplicates", taking photos out of a group, recycling copies and
taking a mark back all change the list. The list is read again from the catalog, but
the cards on screen are only removed, added or redrawn in place: the view must not
return to its top, and reading more while scrolling must not move it either.
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


def scene(shots=None):
    """``shots`` is a folder for window captures at each step, for a look at the result."""
    import faulthandler

    faulthandler.dump_traceback_later(50, exit=True)
    from contextlib import ExitStack, closing
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch

    from PIL import Image, ImageDraw
    from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, Qt, qInstallMessageHandler
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from shiboken6 import delete

    sys.path.insert(0, str(ROOT))
    from luvbox.history import HistoryStore
    from luvbox.storage import resolve_storage
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_gallery import GalleryModel
    from luvbox.qt_wheel import WheelInputFilter

    QQuickStyle.setStyle("Basic")
    application = QApplication([])
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

    def top(item):
        return item.mapToScene(QPointF(0, 0)).y()

    def every(name):
        found = []
        stack = [window.contentItem()]
        while stack:
            item = stack.pop()
            if item.objectName() == name and item.isVisible():
                found.append(item)
            stack.extend(item.childItems())
        return sorted(found, key=lambda item: (top(item), item.mapToScene(QPointF(0, 0)).x()))

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
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-duplicate-place-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        # Small pages, so that a list of a few dozen photos is read in several steps as a long one is.
        resources.enter_context(patch(
            "luvbox.qt_library.GalleryModel",
            side_effect=lambda reader, **options: GalleryModel(reader, batch_size=BATCH, **options)))
        base = Path(folder)
        kept = base / "simulated-recycle"
        kept.mkdir()
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

            for number in range(PAIRS):                 # fourteen pairs: 1101/1102 ... 1141/1142
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
        engine.addImageProvider("unsavedpreview", controller.records.preview_provider())
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel_filter})
        engine.load(ROOT / "assets/qt/MainWindow.qml")
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
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

        cards_model = library.duplicateGroupModel
        events = []
        cards_model.modelReset.connect(lambda: events.append("reset"))
        cards_model.rowsRemoved.connect(lambda _parent, first, last: events.append(("removed", last - first + 1)))
        cards_model.rowsInserted.connect(lambda _parent, first, last: events.append(("inserted", last - first + 1)))
        cards_model.rowsAboutToBeRemoved.connect(lambda *_: fewest.append(cards_model.rowCount()))
        fewest = []
        view = find("duplicateGroupList")

        def scrolled():
            return view.property("contentY") - view.property("originY")

        def cards():
            return every("duplicateGroup")

        def groups_shown():
            return [card.property("group") for card in cards()]

        def calm():
            wait(lambda: not library.duplicatesSettling and not model.busy and not controller.management.mutating,
                 "the list has been read again")
            settle(120)

        # Reading more while scrolling adds cards under the ones shown; the view does not move.
        wait(lambda: len(cards()) >= 2, "first cards shown")
        loaded = cards_model.rowCount()
        assert loaded < PAIRS + 1, loaded
        step = top(cards()[1]) - top(cards()[0])
        assert step > 100, step
        view.setProperty("contentY", view.property("originY") + step + 30)
        wait(lambda: cards_model.rowCount() > loaded and not model.busy, "more groups read on scrolling")
        settle(100)
        assert abs(scrolled() - (step + 30)) < 1, scrolled()
        assert all(event[0] == "inserted" for event in events), events
        assert sum(event[1] for event in events) == cards_model.rowCount() - loaded, events
        while model.count < total:
            before = model.count
            model.loadMore()
            wait(lambda: not model.busy and model.count > before, "the rest read")
        wait(lambda: cards_model.rowCount() == PAIRS + 1, "every group has its card")
        assert "reset" not in events, events
        assert abs(scrolled() - (step + 30)) < 1, scrolled()

        # A list keeps cards ready beyond its edges; these helpers count only what is on screen.
        def view_top():
            return top(view)

        def on_screen():
            """Cards that show at least a part, from the top."""
            return [card for card in cards()
                    if top(card) + card.height() > view_top() + 1 and top(card) < view_top() + view.height() - 1]

        def headed():
            """Cards whose title row, with its buttons, is on screen."""
            return [card for card in on_screen() if view_top() <= top(card) < view_top() + view.height() - 60]

        def inside(card, name):
            return [item for item in every(name) if top(card) <= top(item) < top(card) + card.height()]

        def placed(card):
            # Measured from the list's own top edge: the bar above the list can grow by a row of buttons.
            return round(top(card) - view_top())

        def places():
            return [(card.property("group"), placed(card)) for card in on_screen()]

        # Deep in the list, the first card half scrolled away: a group lower on the screen is marked.
        view.setProperty("contentY", view.property("originY") + 7 * step + 40)
        settle(150)
        before = places()
        assert len(before) >= 3 and len(headed()) >= 2, before
        offset = scrolled()
        target = headed()[1]
        gone = target.property("group")
        del events[:], fewest[:]
        press(inside(target, "duplicateGroupDistinct")[0])
        wait(lambda: library.distinctDuplicateCount == 1, "group marked")
        calm()
        assert model.count == total - 2 and library.duplicateCount == total - 2, (model.count, library.duplicateCount)
        assert events == [("removed", 1)], events
        assert fewest == [PAIRS + 1], fewest                   # the cards were never taken away and put back
        expected = [place for place in before if place[0] != gone]
        assert places()[:len(expected)] == expected, (places(), before)
        assert abs(scrolled() - offset) < 1, (scrolled(), offset)
        assert "중복 아님으로 표시" in library.status, library.status
        shot("1-lower-group-marked")

        # The card at the very top of the screen is marked: the next one takes its place.
        view.setProperty("contentY", view.property("contentY") + top(on_screen()[1]) - view_top())
        settle(150)
        before = places()
        assert before[0][1] == 0, before
        del events[:]
        press(inside(on_screen()[0], "duplicateGroupDistinct")[0])
        wait(lambda: library.distinctDuplicateCount == 2, "top group marked")
        calm()
        assert events == [("removed", 1)], events
        assert places()[0] == (before[1][0], before[0][1]), (places(), before)

        # A copy is recycled from a card on screen: its group is gone, the others stay put.
        before = places()
        target = headed()[1]
        gone = target.property("group")
        del events[:]
        press(inside(target, "duplicateGroupSelect")[0])
        wait(lambda: library.selectedCount == 1, "the copy of that card picked")
        click("galleryRecycleSelected")
        wait(lambda: find("deleteConfirmation").property("visible"), "delete confirmation")
        click("confirmRecycle")
        wait(lambda: not controller.management.mutating and library.duplicateCount == total - 6, "copy recycled")
        calm()
        assert model.count == total - 6, model.count
        assert events == [("removed", 1)], events
        assert places()[0] == before[0] and gone not in [place[0] for place in places()], (places(), before)
        assert len([path for path in files.values() if path.exists()]) == total - 1
        shot("2-copy-recycled")

        # One photo is taken out of the group of four: the card stays where it is, with three photos.
        four = next(row for row in range(cards_model.rowCount()) if library.duplicateGroups[row]["size"] == 4)
        # As far as the list really scrolls (the group of four is its last card).
        furthest = max(0, view.property("contentHeight") - view.height())
        view.setProperty("contentY", view.property("originY") + min(furthest, max(0, four * step - 60)))
        settle(150)
        card = next(card for card in headed() if card.property("size") == 4)
        number, place = card.property("group"), placed(card)
        tiles = inside(card, "duplicateTile")
        assert len(tiles) == 4, len(tiles)
        del events[:]
        press(tiles[3])
        wait(lambda: len(inside(card, "duplicateGroupPhotosDistinct")) == 1, "the card offers to take the photo out")
        offer = inside(card, "duplicateGroupPhotosDistinct")[0]
        spot = (round(offer.mapToScene(QPointF(0, 0)).x()), round(top(offer)), round(offer.width()))
        press(offer)
        try:
            wait(lambda: library.distinctDuplicateCount == 3, "photo taken out")
        except AssertionError:
            now = every("duplicateGroupPhotosDistinct")
            raise AssertionError(
                f"photo taken out: timeout; pressed at {spot}; status={library.status!r} selected={library.selectedCount} "
                f"settling={library.duplicatesSettling} locked={library.copiesLocked} offers now="
                f"{[(round(o.mapToScene(QPointF(0, 0)).x()), round(top(o)), round(o.width()), o.property('enabled')) for o in now]}")
        calm()
        assert events == [], events                              # no card came or went
        card = next(card for card in on_screen() if card.property("group") == number)
        assert (card.property("size"), placed(card)) == (3, place), (card.property("size"), placed(card), place)
        assert len(inside(card, "duplicateTile")) == 3
        assert "묶음에서 뺐습니다" in library.status, library.status
        shot("3-photo-taken-out")

        # Taking that back, and clearing every mark, put cards back without starting the list over.
        click("galleryUndoDistinct")
        wait(lambda: library.distinctDuplicateCount == 2, "last mark taken back")
        calm()
        card = next(card for card in on_screen() if card.property("group") == number)
        assert (card.property("size"), placed(card)) == (4, place), (card.property("size"), placed(card), place)
        assert events == [], events
        click("galleryClearDistinct")
        wait(lambda: library.distinctDuplicateCount == 0, "every mark cleared")
        calm()
        assert all(event[0] == "inserted" for event in events) and sum(event[1] for event in events) == 2, events
        assert number in [place[0] for place in places()], (number, places())
        assert library.duplicateCount == total - 2 and cards_model.rowCount() == PAIRS, library.duplicateCount

        # Another listing starts from its top.
        del events[:]
        click("galleryDuplicateFilter")
        wait(lambda: not library.filterDuplicateOnly and not model.busy, "grid restored")
        assert events == ["reset"] and cards_model.rowCount() == 0, events

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
        print(json.dumps({"ok": True, "kept_place": True, "qml_errors": errors}))
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
class DuplicatePlaceSceneTests(unittest.TestCase):
    def test_the_view_stays_where_it_was_after_each_change(self):
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
