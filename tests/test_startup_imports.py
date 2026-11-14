"""Starting the app does not load the AI classifier.

The names of the five categories are constants. Until 1.9.7 the window read them from the
classifier module while it was being built, and the library's worker did the same at that
moment. Loading that module loads numpy and onnxruntime (several hundred milliseconds), and
onnxruntime asks Windows (WMI) what machine this is: whenever that answer was slow, the
window waited for it. The names now live in ``clip_model``, which loads nothing.
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
NAMES = ["일러스트", "인물", "동물", "짤·텍스트", "풍경·배경"]
# What the classifier brings with it. None of these may be loaded by a start.
HEAVY = ("luvbox.clip_classifier", "onnxruntime", "numpy")


def scene():
    import faulthandler

    faulthandler.dump_traceback_later(50, exit=True)
    from contextlib import ExitStack, closing
    from unittest.mock import patch

    from PIL import Image
    from PySide6.QtCore import QCoreApplication, QEvent, qInstallMessageHandler
    from PySide6.QtWidgets import QApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from shiboken6 import delete

    sys.path.insert(0, str(ROOT))
    from luvbox.history import HistoryStore
    from luvbox.storage import resolve_storage
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_records import PREVIEW_PROVIDER
    from luvbox.qt_wheel import WheelInputFilter

    QQuickStyle.setStyle("Basic")
    application = QApplication([])
    messages = []
    previous = qInstallMessageHandler(lambda _level, _context, message: messages.append(message))
    controller = engine = folder = None
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
        until = time.monotonic() + 15
        while time.monotonic() < until:
            settle()
            if predicate():
                return
            time.sleep(.003)
        raise AssertionError(label + ": timeout")

    def loaded():
        return [name for name in HEAVY if name in sys.modules]

    try:
        folder = resources.enter_context(tempfile.TemporaryDirectory(prefix="luvbox-startup-imports-"))
        resources.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": folder}))
        resources.enter_context(patch("luvbox.qt_schedule.task_status", return_value={"registered": False}))
        base = Path(folder)
        paths = resolve_storage(base / "사진 폴더", "offline")
        with closing(HistoryStore.for_storage(paths)) as store:
            for tweet, category in (("101", "인물"), ("102", "인물"), ("103", "풍경·배경"), ("104", "")):
                path = paths.media_dir / f"writer_{tweet}_1.png"
                Image.new("RGB", (48, 36), (int(tweet) % 200, 90, 120)).save(path)
                store.record_media("flat", {"tweet_id": tweet, "num": 1, "type": "photo", "author": {"name": "writer"},
                                            "date": "2026-11-01T12:00:00+09:00"}, path)
                if category:
                    store.save_ai_tags([("flat", tweet, 1, "photo", category)])
        (base / "LuvBox").mkdir(exist_ok=True)
        (base / "LuvBox/settings.json").write_text(
            json.dumps({"account": "offline", "output_dir": str(paths.output_dir)}), encoding="utf-8")
        assert not loaded(), ("the preparation itself loaded the classifier", loaded())

        controller = ApplicationController()
        wheel_filter = WheelInputFilter()
        engine = QQmlApplicationEngine()
        engine.addImageProvider("thumbs", controller.library.provider)
        engine.addImageProvider(PREVIEW_PROVIDER, controller.records.preview_provider())
        engine.setInitialProperties({"appController": controller, "galleryModel": controller.library.model,
                                     "wheelInputFilter": wheel_filter})
        engine.load(ROOT / "assets/qt/MainWindow.qml")
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        window.setProperty("currentScreen", 1)
        library = controller.library
        # The window has its category names, and the worker has counted the photos in each.
        assert list(library.aiCategories) == NAMES, library.aiCategories
        wait(lambda: not controller.busy and len(library.aiCategoryCounts) == len(NAMES), "category counts read")
        counts = {row["category"]: row["count"] for row in library.aiCategoryCounts}
        assert counts == {"일러스트": 0, "인물": 2, "동물": 0, "짤·텍스트": 0, "풍경·배경": 1}, counts
        after_start = loaded()
        # Looking at one category is a filter, not a classification.
        library.setAiFilter("인물", "")
        wait(lambda: not controller.busy and library.filterAiCategory == "인물", "filter applied")
        wait(lambda: library.model.rowCount() == 2, "two photos in the category")
        library.setAiFilter("없는 분류", "")
        wait(lambda: not controller.busy and library.filterAiCategory == "", "an unknown category clears the filter")
        settle(200)
        after_use = loaded()

        controller.requestClose()
        wait(lambda: controller.shutdownReady, "shutdown")
        window.close()
        wheel_filter.close()
        delete(engine)
        engine = None
        delete(controller)
        controller = None
        print(json.dumps({"ok": True, "after_start": after_start, "after_use": after_use}))
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


class CategoryNameTests(unittest.TestCase):
    def test_one_list_of_names_for_the_classifier_the_catalog_and_the_screen(self):
        from luvbox import clip_model, history

        self.assertEqual(clip_model.CATEGORY_NAMES, NAMES)
        self.assertEqual([item["name"] for item in clip_model.CATEGORIES], NAMES)
        self.assertEqual(len(clip_model.CATEGORY_IDS), len(set(clip_model.CATEGORY_IDS)))
        # The catalog checks a category against its own copy; the two must not drift apart.
        self.assertEqual(history._AI_CATEGORIES, frozenset(NAMES))

    def test_the_names_are_read_without_loading_the_classifier(self):
        code = (
            "import json, sys\n"
            "import luvbox.clip_model as model\n"
            f"early = [name for name in {HEAVY!r} if name in sys.modules]\n"
            "import luvbox.clip_classifier as classifier\n"
            "print(json.dumps({'early': early, 'names': model.CATEGORY_NAMES,\n"
            "                  'same': classifier.CATEGORY_NAMES is model.CATEGORY_NAMES\n"
            "                          and classifier.CATEGORIES is model.CATEGORIES\n"
            "                          and classifier.CATEGORY_IDS is model.CATEGORY_IDS,\n"
            "                  'classifier': 'luvbox.clip_classifier' in sys.modules}))\n")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(report["early"], [])
        self.assertEqual(report["names"], NAMES)
        # The classifier still offers the same lists to whoever asks it.
        self.assertTrue(report["same"] and report["classifier"])

    @unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
    def test_the_gallery_code_is_imported_without_the_classifier(self):
        code = ("import json, sys\n"
                "import luvbox.qt_app\n"
                f"print(json.dumps([name for name in {HEAVY!r} if name in sys.modules]))\n")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1]), [])


@unittest.skipUnless(find_spec("PySide6") is not None, "Qt dependencies are not installed")
class StartSceneTests(unittest.TestCase):
    def test_the_main_window_opens_and_filters_by_category_without_the_classifier(self):
        try:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scene"],
                                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
                                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        except subprocess.TimeoutExpired as error:
            self.fail("scene timed out: " + str(error.stdout or "") + str(error.stderr or ""))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout.splitlines()[-1])
        self.assertTrue(report["ok"])
        self.assertEqual((report["after_start"], report["after_use"]), ([], []))


if __name__ == "__main__":
    if "--scene" in sys.argv:
        scene()
    else:
        unittest.main()
