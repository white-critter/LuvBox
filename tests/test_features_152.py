import unittest
import tempfile
import pathlib
from contextlib import closing
from datetime import datetime, timezone
from PIL import Image

from luvbox.history import HistoryStore
from luvbox.storage import resolve_storage
from luvbox.duplicates import compute_dhash, hamming_distance
from luvbox.qt_library import LibraryRepository, _Request, _format_size


class Features152Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp_dir.name)
        self.paths = resolve_storage(self.root / "Test152", "offline")

    def tearDown(self):
        self.temp_dir.cleanup()

    def register(self, key, path):
        pass

    def test_format_size(self):
        self.assertEqual(_format_size(None), "")
        self.assertEqual(_format_size(0), "")
        self.assertEqual(_format_size(500), "500 B")
        self.assertEqual(_format_size(1024), "1.0 KB")
        self.assertEqual(_format_size(1536), "1.5 KB")
        self.assertEqual(_format_size(1024 * 1024), "1.0 MB")
        self.assertEqual(_format_size(1024 * 1024 * 1024 * 2), "2.00 GB")

    def test_custom_user_tags_and_filtering(self):
        with closing(HistoryStore.for_storage(self.paths)) as store:
            p1 = self.paths.output_dir / "art_101_1.jpg"
            p2 = self.paths.output_dir / "art_102_1.jpg"
            Image.new("RGB", (40, 40), color="red").save(p1)
            Image.new("RGB", (40, 40), color="green").save(p2)
            store.record_media("flat", {"tweet_id": "101", "num": 1, "type": "photo", "author": "artist"}, p1)
            store.record_media("flat", {"tweet_id": "102", "num": 1, "type": "photo", "author": "artist"}, p2)

        repo = LibraryRepository()
        self.addCleanup(repo.configure, None)
        repo.provider = self
        req = _Request(generation=1, account="offline", output_dir=str(self.paths.output_dir), separate_accounts=False)
        repo.configure(req)

        self.assertEqual(repo.user_tags(1), [])
        key1 = "g1_flat_101_1_photo"
        key2 = "g1_flat_102_1_photo"

        # Add tag to 101
        self.assertTrue(repo.add_tag(key1, 1, "wallpaper"))
        self.assertTrue(repo.add_tag(key1, 1, "favorite_set"))
        # Add tag to 102
        self.assertTrue(repo.add_tag(key2, 1, "wallpaper"))

        self.assertEqual([t["tag"] for t in repo.user_tags(1)], ["wallpaper", "favorite_set"])

        # Filter by user_tag "favorite_set" -> only 101
        req_fav = _Request(generation=2, account="offline", output_dir=str(self.paths.output_dir),
                           separate_accounts=False, user_tag="favorite_set")
        repo.configure(req_fav)
        total, rows = repo.read("2", 0, 100)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["key"], "g2_flat_101_1_photo")

        # Filter by user_tag "wallpaper" -> both 101 and 102
        req_wall = _Request(generation=3, account="offline", output_dir=str(self.paths.output_dir),
                            separate_accounts=False, user_tag="wallpaper")
        repo.configure(req_wall)
        total, rows = repo.read("3", 0, 100)
        self.assertEqual(total, 2)

        # Remove tag
        self.assertTrue(repo.remove_tag("g3_flat_101_1_photo", 3, "wallpaper"))
        total, rows = repo.read("3", 0, 100)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["key"], "g3_flat_102_1_photo")

        # Batch tag addition
        batch_added = repo.add_tag_batch(["g3_flat_101_1_photo", "g3_flat_102_1_photo"], 3, "batch_collection")
        self.assertEqual(batch_added, 2)
        self.assertIn("batch_collection", [t["tag"] for t in repo.user_tags(3)])

    def test_library_overview_statistics(self):
        with closing(HistoryStore.for_storage(self.paths)) as store:
            p1 = self.paths.output_dir / "user1_101_1.jpg"
            p2 = self.paths.output_dir / "user2_102_1.jpg"
            Image.new("RGB", (50, 50), color="blue").save(p1)
            Image.new("RGB", (50, 50), color="yellow").save(p2)
            store.record_media("flat", {"tweet_id": "101", "num": 1, "type": "photo", "author": {"name": "user1"},
                                        "date": datetime(2026, 8, 15, tzinfo=timezone.utc)}, p1)
            store.record_media("flat", {"tweet_id": "102", "num": 1, "type": "photo", "author": {"name": "user2"},
                                        "date": datetime(2026, 9, 20, tzinfo=timezone.utc)}, p2)

        repo = LibraryRepository()
        self.addCleanup(repo.configure, None)
        repo.provider = self
        req = _Request(generation=1, account="offline", output_dir=str(self.paths.output_dir), separate_accounts=False)
        repo.configure(req)

        stats = repo.overview(1)
        self.assertIsInstance(stats, dict)
        self.assertEqual(stats["files"], 2)
        self.assertIn("formatted_bytes", stats)
        self.assertEqual(stats["photo"], 2)
        self.assertEqual(len(stats["authors"]), 2)
        authors = {a["author"] for a in stats["authors"]}
        self.assertEqual(authors, {"user1", "user2"})
        months = {m["month"] for m in stats["monthly"]}
        self.assertIn("2026-08", months)
        self.assertIn("2026-09", months)

    def test_preview_metadata_and_hashtags(self):
        with closing(HistoryStore.for_storage(self.paths)) as store:
            p = self.paths.output_dir / "artist_501_1.jpg"
            Image.new("RGB", (128, 64), color="orange").save(p)
            store.record_media("flat", {"tweet_id": "501", "num": 1, "type": "photo", "author": {"name": "art_creator"},
                                        "date": datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)}, p)
            store.record_tweets([("501", "Great artwork! #art #digital", ["#art", "#digital"])])

        repo = LibraryRepository()
        self.addCleanup(repo.configure, None)
        repo.provider = self
        req = _Request(generation=1, account="offline", output_dir=str(self.paths.output_dir), separate_accounts=False)
        repo.configure(req)

        selected = repo.selected("g1_flat_501_1_photo", 1)
        self.assertIsNotNone(selected)
        self.assertEqual(selected["author"], "art_creator")
        self.assertIn("128×64", selected["public_info"])
        self.assertIn("artist_501_1.jpg", selected["public_info"])
        self.assertEqual(selected["hashtags"], ["#art", "#digital"])

    def test_similar_image_detection_and_progress(self):
        with closing(HistoryStore.for_storage(self.paths)) as store:
            # Original 100x100
            p_orig = self.paths.output_dir / "dup_601_1.jpg"
            img = Image.new("RGB", (100, 100), color="teal")
            for x in range(30, 70):
                for y in range(30, 70):
                    img.putpixel((x, y), (255, 0, 0))
            img.save(p_orig, "JPEG", quality=95)

            # Resized 60x60
            p_resized = self.paths.output_dir / "dup_602_1.jpg"
            img_small = img.resize((60, 60), Image.Resampling.LANCZOS)
            img_small.save(p_resized, "JPEG", quality=85)

            # Unrelated image
            p_unrelated = self.paths.output_dir / "dup_603_1.jpg"
            img_diff = Image.new("RGB", (100, 100), color="white")
            for x in range(10, 20):
                for y in range(10, 20):
                    img_diff.putpixel((x, y), (0, 0, 0))
            img_diff.save(p_unrelated, "JPEG", quality=90)

            store.record_media("flat", {"tweet_id": "601", "num": 1, "type": "photo", "author": "u1"}, p_orig)
            store.record_media("flat", {"tweet_id": "602", "num": 1, "type": "photo", "author": "u2"}, p_resized)
            store.record_media("flat", {"tweet_id": "603", "num": 1, "type": "photo", "author": "u3"}, p_unrelated)

        dh1 = compute_dhash(p_orig)
        dh2 = compute_dhash(p_resized)
        dist = hamming_distance(dh1, dh2)
        self.assertLessEqual(dist, 4)

        repo = LibraryRepository()
        self.addCleanup(repo.configure, None)
        repo.provider = self
        req = _Request(generation=1, account="offline", output_dir=str(self.paths.output_dir), separate_accounts=False)
        repo.configure(req)

        progress_calls = []
        def on_progress(cur, tot):
            progress_calls.append((cur, tot))

        dup_count = repo.scan_duplicates(1, progress_callback=on_progress)
        self.assertEqual(dup_count, 2)
        self.assertTrue(len(progress_calls) > 0)

        copy_keys = repo.duplicate_copy_keys(1)
        self.assertEqual(len(copy_keys), 1)
        # Larger filesize (601) is preserved as primary; 602 is the copy
        self.assertIn("602", copy_keys[0])

    def test_cached_user_tag_decrement_and_removal(self):
        from luvbox.qt_library import LibraryController

        class _DummyController:
            _user_tags = []
            _user_tag = ""
            class _Sig:
                def __init__(self):
                    self.count = 0
                def emit(self):
                    self.count += 1
            userTagsChanged = _Sig()
            userTagFilterChanged = _Sig()

        dummy = _DummyController()
        LibraryController._upsert_cached_user_tag(dummy, "풍경", delta=2)
        LibraryController._upsert_cached_user_tag(dummy, "인물", delta=1)
        self.assertEqual(dummy._user_tags, [{"tag": "풍경", "count": 2}, {"tag": "인물", "count": 1}])

        # Decrement 풍경 from 2 -> 1
        LibraryController._upsert_cached_user_tag(dummy, "풍경", delta=-1)
        self.assertEqual(dummy._user_tags, [{"tag": "인물", "count": 1}, {"tag": "풍경", "count": 1}])

        # Set active filter to 인물 and remove its last occurrence -> removed from list & filter reset
        dummy._user_tag = "인물"
        LibraryController._upsert_cached_user_tag(dummy, "인물", delta=-1)
        self.assertEqual(dummy._user_tags, [{"tag": "풍경", "count": 1}])
        self.assertEqual(dummy._user_tag, "")
        self.assertEqual(dummy.userTagFilterChanged.count, 1)


if __name__ == "__main__":
    unittest.main()
