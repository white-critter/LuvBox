import shutil
import tempfile
import unittest
import io
import os
from unittest.mock import patch
from contextlib import closing
from pathlib import Path
from PIL import Image, ImageDraw
from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
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

        self.assertEqual(dh1, dh2)
        hashes = [
            ("flat", "101", 1, "photo", p1.stat().st_size, "hash1", dh1),
            ("flat", "102", 1, "photo", p2.stat().st_size, "hash2", dh2),
            ("flat", "103", 1, "photo", p3.stat().st_size, "hash3", dh3),
        ]
        self.store.save_image_hashes(hashes)
        # The same perceptual hash alone is not a duplicate: the files differ.
        self.assertEqual(self.store.duplicate_counts(), 0)
        self.assertEqual(self.store.record_page(0, 10, "all", "", duplicate_only=True)["total"], 0)

        # The same file hash and size is.
        self.store.save_image_hashes([("flat", "102", 1, "photo", p1.stat().st_size, "hash1", dh2)])
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
        from unittest.mock import patch
        with patch("luvbox.duplicates.cluster_duplicates", side_effect=AssertionError("reclustering")):
            copy_keys = repo.duplicate_copy_keys(1)
        self.assertEqual(len(copy_keys), 1)
        self.assertTrue("201" in copy_keys[0] or "202" in copy_keys[0])

    def test_empty_hashes_and_deleted_media_not_counted_as_duplicates(self):
        p1 = self.create_image("d_301_1.jpg", "red", draw_rect=True)
        p2 = self.create_image("d_302_1.jpg", "blue", draw_rect=False)
        self.store.record_media("flat", {"tweet_id": "301", "num": 1, "type": "photo", "author": "a"}, p1)
        self.store.record_media("flat", {"tweet_id": "302", "num": 1, "type": "photo", "author": "b"}, p2)

        # Empty hashes must never match each other
        self.store.save_image_hashes([
            ("flat", "301", 1, "photo", 100, "", ""),
            ("flat", "302", 1, "photo", 100, "", ""),
        ])
        self.assertEqual(self.store.duplicate_counts(), 0)
        self.assertEqual(self.store.record_page(0, 10, "all", "", duplicate_only=True)["total"], 0)
        self.assertEqual(cluster_duplicates([
            {"key": "k1", "file_hash": "", "dhash": "", "filesize": 100},
            {"key": "k2", "file_hash": "", "dhash": "", "filesize": 100},
        ]), [])

        # Identical hashes count as duplicates while both files exist in catalog
        self.store.save_image_hashes([
            ("flat", "301", 1, "photo", 100, "samehash", "0123456789abcdef"),
            ("flat", "302", 1, "photo", 100, "samehash", "0123456789abcdef"),
        ])
        self.assertEqual(self.store.duplicate_counts(), 2)

        # When one media row is marked deleted (path=''), neither remains a duplicate
        with self.store._db:
            self.store._db.execute("UPDATE media SET path='' WHERE tweet_id='302'")
        self.assertEqual(self.store.duplicate_counts(), 0)
        self.assertEqual(self.store.record_page(0, 10, "all", "", duplicate_only=True)["total"], 0)

    def test_three_copies_and_favorite_kept_first(self):
        paths = resolve_storage(self.root / "FavDup", "offline")
        with closing(HistoryStore.for_storage(paths)) as store:
            p1 = paths.output_dir / "user_401_1.jpg"
            p2 = paths.output_dir / "user_402_1.jpg"
            p3 = paths.output_dir / "user_403_1.jpg"
            im = Image.new("RGB", (60, 60), color="purple")
            im.save(p1)
            im.save(p2)
            im.save(p3)
            store.record_media("flat", {"tweet_id": "401", "num": 1, "type": "photo", "author": "user"}, p1)
            store.record_media("flat", {"tweet_id": "402", "num": 1, "type": "photo", "author": "user"}, p2)
            store.record_media("flat", {"tweet_id": "403", "num": 1, "type": "photo", "author": "user"}, p3)
            # Mark 402 as favorite -> 402 must be kept, 401 and 403 selected as duplicate copies
            store.toggle_favorite("flat", "402", 1, "photo")

        repo = LibraryRepository()
        repo.provider = self
        req = _Request(generation=1, account="offline", output_dir=str(paths.output_dir), separate_accounts=False)
        repo.configure(req)
        self.assertEqual(repo.scan_duplicates(1), 3)
        copy_keys = repo.duplicate_copy_keys(1)
        self.assertEqual(len(copy_keys), 2)
        self.assertTrue(all("402" not in k for k in copy_keys))

    def test_photo_records_for_scan_and_chunked_save(self):
        p1 = self.create_image("s_501_1.jpg", "red")
        p2 = self.create_image("s_502_1.jpg", "blue")
        self.store.record_media("flat", {"tweet_id": "501", "num": 1, "type": "photo", "author": "a"}, p1)
        self.store.record_media("flat", {"tweet_id": "502", "num": 1, "type": "photo", "author": "b"}, p2)

        # photo_records_for_scan should return both active photos
        records = self.store.photo_records_for_scan()
        self.assertEqual(len(records), 2)
        self.assertEqual({r["tweet_id"] for r in records}, {"501", "502"})
        self.assertTrue(all(r["media_type"] == "photo" and r["path"] for r in records))

        # Deleted item (path='') should be excluded
        with self.store._db:
            self.store._db.execute("UPDATE media SET path='' WHERE tweet_id='502'")
        records_after = self.store.photo_records_for_scan()
        self.assertEqual(len(records_after), 1)
        self.assertEqual(records_after[0]["tweet_id"], "501")

        # save_clusters with multiple groups
        self.store.save_image_hashes([
            ("flat", "501", 1, "photo", 100, "h1", "0000000000000001"),
            ("flat", "502", 1, "photo", 100, "h2", "0000000000000002"),
        ])
        group = [("flat", "501", 1, "photo"), ("flat", "502", 1, "photo")]
        saved = self.store.save_clusters([group])
        self.assertEqual(saved, 2)
        # Because 502 has path='', the single remaining active member 501 in cluster 1 is not a duplicate
        self.assertEqual(self.store.duplicate_counts(), 0)
        self.assertEqual(self.store.record_page(0, 10, "all", "", duplicate_only=True)["total"], 0)

    def test_compute_image_hashes_and_pigeonhole_bucketing(self):
        import random
        from luvbox.duplicates import compute_image_hashes

        img_path = self.create_image("single_pass.jpg", "red", draw_rect=True)
        fhash, dhash = compute_image_hashes(img_path)
        self.assertEqual(fhash, compute_file_hash(img_path))
        self.assertEqual(dhash, compute_dhash(img_path))

        bad_path = self.root / "not_an_image.jpg"
        bad_path.write_bytes(b"not a valid raster image")
        bad_fhash, bad_dhash = compute_image_hashes(bad_path)
        self.assertEqual(bad_fhash, compute_file_hash(bad_path))
        self.assertEqual(bad_dhash, "")

        rng = random.Random(12345)
        base_hashes = [rng.getrandbits(64) for _ in range(120)]
        items = [
            {"key": f"k{i}", "file_hash": f"fh{i}", "dhash": f"{h:016x}", "filesize": 1000 + i}
            for i, h in enumerate(base_hashes)
        ]
        # Add 10 near-duplicates within Hamming distance 3
        for i in range(10):
            near = base_hashes[i] ^ 0b10101  # 3 bits flipped
            items.append({"key": f"near{i}", "file_hash": f"fhn{i}", "dhash": f"{near:016x}", "filesize": 500})

        clusters_d4 = cluster_duplicates(items, max_distance=4)
        clusters_d8 = cluster_duplicates(items, max_distance=8)  # fallback path
        self.assertEqual(len(clusters_d4), 10)
        self.assertGreaterEqual(len(clusters_d8), 10)
        for grp in clusters_d4:
            keys = {x["key"] for x in grp}
            self.assertEqual(len(keys), 2)

    def scan_fixture(self):
        paths = resolve_storage(self.root / "signature-scan", "offline")
        with closing(HistoryStore.for_storage(paths)) as store:
            files = [paths.output_dir / f"user_{tid}_1.png" for tid in (801, 802)]
            for tid, path in zip((801, 802), files):
                with Image.new("RGB", (60, 60), "red") as image:
                    image.save(path, compress_level=0)
                store.record_media("flat", {"tweet_id": str(tid), "num": 1, "type": "photo", "author": "user"}, path)
        repo = LibraryRepository()
        repo.configure(_Request(1, "offline", str(paths.output_dir), False))
        return repo, paths, files

    def test_same_size_edit_invalidates_hash_but_unchanged_scan_reuses_it(self):
        repo, paths, files = self.scan_fixture()
        self.assertEqual(repo.scan_duplicates(1), 2)
        with patch("luvbox.duplicates.compute_image_hashes", side_effect=AssertionError("unneeded decode")):
            self.assertEqual(repo.scan_duplicates(1), 2)
        old = files[1].stat()
        with Image.new("RGB", (60, 60), "white") as image:
            image.save(files[1], compress_level=0)
        os.utime(files[1], ns=(old.st_atime_ns, old.st_mtime_ns + 10_000_000))
        self.assertEqual(files[1].stat().st_size, old.st_size)
        self.assertEqual(repo.scan_duplicates(1), 0)
        self.assertEqual(repo.duplicate_copy_keys(1), [])

    def test_unreadable_changed_file_drops_old_hash(self):
        repo, paths, files = self.scan_fixture()
        self.assertEqual(repo.scan_duplicates(1), 2)
        files[1].write_bytes(b"changed")
        with patch("luvbox.duplicates.compute_image_hashes", return_value=("", "")):
            self.assertEqual(repo.scan_duplicates(1), 0)
        with closing(HistoryStore.for_storage(paths)) as store:
            self.assertNotIn(("flat", "802", 1, "photo"), store.get_image_hashes())

    def test_changed_during_decode_is_not_published(self):
        from luvbox import duplicates
        path = self.create_image("changing.png")
        decode = duplicates._dhash_from_stream
        def changed(source, hash_size=8):
            result = decode(source, hash_size)
            info = path.stat()
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 10_000_000))
            return result
        with patch.object(duplicates, "_dhash_from_stream", side_effect=changed):
            self.assertEqual(duplicates.compute_image_hashes(path), ("", ""))

    def test_hash_read_is_bounded_when_stream_grows(self):
        from luvbox.duplicates import _stream_hash
        stream = io.BytesIO(b"x" * 100)
        with patch("luvbox.duplicates.MAX_RASTER_BYTES", 20):
            with self.assertRaises(OSError):
                _stream_hash(stream)
        self.assertEqual(stream.tell(), 21)

    def test_cancel_during_comparison_does_not_publish_clusters(self):
        from luvbox.duplicates import ScanCancelled
        repo, paths, files = self.scan_fixture()
        self.assertEqual(repo.scan_duplicates(1), 2)
        def progress(current, total):
            if current == total:
                repo._duplicate_cancel.set()
        with self.assertRaises(ScanCancelled):
            repo.scan_duplicates(1, progress_callback=progress)
        with closing(HistoryStore.for_storage(paths)) as store:
            self.assertEqual(store.duplicate_counts(), 2)
        repo._duplicate_cancel.clear()
        self.assertEqual(repo.scan_duplicates(1), 2)

    def test_cluster_failure_rolls_back_reset_and_all_updates(self):
        repo, paths, files = self.scan_fixture()
        self.assertEqual(repo.scan_duplicates(1), 2)
        from luvbox.history import HistoryError
        with closing(HistoryStore.for_storage(paths)) as store:
            before = list(store._db.execute("SELECT tweet_id, cluster_id FROM image_hashes ORDER BY tweet_id"))
            store._db.execute("CREATE TEMP TRIGGER fail_cluster BEFORE UPDATE OF cluster_id ON image_hashes "
                              "WHEN NEW.tweet_id='802' AND NEW.cluster_id>0 BEGIN SELECT RAISE(ABORT, 'test failure'); END")
            with self.assertRaises(HistoryError):
                store.save_clusters([[('flat', str(tid), 1, 'photo') for tid in (801, 802)]])
            after = list(store._db.execute("SELECT tweet_id, cluster_id FROM image_hashes ORDER BY tweet_id"))
            self.assertEqual(before, after)

    def test_merge_does_not_combine_unrelated_cluster_numbers(self):
        from luvbox.storage import _merge_history
        repo, paths, files = self.scan_fixture()
        self.assertEqual(repo.scan_duplicates(1), 2)
        with closing(HistoryStore.for_storage(paths)) as source:
            for tid in (901, 902):
                path = self.create_image(f"user_{tid}_1.png", "white", draw_rect=False)
                self.store.record_media("flat", {"tweet_id": str(tid), "num": 1, "type": "photo"}, path)
                self.store.save_image_hashes([("flat", str(tid), 1, "photo", path.stat().st_size, "different", "ffffffffffffffff", "local")])
            self.store.save_clusters([[('flat', str(tid), 1, 'photo') for tid in (901, 902)]])
            with self.store._db:
                _merge_history(self.store._db, source._db)
        rows = self.store._db.execute("SELECT cluster_id, file_signature FROM image_hashes").fetchall()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(tuple(row) == (0, "") for row in rows))

    def test_legacy_hash_schema_upgrades_without_losing_rows(self):
        self.store.save_image_hashes([("flat", "1", 1, "photo", 10, "hash", "0123456789abcdef")])
        self.store._db.execute("ALTER TABLE image_hashes DROP COLUMN file_signature")
        self.store.close()
        self.store = HistoryStore(self.root)
        row = self.store._db.execute("SELECT file_hash, file_signature FROM image_hashes").fetchone()
        self.assertEqual(tuple(row), ("hash", ""))

    def test_invalid_perceptual_hashes_never_form_a_group(self):
        items = [{"key": str(i), "dhash": "z" * 16} for i in range(2)]
        self.assertEqual(cluster_duplicates(items), [])
        with self.assertRaises(ValueError):
            cluster_duplicates([], max_distance=4.5)

    def test_cluster_comparison_checks_cancellation_inside_buckets(self):
        from luvbox.duplicates import ScanCancelled
        calls = []
        def cancel():
            calls.append(1)
            if len(calls) == 3:
                raise ScanCancelled("stop")
        with self.assertRaises(ScanCancelled):
            cluster_duplicates([{"key": str(i), "dhash": f"{i:016x}"} for i in range(200)], check_cancel=cancel)
        self.assertEqual(len(calls), 3)

    def test_cluster_duplicates_tie_breaker_deterministic(self):
        items = [
            {"key": "k_1", "dhash": "0000000000000001", "filesize": 100, "favorite": False, "tweet_id": "100", "num": 1},
            {"key": "k_2", "dhash": "0000000000000001", "filesize": 100, "favorite": False, "tweet_id": "200", "num": 1},
            {"key": "k_3", "dhash": "0000000000000001", "filesize": 100, "favorite": True, "tweet_id": "050", "num": 1},
        ]
        # Favorited item must be first; then higher tweet_id among identical size/fav
        clusters = cluster_duplicates(items, max_distance=1)
        self.assertEqual(len(clusters), 1)
        group = clusters[0]
        self.assertEqual(group[0]["key"], "k_3")
        self.assertEqual(group[1]["key"], "k_2")
        self.assertEqual(group[2]["key"], "k_1")

    def test_duplicate_copy_identities_left_join_preserves_favorites(self):
        p1 = self.create_image("c1.jpg", "blue")
        p2 = self.create_image("c2.jpg", "blue")
        self.store.record_media("flat", {"tweet_id": "301", "num": 1, "type": "photo"}, p1)
        self.store.record_media("flat", {"tweet_id": "302", "num": 1, "type": "photo"}, p2)
        h1 = compute_dhash(p1)
        self.store.save_image_hashes([
            ("flat", "301", 1, "photo", 1000, "h1", h1),
            ("flat", "302", 1, "photo", 2000, "h1", h1),
        ])
        self.store.save_clusters([[("flat", "301", 1, "photo"), ("flat", "302", 1, "photo")]])
        # 301 is smaller (1000 vs 2000), but if favorited, it must be kept as primary copy
        self.store.toggle_favorite("flat", "301", 1, "photo")
        copies = self.store.duplicate_copy_identities()
        self.assertEqual(copies, [("flat", "302", 1, "photo")])

    def test_overview_includes_duplicate_count(self):
        paths = resolve_storage(self.root / "LikesOverview", "offline")
        with closing(HistoryStore.for_storage(paths)) as store:
            p1 = paths.output_dir / "user_401_1.jpg"
            p2 = paths.output_dir / "user_402_1.jpg"
            im = Image.new("RGB", (60, 60), color="blue")
            im.save(p1)
            im.save(p2)
            store.record_media("flat", {"tweet_id": "401", "num": 1, "type": "photo", "author": "user"}, p1)
            store.record_media("flat", {"tweet_id": "402", "num": 1, "type": "photo", "author": "user"}, p2)
            dh = compute_dhash(p1)
            store.save_image_hashes([
                ("flat", "401", 1, "photo", p1.stat().st_size, "hash", dh),
                ("flat", "402", 1, "photo", p2.stat().st_size, "hash", dh),
            ])
            store.save_clusters([[("flat", "401", 1, "photo"), ("flat", "402", 1, "photo")]])

            repo = LibraryRepository()
            repo.configure(_Request(1, "offline", str(paths.output_dir), False))
            ov = repo.overview(1)
            self.assertEqual(ov.get("duplicates"), 2)


if __name__ == "__main__":
    unittest.main()
