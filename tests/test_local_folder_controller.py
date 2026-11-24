"""The "폴더 정리" tab's controller over a real folder: the gallery's behaviour with another source.

Only the vision model (a table of synthetic features) and the Recycle Bin (a
folder the files are moved to) stand in; the folder's list, its database in the
app data folder, the shared AI profile, learning and the recycling checks are
the product's code.
"""

from contextlib import closing
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image
from PySide6.QtCore import QCoreApplication, QUrl

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from tests.test_custom_ai_tags import Features
from luvbox.ai_profile import AiProfileStore
from luvbox.clip_model import ACTIVE_MODEL_ID, CATEGORY_NAMES
from luvbox.local_store import LocalFolderStore
from luvbox.qt_local_folder import LocalFolderController, LocalManagementController
from luvbox.recycling import RecyclingError, file_signature


def picture(path, color=(200, 30, 50), size=(40, 30)):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, color)
    image.putpixel((1, 1), (color[2], color[0], color[1]))
    image.save(path)
    return path


def files_under(folder):
    return sorted(str(path.relative_to(folder)) for path in folder.rglob("*"))


class FolderControllerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="luvbox-folder-tab-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.base / "appdata")})
        environment.start()
        self.addCleanup(environment.stop)
        self.folder = self.base / "내 사진"
        self.folder.mkdir()
        self.controller = None

    def open(self, **expected):
        self.controller = LocalFolderController()
        self.controller.setAutoClassify(False)
        self.addCleanup(self.close)
        self.assertTrue(self.controller.openFolder(str(self.folder)))
        self.settle()

    def close(self):
        if self.controller is not None:
            self.controller.shutdown()
            self.wait(lambda: self.controller.shutdownComplete, "shutdown")

    def wait(self, predicate, label="", timeout=10):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.003)
        self.fail("timeout: " + label)

    def settle(self):
        model = self.controller.model

        def quiet():
            return not self.controller.openingFolder and not self.controller.filesLocked and not model.busy

        # Quiet twice, a moment apart: a finished folder read reloads the list right after.
        for _ in range(2):
            self.wait(quiet, "folder read")
            until = time.monotonic() + 0.05
            while time.monotonic() < until:
                self.app.processEvents()
                time.sleep(0.005)
        self.wait(quiet, "folder read")
        while model.canFetchMore():
            model.loadMore()
            self.wait(lambda: not model.busy, "page")

    def captions(self):
        return sorted(row["caption"] for row in self.controller.model._rows)


