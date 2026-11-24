"""The AI tag manager and the preview's ✓ / × on AI tags, in the real product window.

Adding, switching off, deleting and restoring AI tags; turning one of the
person's own tags into an AI tag; one main button in the dialog; the AI menu
following the tag list; "not this" (×) on a photo's AI tag remembered for
learning; and none of it loading the AI libraries until something is classified.
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

    faulthandler.dump_traceback_later(50, exit=True)
    from contextlib import ExitStack, closing
    from unittest.mock import patch

    from PIL import Image
    from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QPointF, Qt, qInstallMessageHandler
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from shiboken6 import delete

    sys.path.insert(0, str(ROOT))
    from luvbox.ai_profile import AiProfileStore
    from luvbox.clip_model import CATEGORY_NAMES
    from luvbox.history import HistoryStore
    from luvbox.storage import resolve_storage
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

    def wait(predicate, label):
        until = time.monotonic() + 10
        while time.monotonic() < until:
            settle()
            if predicate():
                return
            time.sleep(.003)
        raise AssertionError(label + ": timeout")

    def items(name):
        found, stack = [], [window.contentItem()]
        for popup in window.findChildren(QObject):
            content = popup.property("contentItem") if popup.metaObject().indexOfProperty("contentItem") >= 0 else None
            if isinstance(content, QQuickItem):
                stack.append(content)
        # The tree has no cycles. (A set of Python ids would not do: Qt's wrappers are made
        # anew on every read and their ids come back once the old ones are collected.)
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

    def click(item, label=""):
        settle(60)
        if isinstance(item, str):
            label, item = item, find(item)
        assert item.property("visible") and item.property("enabled"), label
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        settle(30)

    def emphasised():
        return sorted(item.objectName() or str(item.property("text"))
                      for item in window.findChildren(QQuickItem)
                      if item.property("tone") == "primary" and item.property("visible"))

    def loaded():
        return [name for name in HEAVY if name in sys.modules]

    try:
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-ai-tags-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        base = Path(folder)
        paths = resolve_storage(base / "사진 폴더", "offline")
        with closing(HistoryStore.for_storage(paths)) as store:
            for tweet in range(101, 106):
                path = paths.media_dir / f"writer_{tweet}_1.png"
                Image.new("RGB", (48, 36), (tweet % 200, 90, 120)).save(path)
                store.record_media("flat", {"tweet_id": str(tweet), "num": 1, "type": "photo",
                                            "author": {"name": "writer"}, "date": "2026-11-01T12:00:00+09:00"}, path)
            for tweet in ("101", "102"):
                store.add_tag("flat", tweet, 1, "photo", "고양이")
            store.replace_ai_tags([("flat", "103", 1, "photo", ["인물", "일러스트"])])
        (base / "LuvBox").mkdir(exist_ok=True)
        (base / "LuvBox/settings.json").write_text(
            json.dumps({"account": "offline", "output_dir": str(paths.output_dir), "auto_classify": False}),
            encoding="utf-8")

        controller = ApplicationController()
        wheel_filter = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", controller.library.provider)
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel_filter})
        engine.load(ROOT / "assets/qt/MainWindow.qml")
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        window.setProperty("currentScreen", 1)
        library = controller.library
        wait(lambda: not controller.busy and library.model.count == 5, "gallery loaded")
        wait(lambda: len(library.aiCategoryCounts) == 5, "ai counts")
        assert list(library.aiCategories) == CATEGORY_NAMES

        # --- The manager opens from the AI menu, and has one main button.
        click("galleryAiMenuButton")
        menu = find("galleryAiMenu")
        wait(lambda: menu.property("visible"), "ai menu")
        click("galleryAiManage")
        dialog = find("aiTagManagerDialog")
        wait(lambda: dialog.property("visible") and not menu.property("visible"), "manager opened")
        settle(80)
        assert window.property("modalOpen") and not window.property("letterKeysFree")
        assert emphasised() == ["aiTagReclassify"], emphasised()
        assert dialog.property("width") <= window.width() and dialog.property("height") <= window.height()
        assert sorted(row["tag"] for row in library.aiTagRules) == sorted(CATEGORY_NAMES)
        assert all(rule["status_text"] == "기본 분류" for rule in library.aiTagRules), library.aiTagRules
        assert not find("aiTagRestore").property("enabled")

        # --- One of the person's own tags becomes an AI tag with one click.
        wait(lambda: [row["tag"] for row in library.userTags] == ["고양이"], "own tags read")
        wait(lambda: items("aiTagCandidate_고양이"), "own tag offered")
        candidate = find("aiTagCandidate_고양이")
        assert candidate.property("text") == "#고양이 · 2", candidate.property("text")
        click(candidate, "candidate")
        wait(lambda: "고양이" in library.aiCategories, "own tag became an AI tag")
        rule = next(row for row in library.aiTagRules if row["tag"] == "고양이")
        # No feature is stored yet, so its two photos are not examples yet.
        assert rule["status_text"] == "예시 1장 이상 필요" and not rule["built_in"], rule
        wait(lambda: not items("aiTagCandidate_고양이"), "candidate gone")

        # --- A new tag typed in, a default switched off, one deleted and restored.
        field = find("aiTagNewField")
        field.setProperty("text", "  #강아지 ")
        settle(30)
        click("aiTagAdd")
        wait(lambda: "강아지" in library.aiCategories and field.property("text") == "", "typed tag added")
        click(find("aiTagSwitch_동물"), "switch")
        wait(lambda: "동물" not in library.aiCategories, "switched off")
        assert any(row["tag"] == "동물" and not row["enabled"] for row in library.aiTagRules)
        click(find("aiTagDelete_인물"), "delete default")
        wait(lambda: "인물" not in [row["tag"] for row in library.aiTagRules], "default deleted")
        wait(lambda: find("aiTagRestore").property("enabled"), "restore offered")
        click("aiTagRestore")
        wait(lambda: "인물" in library.aiCategories and not find("aiTagRestore").property("enabled"), "restored")
        # The last row may sit below the list's visible part: press its button directly.
        assert QMetaObject.invokeMethod(find("aiTagDelete_강아지"), "click")
        wait(lambda: "강아지" not in library.aiCategories, "own tag deleted")
        with closing(AiProfileStore()) as profile:
            assert profile.known_tag_names() == CATEGORY_NAMES + ["고양이"], profile.known_tag_names()
        # The menu follows: its list is the switched-on tags.
        wait(lambda: [row["category"] for row in library.aiCategoryCounts]
             == ["일러스트", "인물", "짤·텍스트", "풍경·배경", "고양이"], "menu counts follow")
        counts = {row["category"]: row["count"] for row in library.aiCategoryCounts}
        assert counts["고양이"] == 2 and counts["인물"] == 1, counts
        assert loaded() == [], ("managing tags loaded the AI libraries", loaded())

        QTest.keyClick(window, Qt.Key.Key_Escape)
        wait(lambda: not dialog.property("visible"), "manager closed by Esc")

        # --- The preview: an AI tag has ✓ and ×; × says "not this" and is remembered.
        key = next(row["key"] for row in library.model._rows if "_103_" in row["key"])
        library.selectItem(key)
        wait(lambda: not library.selectionBusy and library.selectedKey == key
             and len(library.selectedAiTags) == 2, "photo with two AI tags")
        preview = find("previewDialog")
        assert QMetaObject.invokeMethod(preview, "open")
        wait(lambda: preview.property("visible"), "preview")
        settle(80)
        # The chips sit in the same row as the tag field (a search from the window also passes
        # through each popup and would meet them twice).
        chips, pending = [], [find("inlineTagField").parentItem().parentItem()]
        while pending:
            node = pending.pop()
            if node.objectName() == "previewTagChip" and node.property("visible"):
                chips.append(node)
            pending.extend(node.childItems())
        assert sorted(chip.property("tagName") for chip in chips) == ["인물", "일러스트"]
        target = next(chip for chip in chips if chip.property("tagName") == "인물")
        remove = next(item for item in target.findChildren(QQuickItem) if item.objectName() == "previewTagRemove")
        click(remove, "not this")
        wait(lambda: [row["tag"] for row in library.selectedAiTags] == ["일러스트"], "AI tag taken away")
        with closing(HistoryStore.for_storage(paths, create_media=False)) as store:
            assert store.tag_feedback([("flat", "103", 1, "photo")]) == {("flat", "103", 1, "photo"): {"인물": -1}}
        assert loaded() == [], ("a tag edit loaded the AI libraries", loaded())
        click("previewAiCorrect")
        teach_menu = find("galleryTeachMenu")
        wait(lambda: teach_menu.property("visible"), "preview + AI tag menu")
        assert teach_menu.property("singleOnly") is True
        QTest.keyClick(window, Qt.Key.Key_Escape)
        wait(lambda: not teach_menu.property("visible"), "preview teach menu closed")
        assert QMetaObject.invokeMethod(preview, "close")
        wait(lambda: not preview.property("visible"), "preview closed")

        # --- "지금 다시 분류" runs without the vision model and closes the manager.
        assert QMetaObject.invokeMethod(dialog, "open")
        wait(lambda: dialog.property("visible"), "manager again")
        with patch("luvbox.clip_classifier.get_session", side_effect=AssertionError("no vision model")):
            click("aiTagReclassify")
            wait(lambda: not dialog.property("visible"), "closed on reclassify")
            wait(lambda: not library.busy and "AI 태그를 다시 매겼습니다" in library.status, "reclassified")

        controller.requestClose()
        wait(lambda: controller.shutdownReady, "shutdown")
        window.close()
        wheel_filter.close()
        delete(engine)
        engine = None
        delete(controller)
        controller = None
        errors = [m for m in messages if any(term in m for term in (
            "ReferenceError", "TypeError", "Binding loop", "Cannot assign", "Cannot read", "is not a type"))]
        assert not errors, errors
        print(json.dumps({"ok": True}))
    except BaseException:
        import traceback
        traceback.print_exc()
        print("qt messages:", messages[-10:], file=sys.stderr)
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
class AiTagManagerSceneTests(unittest.TestCase):
    def test_ai_tags_are_managed_and_answered_in_the_real_window(self):
        try:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"],
                                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=90,
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
