"""Adjacent preview navigation follows paged gallery order and fresh selection."""

from contextlib import closing
from importlib.util import find_spec
from pathlib import Path
import tempfile
from threading import Event, Lock, get_ident
import time
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # noqa: F401


_QT = find_spec("PySide6") is not None
if _QT:
    from PySide6.QtCore import QCoreApplication
    from luvbox.preview import Thumbnail
    from luvbox.qt_library import LibraryController, LibraryRepository, _Request


class PagedRepository:
    def __init__(self):
        self.provider = None
        self.request = None
        self.lock = Lock()
        self.catalog = [(str(101 + index), "photo" if index % 2 == 0 else "video") for index in range(5)]
        self.block_page = False
        self.fail_page = False
        self.page_entered, self.page_release = Event(), Event()
        self.selection_entered, self.selection_release = Event(), Event()
        self.block_selection = ""
        self.missing = set()
        self.calls = []

    def configure(self, request):
        with self.lock:
            self.request = request

    def current(self, generation=None):
        with self.lock:
            request = self.request
            return request if request is not None and (generation is None or request.generation == generation) else None

    def rows(self, request):
        rows = [row for row in self.catalog if not request.query or request.query in row[0]]
        if request.media_type != "all":
            rows = [row for row in rows if row[1] == request.media_type]
        if request.order == "posted_oldest":
            rows = list(reversed(rows))
        return [{"key": f"g{request.generation}_flat_{ident}_1_{kind}", "caption": ident} for ident, kind in rows]

    def read(self, token, offset, limit):
        request = self.current()
        if request is None or token != str(request.generation):
            return 0, []
        rows = self.rows(request)
        if offset and self.fail_page:
            raise OSError("private path or token must not be shown")
        if offset and self.block_page:
            self.page_entered.set()
            if not self.page_release.wait(5):
                raise TimeoutError("disposable page fixture was not released")
        return len(rows), rows[offset:offset + limit]

    def selected(self, key, generation):
        request = self.current(generation)
        if request is None:
            return None
        self.calls.append((key, get_ident()))
        if key == self.block_selection:
            self.selection_entered.set()
            if not self.selection_release.wait(5):
                raise TimeoutError("disposable selection fixture was not released")
        if key not in {row["key"] for row in self.rows(request)}:
            return None
        missing = key.split("_")[2] in self.missing
        return {"key": key, "body": "body " + key, "author": "example", "missing": missing,
                "missing_note": "파일 없음" if missing else "", "media_type": key.split("_")[-1],
                "preview": "" if missing else "file:///synthetic/" + key + ".png",
                "pixel_width": 0 if missing else 1600, "pixel_height": 0 if missing else 1000}

    def decode(self, *_args):
        return Thumbnail(None, "synthetic reader only")