class OpeningTests(FolderControllerCase):
    def setUp(self):
        super().setUp()
        picture(self.folder / "a.jpg")
        picture(self.folder / "b.png", (10, 200, 30))
        picture(self.folder / "sub" / "c.gif", (0, 0, 250))
        (self.folder / "notes.txt").write_text("not a photo", encoding="utf-8")

    def test_opening_a_folder_lists_its_photos_and_writes_nothing_into_it(self):
        before = files_under(self.folder)
        self.open()
        self.assertEqual(self.captions(), ["a.jpg", "b.png", "sub/c.gif"])
        for row in self.controller.model._rows:
            self.assertRegex(row["key"], r"\Ag[0-9]+_lf_[0-9]+\Z")
            self.assertTrue(self.controller.model.thumb_url(row["key"]).startswith("image://localthumbs/"))
        self.assertEqual(files_under(self.folder), before)
        self.assertEqual((self.controller.folderPath, self.controller.hasFolder), (str(self.folder), True))
        self.assertEqual(self.controller.recentFolders[0], str(self.folder))
        databases = list((self.base / "appdata" / "LuvBox" / "local-folders").glob("*.sqlite3"))
        self.assertEqual(len(databases), 1)

    def test_a_folder_that_cannot_be_opened_says_why_and_a_folder_url_opens(self):
        self.controller = LocalFolderController()
        self.controller.setAutoClassify(False)
        self.addCleanup(self.close)
        own = self.base / "appdata" / "LuvBox"
        own.mkdir(parents=True, exist_ok=True)
        self.assertTrue(self.controller.openFolder(str(own)))
        self.wait(lambda: self.controller.folderError, "the app's folder is refused")
        self.assertTrue(self.controller.folderError)
        self.assertFalse(self.controller.hasFolder)
        self.assertTrue(self.controller.openFolder(QUrl.fromLocalFile(str(self.folder)).toString()))
        self.settle()
        self.assertEqual((self.controller.folderPath, self.controller.folderError), (str(self.folder), ""))

    def test_subfolders_can_be_left_out(self):
        self.open()
        self.controller.setIncludeSubfolders(False)
        self.settle()
        self.assertEqual(self.captions(), ["a.jpg", "b.png"])

    def test_reading_the_folder_again_shows_new_files_and_drops_removed_ones(self):
        self.open()
        (self.folder / "a.jpg").unlink()
        picture(self.folder / "d.webp", (50, 50, 50))
        self.controller.refreshFolder()
        self.settle()
        self.assertEqual(self.captions(), ["b.png", "d.webp", "sub/c.gif"])
        self.assertIn("폴더를 읽었습니다", self.controller.status)

    def test_a_file_removed_outside_the_app_shows_as_missing_until_the_folder_is_read(self):
        self.open()
        (self.folder / "a.jpg").unlink()
        self.controller.setFolderOrder("size")
        self.settle()
        row = next(row for row in self.controller.model._rows if row["caption"].startswith("a.jpg"))
        self.assertTrue(row["missing"])
        self.assertIn("파일 없음", row["caption"])
        self.assertEqual(self.controller.folderOrder, "size")

    def test_a_photo_that_went_to_the_cloud_after_listing_is_not_opened(self):
        """OneDrive can free a listed photo's space; opening it would download it again."""
        self.open()
        key = next(row["key"] for row in self.controller.model._rows if row["caption"] == "a.jpg")
        with patch("luvbox.qt_local_folder.cloud_only",
                   side_effect=lambda path, **_: Path(path).name == "a.jpg"):
            self.controller.selectItem(key)
            self.wait(lambda: not self.controller.selectionBusy and self.controller.selectedKey == key, "opened")
        self.assertTrue(self.controller.selectedMissing)
        self.assertIn("클라우드", self.controller.selectedMissingNote)
        self.assertEqual(self.controller.selectedPreviewUrl, "")

    def test_the_remembered_folder_is_looked_at_away_from_the_window(self):
        """A remembered folder on a sleeping network drive must not freeze the window."""
        from luvbox import qt_local_folder

        real = qt_local_folder.check_root

        def slow(path):
            time.sleep(1.5)
            return real(path)

        self.controller = LocalFolderController()
        self.controller.setAutoClassify(False)
        self.addCleanup(self.close)
        self.controller.restore_preferences(str(self.folder), [str(self.folder)], True, True)
        with patch("luvbox.qt_local_folder.check_root", side_effect=slow):
            started = time.monotonic()
            self.assertTrue(self.controller.openLastFolder())
            self.assertLess(time.monotonic() - started, 0.5)
            self.wait(lambda: self.controller.hasFolder, "the remembered folder opened")
        self.settle()
        self.assertEqual(self.captions(), ["a.jpg", "b.png", "sub/c.gif"])

    def test_a_remembered_folder_that_is_gone_says_so(self):
        self.controller = LocalFolderController()
        self.controller.setAutoClassify(False)
        self.addCleanup(self.close)
        self.controller.restore_preferences(str(self.base / "없어진 폴더"), [], True, True)
        self.assertTrue(self.controller.openLastFolder())
        self.wait(lambda: self.controller.folderError, "said why")
        self.assertFalse(self.controller.hasFolder)

    def test_a_slow_recent_folder_does_not_block_opening_a_different_folder(self):
        from luvbox import qt_local_folder

        self.open()
        slow_folder = self.base / "slow"
        slow_folder.mkdir()
        next_folder = self.base / "next"
        picture(next_folder / "x.jpg")
        checking = threading.Event()
        release = threading.Event()
        real = qt_local_folder.check_root

        def validate(path):
            if path == str(slow_folder):
                checking.set()
                release.wait(1.5)
            return real(path)

        with patch("luvbox.qt_local_folder.check_root", side_effect=validate):
            try:
                started = time.monotonic()
                self.assertTrue(self.controller.openFolder(str(slow_folder)))
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertTrue(checking.wait(1))
                self.assertTrue(self.controller.openingFolder)
                self.assertEqual(self.controller.folderPath, str(self.folder))
                self.assertTrue(self.controller.openFolder(str(next_folder)))
                self.settle()
                self.assertEqual(self.controller.folderPath, str(next_folder))
            finally:
                release.set()
            self.wait(lambda: not any(worker.is_alive() for worker in self.controller._aux_threads), "folder checks done")
            self.app.processEvents()
        self.assertEqual(self.controller.folderPath, str(next_folder))

    def test_a_recent_folder_can_be_taken_off_the_list_with_or_without_its_record(self):
        """The record (tags, stars, "not duplicates", features) goes only when asked, never the open folder's."""
        from luvbox import local_store

        other = self.base / "다른 폴더"
        picture(other / "x.jpg")
        self.open()
        record = local_store.store_path(self.folder)
        self.controller.openFolder(str(other))
        self.settle()
        self.assertEqual(self.controller.recentFolders, [str(other), str(self.folder)])
        forgotten = []
        self.controller.folderForgotten.connect(forgotten.append)
        self.assertTrue(self.controller.forgetFolder(str(self.folder), False))
        self.wait(lambda: forgotten, "taken off the list")
        self.assertEqual(self.controller.recentFolders, [str(other)])
        self.assertTrue(record.exists())                   # its record stays unless asked
        self.assertFalse(self.controller.forgetFolder(str(other), True))      # the open folder's record stays
        self.assertTrue(local_store.store_path(other).exists())
        self.controller.closeFolder()
        with closing(AiProfileStore()) as profile:
            profile.add_tag("고양이")
            profile.sync_scope("local", local_store.folder_key(other), ["고양이"],
                               [(f"local:{local_store.folder_key(other)}:x.jpg", "고양이", 1, "siglip2_base_224",
                                 bytes(3072))])
        self.assertTrue(self.controller.forgetFolder(str(other), True))
        self.wait(lambda: len(forgotten) == 2, "taken off with its record")
        self.assertEqual(self.controller.recentFolders, [])
        record = local_store.store_path(other)
        self.assertFalse(any(Path(str(record) + suffix).exists() for suffix in ("", "-wal", "-shm")))
        self.assertTrue((other / "x.jpg").exists())                         # the photos themselves stay
        with closing(AiProfileStore()) as profile:
            self.assertEqual(profile.example_counts().get("고양이", (0, 0)), (0, 0))

    def test_the_tab_starts_its_workers_only_when_a_folder_opens(self):
        """Made at every start of the program, the folder tab costs no thread until it is used."""
        import threading

        names = {"qt-library-actions", "qt-library-browse", "qt-gallery-catalog"}

        def running():
            return sorted(thread.name for thread in threading.enumerate() if thread.name in names)

        before = running()
        idle = LocalFolderController()
        self.assertEqual(running(), before)
        idle.shutdown()
        self.wait(lambda: idle.shutdownComplete, "an idle tab closes")
        self.open()
        self.assertGreater(len(running()), len(before))

    def test_overlap_with_the_x_save_folder(self):
        self.open()
        self.controller.checkXOverlap(str(self.base))
        self.wait(lambda: not self.controller.checkingXOverlap, "overlap checked")
        self.assertTrue(self.controller.overlapsXLibrary)
        self.controller.checkXOverlap(str(self.base / "elsewhere"))
        self.wait(lambda: not self.controller.checkingXOverlap, "new overlap checked")
        self.assertFalse(self.controller.overlapsXLibrary)


