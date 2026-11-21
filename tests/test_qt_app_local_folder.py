"""The "폴더 정리" tab inside the application: its settings, the shared AI tags and the X save folder."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image
from PySide6.QtCore import QCoreApplication

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox.app_settings import default_settings


def picture(path, color=(200, 30, 50)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 24), color).save(path)
    return path


class AppCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="luvbox-app-folder-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.appdata = self.base / "appdata"
        for patcher in (patch.dict(os.environ, {"LOCALAPPDATA": str(self.appdata)}),
                        patch("luvbox.qt_schedule.task_status", return_value={"registered": False})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.folder = self.base / "photos"
        picture(self.folder / "a.jpg")
        picture(self.folder / "sub" / "b.png", (10, 200, 10))

    def write_settings(self, values):
        path = self.appdata / "LuvBox" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"output_dir": str(self.base / "x-saves"), **values}), encoding="utf-8")

    def stored(self):
        return json.loads((self.appdata / "LuvBox" / "settings.json").read_text(encoding="utf-8"))

    def start(self):
        from luvbox.qt_app import ApplicationController

        controller = ApplicationController()

        def close():
            controller.requestClose()
            self.wait(lambda: controller.shutdownReady, "shutdown")
        self.addCleanup(close)
        return controller

    def wait(self, predicate, label="", timeout=10):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("timeout: " + label)

    def opened(self, local):
        self.wait(lambda: local.hasFolder and not local.filesLocked and not local.model.busy, "folder read")


class SettingsTests(AppCase):
    def test_the_folder_tab_settings_have_defaults(self):
        defaults = default_settings()
        self.assertEqual((defaults["local_folder_last"], defaults["local_folder_recent"],
                          defaults["local_folder_subfolders"], defaults["local_folder_share_learning"]),
                         ("", [], True, True))

    def test_damaged_values_are_replaced_by_the_defaults(self):
        self.write_settings({"local_folder_last": 5, "local_folder_recent": "C:/not/a/list",
                             "local_folder_subfolders": "yes", "local_folder_share_learning": 1})
        local = self.start().localFolder
        self.assertEqual((local.recentFolders, local.includeSubfolders, local.shareLearning), ([], True, True))
        self.assertFalse(local.hasFolder)

    def test_the_folder_and_its_switches_are_remembered_but_not_opened_at_start(self):
        self.write_settings({})
        controller = self.start()
        local = controller.localFolder
        self.assertTrue(local.openFolder(str(self.folder)))
        self.opened(local)
        local.setIncludeSubfolders(False)
        self.opened(local)
        local.setShareLearning(False)
        self.wait(lambda: self.stored().get("local_folder_share_learning") is False, "saved")
        stored = self.stored()
        self.assertEqual((stored["local_folder_last"], stored["local_folder_recent"], stored["local_folder_subfolders"]),
                         (str(self.folder), [str(self.folder)], False))
        controller.requestClose()
        self.wait(lambda: controller.shutdownReady, "shutdown")
        again = self.start().localFolder
        self.assertEqual((again.recentFolders, again.includeSubfolders, again.shareLearning),
                         ([str(self.folder)], False, False))
        self.assertFalse(again.hasFolder)
        self.assertTrue(again.openLastFolder())
        self.opened(again)
        self.assertEqual(again.folderPath, str(self.folder))


class ForgetTests(AppCase):
    def test_a_folder_taken_off_the_list_is_not_opened_again_at_the_next_start(self):
        self.write_settings({})
        controller = self.start()
        local = controller.localFolder
        local.openFolder(str(self.folder))
        self.opened(local)
        local.closeFolder()
        self.wait(lambda: not local.hasFolder, "closed")
        self.assertTrue(local.forgetFolder(str(self.folder), False))
        self.wait(lambda: self.stored().get("local_folder_recent") == [] and self.stored().get("local_folder_last") == "",
                  "forgotten in the settings")


class SharedTests(AppCase):
    def test_an_ai_tag_changed_in_one_tab_shows_in_the_other(self):
        self.write_settings({})
        controller = self.start()
        self.assertTrue(controller.library.addAiTag("고양이"))
        self.wait(lambda: "고양이" in controller.localFolder.aiCategories, "folder tab sees the new tag")
        self.assertTrue(controller.localFolder.setAiTagEnabled("고양이", False))
        self.wait(lambda: "고양이" not in controller.library.aiCategories, "gallery sees it switched off")

    def test_the_folder_tab_knows_when_it_overlaps_the_x_save_folder(self):
        self.write_settings({"output_dir": str(self.base)})
        controller = self.start()
        local = controller.localFolder
        local.openFolder(str(self.folder))
        self.opened(local)
        self.assertTrue(local.overlapsXLibrary)
        controller.setOutputFolder(str(self.base / "elsewhere"))
        self.wait(lambda: not local.overlapsXLibrary, "no overlap any more")

    def test_a_folder_inside_another_accounts_save_folder_overlaps_too(self):
        """The overlap notice looks at every account's save folder, not only the open account's."""
        self.write_settings({"output_dir": str(self.base / "current"),
                             "record_locations": {"someone": str(self.base)}})
        local = self.start().localFolder
        local.openFolder(str(self.folder))
        self.opened(local)
        self.assertTrue(local.overlapsXLibrary)

    def test_recycling_in_a_folder_inside_the_x_save_folder_has_the_gallery_read_again(self):
        self.write_settings({"output_dir": str(self.base)})
        moved = []

        def recycle(path, expected=None):
            target = self.base / "bin" / Path(path).name
            target.parent.mkdir(exist_ok=True)
            Path(path).rename(target)
            moved.append(Path(path).name)
            return str(target)

        controller = self.start()
        local, management = controller.localFolder, controller.localManagement
        local.openFolder(str(self.folder))
        self.opened(local)
        self.assertFalse(controller.galleryChanged)
        key = next(row["key"] for row in local.model._rows if row["caption"] == "a.jpg")
        with patch("luvbox.local_deletion.recycle_file", side_effect=recycle):
            management.prepareDelete([key])
            self.wait(lambda: management.deleteConfirmationPending or management.error, "manifest")
            management.confirmDelete(False)
            self.wait(lambda: not management.mutating, "recycled")
        self.assertEqual(moved, ["a.jpg"])
        self.wait(lambda: "a.jpg" not in [row["caption"] for row in local.model._rows], "tile gone")
        self.assertTrue(controller.galleryChanged)

    def test_closing_the_application_closes_the_folder_tab(self):
        self.write_settings({})
        controller = self.start()
        local, management = controller.localFolder, controller.localManagement
        controller.requestClose()
        self.wait(lambda: controller.shutdownReady, "shutdown")
        self.assertTrue(local.shutdownComplete)
        self.assertFalse(management._worker.is_alive())


if __name__ == "__main__":
    unittest.main()
