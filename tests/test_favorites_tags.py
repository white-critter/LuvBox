import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from luvbox.history import HistoryStore
from luvbox.qt_library import LibraryRepository, _Request
from luvbox.storage import resolve_storage


class FavoritesAndTagsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)
        self.store = HistoryStore(self.root)
        self.registered = {}

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def register(self, key, path):
        self.registered[key] = path

    def file(self, name):
        p = self.root / name
        p.write_bytes(b"image-sample")
        return p

    def test_favorites_toggle_and_filter(self):
        p1 = self.file("a_101_1.jpg")
        p2 = self.file("b_102_1.jpg")
        p3 = self.file("c_103_1.jpg")

        self.store.record_media("flat", {"tweet_id": "101", "num": 1, "type": "photo", "author": "a"}, p1)
        self.store.record_media("flat", {"tweet_id": "102", "num": 1, "type": "photo", "author": "b"}, p2)
        self.store.record_media("flat", {"tweet_id": "103", "num": 1, "type": "photo", "author": "c"}, p3)

        # Initial state: not favorites
        self.assertFalse(self.store.is_favorite("flat", "101", 1, "photo"))
        self.assertEqual(len(self.store.favorite_identities()), 0)

        # Toggle 101 -> True
        res = self.store.toggle_favorite("flat", "101", 1, "photo")
        self.assertTrue(res)
        self.assertTrue(self.store.is_favorite("flat", "101", 1, "photo"))
        self.assertEqual(len(self.store.favorite_identities()), 1)

        # Toggle 103 -> True
        self.assertTrue(self.store.toggle_favorite("flat", "103", 1, "photo"))
        self.assertEqual(len(self.store.favorite_identities()), 2)

        # Query all vs favorite_only
        all_page = self.store.record_page(0, 10, "all", "")
        self.assertEqual(all_page["total"], 3)

        fav_page = self.store.record_page(0, 10, "all", "", favorite_only=True)
        self.assertEqual(fav_page["total"], 2)
        fav_ids = {r["tweet_id"] for r in fav_page["rows"]}
        self.assertEqual(fav_ids, {"101", "103"})

        # Toggle 101 -> False (unfavorite)
        res_off = self.store.toggle_favorite("flat", "101", 1, "photo")
        self.assertFalse(res_off)
        self.assertFalse(self.store.is_favorite("flat", "101", 1, "photo"))

        fav_page2 = self.store.record_page(0, 10, "all", "", favorite_only=True)
        self.assertEqual(fav_page2["total"], 1)
        self.assertEqual(fav_page2["rows"][0]["tweet_id"], "103")

    def test_tags_add_remove_and_filter(self):
        p1 = self.file("a_201_1.jpg")
        p2 = self.file("b_202_1.jpg")

        self.store.record_media("flat", {"tweet_id": "201", "num": 1, "type": "photo", "author": "a"}, p1)
        self.store.record_media("flat", {"tweet_id": "202", "num": 1, "type": "photo", "author": "b"}, p2)

        # Add tags
        self.assertTrue(self.store.add_tag("flat", "201", 1, "photo", "배경화면"))
        self.assertTrue(self.store.add_tag("flat", "201", 1, "photo", "풍경"))
        self.assertTrue(self.store.add_tag("flat", "202", 1, "photo", "배경화면"))

        # Verify item tags
        tags_201 = self.store.item_tags("flat", "201", 1, "photo")
        self.assertEqual([t["tag"] for t in tags_201], ["배경화면", "풍경"])

        # Verify user tags aggregation
        all_user_tags = self.store.user_tags()
        tag_map = {t["tag"]: t["count"] for t in all_user_tags}
        self.assertEqual(tag_map.get("배경화면"), 2)
        self.assertEqual(tag_map.get("풍경"), 1)

        # Filter by user_tag
        bg_page = self.store.record_page(0, 10, "all", "", user_tag="배경화면")
        self.assertEqual(bg_page["total"], 2)

        landscape_page = self.store.record_page(0, 10, "all", "", user_tag="풍경")
        self.assertEqual(landscape_page["total"], 1)
        self.assertEqual(landscape_page["rows"][0]["tweet_id"], "201")

        # Remove tag
        self.assertTrue(self.store.remove_tag("flat", "201", 1, "photo", "풍경"))
        tags_201_after = self.store.item_tags("flat", "201", 1, "photo")
        self.assertEqual([t["tag"] for t in tags_201_after], ["배경화면"])

    def test_repository_favorite_flag_in_rows(self):
        paths = resolve_storage(self.root / "Likes", "offline")
        with closing(HistoryStore.for_storage(paths)) as store:
            p1 = paths.output_dir / "user_301_1.jpg"
            p1.write_bytes(b"data1")
            p2 = paths.output_dir / "user_302_1.jpg"
            p2.write_bytes(b"data2")

            store.record_media("flat", {"tweet_id": "301", "num": 1, "type": "photo", "author": "user"}, p1)
            store.record_media("flat", {"tweet_id": "302", "num": 1, "type": "photo", "author": "user"}, p2)
            store.toggle_favorite("flat", "301", 1, "photo")

        repo = LibraryRepository()
        repo.provider = self
        req = _Request(generation=1, account="offline", output_dir=str(paths.output_dir), separate_accounts=False)
        repo.configure(req)

        total, rows = repo.read("1", 0, 10)
        self.assertEqual(total, 2)
        row_301 = next(r for r in rows if "301" in r["key"])
        row_302 = next(r for r in rows if "302" in r["key"])
        self.assertTrue(row_301["favorite"])
        self.assertFalse(row_302["favorite"])


if __name__ == "__main__":
    unittest.main()