class DuplicateTests(FolderControllerCase):
    def setUp(self):
        super().setUp()
        original = picture(self.folder / "keep.jpg", size=(64, 48))
        shutil.copyfile(original, self.folder / "copy.jpg")
        picture(self.folder / "other.png", (5, 200, 5))
        self.bin = self.base / "bin"
        self.bin.mkdir()

        def recycle(path, expected=None):
            if expected is not None and file_signature(path) != tuple(expected):
                raise RecyclingError("changed")
            shutil.move(str(path), self.bin / Path(path).name)
            return str(self.bin / Path(path).name)

        stand_in = patch("luvbox.local_deletion.recycle_file", side_effect=recycle)
        stand_in.start()
        self.addCleanup(stand_in.stop)

    def test_stopping_after_folder_sync_refreshes_the_list_and_duplicate_count(self):
        self.open()
        self.assertTrue(self.controller.scanDuplicates(False))
        self.wait(lambda: not self.controller.filesLocked and self.controller.duplicateCount == 2, "first scan")
        (self.folder / "copy.jpg").unlink()
        picture(self.folder / "new.jpg", (0, 30, 80))
        hashing, resume = threading.Event(), threading.Event()
        photos_to_hash = LocalFolderStore.photos_to_hash

        def pause_after_sync(store, **options):
            hashing.set()
            resume.wait(5)
            return photos_to_hash(store, **options)

        with patch.object(LocalFolderStore, "photos_to_hash", pause_after_sync):
            try:
                self.assertTrue(self.controller.scanDuplicates(False))
                self.assertTrue(hashing.wait(5))
                self.controller.cancelDuplicateScan()
            finally:
                resume.set()
            self.settle()

        self.assertEqual(self.captions(), ["keep.jpg", "new.jpg", "other.png"])
        self.assertEqual(self.controller.duplicateCount, 0)
        self.assertIn("중복 검사를 중단했습니다", self.controller.status)

    def test_stopping_after_comparison_keeps_the_previous_duplicate_rule(self):
        from luvbox.duplicates import cluster_images

        self.open()
        self.assertTrue(self.controller.scanDuplicates(False))
        self.wait(lambda: not self.controller.filesLocked and self.controller.duplicateCount == 2, "first scan")
        compared, resume = threading.Event(), threading.Event()

        def pause_after_comparison(*args, **kwargs):
            groups = cluster_images(*args, **kwargs)
            compared.set()
            resume.wait(5)
            return groups

        with patch("luvbox.duplicates.cluster_images", side_effect=pause_after_comparison):
            try:
                self.assertTrue(self.controller.scanDuplicates(True))
                self.assertTrue(compared.wait(5))
                self.controller.cancelDuplicateScan()
            finally:
                resume.set()
            self.settle()

        with closing(LocalFolderStore(self.folder)) as store:
            self.assertEqual(store.duplicate_rule(), "exact")
        self.assertIn("중복 검사를 중단했습니다", self.controller.status)

    def test_exact_scan_skips_image_decoding_and_near_scan_fills_the_cache(self):
        from luvbox import duplicates

        with Image.open(self.folder / "keep.jpg") as original:
            with original.resize((128, 96), Image.Resampling.LANCZOS) as larger:
                larger.save(self.folder / "larger.png")
        self.open()

        with patch.object(duplicates, "_dhash_from_stream", wraps=duplicates._dhash_from_stream) as decode:
            self.assertTrue(self.controller.scanDuplicates(False))
            self.settle()
            self.assertEqual(self.controller.duplicateCount, 2)
            self.assertEqual(decode.call_count, 0)

        keeper = self.folder / "keep.jpg"
        with closing(LocalFolderStore(self.folder)) as store:
            row = store.row_by_rel(keeper.name)
            self.assertEqual(row["file_hash"], duplicates.compute_file_hash(keeper))
            self.assertEqual(row["dhash"], "")
            self.assertNotEqual(row["file_signature"], duplicates.image_signature(keeper))

        with patch.object(duplicates, "_dhash_from_stream", wraps=duplicates._dhash_from_stream) as decode:
            self.assertTrue(self.controller.scanDuplicates(True))
            self.settle()
            self.assertEqual(self.controller.duplicateCount, 3)
            self.assertGreater(decode.call_count, 0)
            decode.reset_mock()
            self.assertTrue(self.controller.scanDuplicates(False))
            self.settle()
            self.assertEqual(self.controller.duplicateCount, 2)
            self.assertTrue(self.controller.scanDuplicates(True))
            self.settle()
            self.assertEqual(self.controller.duplicateCount, 3)
            self.assertEqual(decode.call_count, 0)

    def test_duplicate_scan_updates_a_case_only_renamed_file_in_the_list(self):
        self.open()
        (self.folder / "keep.jpg").rename(self.folder / "temporary.jpg")
        (self.folder / "temporary.jpg").rename(self.folder / "KEEP.jpg")

        self.assertTrue(self.controller.scanDuplicates(False))
        self.settle()

        self.assertEqual(self.captions(), ["KEEP.jpg", "copy.jpg", "other.png"])

    def test_first_duplicate_scan_uses_size_of_the_file_it_hashed(self):
        other = self.folder / "other.png"
        keep = self.folder / "keep.jpg"
        self.open()
        self.assertNotEqual(other.stat().st_size, keep.stat().st_size)
        photos_to_hash = LocalFolderStore.photos_to_hash

        def change_after_listing(store, **options):
            rows = photos_to_hash(store, **options)
            shutil.copyfile(keep, other)
            return rows

        with patch.object(LocalFolderStore, "photos_to_hash", change_after_listing):
            self.assertTrue(self.controller.scanDuplicates(False))
            self.wait(lambda: not self.controller.filesLocked, "scan")
        with closing(LocalFolderStore(self.folder)) as store:
            self.assertEqual(store.duplicate_count(), 3)
            self.assertEqual(store.row_by_rel("other.png")["filesize"], keep.stat().st_size)
        self.settle()
        details = {row["caption"]: row["detail"] for row in self.controller.model._rows}
        self.assertEqual(details["other.png"], details["keep.jpg"])

    def test_alike_comparison_does_not_read_unrelated_duplicate_groups(self):
        resized = self.folder / "resized.png"
        with Image.open(self.folder / "keep.jpg") as image:
            image.resize((256, 192)).save(resized)
        self.open()
        with closing(LocalFolderStore(self.folder)) as store:
            store.save_hashes([("keep.jpg", "h1", "0" * 16, ""), ("resized.png", "h2", "0" * 16, "")])
            store.save_clusters([["keep.jpg", "resized.png"]], "near")
            store.toggle_favorite(store.row_by_rel("keep.jpg")["rowid"])
        self.controller.setDuplicateOnly(True)
        self.settle()
        key = next(row["key"] for row in self.controller.model._rows if row["caption"] == resized.name)

        with patch.object(LocalFolderStore, "duplicate_members", side_effect=AssertionError("whole catalog")):
            manifest = self.controller.alikeManifest(key)

        self.assertEqual(manifest["alike"]["keeper"]["name"], "keep.jpg")
        self.assertEqual(manifest["alike"]["alike"]["name"], resized.name)

    def test_copy_confirmation_does_not_read_unrelated_duplicate_groups(self):
        self.open()
        self.assertTrue(self.controller.scanDuplicates(False))
        self.wait(lambda: not self.controller.filesLocked and self.controller.duplicateCount == 2, "scan")
        self.controller.setDuplicateOnly(True)
        self.settle()
        copy = next(row for row in self.controller.model._rows if row.get("copy"))
        keeper = next(row for row in self.controller.model._rows if row.get("keeper"))

        with (patch.object(LocalFolderStore, "duplicate_members", side_effect=AssertionError("whole catalog")),
              patch.object(LocalFolderStore, "duplicate_copy_rel_paths", side_effect=AssertionError("whole catalog"))):
            manifest = self.controller.deletionManifest([copy["key"]])

        self.assertEqual(manifest["kept"], 0)
        self.assertEqual(manifest["rows"][0]["_copy_keepers"], [str(self.folder / keeper["caption"])])

    def test_copies_are_found_chosen_and_recycled_and_one_file_stays(self):
        self.open()
        self.assertTrue(self.controller.scanDuplicates(False))
        self.wait(lambda: not self.controller.filesLocked and self.controller.duplicateCount == 2, "scan")
        self.controller.setDuplicateOnly(True)
        self.settle()
        self.assertEqual(self.controller.model.count, 2)
        kept = next(row["caption"] for row in self.controller.model._rows if row.get("keeper"))
        copy = next(row["caption"] for row in self.controller.model._rows if row.get("copy"))
        self.assertEqual({kept, copy}, {"keep.jpg", "copy.jpg"})
        self.controller.selectDuplicateCopies()
        self.assertEqual(len(self.controller.selectedKeys), 1)
        management = LocalManagementController(self.controller)
        self.addCleanup(lambda: (management.shutdown(), self.wait(lambda: not management._worker.is_alive())))
        management.prepareDelete(list(self.controller.selectedKeys))
        self.wait(lambda: management.deleteConfirmationPending or management.error, "manifest")
        self.assertEqual((management.error, management.deleteCount), ("", 1))
        management.confirmDelete(False)
        self.wait(lambda: not management.mutating, "recycled")
        self.assertEqual(management.error, "")
        self.assertEqual(sorted(path.name for path in self.bin.iterdir()), [copy])
        self.assertTrue((self.folder / kept).exists())
        self.controller.setDuplicateOnly(False)
        self.settle()
        self.assertNotIn(copy, self.captions())

    def test_choosing_every_file_of_a_group_still_leaves_one(self):
        self.open()
        self.controller.scanDuplicates(False)
        self.wait(lambda: not self.controller.filesLocked and self.controller.duplicateCount == 2, "scan")
        self.controller.setDuplicateOnly(True)
        self.settle()
        management = LocalManagementController(self.controller)
        self.addCleanup(lambda: (management.shutdown(), self.wait(lambda: not management._worker.is_alive())))
        management.prepareDelete([row["key"] for row in self.controller.model._rows])
        self.wait(lambda: management.deleteConfirmationPending or management.error, "manifest")
        # The confirmation already names one file and says the group keeps one.
        self.assertEqual(management.deleteCount, 1)
        self.assertIn("묶음마다 한 장씩 남깁니다", management.deleteSummary)
        management.confirmDelete(False)
        self.wait(lambda: not management.mutating, "recycled")
        self.assertEqual(management.error, "")
        self.assertEqual(len(list(self.bin.iterdir())), 1)
        self.assertEqual(len([name for name in ("keep.jpg", "copy.jpg") if (self.folder / name).exists()]), 1)


