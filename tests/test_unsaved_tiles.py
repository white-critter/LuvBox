"""The unsaved-media list for the tile view: more rows as it scrolls, tiles picked as in the gallery.

A new page is added under what is shown, so the tiles above keep their place and what
was picked. Shift+click picks a range, a dragged rectangle picks what it touches, and
after something was put away the list is read again as far as it had been loaded.
"""

from contextlib import closing
from importlib.util import find_spec
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # noqa: F401  Never open the user's app data.
from luvbox.history import HistoryStore
from luvbox.storage import resolve_storage

_QT = find_spec("PySide6") is not None
if _QT:
    from PySide6.QtCore import QCoreApplication, QSize
    from luvbox import qt_records, thumbcache
    from luvbox.qt_records import PreviewShelf, RecordsController, UnsavedModel, UnsavedPreviewProvider

ACCOUNT = "offline"
TOTAL = 25
PAGE = 10


@unittest.skipUnless(_QT, "Qt dependencies are not installed")
class TileListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="luvbox-tiles-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "appdata")})
        environment.start()
        self.addCleanup(environment.stop)
        paging = patch("luvbox.qt_records._UNSAVED_PAGE", PAGE)
        paging.start()
        self.addCleanup(paging.stop)
        self.paths = resolve_storage(self.root / "Likes", ACCOUNT)
        with closing(HistoryStore.for_storage(self.paths)) as store:
            for index in range(TOTAL):
                tweet = str(1000 + index)
                store.record_declined("flat", {"tweet_id": tweet, "num": 1, "type": "photo",
                                               "author": {"name": f"writer{index:02d}"}},
                                      self.paths.media_dir / f"writer_{tweet}_1.jpg")
        # Every third media has a picture kept on this PC.
        from PIL import Image
        with closing(thumbcache.Cache(ACCOUNT)) as cache:
            for index in range(0, TOTAL, 3):
                cache.put((str(1000 + index), 1, "photo"), Image.new("RGB", (64, 48), (200, 40 + index, 60)))
        self.records = RecordsController()
        self.addCleanup(self.close)
        self.records.setLibrary(ACCOUNT, str(self.paths.output_dir), False)
        self.records.setActive("unsaved", True)
        self.records.setUnsavedView("excluded")
        self.settled(PAGE)

    def close(self):
        self.records.shutdown()
        self.wait(lambda: not self.records._worker.is_alive() and not self.records._shutdown_timer.isActive())

    def wait(self, predicate, label="the list did not settle"):
        until = time.monotonic() + 6
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.003)
        self.fail(label)

    def settled(self, count):
        self.wait(lambda: not self.records.unsavedBusy and len(self.records.unsavedRows) == count,
                  f"expected {count} rows, have {len(self.records.unsavedRows)}")

    def tokens(self):
        return [row["token"] for row in self.records.unsavedRows]

    def picked(self):
        """What the tiles themselves say: the model's 'picked' role, as positions."""
        model = self.records.unsavedModel
        return [number for number in range(model.rowCount())
                if model.data(model.index(number, 0), UnsavedModel.PickedRole)]

    def test_more_rows_arrive_under_what_is_shown(self):
        model = self.records.unsavedModel
        self.assertEqual((model.rowCount(), self.records.unsavedTotal, self.records.unsavedMore), (PAGE, TOTAL, True))
        first = self.tokens()
        resets, inserted = [], []
        model.modelReset.connect(lambda: resets.append(True))
        model.rowsInserted.connect(lambda _parent, start, end: inserted.append((start, end)))
        self.records.toggleUnsavedSelected(first[3])
        self.records.loadMoreUnsaved()
        self.settled(2 * PAGE)
        # The first tiles are the same tiles, still picked; the grid was not rebuilt.
        self.assertEqual(self.tokens()[:PAGE], first)
        self.assertEqual((resets, inserted), ([], [(PAGE, 2 * PAGE - 1)]))
        self.assertEqual((self.records.unsavedSelectedCount, self.picked()), (1, [3]))
        self.assertEqual(len(set(self.tokens())), 2 * PAGE)
        self.records.loadMoreUnsaved()
        self.settled(TOTAL)
        self.assertFalse(self.records.unsavedMore)
        self.assertEqual(model.rowCount(), TOTAL)
        # Every excluded media once, in the list's order.
        authors = [row["author"] for row in self.records.unsavedRows]
        self.assertEqual(len(set(authors)), TOTAL)
        # At the end another request does nothing.
        self.records.loadMoreUnsaved()
        self.app.processEvents()
        self.assertFalse(self.records.unsavedBusy)
        self.assertEqual(len(self.records.unsavedRows), TOTAL)

    def test_the_model_carries_what_a_tile_shows(self):
        model = self.records.unsavedModel
        names = {bytes(name).decode() for name in model.roleNames().values()}
        self.assertEqual(names, set(UnsavedModel.FIELDS) | {"picked"})
        roles = {bytes(name).decode(): role for role, name in model.roleNames().items()}
        row = self.records.unsavedRows[0]
        index = model.index(0, 0)
        for name in UnsavedModel.FIELDS:
            self.assertEqual(model.data(index, roles[name]), row[name], name)
        self.assertIs(model.data(index, roles["picked"]), False)
        self.assertIsNone(model.data(model.index(PAGE, 0), roles["token"]))
        # Picking tells only the tile that changed.
        changed = []
        model.dataChanged.connect(lambda first, last, changed_roles: changed.append((first.row(), last.row(), list(changed_roles))))
        self.records.toggleUnsavedSelected(self.tokens()[4])
        self.assertEqual(changed, [(4, 4, [UnsavedModel.PickedRole])])

    def test_shift_click_picks_the_range_from_the_last_clicked_tile(self):
        tokens = self.tokens()
        self.records.toggleUnsavedSelected(tokens[2])
        self.records.selectUnsavedRangeTo(tokens[6])
        self.assertEqual(self.picked(), [2, 3, 4, 5, 6])
        # Backwards from the same tile, added to what is picked.
        self.records.selectUnsavedRangeTo(tokens[0])
        self.assertEqual(self.picked(), [0, 1, 2, 3, 4, 5, 6])
        # Without a tile clicked before, the range is that tile.
        self.records.clearUnsavedSelection()
        self.records.selectUnsavedRangeTo(tokens[8])
        self.assertEqual(self.picked(), [8])
        self.records.selectUnsavedRangeTo("not a token")
        self.assertEqual(self.picked(), [8])

    def test_a_dragged_rectangle_picks_the_tiles_it_touches(self):
        tokens = self.tokens()
        self.records.toggleUnsavedSelected(tokens[9])
        # Four columns: rows 0-1, columns 1-2 are tiles 1, 2, 5 and 6. Without Ctrl what was picked goes.
        self.records.beginUnsavedBand(False)
        self.records.unsavedBandSelect(0, 1, 1, 2, 4)
        self.assertEqual(self.picked(), [1, 2, 5, 6])
        # The rectangle shrinks while the button is still down.
        self.records.unsavedBandSelect(0, 0, 1, 2, 4)
        self.assertEqual(self.picked(), [1, 2])
        # Beyond the last tile and the last column nothing is invented.
        self.records.unsavedBandSelect(2, 9, 0, 9, 4)
        self.assertEqual(self.picked(), [8, 9])
        self.records.endUnsavedBand()
        # With Ctrl (or Shift) held, a second rectangle adds to the first.
        self.records.beginUnsavedBand(True)
        self.records.unsavedBandSelect(0, 0, 0, 0, 4)
        self.assertEqual(self.picked(), [0, 8, 9])
        self.records.endUnsavedBand()
        # A Shift+click after the drag measures from the last tile the drag picked.
        self.records.selectUnsavedRangeTo(tokens[7])
        self.assertEqual(self.picked(), [0, 7, 8, 9])
        # Outside a drag the calls do nothing.
        self.records.unsavedBandSelect(0, 2, 0, 3, 4)
        self.assertEqual(self.picked(), [0, 7, 8, 9])

    def test_all_loaded_tiles_at_once_and_the_limit_of_one_retry(self):
        self.records.loadMoreUnsaved()
        self.settled(2 * PAGE)
        self.records.selectUnsavedPage()
        self.assertEqual(self.records.unsavedSelectedCount, 2 * PAGE)
        self.records.clearUnsavedSelection()
        self.assertEqual(self.picked(), [])
        with patch.object(qt_records, "MAX_PICKED", 3):
            self.records.selectUnsavedPage()
            self.assertEqual(self.picked(), [0, 1, 2])
            self.assertIn("최대 3개", self.records.unsavedStatus)
            self.records.toggleUnsavedSelected(self.tokens()[5])
            self.assertEqual(self.picked(), [0, 1, 2])
            # Letting one go makes room again.
            self.records.toggleUnsavedSelected(self.tokens()[0])
            self.records.toggleUnsavedSelected(self.tokens()[5])
            self.assertEqual(self.picked(), [1, 2, 5])

    def test_after_something_was_put_away_the_list_is_read_as_far_as_it_was_loaded(self):
        self.records.loadMoreUnsaved()
        self.settled(2 * PAGE)
        tokens = self.tokens()
        gone = {self.records.unsavedRows[index]["author"] for index in (1, 14)}
        self.records.toggleUnsavedSelected(tokens[1])
        self.records.toggleUnsavedSelected(tokens[14])
        self.records.settleSelected()                      # in this view: release the exclusion
        self.wait(lambda: self.records.excludedCount == TOTAL - 2 and not self.records.unsavedBusy
                  and len(self.records.unsavedRows) == 2 * PAGE, "the list was not read again")
        # Two pages were on screen and two pages are again; the released ones are not among them.
        self.assertEqual((self.records.unsavedPage, self.records.unsavedTotal), (1, TOTAL - 2))
        self.assertFalse(gone & {row["author"] for row in self.records.unsavedRows})
        self.assertEqual(self.records.unsavedSelectedCount, 0)
        self.assertTrue(self.records.unsavedMore)
        self.records.loadMoreUnsaved()
        self.settled(TOTAL - 2)
        self.assertEqual(len({row["author"] for row in self.records.unsavedRows}), TOTAL - 2)

    def test_another_list_starts_at_its_top_and_one_page_can_still_be_shown_alone(self):
        self.records.loadMoreUnsaved()
        self.settled(2 * PAGE)
        self.records.toggleUnsavedSelected(self.tokens()[0])
        self.records.setUnsavedView("missing")
        self.wait(lambda: self.records.unsavedView == "missing" and not self.records.unsavedBusy)
        self.assertEqual((len(self.records.unsavedRows), self.records.unsavedSelectedCount,
                          self.records.unsavedModel.rowCount()), (0, 0, 0))
        self.records.setUnsavedView("excluded")
        self.settled(PAGE)
        self.assertEqual(self.records.unsavedPage, 0)
        # The older way of showing page two alone still works.
        self.records.loadUnsaved(2)
        self.wait(lambda: not self.records.unsavedBusy and self.records.unsavedPage == 2)
        self.assertEqual(len(self.records.unsavedRows), TOTAL - 2 * PAGE)
        self.assertFalse(self.records.unsavedMore)

    def test_a_changed_size_limit_reads_everything_loaded_again(self):
        # The limit decides what a retry can store; the list must not shrink to its last part.
        self.records.loadMoreUnsaved()
        self.settled(2 * PAGE)
        authors = [row["author"] for row in self.records.unsavedRows]
        self.records.setVideoLimit(50)
        self.wait(lambda: not self.records.unsavedBusy)
        self.assertEqual([row["author"] for row in self.records.unsavedRows], authors)
        self.assertEqual((self.records.unsavedModel.rowCount(), self.records.unsavedPage, self.records.unsavedMore),
                         (2 * PAGE, 1, True))

    def test_changing_the_list_empties_the_tiles_at_once(self):
        # No tile of the list that was left stays on screen while the next one is read.
        self.records.loadMoreUnsaved()
        self.settled(2 * PAGE)
        resets = []
        self.records.unsavedModel.modelReset.connect(lambda: resets.append(self.records.unsavedModel.rowCount()))
        self.records.setUnsavedView("missing")
        self.assertEqual((resets, self.records.unsavedRows, len(self.records._previews)), ([0], [], 0))
        self.wait(lambda: not self.records.unsavedBusy)
        # Opening the page again starts from an empty list too, then shows the first page.
        self.records.setUnsavedView("excluded")
        self.settled(PAGE)
        self.records.loadMoreUnsaved()
        self.settled(2 * PAGE)
        del resets[:]
        self.records.setActive("unsaved", False)
        self.assertEqual(resets, [])                       # leaving the page changes nothing
        self.records.setActive("unsaved", True)
        self.assertEqual(resets[0], 0)
        self.settled(PAGE)
        self.assertEqual(self.records.unsavedPage, 0)

    def test_a_tile_asks_for_its_picture_and_the_rows_carry_none(self):
        self.records.loadMoreUnsaved()
        self.settled(2 * PAGE)
        rows = self.records.unsavedRows
        kept = [row for row in rows if row["preview"]]
        # The list is newest first, so which rows have one is read from the cache itself.
        with closing(thumbcache.Cache(ACCOUNT, create=False)) as cache:
            stored = cache.stored([(row["tweet_id"], row["num"], row["media_type"])
                                   for row in self.records._unsaved_tokens.values()])
        self.assertEqual(len(kept), len(stored))
        self.assertTrue(0 < len(kept) < len(rows))
        for row in rows:
            self.assertEqual(row["preview"], ("image://unsavedpreview/" + row["token"]) if row in kept else "")
        self.assertLess(sum(len(row["preview"]) for row in rows), 100 * len(rows))
        provider = self.records.preview_provider()
        size = QSize()
        image = provider.requestImage(kept[0]["token"], size, QSize(384, 384))
        self.assertFalse(image.isNull())
        self.assertEqual((size.width(), size.height()), (image.width(), image.height()))
        self.assertLessEqual((image.width(), image.height()), thumbcache.SIZE)
        self.assertAlmostEqual(image.pixelColor(2, 2).red(), 200, delta=6)     # kept as a small lossy picture
        # A query after the name is ignored; a tile without a picture, an unknown name and
        # anything that is not a tile of this list get nothing. No path is ever taken from a name.
        self.assertFalse(provider.requestImage(kept[0]["token"] + "?again=1", QSize(), QSize()).isNull())
        blank = next(row for row in rows if not row["preview"])
        for name in (blank["token"], "nothing", "", "../" + kept[0]["token"], str(self.root / "x.png")):
            self.assertTrue(provider.requestImage(name, QSize(), QSize()).isNull(), name)
        # More rows keep the earlier tiles' pictures; another list forgets them all.
        self.records.loadMoreUnsaved()
        self.settled(TOTAL)
        self.assertFalse(provider.requestImage(kept[0]["token"], QSize(), QSize()).isNull())
        self.assertEqual(len(self.records._previews), sum(1 for row in self.records.unsavedRows if row["preview"]))
        self.records.setUnsavedView("missing")
        self.assertTrue(provider.requestImage(kept[0]["token"], QSize(), QSize()).isNull())

    def test_a_picture_that_cannot_be_read_is_blank_not_an_error(self):
        shelf = PreviewShelf()
        provider = UnsavedPreviewProvider(shelf)
        shelf.replace(ACCOUNT, {"gone": ("999999", 1, "photo"), "odd": ("1000", 1, "photo")})
        self.assertTrue(provider.requestImage("gone", QSize(), QSize()).isNull())     # pruned since it was listed
        with patch("luvbox.thumbcache.recall", side_effect=RuntimeError("unreadable")):
            self.assertTrue(provider.requestImage("odd", None, QSize()).isNull())
        shelf.replace("another", {})
        self.assertIsNone(shelf.lookup("odd"))


if __name__ == "__main__":
    unittest.main()
