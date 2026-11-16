"""Catalog defects found by the 1.10.8 inspection.

The duplicate count is what the duplicate view lists, and removing a tag that
has nothing to do with the AI categories leaves an unclassified photo to be
classified.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox.history import HistoryStore


class _Store(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = HistoryStore(self.root)
        self.addCleanup(self.store.close)

    def photo(self, tweet, content=b"image-sample"):
        path = self.root / f"a_{tweet}_1.jpg"
        path.write_bytes(content)
        self.store.record_media("flat", {"tweet_id": str(tweet), "num": 1, "type": "photo", "author": "a"}, path)
        return ("flat", str(tweet), 1, "photo")


class DuplicateCountMatchesTheViewTests(_Store):
    def test_hashes_of_a_scan_that_never_grouped_are_not_counted(self):
        """A stopped scan has saved hashes but no groups: the chip must not promise photos the view cannot list."""
        first, second, third = self.photo(101), self.photo(102), self.photo(103, b"another")
        self.store.save_image_hashes([(*first, 12, "same", "0123456789abcdef"),
                                      (*second, 12, "same", "0123456789abcdef"),
                                      (*third, 7, "other", "fedcba9876543210")])
        self.assertEqual(self.store.duplicate_page(0, 100)["total"], 0)
        self.assertEqual(self.store.duplicate_counts(), 0)
        # Once the scan has grouped them, both say the same again.
        self.store.save_clusters([[first, second]])
        self.assertEqual(self.store.duplicate_page(0, 100)["total"], 2)
        self.assertEqual(self.store.duplicate_counts(), 2)

    def test_a_group_whose_other_file_is_gone_counts_nothing(self):
        first, second = self.photo(201), self.photo(202)
        self.store.save_image_hashes([(*first, 12, "same", "0123456789abcdef"),
                                      (*second, 12, "same", "0123456789abcdef")])
        self.store.save_clusters([[first, second]])
        self.assertEqual(self.store.duplicate_counts(), 2)
        with self.store._db:
            self.store._db.execute("UPDATE media SET path='' WHERE tweet_id='202'")
        self.assertEqual((self.store.duplicate_counts(), self.store.duplicate_page(0, 100)["total"]), (0, 0))


class RemovingATagAndTheAiMarkTests(_Store):
    BLOB = bytes(2048)

    def marks(self, identity):
        return [tuple(row) for row in self.store._db.execute(
            "SELECT tag,source FROM media_tags WHERE tweet_id=? ORDER BY tag", (identity[1],))]

    def test_removing_an_unrelated_tag_leaves_an_unclassified_photo_to_be_classified(self):
        identity = self.photo(301)
        self.store.save_embeddings([(*identity, self.BLOB, 0.5)])
        self.assertEqual(self.store.count_embeddings_without_labels(), 1)
        self.store.add_tag(*identity, "여행")
        self.assertTrue(self.store.remove_tag(*identity, "여행"))
        self.assertEqual(self.marks(identity), [])
        self.assertEqual(self.store.count_embeddings_without_labels(), 1)

    def test_removing_a_tag_named_like_a_category_still_keeps_the_photo_from_being_labelled_again(self):
        identity = self.photo(302)
        self.store.save_embeddings([(*identity, self.BLOB, 0.5)])
        self.store.replace_estimated_labels([(*identity, "인물")])
        self.store.add_tag(*identity, "인물")          # the person's tag takes the AI label's place
        self.assertEqual(self.marks(identity), [("인물", "user")])
        self.assertTrue(self.store.remove_tag(*identity, "인물"))
        self.assertEqual(self.marks(identity), [("", "ai")])
        self.assertEqual(self.store.count_embeddings_without_labels(), 0)


if __name__ == "__main__":
    unittest.main()
