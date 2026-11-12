"""Recycled files leave the gallery in place; the list and its position stay."""
from contextlib import closing
from importlib.util import find_spec
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # noqa: F401  Never open the user's app data.

_QT = find_spec("PySide6") is not None
if _QT:
    from PySide6.QtCore import QCoreApplication
    from luvbox.history import HistoryStore
    from luvbox.qt_app import ApplicationController
    from luvbox.qt_gallery import GalleryModel, _REMOVE_RUN_CAP
    from luvbox.qt_library import LibraryController
    from luvbox.qt_management import ManagementController, _recycled_keys
    from luvbox.recycling import RecyclingError
    from luvbox.storage import resolve_storage


def _settle(app, predicate, seconds=8):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        app.processEvents()
        if predicate():
            return True
        time.sleep(.003)
    return False


@unittest.skipUnless(_QT, "Qt dependencies are not installed")
class ModelRowRemovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def model(self, count, batch=1000):
        rows = [{"key": f"item{index}", "caption": str(index)} for index in range(count)]
        model = GalleryModel(lambda query, offset, limit: (len(rows), rows[offset:offset + limit]),
                             batch_size=batch)
        self.addCleanup(model.shutdown)
        self.assertTrue(_settle(self.app, lambda: not model.busy and model.count == min(count, batch)))
        return model

    def keys(self, model):
        return [model.data(model.index(row), model.KeyRole) for row in range(model.count)]

    def test_runs_are_removed_from_the_end_and_the_index_stays_exact(self):
        model = self.model(10)
        removed = []
        model.rowsRemoved.connect(lambda _parent, first, last: removed.append((first, last)))
        self.assertEqual(model.removeKeys(["item7", "item2", "item3", "item9", "unknown", "item2"]), 4)
        self.assertEqual(removed, [(9, 9), (7, 7), (2, 3)])
        self.assertEqual(self.keys(model), ["item0", "item1", "item4", "item5", "item6", "item8"])
        self.assertEqual(model._positions, {key: row for row, key in enumerate(self.keys(model))})
        self.assertEqual(model._keys, set(self.keys(model)))
        self.assertEqual((model.count, model.total), (6, 6))

    def test_a_loading_page_or_a_widely_scattered_selection_asks_for_a_reload(self):
        model = self.model(_REMOVE_RUN_CAP * 2 + 4)
        scattered = [f"item{index}" for index in range(0, _REMOVE_RUN_CAP * 2 + 2, 2)]
        self.assertEqual(model.removeKeys(scattered), 0)
        self.assertEqual(model.count, _REMOVE_RUN_CAP * 2 + 4)
        model._busy = True
        self.assertEqual(model.removeKeys(["item1"]), 0)
        model._busy = False
        self.assertEqual(model.removeKeys([]), 0)
        self.assertEqual(model.removeKeys(["item1"]), 1)

    def test_total_never_drops_below_the_rows_still_shown(self):
        model = self.model(5, batch=3)
        self.assertEqual((model.count, model.total), (3, 5))
        self.assertEqual(model.removeKeys(["item0"]), 1)
        self.assertEqual((model.count, model.total), (2, 4))
        self.assertTrue(model.canFetchMore())


@unittest.skipUnless(_QT, "Qt dependencies are not installed")
class RecycledKeyTests(unittest.TestCase):
    def test_keys_follow_the_paths_that_really_moved(self):
        rows = [{"_key": "g1_flat_1_1_photo", "path": r"C:\media\A.jpg"},
                {"_key": "g1_flat_2_1_photo", "path": r"C:\media\b.jpg"}]
        self.assertEqual(_recycled_keys(rows, [r"c:\MEDIA\a.jpg"]), ["g1_flat_1_1_photo"])
        self.assertEqual(_recycled_keys(rows, []), [])
        self.assertIsNone(_recycled_keys(rows, None))
        self.assertIsNone(_recycled_keys([{"path": r"C:\media\A.jpg"}], [r"C:\media\A.jpg"]))


