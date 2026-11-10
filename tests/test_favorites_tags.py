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

        # A file that is no longer in the library must not keep the tag in the filter list.
        with self.store._db:
            self.store._db.execute("UPDATE media SET path='' WHERE tweet_id='202'")
        visible = {t["tag"]: t["count"] for t in self.store.user_tags()}
        self.assertEqual(visible.get("배경화면"), 1)
        self.assertEqual(visible.get("풍경"), 1)

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

    def test_merge_and_recovery_preserve_favorites_tags_and_hashes(self):
        import sqlite3
        from luvbox.storage import _merge_history
        from luvbox.recovery import rebuild_history

        p1 = self.file("m_501_1.jpg")
        self.store.record_media("flat", {"tweet_id": "501", "num": 1, "type": "photo", "author": "u"}, p1)
        self.store.toggle_favorite("flat", "501", 1, "photo")
        self.store.add_tag("flat", "501", 1, "photo", "best")
        self.store.save_image_hashes([("flat", "501", 1, "photo", 12, "fhash501", "0123456789abcdef")])
        self.store.close()

        # 1. Test _merge_history into a fresh target db
        src_db = self.root / ".luvbox-history.sqlite3"
        dst_dir = self.root / "merged"
        dst_dir.mkdir()
        HistoryStore(dst_dir).close()
        dst_db = dst_dir / ".luvbox-history.sqlite3"
        with closing(sqlite3.connect(dst_db)) as target, closing(sqlite3.connect(src_db)) as source:
            with target:
                _merge_history(target, source)
        with closing(HistoryStore(dst_dir)) as merged_store:
            self.assertTrue(merged_store.is_favorite("flat", "501", 1, "photo"))
            self.assertEqual([t["tag"] for t in merged_store.item_tags("flat", "501", 1, "photo")], ["best"])
            self.assertIn(("flat", "501", 1, "photo"), merged_store.get_image_hashes())

        # 2. Test rebuild_history preserves favorites, media_tags, and image_hashes
        rebuilt_dir = self.root / "rebuilt"
        rebuilt_dir.mkdir()
        rebuild_history(src_db, rebuilt_dir / ".luvbox-history.sqlite3")
        with closing(HistoryStore(rebuilt_dir)) as rebuilt_store:
            self.assertTrue(rebuilt_store.is_favorite("flat", "501", 1, "photo"))
            self.assertEqual([t["tag"] for t in rebuilt_store.item_tags("flat", "501", 1, "photo")], ["best"])
            self.assertIn(("flat", "501", 1, "photo"), rebuilt_store.get_image_hashes())

        # 3. Test _merge_history when both target and source have pre-1.6.0 media_tags without 'source' column
        legacy_a = self.root / "legacy_a.sqlite3"
        legacy_b = self.root / "legacy_b.sqlite3"
        legacy_schema = (
            "CREATE TABLE media_tags ("
            "folder_mode TEXT NOT NULL, tweet_id TEXT NOT NULL, num INTEGER NOT NULL, "
            "media_type TEXT NOT NULL, tag TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '', "
            "PRIMARY KEY (folder_mode, tweet_id, num, media_type, tag))"
        )
        with closing(sqlite3.connect(legacy_a)) as db_a, closing(sqlite3.connect(legacy_b)) as db_b:
            with db_a:
                db_a.execute(legacy_schema)
                db_a.execute("INSERT INTO media_tags VALUES ('flat','701',1,'photo','풍경','2026-11-09')")
            with db_b:
                db_b.execute(legacy_schema)
                db_b.execute("INSERT INTO media_tags VALUES ('flat','702',1,'photo','인물','2026-11-09')")
            with db_a:
                _merge_history(db_a, db_b)
            rows = db_a.execute("SELECT tweet_id, tag, source FROM media_tags ORDER BY tweet_id").fetchall()
            self.assertEqual(rows, [("701", "풍경", "user"), ("702", "인물", "user")])


if __name__ == "__main__":
    unittest.main()
