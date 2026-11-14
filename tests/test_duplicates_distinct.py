"""Groups the owner marks "not duplicates" stay out of the duplicate view, also after the next scan.

The marks are kept in the catalog's existing key/value table, and the files keep
their group number negated, so nothing lists them and taking a mark back returns
them to their group without another scan.
"""

from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # noqa: F401  Never open the user's app data.
from luvbox.duplicates import split_distinct
from luvbox.history import HistoryStore
from luvbox.qt_library import LibraryRepository, _Request
from luvbox.storage import resolve_storage

ACCOUNT = "offline"


class SplitDistinctTests(unittest.TestCase):
    def test_a_whole_group_marked_distinct_leaves_no_group(self):
        self.assertEqual(split_distinct(["a", "b", "c"], [frozenset("abc")]), [])

    def test_unmarked_members_stay_together(self):
        self.assertEqual(split_distinct(["a", "b", "c"], []), [["a", "b", "c"]])
        self.assertEqual(split_distinct(["a", "b", "c"], [frozenset("xy")]), [["a", "b", "c"]])

    def test_one_member_set_apart_leaves_the_rest(self):
        # c is not a duplicate of a or of b; a and b still are.
        self.assertEqual(split_distinct(["a", "b", "c"], [frozenset("ac"), frozenset("bc")]), [["a", "b"]])

    def test_a_new_member_joins_marked_members_again(self):
        # a and b were marked; d is the same as both, so all three are shown.
        self.assertEqual(split_distinct(["a", "b", "d"], [frozenset("ab")]), [["a", "b", "d"]])

    def test_a_group_can_fall_into_two(self):
        marks = [frozenset(pair) for pair in ("ac", "ad", "bc", "bd")]
        self.assertEqual(split_distinct(["a", "b", "c", "d"], marks), [["a", "b"], ["c", "d"]])

    def test_fewer_than_two_members_is_no_group(self):
        self.assertEqual(split_distinct(["a"], []), [])
        self.assertEqual(split_distinct([], []), [])


class DistinctCatalog(unittest.TestCase):
    """Two identical files (pair), three identical files (trio) and one file of its own."""

    PAIR = ("101", "102")
    TRIO = ("201", "202", "203")

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="luvbox-distinct-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "appdata")})
        environment.start()
        self.addCleanup(environment.stop)
        self.paths = resolve_storage(self.root / "Likes", ACCOUNT)
        with closing(HistoryStore.for_storage(self.paths)) as store:
            for tweet, content in ((self.PAIR[0], b"pair"), (self.PAIR[1], b"pair"), (self.TRIO[0], b"trio"),
                                   (self.TRIO[1], b"trio"), (self.TRIO[2], b"trio"), ("301", b"alone")):
                self.add(store, tweet, content)
        self.repository = LibraryRepository()
        self.addCleanup(self.repository.configure, None)
        self.repository.provider = self
        self.configure()
        self.scan()

    def register(self, key, path):
        pass

    def add(self, store, tweet, content):
        path = self.paths.output_dir / f"writer_{tweet}_1.jpg"
        path.write_bytes(content * 40)
        store.record_media("flat", {"tweet_id": tweet, "num": 1, "type": "photo", "author": {"name": "writer"},
                                    "date": "2026-09-01T12:00:00+09:00"}, path)

    def configure(self, **filters):
        self.repository.configure(_Request(generation=1, account=ACCOUNT, output_dir=str(self.paths.output_dir),
                                           separate_accounts=False, **filters))

    def scan(self):
        # Hash with the real routine; only the picture decoding is replaced (the files are not pictures).
        with patch("luvbox.duplicates.compute_image_hashes",
                   side_effect=lambda path: (__import__("hashlib").sha256(Path(path).read_bytes()).hexdigest(), "")):
            return self.repository.scan_duplicates(1)

    def store(self):
        return closing(HistoryStore.for_storage(self.paths, create_media=False))

    def groups(self, store):
        return sorted(sorted(member["identity"][1] for member in members)
                      for members in store.duplicate_members().values())

    def key(self, tweet):
        return f"g1_flat_{tweet}_1_photo"

    def shown(self):
        """Tweets in the duplicate view, as the gallery reads it."""
        self.configure(duplicate_only=True)
        total, rows = self.repository.read("1", 0, 100)
        self.configure()
        return total, sorted(row["key"].split("_")[2] for row in rows)


