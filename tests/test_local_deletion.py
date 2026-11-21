"""Recycling in the "폴더 정리" tab: the gallery's checks, with no X records involved.

The Recycle Bin itself is replaced by a stand-in that moves the file aside, so
no test file reaches the real Recycle Bin; every check before it is the product's.
"""

import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox.local_store import LocalFolderStore
from luvbox.local_deletion import run_local_deletion
from luvbox.recycling import RecyclingError, file_signature


def picture(path, color=(200, 30, 50), size=(64, 48), quality=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, color)
    for x in range(0, size[0], 4):
        image.putpixel((x, size[1] // 2), (x * 3 % 256, 40, 90))
    if quality is not None:
        image.save(path, quality=quality)
    else:
        image.save(path)
    return path


class DeletionCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="luvbox-local-delete-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.base / "appdata")})
        environment.start()
        self.addCleanup(environment.stop)
        self.folder = self.base / "folder"
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.moved = []

        def recycle(path, expected=None):
            if expected is not None and file_signature(path) != tuple(expected):
                raise RecyclingError("changed")
            target = self.bin / Path(path).name
            shutil.move(str(path), target)
            self.moved.append(Path(path).name)
            return str(target)

        stand_in = patch("luvbox.local_deletion.recycle_file", side_effect=recycle)
        stand_in.start()
        self.addCleanup(stand_in.stop)

    def row(self, name, **extra):
        path = self.folder / name
        return {"path": str(path), "filename": path.name, "rel_path": name, "filesize": path.stat().st_size,
                "_delete_signature": file_signature(path), **extra}

    def run_rows(self, rows):
        events = []
        result = run_local_deletion(self.folder, rows, threading.Event(), events.append)
        return result, events

    def listed(self):
        with LocalFolderStore(self.folder) as store:
            return [row["rel_path"] for row in store.records(0, 100, order="name")[1]]


class PlainTests(DeletionCase):
    def setUp(self):
        super().setUp()
        picture(self.folder / "a.jpg")
        picture(self.folder / "b.jpg", (10, 20, 30))
        with LocalFolderStore(self.folder) as store:
            store.sync_folder(True)
            store.add_tag(["a.jpg"], "고양이")

    def test_a_chosen_file_goes_to_the_bin_and_its_row_is_kept_as_gone(self):
        result, events = self.run_rows([self.row("a.jpg")])
        self.assertEqual((result["recycled"], result["failed"], result["protected"]), (1, 0, 0))
        self.assertEqual(self.moved, ["a.jpg"])
        self.assertEqual(self.listed(), ["b.jpg"])
        self.assertEqual(events[-1]["type"], "done")
        # Restored from the bin: it comes back with its tag.
        shutil.move(str(self.bin / "a.jpg"), self.folder / "a.jpg")
        with LocalFolderStore(self.folder) as store:
            store.sync_folder(True)
            self.assertEqual(store.user_tags(), [{"tag": "고양이", "count": 1}])

    def test_a_file_that_changed_after_it_was_chosen_stays(self):
        row = self.row("a.jpg")
        picture(self.folder / "a.jpg", (1, 2, 3), size=(80, 80))
        result, _events = self.run_rows([row])
        self.assertEqual((result["recycled"], result["failed"]), (0, 1))
        self.assertEqual(self.moved, [])

    def test_a_file_outside_the_folder_or_a_path_without_signature_is_refused(self):
        outside = picture(self.base / "outside.jpg")
        rows = [{"path": str(outside), "filename": "outside.jpg", "rel_path": "../outside.jpg",
                 "_delete_signature": file_signature(outside)},
                dict(self.row("b.jpg"), _delete_signature=None)]
        result, _events = self.run_rows(rows)
        self.assertEqual((result["recycled"], result["failed"]), (0, 2))
        self.assertTrue(outside.exists())

    @unittest.skipUnless(os.name == "nt", "Windows junctions")
    def test_a_file_reached_through_a_junction_is_refused(self):
        import _winapi

        elsewhere = self.base / "elsewhere"
        picture(elsewhere / "c.jpg")
        _winapi.CreateJunction(str(elsewhere), str(self.folder / "joined"))
        path = self.folder / "joined" / "c.jpg"
        row = {"path": str(path), "filename": "c.jpg", "rel_path": "joined/c.jpg",
               "_delete_signature": (0, 0, 0, 0)}
        result, _events = self.run_rows([row])
        self.assertEqual((result["recycled"], result["failed"]), (0, 1))
        self.assertTrue((elsewhere / "c.jpg").exists())

    def test_a_file_only_in_the_cloud_is_never_moved(self):
        """Moving it would first download it; the list says so and leaves it."""
        with patch("luvbox.local_deletion.cloud_only", side_effect=lambda path: Path(path).name == "a.jpg"):
            result, _events = self.run_rows([self.row("a.jpg"), self.row("b.jpg")])
        self.assertEqual((result["recycled"], result["failed"]), (1, 1))
        self.assertEqual(self.moved, ["b.jpg"])

    def test_a_stop_before_the_turn_of_a_file_leaves_it(self):
        stop = threading.Event()
        stop.set()
        result = run_local_deletion(self.folder, [self.row("a.jpg")], stop, lambda _event: None)
        self.assertEqual((result["recycled"], result["cancelled"]), (0, 1))


class CopyTests(DeletionCase):
    def setUp(self):
        super().setUp()
        original = picture(self.folder / "keep.jpg")
        shutil.copyfile(original, self.folder / "copy1.jpg")
        shutil.copyfile(original, self.folder / "copy2.jpg")
        picture(self.folder / "other.jpg", (90, 90, 90))
        with LocalFolderStore(self.folder) as store:
            store.sync_folder(True)

    def copy(self, name, keepers):
        return self.row(name, _copy_keepers=[str(self.folder / keeper) for keeper in keepers], _copy_rule="exact")

    def test_a_copy_goes_only_while_another_file_with_its_bytes_stays(self):
        result, _events = self.run_rows([self.copy("copy1.jpg", ["keep.jpg"]), self.copy("copy2.jpg", ["keep.jpg"])])
        self.assertEqual(result["recycled"], 2)
        self.assertTrue((self.folder / "keep.jpg").exists())

    def test_the_last_file_of_a_group_never_goes(self):
        # Each names only the other as what stays: when its turn comes the other is already gone.
        rows = [self.copy("copy1.jpg", ["copy2.jpg"]), self.copy("copy2.jpg", ["copy1.jpg"])]
        result, _events = self.run_rows(rows)
        self.assertEqual((result["recycled"], result["protected"]), (1, 1))
        self.assertEqual(len(list(self.folder.glob("copy*.jpg"))), 1)

    def test_a_different_picture_or_a_hard_link_is_no_copy_to_leave(self):
        os.link(self.folder / "copy2.jpg", self.folder / "linked.jpg")
        rows = [self.copy("copy1.jpg", ["other.jpg"]), self.copy("copy2.jpg", ["linked.jpg"])]
        result, _events = self.run_rows(rows)
        self.assertEqual((result["recycled"], result["protected"]), (0, 2))


class AlikeTests(DeletionCase):
    def setUp(self):
        super().setUp()
        picture(self.folder / "big.png", size=(256, 192))
        picture(self.folder / "small.jpg", size=(256, 192), quality=70)
        picture(self.folder / "different.png", (5, 200, 5), size=(256, 192))

    def alike(self, name, keeper):
        return self.row(name, _alike_keeper=str(self.folder / keeper),
                        _alike_keeper_signature=file_signature(self.folder / keeper))

    def test_one_compared_photo_goes_beside_the_photo_it_was_compared_with(self):
        result, _events = self.run_rows([self.alike("small.jpg", "big.png")])
        self.assertEqual(result["recycled"], 1)

    def test_never_with_another_file_and_never_beside_a_different_picture(self):
        result, _events = self.run_rows([self.alike("small.jpg", "big.png"), self.row("different.png")])
        self.assertEqual(result["protected"], 1)
        self.assertNotIn("small.jpg", self.moved)
        shutil.move(str(self.bin / "different.png"), self.folder / "different.png")    # chosen plainly, it went
        result, _events = self.run_rows([self.alike("small.jpg", "different.png")])
        self.assertEqual((result["recycled"], result["protected"]), (0, 1))

    def test_the_same_compared_photo_named_twice_is_still_one_photo(self):
        row = self.alike("small.jpg", "big.png")
        result, _events = self.run_rows([row, dict(row)])
        self.assertEqual((result["total"], result["recycled"]), (1, 1))

    def test_a_keeper_that_changed_since_the_comparison_keeps_the_photo(self):
        row = self.alike("small.jpg", "big.png")
        picture(self.folder / "big.png", (0, 0, 0), size=(256, 192))
        result, _events = self.run_rows([row])
        self.assertEqual((result["recycled"], result["protected"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
