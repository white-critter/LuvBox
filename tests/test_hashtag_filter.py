import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox.history import HistoryStore
from luvbox.qt_library import LibraryRepository, _Request
from luvbox.storage import resolve_storage


class HashtagFilterTests(unittest.TestCase):
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
        p.write_bytes(b"image-data-sample")
        return p

    def test_hashtags_aggregation_and_filtering(self):
        # Tweet 1001: cat, cute
        p1 = self.file("alice_1001_1.jpg")
        self.store.record_media("flat", {"tweet_id": "1001", "num": 1, "type": "photo", "author": "alice"}, p1)

        # Tweet 1002: cat, sleepy
        p2 = self.file("bob_1002_1.jpg")
        self.store.record_media("flat", {"tweet_id": "1002", "num": 1, "type": "photo", "author": "bob"}, p2)

        # Tweet 1003: dog, cute
        p3 = self.file("charlie_1003_1.jpg")
        self.store.record_media("flat", {"tweet_id": "1003", "num": 1, "type": "photo", "author": "charlie"}, p3)

        self.store.record_tweets([
            ("1001", "A cute cat", ["cat", "cute"]),
            ("1002", "A sleepy cat", ["cat", "sleepy"]),
            ("1003", "A cute dog", ["dog", "cute"]),
        ])

        # 1. Test hashtags aggregation
        tags = self.store.hashtags(include_deleted=False)
        tag_dict = {t["tag"]: t["count"] for t in tags}
        self.assertEqual(tag_dict.get("#cat"), 2)
        self.assertEqual(tag_dict.get("#cute"), 2)
        self.assertEqual(tag_dict.get("#sleepy"), 1)
        self.assertEqual(tag_dict.get("#dog"), 1)

        # 2. Test record_page filtering with hashtag
        cat_page = self.store.record_page(0, 10, "all", "", hashtag="#cat")
        self.assertEqual(cat_page["total"], 2)
        cat_tweet_ids = {r["tweet_id"] for r in cat_page["rows"]}
        self.assertEqual(cat_tweet_ids, {"1001", "1002"})

        dog_page = self.store.record_page(0, 10, "all", "", hashtag="dog")  # without #
        self.assertEqual(dog_page["total"], 1)
        self.assertEqual(dog_page["rows"][0]["tweet_id"], "1003")

        cute_page = self.store.record_page(0, 10, "all", "", hashtag="#cute")
        self.assertEqual(cute_page["total"], 2)
        cute_tweet_ids = {r["tweet_id"] for r in cute_page["rows"]}
        self.assertEqual(cute_tweet_ids, {"1001", "1003"})

    def test_library_repository_hashtag_integration(self):
        paths = resolve_storage(self.root / "Likes", "offline")
        with closing(HistoryStore.for_storage(paths)) as store:
            p1 = paths.output_dir / "user_2001_1.jpg"
            p1.write_bytes(b"image-data")
            store.record_media("flat", {"tweet_id": "2001", "num": 1, "type": "photo", "author": "user"}, p1)
            store.record_tweets([("2001", "test tweet", ["art", "design"])])

        repo = LibraryRepository()
        repo.provider = self
        req = _Request(generation=1, account="offline", output_dir=str(paths.output_dir), separate_accounts=False, hashtag="#art")
        repo.configure(req)

        # Test repository hashtags()
        tags = repo.hashtags(1)
        tag_dict = {t["tag"]: t["count"] for t in tags}
        self.assertIn("#art", tag_dict)
        self.assertIn("#design", tag_dict)

        # Test repository read with hashtag filter
        total, rows = repo.read("1", 0, 10)
        self.assertEqual(total, 1)
        self.assertEqual(len(rows), 1)
        self.assertIn("2001", rows[0]["key"])

    def test_exact_hashtag_token_matching_and_pre_150_schema_upgrade(self):
        import sqlite3
        from luvbox.qt_library import _ReadOnlyHistory

        # 1. Exact token matching: #cat must not match #category or #catgirl
        p1 = self.file("u_3001_1.jpg")
        p2 = self.file("u_3002_1.jpg")
        self.store.record_media("flat", {"tweet_id": "3001", "num": 1, "type": "photo", "author": "u"}, p1)
        self.store.record_media("flat", {"tweet_id": "3002", "num": 1, "type": "photo", "author": "u"}, p2)
        self.store.record_tweets([
            ("3001", "hello #cat world", ["cat"]),
            ("3002", "hello #category #catgirl", ["category", "catgirl"]),
        ])
        cat_page = self.store.record_page(0, 10, "all", "", hashtag="#cat")
        self.assertEqual([r["tweet_id"] for r in cat_page["rows"]], ["3001"])

        # 2. Pre-1.5.0 catalog schema without tweets.hashtags, favorites, media_tags, image_hashes
        legacy_root = self.root / "legacy_catalog"
        paths = resolve_storage(legacy_root, "offline")
        with closing(HistoryStore.for_storage(paths)) as s:
            img = paths.output_dir / "user_4001_1.jpg"
            img.write_bytes(b"img")
            s.record_media("flat", {"tweet_id": "4001", "num": 1, "type": "photo", "author": "user"}, img)
        db_file = paths.record_dir / ".luvbox-history.sqlite3"
        with closing(sqlite3.connect(db_file)) as conn:
            with conn:
                conn.execute("DROP TABLE tweets")
                conn.execute("CREATE TABLE tweets (tweet_id TEXT PRIMARY KEY, content TEXT NOT NULL, updated_at TEXT NOT NULL)")
                conn.execute("INSERT INTO tweets(tweet_id, content, updated_at) VALUES ('4001', 'Legacy post #retro #pixelart', '2026-01-01T00:00:00+09:00')")
                conn.execute("DROP TABLE favorites")
                conn.execute("DROP TABLE media_tags")
                conn.execute("DROP TABLE image_hashes")

        # Opening read-only before any writable migration must succeed without OperationalError
        with closing(_ReadOnlyHistory(paths)) as ro:
            page = ro.record_page(0, 10, "all", "")
            self.assertEqual(page["total"], 1)
            self.assertEqual(ro.hashtags(), [])
            self.assertEqual(ro.favorite_identities(), set())
            self.assertEqual(ro.duplicate_counts(), 0)

        # Opening writable HistoryStore must add tweets.hashtags and backfill from content
        with closing(HistoryStore.for_storage(paths)) as migrated:
            tags = {t["tag"]: t["count"] for t in migrated.hashtags()}
            self.assertEqual(tags.get("#retro"), 1)
            self.assertEqual(tags.get("#pixelart"), 1)
            retro_page = migrated.record_page(0, 10, "all", "", hashtag="#retro")
            self.assertEqual(retro_page["total"], 1)


if __name__ == "__main__":
    unittest.main()