@unittest.skipUnless(_QT, "Qt dependencies are not installed")
class InPlaceRecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="luvbox-in-place-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "사진 보관"
        self.output.mkdir()
        self.kept = self.root / "simulated-recycle"
        self.kept.mkdir()
        for name in ("luvbox.storage.records_root", "luvbox.qt_library.records_root"):
            replacement = patch(name, return_value=self.root / "records")
            replacement.start()
            self.addCleanup(replacement.stop)
        state = patch("luvbox.protection.state_directory", return_value=self.root / "protection")
        state.start()
        self.addCleanup(state.stop)
        self.paths = resolve_storage(self.output, "alice")
        with closing(HistoryStore.for_storage(self.paths)) as store:
            for number in range(1, 8):
                path = self.output / f"alice_{100 + number}_1.jpg"
                path.write_bytes(f"fixture-{number}".encode())
                store.record_media("flat", {"tweet_id": str(100 + number), "num": 1, "type": "photo",
                                            "author": {"name": "alice"}}, path)
        self.library = LibraryController()
        self.library.model._batch_size = 3
        self.management = ManagementController(self.library)
        self.addCleanup(self.close)
        self.shell = SimpleNamespace(_management=self.management, _library=self.library, _gallery_dirty=True,
                                     changed=SimpleNamespace(emit=lambda: None))
        self.management.libraryChanged.connect(
            lambda: ApplicationController._managed_library_changed(self.shell))
        self.library.setLibrary("alice", str(self.output), False)
        self.management.setLibrary("alice", str(self.output), False)
        self.wait(lambda: not self.library.model.busy and self.library.model.count == 3)
        self.library.model.loadMore()
        self.wait(lambda: not self.library.model.busy and self.library.model.count == 6)

    def wait(self, predicate):
        if not _settle(self.app, predicate):
            self.fail("gallery did not settle")

    def close(self):
        self.management.shutdown()
        self.library.shutdown()
        self.wait(lambda: not self.management._worker.is_alive() and self.library.shutdownComplete)
        self.app.processEvents()

    def keys(self):
        return [row["key"] for row in self.library.model._rows]

    def recycle(self, keys, *, fail=False):
        def move(path, expected):
            if fail:
                raise RecyclingError("fixture failure")
            path = Path(path)
            target = self.kept / path.name
            path.rename(target)
            return str(target)

        self.management.prepareDelete(keys)
        self.wait(lambda: self.management.deleteConfirmationPending)
        with patch("luvbox.deletion.recycle_file", side_effect=move):
            self.management.confirmDelete(False)
            self.wait(lambda: not self.management.mutating)
        self.app.processEvents()

    def test_recycled_tiles_leave_and_the_next_page_continues_after_the_last_row(self):
        before = self.keys()
        generation = self.library._generation
        self.library.toggleSelection(before[1])
        self.library.toggleSelection(before[4])
        self.library.selectItem(before[4])
        self.wait(lambda: not self.library.selectionBusy and self.library.selectedKey == before[4])
        self.recycle([before[1], before[4]])
        self.assertEqual(self.library._generation, generation)
        self.assertEqual(self.keys(), [before[0], before[2], before[3], before[5]])
        self.assertEqual(self.library.selectedKeys, [])
        # The photo that took the opened one's place is opened at once, without waiting for the grid.
        self.assertTrue(self.library.selectionBusy or self.library.selectedKey == before[5])
        self.wait(lambda: not self.library.selectionBusy and self.library.selectedKey == before[5])
        self.assertEqual(self.library.model.total, 5)
        self.assertTrue(self.shell._gallery_dirty)
        self.assertFalse(self.library.canSelect(before[1]))
        # The seventh file was never shown. It must arrive once, after the last shown row.
        self.library.model.loadMore()
        self.wait(lambda: not self.library.model.busy and self.library.model.count == 5)
        self.assertEqual(self.library.model.error, "")
        self.assertEqual(len(set(self.keys())), 5)
        self.assertEqual(self.keys()[:4], [before[0], before[2], before[3], before[5]])
        self.assertFalse(self.library.model.canFetchMore())

    def test_removing_the_opened_photo_never_leaves_a_moment_with_nothing_selected(self):
        # A preview closes itself when nothing is selected and nothing is loading. After a
        # recycle that moment existed until the grid moved its highlight, and a count
        # arriving in between closed the preview that had just come back.
        before = self.keys()
        self.library.selectItem(before[2])
        self.wait(lambda: not self.library.selectionBusy and self.library.selectedKey == before[2])
        states = []
        self.library.changed.connect(
            lambda: states.append(self.library.selectionBusy or bool(self.library.selectedKey)))
        self.assertTrue(self.library.removeDeleted([before[2]]))
        self.assertTrue(states and all(states), states)
        self.assertTrue(self.library.selectionBusy)
        self.wait(lambda: not self.library.selectionBusy and self.library.selectedKey == before[3])
        self.assertTrue(all(states), states)
        # The last photo hands over to the one before it; an emptied list has nothing to open.
        last = self.keys()[-1]
        self.library.selectItem(last)
        self.wait(lambda: not self.library.selectionBusy and self.library.selectedKey == last)
        self.assertTrue(self.library.removeDeleted([last]))
        self.wait(lambda: not self.library.selectionBusy and self.library.selectedKey == self.keys()[-1])
        self.assertTrue(self.library.removeDeleted(self.keys()))
        self.assertEqual((self.library.selectedKey, self.library.selectionBusy, self.library.model.count),
                         ("", False, 0))

    def test_a_remaining_tile_can_be_recycled_next_without_reselecting(self):
        before = self.keys()
        self.recycle([before[0]])
        self.assertEqual(self.keys(), before[1:])
        self.recycle([before[1]])
        self.assertEqual(self.keys(), before[2:])
        self.assertEqual(self.management.error, "")

    def test_a_failed_move_reloads_the_list(self):
        before = self.keys()
        generation = self.library._generation
        self.recycle([before[0]], fail=True)
        self.wait(lambda: self.library._generation == generation + 1 and not self.library.model.busy)
        self.assertFalse(self.shell._gallery_dirty)
        self.assertEqual(self.library.model.count, 3)

    def test_duplicate_view_reloads_because_remaining_originals_leave_it_too(self):
        self.assertFalse(self.library.removeDeleted([]))
        key = self.keys()[0]
        self.library._duplicate_only = True
        self.assertFalse(self.library.removeDeleted([key]))
        self.library._duplicate_only = False
        self.assertFalse(self.library.removeDeleted(["g999_flat_101_1_photo"]))
        self.assertEqual(len(self.keys()), 6)


if __name__ == "__main__":
    unittest.main()