class StoreTests(DistinctCatalog):
    def test_the_scan_finds_both_groups(self):
        with self.store() as store:
            self.assertEqual(self.groups(store), [list(self.PAIR), list(self.TRIO)])
            self.assertEqual(store.duplicate_counts(), 5)
            self.assertEqual(store.distinct_duplicate_count(), 0)

    def test_a_marked_group_leaves_every_duplicate_listing(self):
        with self.store() as store:
            members = store.set_duplicate_group_distinct("flat", self.PAIR[0], 1, "photo")
            self.assertEqual(members, [["flat", tweet, 1, "photo"] for tweet in self.PAIR])
            self.assertEqual(self.groups(store), [list(self.TRIO)])
            self.assertEqual(store.duplicate_counts(), 3)
            self.assertEqual(store.duplicate_page(0, 100)["total"], 3)
            self.assertEqual(store.record_page(0, 100, include_deleted=False, duplicate_only=True)["total"], 3)
            # Nothing of the pair is offered as a copy to remove.
            copies = {identity[1] for identity in store.duplicate_copy_identities()}
            self.assertFalse(copies & set(self.PAIR))
            self.assertEqual(len(copies), 2)
            self.assertEqual(store.distinct_duplicate_count(), 1)
            # The photos themselves are still in the gallery.
            self.assertEqual(store.record_page(0, 100, include_deleted=False)["total"], 6)

    def test_any_member_marks_the_whole_group(self):
        with self.store() as store:
            members = store.set_duplicate_group_distinct("flat", self.TRIO[2], 1, "photo")
            self.assertEqual(sorted(member[1] for member in members), list(self.TRIO))
            self.assertEqual(self.groups(store), [list(self.PAIR)])

    def test_a_file_in_no_group_changes_nothing(self):
        with self.store() as store:
            self.assertIsNone(store.set_duplicate_group_distinct("flat", "301", 1, "photo"))
            self.assertIsNone(store.set_duplicate_group_distinct("flat", "999", 1, "photo"))
            self.assertEqual(store.distinct_duplicate_count(), 0)
            self.assertEqual(self.groups(store), [list(self.PAIR), list(self.TRIO)])

    def test_the_mark_survives_the_next_scan(self):
        with self.store() as store:
            store.set_duplicate_group_distinct("flat", self.PAIR[0], 1, "photo")
        self.assertEqual(self.scan(), 3)
        with self.store() as store:
            self.assertEqual(self.groups(store), [list(self.TRIO)])
            self.assertEqual(store.distinct_duplicate_count(), 1)

    def test_a_new_copy_brings_a_marked_group_back_with_all_its_files(self):
        with self.store() as store:
            store.set_duplicate_group_distinct("flat", self.PAIR[0], 1, "photo")
            self.add(store, "103", b"pair")
        self.assertEqual(self.scan(), 6)
        with self.store() as store:
            self.assertEqual(self.groups(store), [[*self.PAIR, "103"], list(self.TRIO)])

    def test_taking_a_mark_back_returns_the_group_without_a_scan(self):
        with self.store() as store:
            before = store.duplicate_page(0, 100)["rows"]
            members = store.set_duplicate_group_distinct("flat", self.PAIR[1], 1, "photo")
            self.assertTrue(store.undo_duplicates_distinct(members))
            self.assertEqual(self.groups(store), [list(self.PAIR), list(self.TRIO)])
            self.assertEqual(store.distinct_duplicate_count(), 0)
            after = store.duplicate_page(0, 100)["rows"]
            self.assertEqual([(row["tweet_id"], row["cluster_id"], row["cluster_rank"]) for row in after],
                             [(row["tweet_id"], row["cluster_id"], row["cluster_rank"]) for row in before])
            # Taking it back twice, or something that was never marked, does nothing.
            self.assertFalse(store.undo_duplicates_distinct(members))
            self.assertFalse(store.undo_duplicates_distinct([["flat", "301", 1, "photo"], ["flat", "999", 1, "photo"]]))
            self.assertFalse(store.undo_duplicates_distinct("nonsense"))

    def test_taking_a_mark_back_after_a_scan_renumbered_the_groups(self):
        with self.store() as store:
            members = store.set_duplicate_group_distinct("flat", self.TRIO[0], 1, "photo")
        self.scan()
        with self.store() as store:
            self.assertEqual(self.groups(store), [list(self.PAIR)])
            self.assertTrue(store.undo_duplicates_distinct(members))
            self.assertEqual(self.groups(store), [list(self.PAIR), list(self.TRIO)])

    def test_clearing_returns_every_marked_group(self):
        with self.store() as store:
            store.set_duplicate_group_distinct("flat", self.PAIR[0], 1, "photo")
            store.set_duplicate_group_distinct("flat", self.TRIO[0], 1, "photo")
            self.assertEqual(self.groups(store), [])
            self.assertEqual(store.duplicate_counts(), 0)
            self.assertEqual(store.distinct_duplicate_count(), 2)
            self.assertEqual(store.clear_duplicates_distinct(), 2)
            self.assertEqual(self.groups(store), [list(self.PAIR), list(self.TRIO)])
            self.assertEqual(store.duplicate_counts(), 5)
            self.assertEqual(store.distinct_duplicate_count(), 0)
            self.assertEqual(store.clear_duplicates_distinct(), 0)
        # And the next scan groups them as before.
        self.assertEqual(self.scan(), 5)

    def test_the_catalog_keeps_its_tables_and_an_earlier_version_sees_no_group(self):
        with self.store() as store:
            tables = {row[0] for row in store._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            store.set_duplicate_group_distinct("flat", self.PAIR[0], 1, "photo")
            self.assertEqual({row[0] for row in store._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}, tables)
            stored = json.loads(store._db.execute(
                "SELECT value FROM catalog_meta WHERE key='duplicate_distinct'").fetchone()[0])
            self.assertEqual(stored, [[["flat", tweet, 1, "photo"] for tweet in self.PAIR]])
            # Versions up to 1.9.4 list a group by a number above zero; the marked files carry it negated.
            numbers = dict(store._db.execute("SELECT tweet_id, cluster_id FROM image_hashes"))
            self.assertLess(numbers[self.PAIR[0]], 0)
            self.assertEqual(numbers[self.PAIR[0]], numbers[self.PAIR[1]])
            self.assertTrue(all(numbers[tweet] > 0 for tweet in self.TRIO))
            self.assertEqual(numbers["301"], 0)

    def test_a_damaged_entry_reads_as_no_mark(self):
        with self.store() as store:
            for value in ("not json", "{}", "[[1, 2]]", '[[["flat", "101", "1", "photo"], ["flat", "102", 1]]]'):
                with store._db:
                    store._db.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('duplicate_distinct',?)",
                                      (value,))
                self.assertEqual(store.distinct_duplicate_count(), 0, value)
        self.assertEqual(self.scan(), 5)
        with self.store() as store:
            # Clearing removes the unreadable entry as well.
            store.clear_duplicates_distinct()
            self.assertIsNone(store._db.execute(
                "SELECT value FROM catalog_meta WHERE key='duplicate_distinct'").fetchone())


