"""Picking a '비슷함' photo and pressing the Recycle Bin opens the comparison it needs.

A '비슷함' photo (the same picture at another size or quality) goes only alone,
after a person looked at it beside the photo its group keeps. Before this the
comparison opened only from the preview: picked with its check box and sent
with "휴지통으로" or Delete, the photo went the copies' way, where it always
stays, and nothing moved. One such photo picked alone now opens the
comparison; several picked alone are told to go one at a time, before any
confirmation; mixed with copies, the copies go as before and it stays.

The library is a stand-in that says which keys are '비슷함' and records which
check was asked for; the routing is ManagementController's.
"""

import time
import unittest

from PySide6.QtCore import QCoreApplication

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox.qt_management import ManagementController


class _Library:
    filesLocked = False
    holdReason = ""

    def __init__(self, alike):
        self.alike = set(alike)
        self.asked = []

    def alikeKeys(self, keys):
        return [key for key in keys if key in self.alike]

    def deletionManifest(self, keys):
        self.asked.append(("copies", list(keys)))
        return {"location": None, "rows": [{"path": "C:/x/" + key, "_key": key} for key in keys], "kept": 0}

    def alikeManifest(self, key):
        self.asked.append(("compare", key))
        return {"location": None, "rows": [{"path": "C:/x/" + key, "_key": key}], "kept": 0,
                "alike": {"alike": {"name": key}, "keeper": {"name": "kept"}}}


class AlikeChoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def start(self, alike):
        self.library = _Library(alike)
        self.management = ManagementController(self.library)
        self.requested = []
        self.management.deleteConfirmationRequested.connect(lambda: self.requested.append(True))

        def close():
            self.management.shutdown()
            self.wait(lambda: not self.management._worker.is_alive())
        self.addCleanup(close)

    def wait(self, predicate, timeout=5):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("management did not settle")

    def settle(self):
        self.wait(lambda: not self.management.mutating or self.management.deleteConfirmationPending)

    def test_one_similar_photo_picked_alone_opens_the_comparison(self):
        self.start(alike={"g1_flat_502_1_photo"})
        self.management.prepareDelete(["g1_flat_502_1_photo"])
        self.settle()
        self.assertEqual(self.library.asked, [("compare", "g1_flat_502_1_photo")])
        self.assertEqual(self.requested, [True])
        self.assertEqual(self.management.deleteAlike["alike"]["name"], "g1_flat_502_1_photo")

    def test_several_similar_photos_picked_alone_are_told_to_go_one_at_a_time(self):
        self.start(alike={"g1_flat_502_1_photo", "g1_flat_602_1_photo"})
        self.management.prepareDelete(["g1_flat_502_1_photo", "g1_flat_602_1_photo"])
        self.app.processEvents()
        self.assertEqual(self.library.asked, [])
        self.assertEqual(self.requested, [])
        self.assertFalse(self.management.mutating)
        self.assertIn("한 장씩", self.management.error)
        self.assertIn("비교", self.management.error)

    def test_mixed_with_copies_the_copies_go_the_copies_way(self):
        self.start(alike={"g1_flat_502_1_photo"})
        self.management.prepareDelete(["g1_flat_502_1_photo", "g1_flat_601_1_photo"])
        self.settle()
        self.assertEqual(self.library.asked, [("copies", ["g1_flat_502_1_photo", "g1_flat_601_1_photo"])])

    def test_anything_else_goes_the_usual_way(self):
        self.start(alike=set())
        self.management.prepareDelete(["g1_flat_601_1_photo"])
        self.settle()
        self.assertEqual(self.library.asked, [("copies", ["g1_flat_601_1_photo"])])


if __name__ == "__main__":
    unittest.main()