class AiTests(FolderControllerCase):
    def setUp(self):
        super().setUp()
        self.features = Features(seed=7)
        self.vectors = {}
        self.encoded = []
        for index in range(10):
            self.add("cat", f"cat{index}.png", spread=0.35)
        for index in range(30):
            self.add(f"other{index % 3}", f"other{index}.png")
        for target, value in (("luvbox.clip_classifier.is_available", True),
                              ("luvbox.clip_classifier.get_session", object()),
                              ("luvbox.clip_classifier.release_session", None),
                              ("luvbox.clip_model.model_file_present", True)):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        encoder = patch("luvbox.clip_classifier.classify_detailed", side_effect=self.fake_encode)
        encoder.start()
        self.addCleanup(encoder.stop)
        with closing(AiProfileStore()) as profile:
            for name in CATEGORY_NAMES:
                profile.set_tag_enabled(name, False)
            profile.add_tag("고양이")

    def add(self, cluster, name, spread=0.5):
        picture(self.folder / name, (len(self.vectors) * 5 % 250, 40, 90))
        self.vectors[name] = self.features.photo(cluster, spread)

    def fake_encode(self, paths, threshold=None, batch_size=16, session=None):
        self.encoded.extend(Path(path).name for path in paths)
        return [(None, 0.5, np.ascontiguousarray(self.vectors[Path(path).name], dtype=np.float32)) for path in paths]

    def key_of(self, name):
        def found():
            model = self.controller.model
            if model.canFetchMore():
                model.loadMore()
            return next((row["key"] for row in model._rows if row["caption"] == name), None)
        self.wait(lambda: found() is not None, "tile " + name)
        return found()

    def tag_cats(self, count):
        for index in range(count):
            self.controller.toggleSelection(self.key_of(f"cat{index}.png"))
        self.assertEqual(self.controller.addTagToSelection("고양이"), count)
        self.controller.clearSelection()

    def classify(self):
        self.assertTrue(self.controller.scanAiTags())
        self.wait(lambda: not self.controller.aiScanning and not self.controller.filesLocked, "classified", 30)

    def test_the_shared_tags_classify_the_folder_and_its_examples_join_the_profile(self):
        self.open()
        self.assertIn("고양이", self.controller.aiCategories)
        self.tag_cats(6)
        self.classify()
        self.controller.setAiFilter("고양이", "")
        self.settle()
        self.assertEqual(self.captions(), sorted(f"cat{index}.png" for index in range(10)))
        with closing(AiProfileStore()) as profile:
            self.assertEqual(profile.example_counts()["고양이"], (6, 0))

    def test_a_folder_is_classified_by_itself_only_once_the_person_classified_it(self):
        """Opening a big folder to clear duplicates must not start a long classification on its own."""
        self.controller = LocalFolderController()
        self.addCleanup(self.close)
        self.controller._auto_ai_timer.setInterval(10)
        self.assertTrue(self.controller.openFolder(str(self.folder)))
        self.settle()
        until = time.monotonic() + 0.5
        while time.monotonic() < until:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual((self.encoded, self.controller.aiScanning), ([], False))
        self.classify()                                  # the person runs it once here
        self.add("cat", "new.png", spread=0.35)
        self.encoded.clear()
        self.controller.refreshFolder()
        self.wait(lambda: self.encoded == ["new.png"] and not self.controller.filesLocked, "new photo classified", 30)

    def test_learning_kept_to_the_folder_leaves_the_profile_as_it_was(self):
        self.open()
        self.tag_cats(6)
        self.classify()
        self.controller.setShareLearning(False)
        self.assertFalse(self.controller.shareLearning)
        with closing(AiProfileStore()) as profile:
            self.assertEqual(profile.example_counts().get("고양이", (0, 0)), (0, 0))
        self.controller.setShareLearning(True)
        with closing(AiProfileStore()) as profile:
            self.assertEqual(profile.example_counts()["고양이"], (6, 0))

    def test_with_learning_kept_to_the_folder_classifying_leaves_the_shared_profile_alone(self):
        """Sharing off: the folder is classified with the shared tags, which it only reads."""
        with closing(AiProfileStore()) as profile:       # another library taught #고양이 meanwhile
            rows = [(f"x:other:{index}", "고양이", 1, ACTIVE_MODEL_ID, self.features.photo("cat", 0.35).tobytes())
                    for index in range(6)]
            profile.sync_scope("x", "other", ["고양이"], rows)
        self.open()
        self.controller.setShareLearning(False)
        self.tag_cats(6)
        with closing(AiProfileStore()) as profile:
            before = (profile.rules_version(), profile.load_tag_weights(), profile.example_counts())
        self.classify()
        with closing(AiProfileStore()) as profile:
            self.assertEqual((profile.rules_version(), profile.load_tag_weights(), profile.example_counts()), before)

    def test_with_learning_kept_to_the_folder_a_yes_adds_no_shared_tag(self):
        self.open()
        self.controller.setShareLearning(False)
        self.classify()
        self.assertTrue(self.controller.teachTag(self.key_of("cat1.png"), "강아지", True))
        self.wait(lambda: not self.controller.filesLocked, "taught", 30)
        with closing(AiProfileStore()) as profile:
            self.assertNotIn("강아지", profile.known_tag_names())
        with closing(LocalFolderStore(self.folder)) as store:
            self.assertEqual(store.tag_feedback()["cat1.png"], {"강아지": 1})     # the answer stays in the folder

    def test_the_sharing_switch_waits_for_a_running_classification(self):
        self.open()
        self.controller._exclusive = "ai"
        try:
            self.controller.setShareLearning(False)
            self.assertTrue(self.controller.shareLearning)
        finally:
            self.controller._exclusive = ""
        self.controller.setShareLearning(False)
        self.assertFalse(self.controller.shareLearning)

    def test_a_filter_on_a_tag_switched_off_elsewhere_is_let_go(self):
        """The gallery switches #고양이 off while this tab lists only #고양이: the list shows every photo again."""
        self.open()
        self.tag_cats(6)
        self.classify()
        self.controller.setAiFilter("고양이", "")
        self.settle()
        self.assertEqual(self.controller.filterAiCategory, "고양이")
        with closing(AiProfileStore()) as profile:
            profile.set_tag_enabled("고양이", False)
        self.controller.reloadAiProfile()                 # what the other tab's change announces
        self.settle()
        self.assertEqual(self.controller.filterAiCategory, "")
        self.assertEqual(len(self.captions()), 40)
        shown = self.controller._repository.selected(self.key_of("cat8.png"), self.controller._generation)
        self.assertEqual(shown["ai_tags"], [])

    def test_teaching_a_photo_is_remembered_in_the_folder(self):
        self.open()
        self.tag_cats(6)
        self.classify()
        self.assertTrue(self.controller.teachTag(self.key_of("cat7.png"), "고양이", False))
        self.wait(lambda: not self.controller.filesLocked, "taught", 30)
        with closing(AiProfileStore()) as profile:
            self.assertEqual(profile.example_counts()["고양이"], (6, 1))
        self.controller.setAiFilter("고양이", "")
        self.settle()
        self.assertNotIn("cat7.png", self.captions())

    def test_clearing_ai_tags_stops_auto_classify_until_explicit_run(self):
        self.open()
        self.tag_cats(6)
        self.classify()
        self.assertTrue(self.controller.clearAiTags())
        self.wait(lambda: not self.controller.filesLocked and self.controller.aiScanning is False, "cleared", 30)
        self.assertFalse(self.controller._repository.auto_ai_pending(self.controller._generation))
        self.add("cat", "new_after_clear.png", spread=0.35)
        self.encoded.clear()
        self.controller._auto_ai_timer.setInterval(10)
        self.controller.refreshFolder()
        self.wait(lambda: not self.controller.filesLocked, "refreshed", 30)
        until = time.monotonic() + 0.4
        while time.monotonic() < until:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(self.encoded, [])
        self.classify()
        self.assertIn("new_after_clear.png", self.encoded)


if __name__ == "__main__":
    unittest.main()