class RepositoryTests(DistinctCatalog):
    def test_the_duplicate_view_drops_the_group_and_gets_it_back(self):
        self.assertEqual(self.shown(), (5, sorted(self.PAIR + self.TRIO)))
        members = self.repository.mark_group_distinct(self.key(self.PAIR[0]), 1)
        self.assertEqual(len(members), 2)
        self.assertEqual(self.shown(), (3, sorted(self.TRIO)))
        self.assertEqual(self.repository.duplicate_counts(1), 3)
        self.assertEqual(self.repository.distinct_counts(1), 1)
        self.assertEqual(sorted(key.split("_")[2] for key in self.repository.duplicate_copy_keys(1)),
                         sorted(self.TRIO)[:2])
        self.assertTrue(self.repository.undo_group_distinct(members, 1))
        self.assertEqual(self.shown(), (5, sorted(self.PAIR + self.TRIO)))
        self.assertEqual(self.repository.distinct_counts(1), 0)

    def test_clearing_through_the_repository(self):
        self.repository.mark_group_distinct(self.key(self.TRIO[1]), 1)
        self.assertEqual(self.shown(), (2, sorted(self.PAIR)))
        self.assertEqual(self.repository.clear_distinct(1), 1)
        self.assertEqual(self.shown(), (5, sorted(self.PAIR + self.TRIO)))

    def test_a_stale_or_foreign_key_changes_nothing(self):
        for key in ("g2_flat_101_1_photo", "g1_flat_301_1_photo", "nonsense", "", None):
            self.assertIsNone(self.repository.mark_group_distinct(key, 1), key)
        self.assertIsNone(self.repository.mark_group_distinct(self.key(self.PAIR[0]), 2))
        self.assertFalse(self.repository.undo_group_distinct([], 1))
        self.assertEqual(self.shown(), (5, sorted(self.PAIR + self.TRIO)))


if __name__ == "__main__":
    unittest.main()
