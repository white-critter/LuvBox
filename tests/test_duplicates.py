import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from PIL import Image, ImageDraw
from luvbox.duplicates import compute_dhash, compute_file_hash, hamming_distance, cluster_duplicates
from luvbox.history import HistoryStore
from luvbox.qt_library import LibraryRepository, _Request
from luvbox.storage import resolve_storage


class DuplicateTests(unittest.TestCase):
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

    def create_image(self, name, color="red", draw_rect=True, size=(100, 100)):
        p = self.root / name
        im = Image.new("RGB", size, color=color)
        if draw_rect:
            d = ImageDraw.Draw(im)
            d.rectangle([20, 20, 80, 80], fill="blue")
        im.save(p)
        return p

    def test_dhash_and_clustering(self):
        # f1 and f2 are similar (scaled version of same art)
        f1 = self.create_image("pic1.png", "red", draw_rect=True, size=(100, 100))
        f2 = self.create_image("pic2.jpg", "red", draw_rect=True, size=(80, 80))
        # f3 is completely different
        f3 = self.create_image("pic3.png", "green", draw_rect=False, size=(100, 100))

        h1 = compute_dhash(f1)
        h2 = compute_dhash(f2)
        h3 = compute_dhash(f3)

        self.assertLessEqual(hamming_distance(h1, h2), 6)
        self.assertGreater(hamming_distance(h1, h3), 6)

        items = [
            {"key": "k1", "file_hash": compute_file_hash(f1), "dhash": h1, "filesize": 1000},
            {"key": "k2", "file_hash": compute_file_hash(f2), "dhash": h2, "filesize": 800},
            {"key": "k3", "file_hash": compute_file_hash(f3), "dhash": h3, "filesize": 500},
        ]
        clusters = cluster_duplicates(items, max_distance=6)
        self.assertEqual(len(clusters), 1)
        self.assertEqual({x["key"] for x in clusters[0]}, {"k1", "k2"})

    def test_store_hashes_and_duplicate_filter(self):
        p1 = self.create_image("a_101_1.jpg", "red", draw_rect=True)
        p2 = self.create_image("b_102_1.jpg", "red", draw_rect=True)  # duplicate dhash
        p3 = self.create_image("c_103_1.jpg", "yellow", draw_rect=False)  # unique

        self.store.record_media("flat", {"tweet_id": "101", "num": 1, "type": "photo", "author": "a"}, p1)
        self.store.record_media("flat", {"tweet_id": "102", "num": 1, "type": "photo", "author": "b"}, p2)
        self.store.record_media("flat", {"tweet_id": "103", "num": 1, "type": "photo", "author": "c"}, p3)

        dh1 = compute_dhash(p1)
        dh2 = compute_dhash(p2)
        dh3 = compute_dhash(p3)

        hashes = [
            ("flat", "101", 1, "photo", p1.stat().st_size, "hash1", dh1),
            ("flat", "102", 1, "photo", p2.stat().st_size, "hash2", dh2),
            ("flat", "103", 1, "photo", p3.stat().st_size, "hash3", dh3),
        ]
        self.store.save_image_hashes(hashes)

        # Total duplicate count should be 2 (101 and 102)
        self.assertEqual(self.store.duplicate_counts(), 2)

        # Filter duplicate_only
        dup_page = self.store.record_page(0, 10, "all", "", duplicate_only=True)
        self.assertEqual(dup_page["total"], 2)
        dup_ids = {r["tweet_id"] for r in dup_page["rows"]}
        self.assertEqual(dup_ids, {"101", "102"})

    def test_repository_scan_and_copy_keys(self):
        paths = resolve_storage(self.root / "Likes", "offline")
        with closing(HistoryStore.for_storage(paths)) as store:
            p1 = paths.output_dir / "user_201_1.jpg"
            p2 = paths.output_dir / "user_202_1.jpg"
            p3 = paths.output_dir / "user_203_1.jpg"

            # Create p1 and p2 as identical images
            im = Image.new("RGB", (60, 60), color="blue")
            im.save(p1)
            im.save(p2)
            # Create p3 as different image
            im3 = Image.new("RGB", (60, 60), color="white")
            im3.save(p3)

            store.record_media("flat", {"tweet_id": "201", "num": 1, "type": "photo", "author": "user"}, p1)
            store.record_media("flat", {"tweet_id": "202", "num": 1, "type": "photo", "author": "user"}, p2)
            store.record_media("flat", {"tweet_id": "203", "num": 1, "type": "photo", "author": "user"}, p3)

        repo = LibraryRepository()
        repo.provider = self
        req = _Request(generation=1, account="offline", output_dir=str(paths.output_dir), separate_accounts=False)
        repo.configure(req)

        # Run duplicate scan
        count = repo.scan_duplicates(1)
        self.assertEqual(count, 2)

        # Get copy keys (should pick 1 duplicate to discard, keep 1 primary)
        copy_keys = repo.duplicate_copy_keys(1)
        self.assertEqual(len(copy_keys), 1)
        self.assertTrue("201" in copy_keys[0] or "202" in copy_keys[0])


if __name__ == "__main__":
    unittest.main()