@unittest.skipUnless(_QT, "Qt dependencies are not installed")
class PreviewNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.repository = PagedRepository()
        patcher = patch("luvbox.qt_library.LibraryRepository", return_value=self.repository)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.controller = LibraryController()
        self.controller.model._batch_size = 2
        self.controller.setLibrary("synthetic", str(Path(tempfile.gettempdir()) / "not-a-real-library"), False)
        self.addCleanup(self.close)
        self.wait(lambda: not self.controller.model.busy and self.controller.model.count == 2)

    def wait(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.002)
        self.fail("Qt preview navigation did not settle")

    def close(self):
        self.repository.page_release.set()
        self.repository.selection_release.set()
        self.controller.shutdown()
        self.wait(lambda: self.controller.shutdownComplete)

    def key(self, index):
        return self.controller.model._rows[index]["key"]

    def select(self, index):
        key = self.key(index)
        self.controller.selectItem(key)
        self.wait(lambda: not self.controller.busy and self.controller.selectedKey == key)

    def test_previous_next_follow_model_order_across_pages_without_wrap(self):
        self.select(0)
        self.assertFalse(self.controller.canPrevious)
        self.assertFalse(self.controller.previousItem())
        for index in range(1, 5):
            self.assertTrue(self.controller.canNext)
            self.assertTrue(self.controller.nextItem())
            self.wait(lambda: not self.controller.busy and self.controller.selectedIndex == index)
            self.assertEqual(self.controller.selectedKey.split("_")[2], str(101 + index))
            self.assertEqual(self.controller.selectedBody, "body " + self.controller.selectedKey)
        self.assertEqual(self.controller.model.count, 5)
        self.assertFalse(self.controller.canNext)
        self.assertFalse(self.controller.nextItem())
        self.assertTrue(self.controller.previousItem())
        self.wait(lambda: not self.controller.busy and self.controller.selectedIndex == 3)
        self.assertTrue(all(thread != get_ident() for _key, thread in self.repository.calls))

    def test_paged_navigation_holds_current_photo_and_cannot_override_manual_selection(self):
        self.select(1)
        old = self.controller.selectedPreviewUrl
        self.repository.block_page = True
        self.assertTrue(self.controller.nextItem())
        self.assertTrue(self.repository.page_entered.wait(1))
        self.assertEqual(self.controller.selectedPreviewUrl, old)
        self.assertTrue(self.controller.busy)
        self.assertFalse(self.controller.canNext)
        self.select(0)
        selected = self.controller.selectedKey
        self.repository.page_release.set()
        self.wait(lambda: not self.controller.model.busy)
        self.assertEqual(self.controller.selectedKey, selected)
        self.assertEqual(self.controller.selectedIndex, 0)

    def test_query_change_cancels_old_page_navigation(self):
        self.select(1)
        self.repository.block_page = True
        self.controller.nextItem()
        self.assertTrue(self.repository.page_entered.wait(1))
        self.controller.search("105")
        self.repository.page_release.set()
        self.wait(lambda: not self.controller.model.busy and self.controller.model.count == 1)
        self.assertEqual(self.controller.selectedKey, "")
        self.assertFalse(self.controller.canNext)
        self.select(0)
        self.assertEqual(self.controller.selectedKey.split("_")[2], "105")
        self.assertFalse(self.controller.canPrevious)
        self.assertFalse(self.controller.canNext)

    def test_filtered_sorted_gallery_order_is_used_for_adjacent_items(self):
        self.controller.setFilters("photo", "posted_oldest", "")
        self.wait(lambda: not self.controller.model.busy and self.controller.model.count == 2)
        self.select(0)
        self.assertEqual(self.controller.selectedKey.split("_")[2], "105")
        self.controller.nextItem()
        self.wait(lambda: not self.controller.busy and self.controller.selectedIndex == 1)
        self.assertEqual(self.controller.selectedKey.split("_")[2], "103")
        self.controller.nextItem()
        self.wait(lambda: not self.controller.busy and self.controller.selectedIndex == 2)
        self.assertEqual(self.controller.selectedKey.split("_")[2], "101")
        self.assertEqual(self.repository.current().media_type, "photo")
        self.assertFalse(self.controller.canNext)

    def test_late_metadata_cannot_replace_a_fresh_selection_and_grid_sync_is_idempotent(self):
        self.repository.block_selection = self.key(0)
        self.controller.selectItem(self.key(0))
        self.assertTrue(self.repository.selection_entered.wait(1))
        serial = self.controller._selection_serial
        self.controller.selectItem(self.key(0))
        self.assertEqual(self.controller._selection_serial, serial)
        self.controller.selectItem(self.key(1))
        self.repository.selection_release.set()
        self.wait(lambda: not self.controller.busy)
        self.assertEqual(self.controller.selectedIndex, 1)
        self.assertEqual(self.controller.selectedKey, self.key(1))
        calls, serial = len(self.repository.calls), self.controller._selection_serial
        self.controller.selectItem(self.key(1))
        self.assertFalse(self.controller.busy)
        self.assertEqual(len(self.repository.calls), calls)
        self.assertEqual(self.controller._selection_serial, serial)

    def test_page_failure_keeps_current_selection_and_uses_fixed_error(self):
        self.select(1)
        selected = self.controller.selectedKey
        self.repository.fail_page = True
        self.assertTrue(self.controller.nextItem())
        self.wait(lambda: not self.controller.busy)
        self.assertEqual(self.controller.selectedKey, selected)
        self.assertIn("불러오지 못했습니다", self.controller.status)
        self.assertNotIn("private", self.controller.status)
        self.assertFalse(self.controller.canNext)
        self.assertTrue(self.controller.canPrevious)

    def test_missing_files_keep_their_identity_and_do_not_trap_navigation(self):
        self.repository.missing.add("103")
        self.select(1)
        self.controller.nextItem()
        self.wait(lambda: not self.controller.busy and self.controller.selectedIndex == 2)
        self.assertTrue(self.controller.selectedMissing)
        self.assertEqual(self.controller.selectedPreviewUrl, "")
        self.assertEqual((self.controller.selectedPixelWidth, self.controller.selectedPixelHeight), (0, 0))
        self.assertTrue(self.controller.nextItem())
        self.wait(lambda: not self.controller.busy and self.controller.selectedIndex == 3)
        self.assertFalse(self.controller.selectedMissing)
        self.assertEqual((self.controller.selectedPixelWidth, self.controller.selectedPixelHeight), (1600, 1000))

    def test_closing_cancels_paged_navigation_and_disables_controls(self):
        self.select(1)
        self.repository.block_page = True
        self.controller.nextItem()
        self.assertTrue(self.repository.page_entered.wait(1))
        self.controller.shutdown()
        self.assertFalse(self.controller.canNext)
        self.assertFalse(self.controller.canPrevious)
        self.assertFalse(self.controller.nextItem())
        self.repository.page_release.set()
        self.wait(lambda: self.controller.shutdownComplete)


