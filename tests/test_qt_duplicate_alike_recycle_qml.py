"""A '비슷함' photo goes to the Recycle Bin only after a person compared it with the photo kept.

On the card, a similar photo has '비교'; in the preview, its '휴지통으로' reads
'남김과 비교 후 휴지통으로' (the Delete key does the same). Both open one window
with the photo its group keeps on the left and this photo on the right, at
their sizes; '이 사진 휴지통으로' sends this one photo, '취소' nothing. A photo
kept or offered as a copy has no '비교', and when the photo kept is gone the
comparison is refused with the reason. The copy choices are unchanged.
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


def scene(shots=None):
    """``shots`` is a folder for window captures at each step, for a look at the result."""
    import faulthandler

    faulthandler.dump_traceback_later(50, exit=True)
    from contextlib import ExitStack, closing
    from datetime import datetime, timezone, timedelta
    import random
    from unittest.mock import patch

    from PIL import Image, ImageDraw, ImageFilter
    from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, Qt, qInstallMessageHandler
    from PySide6.QtGui import QFont, QFontDatabase, QMouseEvent
    from PySide6.QtWidgets import QApplication
    from PySide6.QtQml import QQmlApplicationEngine, QQmlExpression, qmlContext
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
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
        until = time.monotonic() + 10
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

    def every(name, top=None):
        found = []
        stack = [top or window.contentItem()]
        while stack:
            item = stack.pop()
            if item.objectName() == name and item.isVisible():
                found.append(item)
            stack.extend(item.childItems())
        return sorted(found, key=lambda item: (item.mapToScene(QPointF(0, 0)).y(), item.mapToScene(QPointF(0, 0)).x()))

    def overlay(name):
        """A control inside a popup: popups live on the window's overlay, not under the content item."""
        return every(name, attached(window.contentItem(), "Overlay.overlay"))

    def shot(name):
        if shots is not None:
            settle(200)
            window.grabWindow().save(str(Path(shots) / (name + ".png")))

    def post(kind, item, button, held):
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        position = QPointF(window.mapToGlobal(point.toPoint()))
        QCoreApplication.postEvent(window, QMouseEvent(kind, point, position, button, held,
                                                       Qt.KeyboardModifier.NoModifier))

    def press(item):
        # Posted events, as in the other duplicate scenes: QTest.mouseClick keeps Python's lock while
        # Qt's image thread may call the thumbnail provider. A control that just appeared is placed
        # by its layout a moment later, so wait before reading its position.
        settle(60)
        post(QEvent.Type.MouseButtonPress, item, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
        post(QEvent.Type.MouseButtonRelease, item, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
        settle(30)

    def double_press(item):
        # What the system sends for a double click, with times, as the gallery reload scene does.
        settle(450)
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        position = QPointF(window.mapToGlobal(point.toPoint()))
        start = int(time.monotonic() * 1000)
        for kind, after in ((QEvent.Type.MouseButtonPress, 0), (QEvent.Type.MouseButtonRelease, 5),
                            (QEvent.Type.MouseButtonPress, 80), (QEvent.Type.MouseButtonDblClick, 80),
                            (QEvent.Type.MouseButtonRelease, 85)):
            held = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
            event = QMouseEvent(kind, point, position, Qt.MouseButton.LeftButton, held, Qt.KeyboardModifier.NoModifier)
            event.setTimestamp(start + after)
            QCoreApplication.postEvent(window, event)
        settle(30)

    def key(code):
        # Posted keys pass the window's shortcuts by (Delete in the preview is one), so this one is typed.
        settle(60)
        QTest.keyClick(window, code)
        settle(30)

    def click(name):
        settle(30)
        item = find(name)
        assert item.property("visible") and item.property("enabled"), name
        press(item)

    def tweet(key):
        return key.split("_")[2]

    def attached(item, expression):
        return QQmlExpression(qmlContext(item), item, expression).evaluate()[0]

    try:
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-duplicate-alike-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        base = Path(folder)
        kept = base / "simulated-recycle"
        kept.mkdir()
        elsewhere = base / "moved-away"
        elsewhere.mkdir()
        paths = resolve_storage(base / "사진 폴더", "offline")

        def photo(seed, size=(1200, 900)):
            """A busy picture without text, as the near rule's own tests use."""
            randomizer = random.Random(seed)
            image = Image.new("RGB", (9, 7))
            image.putdata([tuple(randomizer.randrange(256) for _ in range(3)) for _ in range(63)])
            image = image.resize(size, Image.Resampling.BICUBIC)
            draw = ImageDraw.Draw(image)
            for _ in range(25):
                x, y, radius = randomizer.randrange(size[0]), randomizer.randrange(size[1]), randomizer.randrange(20, 120)
                draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                             fill=tuple(randomizer.randrange(256) for _ in range(3)))
            return image.filter(ImageFilter.GaussianBlur(2))

        files = {}
        with closing(HistoryStore.for_storage(paths)) as store:
            def add(number, source=None, image=None, **options):
                path = paths.media_dir / f"user_{number}_1.jpg"
                if source is not None:
                    path.write_bytes(files[source].read_bytes())
                else:
                    image.save(path, "JPEG", **options)
                files[number] = path
                store.record_media("flat", {"tweet_id": str(number), "num": 1, "type": "photo",
                                            "author": {"name": "user"},
                                            "date": datetime(2026, 10, 1, 12, tzinfo=timezone(timedelta(hours=9)))}, path)

            # Two near pairs (a photo and the photo saved smaller), a group of two identical files, a photo alone.
            add(501, image=photo(7), quality=95)
            add(503, image=photo(7).resize((900, 675), Image.Resampling.LANCZOS), quality=85)
            add(601, image=photo(11, (800, 600)), quality=92)
            add(602, source=601)
            add(801, image=photo(29), quality=95)
            add(803, image=photo(29).resize((900, 675), Image.Resampling.LANCZOS), quality=85)
            add(701, image=photo(23, (800, 600)), quality=92)
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
        window.setWidth(1100)
        window.setHeight(760)
        window.setProperty("currentScreen", 1)
        library, model, management = controller.library, controller.library.model, controller.management
        wait(lambda: not controller.busy and not model.busy and model.count == 7, "gallery loaded")

        click("galleryDuplicateFilter")
        wait(lambda: library.filterDuplicateOnly and not model.busy, "duplicate view")
        click("galleryDuplicateNear")
        click("galleryScanDuplicates")
        wait(lambda: not library.busy and library.duplicateCount == 6 and not model.busy and model.count == 6,
             "near scan finished")
        marks = lambda group: [(tweet(item["key"]), item["keeper"], item["copy"]) for item in group["items"]]
        groups = {frozenset(tweet(item["key"]) for item in group["items"]): group for group in library.duplicateGroups}
        assert marks(groups[frozenset({"501", "503"})]) == [("501", True, False), ("503", False, False)]
        assert marks(groups[frozenset({"601", "602"})]) == [("602", True, False), ("601", False, True)]
        assert marks(groups[frozenset({"801", "803"})]) == [("801", True, False), ("803", False, False)]

        # '비교' only on the similar photos.
        wait(lambda: len(every("duplicateGroup")) == 3 and len(every("duplicateTile")) == 6, "group cards shown")
        compares = {tweet(attached(tile, "tile.modelData.key")): every("duplicateCompare", tile)
                    for tile in every("duplicateTile")}
        assert {number: len(found) for number, found in compares.items()} == \
            {"501": 0, "503": 1, "601": 0, "602": 0, "801": 0, "803": 1}, compares
        compare = compares["503"][0]
        assert attached(compare, "Accessible.name").endswith("남길 사진과 비교 후 휴지통으로")
        # The card's copy button and the bulk choice are as before.
        texts = sorted(button.property("text") for button in every("duplicateGroupSelect"))
        assert texts == ["사본 0장 선택", "사본 0장 선택", "사본 1장 선택"], texts
        shot("1-cards")

        # '비교' on the card: the window with both photos, nothing picked by the click.
        press(compare)
        dialog = find("alikeConfirmation")
        wait(lambda: dialog.property("visible") and dialog.property("opened"), "comparison shown")
        assert library.selectedCount == 0
        shown = management.deleteAlike
        assert (shown["keeper"]["pixels"], shown["alike"]["pixels"]) == ("1200×900", "900×675"), shown
        assert (shown["keeper"]["name"], shown["alike"]["name"]) == ("user_501_1.jpg", "user_503_1.jpg"), shown
        keeper_picture, target_picture = overlay("alikeKeeperPicture")[0], overlay("alikeTargetPicture")[0]
        wait(lambda: attached(keeper_picture, "status === Image.Ready") and attached(target_picture, "status === Image.Ready"),
             "both pictures drawn")
        assert keeper_picture.property("source").toString().endswith("user_501_1.jpg")
        assert target_picture.property("source").toString().endswith("user_503_1.jpg")
        assert overlay("alikeKeeperSize")[0].property("text").startswith("1200×900 · ")
        assert overlay("alikeTargetSize")[0].property("text").startswith("900×675 · ")
        summary = overlay("alikeSummaryText")[0].property("text")
        assert "‘비슷함’ 사진 한 장만" in summary and "다시 확인" in summary, summary
        # Two pictures side by side, the photo kept on the left.
        left = keeper_picture.mapToScene(QPointF(0, 0)).x()
        right = target_picture.mapToScene(QPointF(0, 0)).x()
        assert left + keeper_picture.width() <= right, (left, right)
        assert abs(keeper_picture.width() - target_picture.width()) <= 1
        assert not find("deleteConfirmation").property("visible")
        shot("2-comparison")

        # '취소': nothing moves, and the pictures are let go.
        click("cancelAlikeRecycle")
        wait(lambda: not dialog.property("visible") and not management.mutating
             and not management.deleteConfirmationPending, "comparison cancelled")
        assert target_picture.property("source").toString() == ""
        assert all(files[number].exists() for number in files)

        # The preview of the similar photo: its button compares too.
        target_tile = next(tile for tile in every("duplicateTile") if tweet(attached(tile, "tile.modelData.key")) == "503")
        double_press(target_tile)
        preview = find("previewDialog")
        wait(lambda: preview.property("visible") and library.selectedKey.endswith("_503_1_photo")
             and not library.selectionBusy, "the similar photo opened large")
        assert library.selectedCount == 0, library.selectedCount
        recycle = find("previewRecycle")
        wait(lambda: recycle.property("text") == "남김과 비교 후 휴지통으로", "the preview offers the comparison")
        assert library.selectedAlike
        # The photo kept, opened large: an ordinary '휴지통으로'.
        click("previewPreviousButton")
        wait(lambda: library.selectedKey.endswith("_501_1_photo") and not library.selectionBusy, "the photo kept")
        assert recycle.property("text") == "휴지통으로" and not library.selectedAlike
        click("previewNextButton")
        wait(lambda: library.selectedKey.endswith("_503_1_photo") and not library.selectionBusy, "back to it")
        wait(lambda: recycle.property("text") == "남김과 비교 후 휴지통으로", "and its comparison")
        shot("3-preview")

        # The Delete key in the preview opens the same comparison; Escape takes it back.
        key(Qt.Key.Key_Delete)
        wait(lambda: dialog.property("visible") and dialog.property("opened"), "comparison from the Delete key")
        assert not preview.property("visible")
        key(Qt.Key.Key_Escape)
        wait(lambda: not dialog.property("visible") and not management.deleteConfirmationPending
             and not management.mutating, "taken back")
        wait(lambda: preview.property("visible") and library.selectedKey.endswith("_503_1_photo"),
             "the preview comes back on the same photo")
        assert files[503].exists()

        # The button, then '이 사진 휴지통으로': this one photo goes, the photo kept stays.
        click("previewRecycle")
        wait(lambda: dialog.property("visible") and dialog.property("opened"), "comparison from the button")
        click("confirmAlikeRecycle")
        wait(lambda: not management.mutating and not model.busy and not library.duplicatesSettling
             and library.duplicateCount == 4 and model.count == 4, "the similar photo recycled")
        assert not files[503].exists() and (kept / "user_503_1.jpg").exists()
        assert all(files[number].exists() for number in (501, 601, 602, 701, 801, 803))
        status = management.status
        assert "휴지통 이동 1개" in status and "그대로 둔" not in status, status
        wait(lambda: sorted(frozenset(tweet(item["key"]) for item in group["items"]) for group in library.duplicateGroups)
             == sorted([frozenset({"601", "602"}), frozenset({"801", "803"})]), "the pair is no longer a group")
        shot("4-recycled")

        # The preview closed with the move (its photo left the list) and did not come back.
        settle(200)
        assert not preview.property("visible")

        def held(action):
            """Run ``action`` on a file of the group just drawn again: its thumbnail may be being read."""
            until = time.monotonic() + 5
            while True:
                try:
                    return action()
                except PermissionError:
                    if time.monotonic() >= until:
                        raise
                    settle(20)

        # The photo kept is saved again while the comparison is open: at the move it is not the file
        # that was shown, so the similar photo stays, and that reads as an error, not as a result.
        wait(lambda: len(every("duplicateCompare")) == 1, "one similar photo left")
        press(every("duplicateCompare")[0])
        wait(lambda: dialog.property("visible") and dialog.property("opened"), "comparison of the other pair")
        assert management.deleteAlike["keeper"]["name"] == "user_801_1.jpg"

        def save_again():
            with Image.open(files[801]) as source:
                picture = source.convert("RGB")
            picture.save(files[801], "JPEG", quality=80)
        held(save_again)
        click("confirmAlikeRecycle")
        wait(lambda: not management.mutating and not model.busy and not library.duplicatesSettling
             and "남길 사진이 그대로 있고" in management.error, "kept at the move")
        assert files[803].exists() and not (kept / "user_803_1.jpg").exists()
        assert "그대로 둔 파일 1개" in management.error and not management.status, management.error
        wait(lambda: not window.property("transientNotice"), "earlier notices gone")
        message = find("galleryManagementMessage")
        wait(lambda: message.property("visible") and "남길 사진이 그대로 있고" in message.property("text"),
             "the reason in the status line")
        assert not preview.property("visible")
        shot("5-kept-at-move")

        # The photo kept is gone (moved outside LuvBox): the comparison is refused and says why.
        held(lambda: os.replace(files[801], elsewhere / files[801].name))
        wait(lambda: len(every("duplicateCompare")) == 1, "the similar photo still offered")
        press(every("duplicateCompare")[0])
        wait(lambda: not management.mutating and "남길 사진 파일이 없어" in management.error, "refused with the reason")
        wait(lambda: message.property("visible") and "남길 사진 파일이 없어" in message.property("text"),
             "the reason in the status line")
        settle(100)
        assert not dialog.property("visible") and not find("deleteConfirmation").property("visible")
        assert files[803].exists()
        shot("6-refused")

        # Refused from the preview: the reason shows in the preview, which stays open on the photo.
        similar_tile = next(tile for tile in every("duplicateTile") if tweet(attached(tile, "tile.modelData.key")) == "803")
        double_press(similar_tile)
        wait(lambda: preview.property("visible") and library.selectedKey.endswith("_803_1_photo")
             and not library.selectionBusy, "the similar photo opened large")
        notice = find("previewNotice")
        assert not notice.property("visible")
        wait(lambda: recycle.property("text") == "남김과 비교 후 휴지통으로", "its comparison offered")
        click("previewRecycle")
        wait(lambda: not management.mutating and notice.property("visible")
             and "남길 사진 파일이 없어" in notice.property("text"), "the reason in the preview")
        assert preview.property("visible") and not dialog.property("visible")
        assert files[803].exists()
        shot("7-refused-in-preview")
        # Another photo: the note was about the one before.
        click("previewPreviousButton")
        wait(lambda: not library.selectedKey.endswith("_803_1_photo") and not library.selectionBusy, "moved on")
        wait(lambda: not notice.property("visible"), "the note leaves with its photo")
        # A reason about another photo, asked for elsewhere while this one is open, stays out of it.
        similar_key = next(item["key"] for group in library.duplicateGroups for item in group["items"]
                           if tweet(item["key"]) == "803")
        management.prepareAlikeDelete(similar_key)
        wait(lambda: not management.mutating and "남길 사진 파일이 없어" in management.error, "refused again")
        settle(100)
        assert preview.property("visible") and not notice.property("visible")
        key(Qt.Key.Key_Escape)
        wait(lambda: not preview.property("visible"), "preview closed")

        # '사본 일괄 선택' still picks the one copy, never the similar photo.
        click("gallerySelectDuplicates")
        wait(lambda: library.selectedCount == 1, "the copy picked")
        assert [tweet(key) for key in library.selectedKeys] == ["601"], library.selectedKeys

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
            "Unable to assign", "Parameter", "injection"))]
        assert not errors, errors
        print(json.dumps({"ok": True, "qml_errors": errors}))
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
class DuplicateAlikeRecycleSceneTests(unittest.TestCase):
    def test_a_similar_photo_goes_alone_after_it_was_compared(self):
        try:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"],
                                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
                                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8"})
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
