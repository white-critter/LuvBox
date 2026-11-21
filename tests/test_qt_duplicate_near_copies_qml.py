"""In a group found with '화질만 다른 사진 포함' only files with the same bytes are offered as copies.

A photo saved smaller or at another quality is shown in the group, but the
Recycle Bin check keeps it unless a file with its very bytes stays, so the view
must not offer it as a copy either: not on the card, not with '사본 일괄 선택',
and its tile says it is only similar. A group of identical files is offered as
before.
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

    def hover(item):
        settle(60)
        post(QEvent.Type.MouseMove, item, Qt.MouseButton.NoButton, Qt.MouseButton.NoButton)
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
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-duplicate-near-copies-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        base = Path(folder)
        kept = base / "simulated-recycle"
        kept.mkdir()
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

            # The near group: a photo, a file with its very bytes, and the photo saved smaller.
            add(501, image=photo(7), quality=95)
            add(502, source=501)
            add(503, image=photo(7).resize((900, 675), Image.Resampling.LANCZOS), quality=85)
            # A group of three identical files.
            add(601, image=photo(11, (800, 600)), quality=92)
            add(602, source=601)
            add(603, source=601)
            # A photo of its own.
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
        library, model = controller.library, controller.library.model
        wait(lambda: not controller.busy and not model.busy and model.count == 7, "gallery loaded")

        click("galleryDuplicateFilter")
        wait(lambda: library.filterDuplicateOnly and not model.busy, "duplicate view")
        click("galleryDuplicateNear")
        assert window.property("duplicateNear")
        click("galleryScanDuplicates")
        wait(lambda: not library.busy and library.duplicateCount == 6 and not model.busy and model.count == 6,
             "near scan finished")

        # What the cards are given: the photo kept, which ones are copies.
        groups = {frozenset(tweet(item["key"]) for item in group["items"]): group for group in library.duplicateGroups}
        near, exact = groups[frozenset({"501", "502", "503"})], groups[frozenset({"601", "602", "603"})]
        marks = lambda group: [(tweet(item["key"]), item["keeper"], item["copy"]) for item in group["items"]]
        assert marks(near) == [("502", True, False), ("501", False, True), ("503", False, False)], marks(near)
        assert marks(exact) == [("603", True, False), ("602", False, True), ("601", False, True)], marks(exact)

        # What the cards show.
        wait(lambda: len(every("duplicateGroup")) == 2 and len(every("duplicateTile")) == 6, "group cards shown")
        cards = {frozenset(tweet(item["key"]) for item in card.property("items")): card
                 for card in every("duplicateGroup")}
        near_card, exact_card = cards[frozenset({"501", "502", "503"})], cards[frozenset({"601", "602", "603"})]

        def badges(card):
            return [(label.property("text"), bool(role.property("tip")))
                    for role in every("duplicateRole", card)
                    for label in every("duplicateRoleLabel", role)]

        assert badges(near_card) == [("남김", False), ("사본", False), ("비슷함", True)], badges(near_card)
        assert badges(exact_card) == [("남김", False), ("사본", False), ("사본", False)], badges(exact_card)
        near_select = every("duplicateGroupSelect", near_card)[0]
        exact_select = every("duplicateGroupSelect", exact_card)[0]
        assert near_select.property("text") == "사본 1장 선택", near_select.property("text")
        assert exact_select.property("text") == "사본 2장 선택", exact_select.property("text")
        similar = every("duplicateTile", near_card)[2]
        assert attached(similar, "Accessible.name").startswith("비슷한 사진 "), attached(similar, "Accessible.name")
        tip = every("duplicateRole", near_card)[2].property("tip")
        assert "크기·화질" in tip and "‘비교’" in tip, tip
        shot("1-near-and-exact-groups")

        # The badge of the similar photo says why when the pointer rests on it, instead of the tile's line.
        similar_badge = every("duplicateRole", near_card)[2]
        hover(similar_badge)
        wait(lambda: attached(similar_badge, "ToolTip.visible"), "the badge explains itself")
        assert attached(similar_badge, "ToolTip.text") == tip
        assert not attached(similar, "tileMouse.ToolTip.visible")
        shot("2-similar-explained")
        # A badge of a copy says nothing more; the tile's own line is shown there, as before.
        copy_badge = every("duplicateRole", exact_card)[1]
        copy_tile = every("duplicateTile", exact_card)[1]
        hover(copy_badge)
        wait(lambda: not attached(similar_badge, "ToolTip.visible"), "the explanation leaves with the pointer")
        wait(lambda: attached(copy_tile, "tileMouse.ToolTip.visible"), "the tile's line on a copy's badge")
        assert not attached(copy_badge, "ToolTip.visible")
        # A click on the badge still picks its photo.
        press(similar_badge)
        wait(lambda: library.selectedCount == 1 and library.isSelected(near["items"][2]["key"]), "picked on its badge")
        press(similar_badge)
        wait(lambda: library.selectedCount == 0, "and let go")

        # The card's button picks only the file with the same bytes.
        press(near_select)
        wait(lambda: library.selectedCount == 1, "the near card's copy picked")
        assert library.isSelected(near["items"][1]["key"]) and not library.isSelected(near["items"][2]["key"])
        wait(lambda: near_select.property("text") == "선택 해제", "the button offers to let go")
        press(near_select)
        wait(lambda: library.selectedCount == 0, "and let go")
        press(exact_select)
        wait(lambda: library.selectedCount == 2, "the exact card's copies picked")
        press(exact_select)
        wait(lambda: library.selectedCount == 0, "and let go")

        # '사본 일괄 선택' after a near scan: only files with the same bytes.
        click("gallerySelectDuplicates")
        wait(lambda: library.selectedCount == 3, "every copy picked")
        picked = sorted(tweet(key) for key in library.selectedKeys)
        assert picked == ["501", "601", "602"], picked
        notice = window.property("transientNotice")
        assert "3개" in notice and "비슷함" in notice and "1장" in notice, notice
        shot("3-bulk-copies")

        # Picked by hand all the same, the similar photo is said to stay before anything moves.
        press(every("duplicateTile", near_card)[2])
        wait(lambda: library.selectedCount == 4, "the similar photo picked by hand")
        click("galleryRecycleSelected")
        confirmation = find("deleteConfirmation")
        wait(lambda: confirmation.property("visible"), "delete confirmation")
        summary = find("deleteSummaryText").property("text")
        assert "같은 파일이 남는 경우에만" in summary, summary
        assert "‘비슷함’ 1장은 ‘비교’에서" in summary, summary
        shot("4-confirmation")
        click("confirmRecycle")
        wait(lambda: not controller.management.mutating and not model.busy and not library.duplicatesSettling
             and model.count == 2 and library.duplicateCount == 2, "copies recycled, the similar photo kept")
        assert not any(files[number].exists() for number in (501, 601, 602))
        assert all(files[number].exists() for number in (502, 503, 603, 701))
        status = controller.management.status
        assert "그대로 두었습니다" in status and "비슷한 사진도 검사" in status, status
        wait(lambda: [marks(group) for group in library.duplicateGroups] == [[("502", True, False), ("503", False, False)]],
             "the photo kept and the similar one stay a group")
        wait(lambda: [b for card in every("duplicateGroup") for b in badges(card)] == [("남김", False), ("비슷함", True)],
             "the kept pair shows the photo to keep and the similar one")
        wait(lambda: every("duplicateGroupSelect")[0].property("text") == "사본 0장 선택"
             and not every("duplicateGroupSelect")[0].property("enabled"), "nothing on the card to offer")
        click("gallerySelectDuplicates")
        wait(lambda: "선택할 중복 사본이 없습니다" in window.property("transientNotice"), "nothing left to offer")
        assert library.selectedCount == 0
        shot("5-similar-left")

        assert find("galleryDuplicateNear").property("text") == "비슷한 사진도 검사"
        # Picked alone with its check box and sent with "휴지통으로", the similar photo opens the
        # comparison it needs. (It used to go the copies' way, stay, and nothing moved.)
        similar = every("duplicateTile", every("duplicateGroup")[0])[1]
        press(similar)
        wait(lambda: library.selectedCount == 1, "the similar photo picked alone")
        click("galleryRecycleSelected")
        dialog = find("alikeConfirmation")
        wait(lambda: dialog.property("visible"), "the comparison opened")
        shot("6-similar-compared")
        click("confirmAlikeRecycle")
        wait(lambda: not controller.management.mutating and not files[503].exists(), "the similar photo sent")
        assert files[502].exists() and files[701].exists()

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
class DuplicateNearCopiesSceneTests(unittest.TestCase):
    def test_only_files_with_the_same_bytes_are_offered_as_copies(self):
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