@unittest.skipUnless(_QT, "Qt dependencies are not installed")
class SelectedDimensionsTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        from luvbox.history import HistoryStore
        from luvbox.storage import resolve_storage

        temporary = tempfile.TemporaryDirectory(prefix="luvbox-selected-dimensions-")
        self.addCleanup(temporary.cleanup)
        self.paths = resolve_storage(Path(temporary.name) / "media", "synthetic")
        self.photo = self.paths.media_dir / "example_101_1.jpg"
        self.paths.media_dir.mkdir(parents=True, exist_ok=True)
        with Image.new("RGB", (1600, 1000), "red") as image:
            exif = Image.Exif()
            exif[274] = 6
            image.save(self.photo, exif=exif)
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store.record_media("flat", {"tweet_id": "101", "num": 1, "type": "photo",
                                        "author": {"name": "example"}}, self.photo)
        self.repository = LibraryRepository()
        self.repository.configure(_Request(1, "synthetic", str(self.paths.output_dir), False))

    def test_original_dimensions_are_oriented_and_exposed_with_selection(self):
        selected = self.repository.selected("g1_flat_101_1_photo", 1)
        self.assertEqual((selected["pixel_width"], selected["pixel_height"]), (1000, 1600))
        self.assertEqual((selected["source_width"], selected["source_height"]), (1600, 1000))
        self.assertFalse(selected["missing"])
        self.assertTrue(selected["preview"])

    def test_replaced_file_cannot_publish_original_dimensions_or_preview(self):
        from luvbox.recycling import file_signature

        before = file_signature(self.photo)
        changed = (before[0], before[1] + 1, before[2], before[3])
        with patch("luvbox.qt_library.file_signature", side_effect=[before, changed]):
            selected = self.repository.selected("g1_flat_101_1_photo", 1)
        self.assertTrue(selected["missing"])
        self.assertEqual(selected["preview"], "")
        self.assertEqual((selected["pixel_width"], selected["pixel_height"]), (0, 0))
        self.assertEqual((selected["source_width"], selected["source_height"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
