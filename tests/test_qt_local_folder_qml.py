"""The "폴더 정리" tab in the real product window.

The third tab opens a folder of this PC (the folder dialog itself is the
system's, so the scene opens the folder through the same slot it calls), lists
its photos with their own thumbnails, previews one, finds duplicate copies and
moves one to the Recycle Bin (a stand-in folder here) with the gallery's
confirmation, without the X "다시 받지 않기" switch. The empty tab has one main
button and the opened tab none, the gallery tab is not shown as chosen while
this one is, and none of it loads the AI libraries.
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
HEAVY = ("luvbox.clip_classifier", "luvbox.tag_learner", "onnxruntime", "numpy")


def scene():
    import faulthandler

    faulthandler.dump_traceback_later(80, exit=True)
    from contextlib import ExitStack
    import shutil
    import threading
    from unittest.mock import patch

    from PIL import Image
    from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, Qt, qInstallMessageHandler
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from shiboken6 import delete

    sys.path.insert(0, str(ROOT))
    from luvbox.local_store import check_root
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_wheel import WheelInputFilter

    QQuickStyle.setStyle("Basic")
    application = QApplication([])
    messages = []
    previous = qInstallMessageHandler(lambda _level, _context, message: messages.append(message))
    controller = engine = window = None
    resources = ExitStack()

    def settle(milliseconds=0):
        until = time.monotonic() + milliseconds / 1000
        while True:
            application.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if time.monotonic() >= until:
                return
            time.sleep(.003)

    def wait(predicate, label, timeout=10):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            settle()
            if predicate():
                return
            time.sleep(.003)
        raise AssertionError(label + ": timeout")

    def items(name):
        found, stack = [], [window.contentItem()]
        # Popups draw in the window's overlay, outside contentItem: their content is looked at too
        # (only theirs: a list's contentItem is already below contentItem).
        for popup in window.findChildren(QObject):
            content = popup.property("contentItem") if popup.inherits("QQuickPopup") else None
            if isinstance(content, QQuickItem):
                stack.append(content)
        while stack:
            node = stack.pop()
            if node.objectName() == name:
                found.append(node)
            stack.extend(node.childItems())
        return found

    def find(name):
        result = window.findChild(QObject, name)
        if result is not None:
            return result
        found = items(name)
        if not found:
            raise AssertionError("Missing product control: " + name)
        return found[0]

    def shown(name):
        return [item for item in items(name) if item.isVisible()]

    def point_of(item):
        return item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()

    def click(item, label="", double=False):
        settle(60)
        if isinstance(item, str):
            label, item = item, find(item)
        assert item.property("visible") and item.property("enabled"), label
        if double:
            QTest.mouseDClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point_of(item))
        else:
            QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point_of(item))
        settle(30)

    def emphasised():
        return sorted(item.objectName() or str(item.property("text"))
                      for item in window.findChildren(QQuickItem)
                      if item.property("tone") == "primary" and item.isVisible())

    def loaded():
        return [name for name in HEAVY if name in sys.modules]

    try:
        base = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-folder-tab-qml-")))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": str(base)}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        photos = base / "정리할 사진"
        photos.mkdir()
        for index in range(4):
            Image.new("RGB", (64, 48), (40 + index * 50, 90, 120)).save(photos / f"photo{index}.png")
        shutil.copyfile(photos / "photo0.png", photos / "photo0 - 복사본.png")
        shutil.copyfile(photos / "photo1.png", photos / "photo1 - 복사본.png")
        bin_folder = base / "bin"
        bin_folder.mkdir()

        def recycle(path, expected=None):
            target = bin_folder / Path(path).name
            Path(path).rename(target)
            return str(target)

        resources.enter_context(patch("luvbox.local_deletion.recycle_file", side_effect=recycle))
        (base / "LuvBox").mkdir(exist_ok=True)
        (base / "LuvBox/settings.json").write_text(
            json.dumps({"output_dir": str(base / "X 저장"), "auto_classify": False}), encoding="utf-8")

        controller = ApplicationController()
        wheel_filter = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", controller.library.provider)
        engine.addImageProvider("localthumbs", controller.localFolder.provider)
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel_filter})
        engine.load(ROOT / "assets/qt/MainWindow.qml")
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        window.resize(1280, 860)
        local = controller.localFolder

        # --- The third tab: chosen alone, its empty state has one main button.
        click("navLocalFolder")
        wait(lambda: window.property("currentScreen") == 3, "folder tab shown")
        assert find("navLocalFolder").property("checked") and not find("navGallery").property("checked")
        wait(lambda: shown("localFolderEmpty"), "empty state")
        assert emphasised() == ["localFolderChoose"], emphasised()

        # --- A folder opens (the folder dialog calls this slot) and lists its photos.
        assert local.openFolder(str(photos))
        wait(lambda: local.hasFolder and not local.filesLocked and local.model.count == 6, "folder listed")
        wait(lambda: len(shown("localFolderTile")) == 6, "tiles shown")
        assert emphasised() == [], emphasised()
        # The keys work at once: the list has the focus, not the hidden "폴더 선택".
        wait(lambda: find("localFolderGrid").property("activeFocus"), "the list has the keys")
        # Inside the X save folder the notice says what really happens to a photo sent away here.
        local.checkXOverlap(str(base))
        wait(lambda: shown("localFolderOverlap"), "overlap notice")
        notice = next(item for item in shown("localFolderOverlap")[0].childItems() if item.property("text"))
        assert "다시 받을 수 있습니다" in notice.property("text"), notice.property("text")
        local.checkXOverlap(str(base / "X 저장"))
        wait(lambda: not local.checkingXOverlap and not shown("localFolderOverlap"), "notice gone")
        assert find("localFolderPath").property("text") == str(photos)
        assert find("localFolderMediaType").property("visible") and find("localFolderDensity").property("visible")
        search = find("localFolderSearch")
        QTest.keyClick(window, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        for key in (Qt.Key.Key_P, Qt.Key.Key_H, Qt.Key.Key_O, Qt.Key.Key_T, Qt.Key.Key_O, Qt.Key.Key_0):
            QTest.keyClick(window, key)
        wait(lambda: local.model.count == 2 and not local.model.busy, "file name filter applied")
        click("localFolderSubfolders")
        wait(lambda: not local.includeSubfolders and not local.busy and not local.model.busy, "subfolders excluded")
        assert search.property("text") == "photo0" and local.model.count == 2
        finish_folder_check = threading.Event()

        def check_unavailable_folder(path):
            finish_folder_check.wait(5)
            return check_root(path)

        with patch("luvbox.qt_local_folder.check_root", side_effect=check_unavailable_folder):
            try:
                assert local.openFolder(str(base / "없는 폴더"))
                wait(lambda: "폴더를 확인하는 중" in find("localFolderStatus").property("text"), "opening progress")
                assert search.property("text") == "photo0" and local.model.count == 2
            finally:
                finish_folder_check.set()
            wait(lambda: local.folderError, "missing folder reported")
        assert search.property("text") == "photo0" and local.model.count == 2
        search.forceActiveFocus()
        QTest.keyClick(window, Qt.Key.Key_Escape)
        wait(lambda: search.property("text") == "" and local.model.count == 6, "file name filter cleared")
        wait(lambda: len(shown("localFolderTile")) == 6, "all tiles drawn again")
        tile = shown("localFolderTile")[0]
        thumb = next(item for item in tile.childItems() if item.objectName() == "localFolderThumb")
        wait(lambda: thumb.property("paintedWidth") > 0, "thumbnail drawn")
        assert thumb.property("source").toString().startswith("image://localthumbs/"), thumb.property("source")

        # --- The preview opens on a double click and closes with Esc.
        click(tile, "tile", double=True)
        preview = find("localFolderPreview")
        wait(lambda: preview.property("visible") and local.selectedKey, "preview open")
        assert find("localFolderPreviewMaximize").property("visible")
        assert window.property("modalOpen")
        preview_media = find("localFolderPreviewImage")
        wait(lambda: preview_media.property("ready"), "preview MediaPreview ready")
        assert preview_media.property("canZoomIn")
        wait(lambda: str(photos.name) not in find("localFolderPreviewInfo").property("text")
             and find("localFolderPreviewInfo").property("text"), "photo details")
        QTest.keyClick(window, Qt.Key.Key_Escape)
        wait(lambda: not preview.property("visible"), "preview closed")
        assert preview_media.property("_released"), "preview released handle on close"
        # The tab's keys: F stars the opened photo; Ctrl+F focuses search without toggling favorite; F1 lists the keys.
        opened = local.selectedKey
        QTest.keyClick(window, Qt.Key.Key_F)
        wait(lambda: local.selectedIsFavorite, "starred with F")
        QTest.keyClick(window, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        wait(lambda: find("localFolderSearch").property("activeFocus"), "Ctrl+F focuses search")
        assert local.selectedIsFavorite, "Ctrl+F must not toggle favorite"
        QTest.keyClick(window, Qt.Key.Key_Escape)
        wait(lambda: find("localFolderGrid").property("activeFocus"), "Esc returns focus to grid")
        QTest.keyClick(window, Qt.Key.Key_F1)
        help_dialog = find("shortcutHelp")
        wait(lambda: help_dialog.property("visible"), "key list")
        assert any(section["title"] == "폴더 정리" for section in help_dialog.property("sections").toVariant())
        QTest.keyClick(window, Qt.Key.Key_Escape)
        wait(lambda: not help_dialog.property("visible"), "key list closed")
        assert local.selectedKey == opened
        # Dragging across tiles draws a rectangle and picks what it touches.
        tiles = sorted(shown("localFolderTile"), key=lambda item: (item.mapToScene(QPointF(0, 0)).y(),
                                                                   item.mapToScene(QPointF(0, 0)).x()))
        begin, finish = point_of(tiles[0]), point_of(tiles[1])
        QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, begin)
        for step in range(1, 7):
            QTest.mouseMove(window, QPoint(begin.x() + (finish.x() - begin.x()) * step // 6, begin.y()))
            settle(15)
        wait(lambda: find("localFolderBand").property("visible"), "selection rectangle")
        wait(lambda: local.selectedCount == 2, "two tiles picked by the rectangle")
        QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, finish)
        settle(60)
        assert not find("localFolderBand").property("visible")
        assert set(local.selectedKeys) == {tiles[0].property("key"), tiles[1].property("key")}, local.selectedKeys
        local.clearSelection()
        local.setSelectionMode(False)

        # --- Duplicates: found and shown as groups, with the gallery's tools.
        click("localFolderDuplicates")
        wait(lambda: shown("localFolderDuplicateTools"), "duplicate tools shown")
        assert find("localDuplicateHelp").property("visible")
        assert find("localFolderNear").property("text") == "비슷한 사진도 검사"
        click("localFolderScanDuplicates")
        wait(lambda: not local.filesLocked and local.duplicateCount == 4, "duplicates found")
        wait(lambda: shown("localDuplicateReview") and len(shown("duplicateGroup")) == 2, "duplicate view")

        def mark_first_group():
            group = shown("duplicateGroup")[0]
            click(next(item for item in group.findChildren(QQuickItem) if item.objectName() == "duplicateGroupDistinct"),
                  "not duplicates")
            wait(lambda: len(shown("duplicateGroup")) == 1 and local.distinctDuplicateCount == 1, "group marked")

        # "중복 아님" taken back, set again and cleared.
        mark_first_group()
        click("localFolderUndoDistinct")
        wait(lambda: len(shown("duplicateGroup")) == 2 and local.distinctDuplicateCount == 0, "mark taken back")
        mark_first_group()
        assert find("localFolderClearDistinct").property("text") == "중복 아님 1개 초기화",             find("localFolderClearDistinct").property("text")
        click("localFolderClearDistinct")
        wait(lambda: len(shown("duplicateGroup")) == 2 and local.distinctDuplicateCount == 0, "marks cleared")
        # Every copy picked at once and sent with the visible button and the gallery's confirmation.
        click("localFolderSelectDuplicates")
        wait(lambda: len(local.selectedKeys) == 2, "copies chosen")
        click("localFolderRecycleSelected")
        confirmation = find("localDeleteConfirmation")
        wait(lambda: confirmation.property("visible"), "confirmation")
        assert not any(item.isVisible() for item in items("deleteKeepSwitch")), "no X re-download switch here"
        assert emphasised() == ["confirmRecycle"], emphasised()
        click(next(item for item in shown("confirmRecycle")), "move")
        wait(lambda: not controller.localManagement.mutating and len(list(bin_folder.iterdir())) == 2, "moved")
        left = sorted(path.name for path in photos.iterdir())
        assert len([name for name in left if name.startswith("photo0")]) == 1, left
        assert len([name for name in left if name.startswith("photo1")]) == 1, left
        # What the Recycle Bin did is said on the tab's status line, as in the gallery.
        wait(lambda: "휴지통 이동 2개" in find("localFolderStatus").property("text"), "the move's result shown")
        # An X save running elsewhere does not switch this tab's comparison off ...
        controller._preparing = True
        controller.changed.emit()
        settle(40)
        assert controller.busy and not find("localDuplicateReview").property("workBusy")
        controller._preparing = False
        controller.changed.emit()
        # ... but a Recycle Bin that waits (a save into an overlapping folder) shows on the button.
        click("localFolderDuplicates")
        wait(lambda: not local.filterDuplicateOnly, "list again")
        local.selectLoaded()
        wait(lambda: local.selectedCount > 0, "photos picked")
        # The folder lies inside the X save folder and a save is running: the app holds the Recycle Bin.
        local.checkXOverlap(str(base))
        wait(lambda: local.overlapsXLibrary, "overlap checked")
        controller.download._running = True
        controller._state_changed()
        settle(40)
        assert controller.localManagement.locked
        recycle = find("localFolderRecycleSelected")
        assert not recycle.property("enabled") and recycle.property("tip"), (recycle.property("enabled"),
                                                                              recycle.property("tip"))
        QTest.keyClick(window, Qt.Key.Key_Delete)
        settle(60)
        assert not find("localDeleteConfirmation").property("visible")
        assert "저장" in find("localFolderStatus").property("text"), find("localFolderStatus").property("text")
        controller.download._running = False
        local.checkXOverlap(str(base / "X 저장"))
        controller._state_changed()
        settle(40)
        assert not controller.localManagement.locked
        local.clearSelection()
        local.setSelectionMode(False)
        # A focused tag list keeps letters for itself: D does not open the duplicate view behind it.
        local.selectLoaded()
        wait(lambda: local.selectedCount > 0, "photos picked again")
        combo = find("localFolderUserTag")
        combo.forceActiveFocus()
        settle(40)
        QTest.keyClick(window, Qt.Key.Key_D)
        settle(60)
        assert not local.filterDuplicateOnly
        QTest.keyClick(window, Qt.Key.Key_Delete)          # not a letter: the list passes it on
        settle(60)
        assert not find("localDeleteConfirmation").property("visible") and not controller.localManagement.mutating
        local.clearSelection()
        local.setSelectionMode(False)
        local.setDuplicateOnly(True)
        wait(lambda: local.filterDuplicateOnly, "duplicate view for the next step")

        # --- The AI tag manager of this tab is the shared one.
        click("localFolderDuplicates")
        wait(lambda: not local.filterDuplicateOnly, "list again")
        click("localFolderAiMenuButton")
        wait(lambda: find("localFolderAiMenu").property("visible"), "AI menu")
        click("localFolderAiManage")
        manager = find("localFolderAiManager")
        wait(lambda: manager.property("visible"), "AI tag manager")
        assert window.property("modalOpen")
        manager.close()
        wait(lambda: not manager.property("visible"), "manager closed")
        assert loaded() == [], ("the folder tab loaded the AI libraries", loaded())

        # --- A recent folder taken off the list from the empty tab (reached via the recent-folder menu's close action).
        from luvbox import local_store

        click("localFolderRecentButton")
        wait(lambda: find("localFolderRecentMenu").property("visible"), "recent menu")
        click("localFolderCloseAction")
        wait(lambda: not local.hasFolder and shown("localFolderEmpty"), "empty tab again")
        wait(lambda: shown("localFolderForget"), "the recent folder offers ×")
        click(shown("localFolderForget")[0], "take it off")
        dialog = find("localFolderForgetDialog")
        wait(lambda: dialog.property("visible"), "asked first")
        assert window.property("modalOpen")
        # One main button in the dialog (the empty tab's own stays behind it).
        assert [name for name in emphasised() if name != "localFolderChoose"] == ["localFolderForgetConfirm"],             emphasised()
        assert not find("localFolderForgetErase").property("checked")
        click(next(item for item in shown("localFolderForgetConfirm")), "confirm")
        wait(lambda: not dialog.property("visible") and list(local.recentFolders) == [], "off the list")
        assert local_store.store_path(photos).exists()

        # --- Back to the gallery tab: chosen alone again.
        click("navGallery")
        wait(lambda: window.property("currentScreen") == 1, "gallery shown")
        assert find("navGallery").property("checked") and not find("navLocalFolder").property("checked")

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
            "Unable to assign", "Error:"))]
        assert not errors, errors
        print(json.dumps({"ok": True}))
    except BaseException:
        import traceback
        traceback.print_exc()
        print("qt messages:", messages[-15:], file=sys.stderr)
        sys.stdout.flush()
        sys.stderr.flush()
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
class LocalFolderSceneTests(unittest.TestCase):
    def test_the_folder_tab_in_the_real_window(self):
        try:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"],
                                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120,
                                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        except subprocess.TimeoutExpired as error:
            self.fail("scene timed out: " + str(error.stdout or "") + str(error.stderr or ""))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout.splitlines()[-1])["ok"])


if __name__ == "__main__":
    if "--scene" in sys.argv:
        scene()
    else:
        unittest.main()
