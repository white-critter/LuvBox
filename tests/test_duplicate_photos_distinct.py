"""Single photos taken out of a duplicate group, and the card list that changes in place.

"중복 아님" on a whole group is in test_duplicates_distinct. Here some photos of a
group are other pictures: only they leave it, the rest stays a group, and the choice
holds through later scans. The marks are kept under their own key beside the group
marks. The second half is the model the cards are drawn from: after a change it
removes, adds and redraws rows instead of starting the list again.
"""

from importlib.util import find_spec
import json
import unittest

from tests import app_data_isolation  # noqa: F401  Never open the user's app data.
from tests.test_duplicates_distinct import DistinctCatalog

_QT = find_spec("PySide6") is not None
if _QT:
    from PySide6.QtCore import QCoreApplication
    from luvbox.qt_library import DuplicateGroupModel


def photo(tweet):
    return ("flat", tweet, 1, "photo")


class PhotoMarkTests(DistinctCatalog):
    PAIR = DistinctCatalog.PAIR
    TRIO = DistinctCatalog.TRIO

    def numbers(self, store):
        return dict(store._db.execute("SELECT tweet_id, cluster_id FROM image_hashes"))

    def test_one_photo_leaves_its_group_and_the_rest_stays(self):
        with self.store() as store:
            mark = store.set_duplicate_photos_distinct([photo("203")])
            self.assertEqual(mark, {"photos": [["flat", "203", 1, "photo"]],
                                    "group": [["flat", tweet, 1, "photo"] for tweet in self.TRIO]})
            self.assertEqual(self.groups(store), [list(self.PAIR), ["201", "202"]])
            self.assertEqual(store.duplicate_counts(), 4)
            self.assertEqual(store.record_page(0, 100, include_deleted=False, duplicate_only=True)["total"], 4)
            self.assertEqual(store.distinct_duplicate_count(), 1)
            # It is not offered as a copy to remove, and it is still in the gallery.
            self.assertNotIn("203", {identity[1] for identity in store.duplicate_copy_identities()})
            self.assertEqual(store.record_page(0, 100, include_deleted=False)["total"], 6)
            # The same choice again changes nothing more.
            self.assertIsNone(store.set_duplicate_photos_distinct([photo("203")]))
            self.assertEqual(store.distinct_duplicate_count(), 1)

    def test_a_pair_left_with_one_photo_is_no_group(self):
        with self.store() as store:
            self.assertIsNotNone(store.set_duplicate_photos_distinct([photo(self.PAIR[1])]))
            self.assertEqual(self.groups(store), [list(self.TRIO)])
            self.assertEqual(store.duplicate_counts(), 3)

    def test_photos_taken_out_together_are_one_mark_and_each_stands_alone(self):
        with self.store() as store:
            self.add(store, "204", b"trio")
        self.assertEqual(self.scan(), 6)
        with self.store() as store:
            mark = store.set_duplicate_photos_distinct([photo("204"), photo("203")])
            self.assertEqual([item[1] for item in mark["photos"]], ["203", "204"])
            # The two that stay are a group; the two taken out are not a group of their own.
            self.assertEqual(self.groups(store), [list(self.PAIR), ["201", "202"]])
            self.assertEqual(store.distinct_duplicate_count(), 1)
        self.assertEqual(self.scan(), 4)
        with self.store() as store:
            self.assertEqual(self.groups(store), [list(self.PAIR), ["201", "202"]])

    def test_what_is_not_some_photos_of_one_group_changes_nothing(self):
        with self.store() as store:
            before = self.numbers(store)
            for chosen in ([photo(tweet) for tweet in self.TRIO],              # the whole group: that is the group's mark
                           [photo("101"), photo("201")],                        # of two groups
                           [photo("301")], [photo("999")],                      # in no group, unknown
                           [photo("201"), photo("999")], [], None, "nonsense", [("flat", "201")]):
                self.assertIsNone(store.set_duplicate_photos_distinct(chosen), chosen)
            self.assertEqual(self.numbers(store), before)
            self.assertEqual(store.distinct_duplicate_count(), 0)
            self.assertIsNone(store._db.execute(
                "SELECT value FROM catalog_meta WHERE key='duplicate_distinct_photos'").fetchone())

    def test_the_mark_survives_the_next_scan(self):
        with self.store() as store:
            store.set_duplicate_photos_distinct([photo("202")])
        self.assertEqual(self.scan(), 4)
        with self.store() as store:
            self.assertEqual(self.groups(store), [list(self.PAIR), ["201", "203"]])
            self.assertEqual(store.distinct_duplicate_count(), 1)

    def test_a_new_copy_brings_the_photo_back_with_its_group(self):
        with self.store() as store:
            store.set_duplicate_photos_distinct([photo("203")])
            self.add(store, "204", b"trio")
        self.assertEqual(self.scan(), 6)
        with self.store() as store:
            self.assertEqual(self.groups(store), [list(self.PAIR), [*self.TRIO, "204"]])

    def test_taking_it_back_returns_the_photo_without_a_scan(self):
        with self.store() as store:
            before = store.duplicate_page(0, 100)["rows"]
            mark = store.set_duplicate_photos_distinct([photo("203")])
            self.assertTrue(store.undo_duplicate_photos_distinct(mark))
            self.assertEqual(self.groups(store), [list(self.PAIR), list(self.TRIO)])
            self.assertEqual(store.distinct_duplicate_count(), 0)
            after = store.duplicate_page(0, 100)["rows"]
            self.assertEqual([(row["tweet_id"], row["cluster_id"], row["cluster_rank"]) for row in after],
                             [(row["tweet_id"], row["cluster_id"], row["cluster_rank"]) for row in before])
            # Twice, or something that was never marked, does nothing.
            self.assertFalse(store.undo_duplicate_photos_distinct(mark))
            for nothing in ({"photos": [list(photo("201"))], "group": [list(photo("201")), list(photo("999"))]},
                            {}, None, "nonsense", {"photos": "x", "group": 1}):
                self.assertFalse(store.undo_duplicate_photos_distinct(nothing), nothing)

    def test_taking_it_back_after_a_scan_renumbered_the_groups(self):
        with self.store() as store:
            store.set_duplicate_group_distinct("flat", self.PAIR[0], 1, "photo")      # the trio becomes group 1
            mark = store.set_duplicate_photos_distinct([photo("201")])
        self.scan()
        with self.store() as store:
            self.assertEqual(self.groups(store), [["202", "203"]])
            self.assertTrue(store.undo_duplicate_photos_distinct(mark))
            self.assertEqual(self.groups(store), [list(self.TRIO)])

    def test_marking_the_rest_as_a_group_does_not_bring_the_photo_back(self):
        # 203 was taken out; then the two that stayed are called "not duplicates" as a group.
        with self.store() as store:
            store.set_duplicate_photos_distinct([photo("203")])
            members = store.set_duplicate_group_distinct("flat", "201", 1, "photo")
            self.assertEqual(sorted(member[1] for member in members), ["201", "202"])
            self.assertEqual(self.groups(store), [list(self.PAIR)])
            self.assertEqual(store.distinct_duplicate_count(), 2)
            # Taking the group mark back returns the two, not the photo taken out before.
            self.assertTrue(store.undo_duplicates_distinct(members))
            self.assertEqual(self.groups(store), [list(self.PAIR), ["201", "202"]])
        self.assertEqual(self.scan(), 4)

    def test_clearing_returns_photos_and_groups(self):
        with self.store() as store:
            store.set_duplicate_photos_distinct([photo("203")])
            store.set_duplicate_group_distinct("flat", self.PAIR[0], 1, "photo")
            self.assertEqual(self.groups(store), [["201", "202"]])
            self.assertEqual(store.distinct_duplicate_count(), 2)
            self.assertEqual(store.clear_duplicates_distinct(), 2)
            self.assertEqual(self.groups(store), [list(self.PAIR), list(self.TRIO)])
            self.assertEqual((store.duplicate_counts(), store.distinct_duplicate_count()), (5, 0))
            self.assertEqual(store.clear_duplicates_distinct(), 0)
        self.assertEqual(self.scan(), 5)

    def test_kept_beside_the_group_marks_with_the_tables_as_they_are(self):
        with self.store() as store:
            tables = {row[0] for row in store._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            store.set_duplicate_photos_distinct([photo("203")])
            self.assertEqual({row[0] for row in store._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}, tables)
            stored = json.loads(store._db.execute(
                "SELECT value FROM catalog_meta WHERE key='duplicate_distinct_photos'").fetchone()[0])
            self.assertEqual(stored, [{"photos": [["flat", "203", 1, "photo"]],
                                       "group": [["flat", tweet, 1, "photo"] for tweet in self.TRIO]}])
            # The entry of the group marks, which 1.9.5 to 1.9.8 read and rewrite, is not touched.
            self.assertIsNone(store._db.execute(
                "SELECT value FROM catalog_meta WHERE key='duplicate_distinct'").fetchone())
            numbers = self.numbers(store)
            self.assertEqual(numbers["203"], -numbers["201"])
            self.assertEqual(numbers["201"], numbers["202"])
            self.assertGreater(numbers["201"], 0)

    def test_a_damaged_entry_reads_as_no_mark(self):
        with self.store() as store:
            for value in ("not json", "[]", "[[1, 2]]", '[{"photos": "x", "group": []}]',
                          '[{"photos": [["flat", "203", 1, "photo"]], "group": [["flat", "203", 1, "photo"]]}]',
                          '[{"photos": [["flat", "999", 1, "photo"]], "group": [["flat", "201", 1, "photo"]]}]'):
                with store._db:
                    store._db.execute(
                        "INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('duplicate_distinct_photos',?)", (value,))
                self.assertEqual(store.distinct_duplicate_count(), 0, value)
        self.assertEqual(self.scan(), 5)
        with self.store() as store:
            store.clear_duplicates_distinct()
            self.assertIsNone(store._db.execute(
                "SELECT value FROM catalog_meta WHERE key='duplicate_distinct_photos'").fetchone())


class PhotoMarkRepositoryTests(DistinctCatalog):
    def test_the_duplicate_view_loses_the_photo_and_gets_it_back(self):
        self.assertEqual(self.shown(), (5, sorted(self.PAIR + self.TRIO)))
        mark = self.repository.mark_photos_distinct([self.key("202")], 1)
        self.assertEqual([item[1] for item in mark["photos"]], ["202"])
        self.assertEqual(self.shown(), (4, sorted(self.PAIR + ("201", "203"))))
        self.assertEqual(self.repository.duplicate_counts(1), 4)
        self.assertEqual(self.repository.distinct_counts(1), 1)
        self.assertNotIn("202", {key.split("_")[2] for key in self.repository.duplicate_copy_keys(1)})
        self.assertTrue(self.repository.undo_photos_distinct(mark, 1))
        self.assertEqual(self.shown(), (5, sorted(self.PAIR + self.TRIO)))
        self.assertEqual(self.repository.distinct_counts(1), 0)
        # Clearing takes photo marks with it.
        self.repository.mark_photos_distinct([self.key("202")], 1)
        self.assertEqual(self.repository.clear_distinct(1), 1)
        self.assertEqual(self.shown(), (5, sorted(self.PAIR + self.TRIO)))

    def test_a_stale_or_foreign_key_changes_nothing(self):
        for keys in (["g2_flat_201_1_photo"], [self.key("201"), "nonsense"], [self.key("201"), None], [], None,
                     [self.key("101"), self.key("201")], [self.key(tweet) for tweet in self.TRIO]):
            self.assertIsNone(self.repository.mark_photos_distinct(keys, 1), keys)
        self.assertIsNone(self.repository.mark_photos_distinct([self.key("201")], 2))
        self.assertFalse(self.repository.undo_photos_distinct(None, 1))
        self.assertEqual(self.shown(), (5, sorted(self.PAIR + self.TRIO)))


def group(number, *keys, size=None, position=0):
    return {"group": number, "number": position, "size": size or len(keys),
            "items": [{"key": key, "caption": key, "keeper": index == 0} for index, key in enumerate(keys)]}


def numbered(groups):
    return [dict(item, number=index + 1) for index, item in enumerate(groups)]


@unittest.skipUnless(_QT, "Qt dependencies are not installed")
class GroupModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.model = DuplicateGroupModel()
        self.events = []
        self.model.modelReset.connect(lambda: self.events.append("reset"))
        self.model.rowsRemoved.connect(lambda _parent, first, last: self.events.append(("removed", first, last)))
        self.model.rowsInserted.connect(lambda _parent, first, last: self.events.append(("inserted", first, last)))
        self.model.dataChanged.connect(lambda first, _last, roles: self.events.append(
            ("changed", first.row(), sorted(self.names[role] for role in roles))))
        self.names = {role: bytes(name).decode() for role, name in self.model.roleNames().items()}
        self.start = numbered([group(11, "a1", "a2"), group(12, "b1", "b2", "b3"), group(13, "c1", "c2"),
                               group(14, "d1", "d2")])
        self.model.adopt(self.start)
        del self.events[:]

    def shown(self):
        roles = {name: role for role, name in self.names.items()}
        return [{name: self.model.data(self.model.index(row, 0), roles[name]) for name in DuplicateGroupModel.FIELDS}
                for row in range(self.model.rowCount())]

    def test_a_card_reads_its_group(self):
        self.assertEqual(set(self.names.values()), {"group", "number", "size", "items"})
        self.assertEqual(self.shown(), self.start)
        self.assertIsNone(self.model.data(self.model.index(9, 0), next(iter(self.names))))

    def test_a_group_put_away_is_removed_and_the_rest_only_renumbered(self):
        after = numbered([self.start[0], self.start[2], self.start[3]])
        self.model.adopt(after)
        self.assertEqual(self.events, [("removed", 1, 1), ("changed", 1, ["number"]), ("changed", 2, ["number"])])
        self.assertEqual(self.shown(), after)

    def test_a_group_that_lost_a_photo_is_redrawn_in_place(self):
        after = numbered([self.start[0], group(12, "b1", "b2"), self.start[2], self.start[3]])
        self.model.adopt(after)
        self.assertEqual(self.events, [("changed", 1, ["items", "size"])])
        self.assertEqual(self.shown(), after)

    def test_more_groups_read_are_added_after_the_ones_shown(self):
        after = numbered(self.start + [group(15, "e1", "e2"), group(16, "f1", "f2")])
        self.model.adopt(after)
        self.assertEqual(self.events, [("inserted", 4, 5)])
        self.assertEqual(self.shown(), after)

    def test_a_group_that_returns_goes_back_to_its_place(self):
        self.model.adopt(numbered([self.start[0], self.start[3]]))
        del self.events[:]
        self.model.adopt(self.start)
        self.assertEqual(self.events, [("inserted", 1, 2), ("changed", 3, ["number"])])
        self.assertEqual(self.shown(), self.start)

    def test_the_same_list_read_again_with_new_keys_redraws_cards_without_starting_over(self):
        # After a reload every key is new; the groups are the same ones.
        after = numbered([group(11, "A1", "A2"), group(13, "C1", "C2"), group(14, "D1", "D2")])
        self.model.adopt(after)
        self.assertNotIn("reset", self.events)
        self.assertEqual(self.events[0], ("removed", 1, 1))
        self.assertEqual(self.shown(), after)

    def test_another_list_starts_over(self):
        for other in (numbered([group(21, "x1", "x2")]),                                # nothing in common
                      numbered([self.start[1], self.start[0]]),                         # another order
                      []):
            self.model.adopt(self.start)
            del self.events[:]
            self.model.adopt(other)
            self.assertEqual(self.events, ["reset"], other)
            self.assertEqual(self.shown(), other)
        self.model.adopt([])
        del self.events[:]
        self.model.adopt([])                                                            # nothing to nothing
        self.assertEqual(self.events, [])

    def test_the_same_groups_again_tell_nothing(self):
        self.model.adopt(numbered(self.start))
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
