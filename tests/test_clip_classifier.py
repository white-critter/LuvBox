"""Unit tests for AI CLIP image classification engine and UI/DB integration."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox.clip_classifier import (
    CATEGORY_NAMES,
    categories_from_embeddings,
    classify_batch,
    classify_image,
    get_category_embeddings,
    is_available,
    is_model_ready,
    preprocess_image,
    release_session,
    resolve_model_path,
)
from luvbox.history import HistoryStore
from luvbox.storage import StoragePaths


class TestClipClassifier(unittest.TestCase):
    def test_category_embeddings(self):
        embeds = get_category_embeddings()
        self.assertEqual(embeds.shape, (5, 512))
        self.assertEqual(embeds.dtype, np.float32)
        norms = np.linalg.norm(embeds, axis=1)
        for norm in norms:
            self.assertAlmostEqual(norm, 1.0, places=4)
        self.assertEqual(CATEGORY_NAMES, ["일러스트", "인물", "동물", "짤·텍스트", "풍경·배경"])
        scored = categories_from_embeddings(embeds)
        self.assertEqual([name for name, _confidence in scored], CATEGORY_NAMES)

    def test_encode_skip_signature_keeps_real_photos(self):
        from luvbox.clip_classifier import encode_skip_signature

        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            good = folder / "ok.jpg"
            bad = folder / "bad.jpg"
            Image.new("RGB", (8, 8), color="blue").save(good)
            bad.write_bytes(b"this is not a photo")
            self.assertIsNone(encode_skip_signature(good))
            signature = encode_skip_signature(bad)
            self.assertEqual(signature, (bad.stat().st_size, bad.stat().st_mtime_ns))
        blank = categories_from_embeddings(np.zeros(512, dtype=np.float32))
        self.assertIsNone(blank[0][0])
        self.assertLess(blank[0][1], 0.45)

    def test_model_resolution_and_signature_cache(self):
        path = resolve_model_path()
        self.assertIsInstance(path, Path)
        if path.is_file():
            self.assertTrue(is_model_ready(path))
            # Second call should hit _VERIFIED_MODEL_SIG without re-reading the file
            with patch("builtins.open", side_effect=AssertionError("Should not re-read model on cached signature")):
                self.assertTrue(is_model_ready(path))
            self.assertTrue(is_available())

    def test_preprocess_security_and_bounds(self):
        with tempfile.TemporaryDirectory() as td:
            temp_dir = Path(td)

            # 1. Non-existent file
            self.assertIsNone(preprocess_image(temp_dir / "missing.jpg"))

            # 2. Valid small image
            img_path = temp_dir / "sample.jpg"
            img = Image.new("RGB", (300, 200), color=(128, 64, 32))
            img.save(img_path)

            tensor = preprocess_image(img_path)
            self.assertIsNotNone(tensor)
            self.assertEqual(tensor.shape, (3, 224, 224))
            self.assertEqual(tensor.dtype, np.float32)

            # 3. Extreme aspect ratio image (e.g. tall strip) and RGBA mode
            tall_path = temp_dir / "tall_rgba.png"
            Image.new("RGBA", (40, 2000), color=(10, 120, 240, 200)).save(tall_path)
            tall_tensor = preprocess_image(tall_path)
            self.assertIsNotNone(tall_tensor)
            self.assertEqual(tall_tensor.shape, (3, 224, 224))
            self.assertEqual(tall_tensor.dtype, np.float32)

            # 4. Oversized raster bomb
            with patch("luvbox.clip_classifier.MAX_PIXELS", 50):
                self.assertIsNone(preprocess_image(img_path))

            # 5. Oversized file bytes
            with patch("luvbox.clip_classifier.MAX_RASTER_BYTES", 10):
                self.assertIsNone(preprocess_image(img_path))

    def test_classification_inference_and_release(self):
        if not is_available():
            self.skipTest("ONNX model or onnxruntime not available in this test environment")

        with tempfile.TemporaryDirectory() as td:
            temp_dir = Path(td)
            img1 = temp_dir / "test1.png"
            img2 = temp_dir / "test2.png"

            Image.new("RGB", (256, 256), color=(255, 200, 200)).save(img1)
            Image.new("RGB", (320, 240), color=(50, 150, 50)).save(img2)

            # Single image classification
            tag, conf = classify_image(img1, threshold=0.1)
            self.assertTrue(tag is None or tag in CATEGORY_NAMES)
            self.assertIsInstance(conf, float)
            self.assertGreaterEqual(conf, 0.0)
            self.assertLessEqual(conf, 1.0)

            # Batch classification
            results = classify_batch([img1, img2], threshold=0.1, batch_size=2)
            self.assertEqual(len(results), 2)
            for res_tag, res_conf in results:
                self.assertTrue(res_tag is None or res_tag in CATEGORY_NAMES)
                self.assertIsInstance(res_conf, float)

            # Session release frees memory cleanly
            release_session()


class TestHistoryStoreAiTags(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self.td.name)
        self.db_path = self.temp_dir / ".luvbox-history.sqlite3"
        self.store = HistoryStore(self.db_path)

    def tearDown(self):
        self.store.close()
        self.td.cleanup()

    def test_ai_tags_crud(self):
        # Insert a dummy media record
        with self.store._db:
            self.store._db.execute(
                "INSERT INTO media(folder_mode, tweet_id, num, media_type, filename, path, status, record_source, updated_at) "
                "VALUES ('flat', '123456', 1, 'photo', 'photo1.jpg', 'C:/test/photo1.jpg', 'saved', 'download', '2026-11-10 10:00:00')"
            )
            self.store._db.execute(
                "INSERT INTO media(folder_mode, tweet_id, num, media_type, filename, path, status, record_source, updated_at) "
                "VALUES ('flat', '789012', 1, 'photo', 'photo2.jpg', 'C:/test/photo2.jpg', 'saved', 'download', '2026-11-10 10:01:00')"
            )

        # 1. unclassified_photo_records returns both
        unclass = self.store.unclassified_photo_records()
        self.assertEqual(len(unclass), 2)
        self.assertEqual(unclass[0]["tweet_id"], "123456")

        # 2. Add a user tag to photo 1
        self.store.add_tag("flat", "123456", 1, "photo", "내최애", source="user")

        # User tag does NOT mark it as AI-classified
        unclass_after_user = self.store.unclassified_photo_records()
        self.assertEqual(len(unclass_after_user), 2)

        # 3. Save AI tag for photo 1
        saved = self.store.save_ai_tags([("flat", "123456", 1, "photo", "일러스트")])
        self.assertEqual(saved, 1)

        # Now only photo 2 is unclassified
        unclass_after_ai = self.store.unclassified_photo_records()
        self.assertEqual(len(unclass_after_ai), 1)
        self.assertEqual(unclass_after_ai[0]["tweet_id"], "789012")

        # 4. Check item_tags on photo 1 has both user and AI tag
        tags = self.store.item_tags("flat", "123456", 1, "photo")
        self.assertEqual(len(tags), 2)
        sources = {t["source"]: t["tag"] for t in tags}
        self.assertEqual(sources.get("user"), "내최애")
        self.assertEqual(sources.get("ai"), "일러스트")

        # 5. ai_tag_stats
        stats = self.store.ai_tag_stats()
        self.assertEqual(stats, {"일러스트": 1})

        # 6. 내 태그는 직접 붙인 태그만 포함한다. AI 분류는 별도다.
        all_tags = self.store.user_tags()
        tag_names = {t["tag"] for t in all_tags}
        self.assertIn("내최애", tag_names)
        self.assertNotIn("일러스트", tag_names)

        # 7. clear_ai_tags preserves user tag!
        cleared = self.store.clear_ai_tags()
        self.assertEqual(cleared, 1)
        self.assertEqual(self.store.ai_tag_stats(), {})

        remaining = self.store.item_tags("flat", "123456", 1, "photo")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["tag"], "내최애")
        self.assertEqual(remaining[0]["source"], "user")

    def test_ai_tag_sentinel_and_user_removal_persistence(self):
        with self.store._db:
            self.store._db.execute(
                "INSERT INTO media(folder_mode, tweet_id, num, media_type, filename, path, status, record_source, updated_at) "
                "VALUES ('flat', '111', 1, 'photo', 'p1.jpg', 'C:/test/p1.jpg', 'saved', 'download', '2026-11-10 10:00:00')"
            )
            self.store._db.execute(
                "INSERT INTO media(folder_mode, tweet_id, num, media_type, filename, path, status, record_source, updated_at) "
                "VALUES ('flat', '222', 1, 'photo', 'p2.jpg', 'C:/test/p2.jpg', 'saved', 'download', '2026-11-10 10:01:00')"
            )

        # Save AI tag for p1 ('동물') and low-confidence sentinel ('') for p2
        applied = self.store.save_ai_tags([
            ("flat", "111", 1, "photo", "동물"),
            ("flat", "222", 1, "photo", ""),
        ])
        self.assertEqual(applied, 1)

        # Neither p1 nor p2 should appear in unclassified_photo_records()
        self.assertEqual(self.store.unclassified_photo_records(), [])

        # Sentinel '' must never appear in item_tags, user_tags, or ai_tag_stats
        self.assertEqual(self.store.item_tags("flat", "222", 1, "photo"), [])
        self.assertEqual(self.store.ai_tag_stats(), {"동물": 1})
        self.assertEqual([t["tag"] for t in self.store.user_tags()], [])

        # User manually removes AI tag '동물' from p1 -> sentinel preserved so incremental scan won't re-add it
        self.assertTrue(self.store.remove_tag("flat", "111", 1, "photo", "동물"))
        self.assertEqual(self.store.item_tags("flat", "111", 1, "photo"), [])
        self.assertEqual(self.store.unclassified_photo_records(), [])

        # clear_ai_tags removes all AI tags and sentinels so full re-classification is possible
        cleared = self.store.clear_ai_tags()
        self.assertEqual(cleared, 0)
        self.assertEqual(len(self.store.unclassified_photo_records()), 2)

    def test_add_tags_batch_and_model_cache(self):
        with self.store._db:
            self.store._db.execute(
                "INSERT INTO media(folder_mode, tweet_id, num, media_type, filename, path, status, record_source, updated_at) "
                "VALUES ('flat', '111', 1, 'photo', 'p1.jpg', 'C:/test/p1.jpg', 'saved', 'download', '2026-11-10 10:00:00')"
            )
            self.store._db.execute(
                "INSERT INTO media(folder_mode, tweet_id, num, media_type, filename, path, status, record_source, updated_at) "
                "VALUES ('flat', '222', 1, 'photo', 'p2.jpg', 'C:/test/p2.jpg', 'saved', 'download', '2026-11-10 10:01:00')"
            )
        added = self.store.add_tags_batch([
            ("flat", "111", 1, "photo"),
            ("flat", "222", 1, "photo"),
        ], "#배치태그", source="user")
        self.assertEqual(added, 2)
        self.assertEqual(self.store.user_tags(), [{"tag": "배치태그", "count": 2}])

    def test_user_tag_upgrades_ai_tag_source(self):
        self.store.save_ai_tags([("flat", "111", 1, "photo", "동물")])
        self.assertEqual(self.store.item_tags("flat", "111", 1, "photo"), [{"tag": "동물", "source": "ai"}])
        # User explicitly adds the same tag -> promoted to 'user' and preserved on clear_ai_tags()
        self.store.add_tag("flat", "111", 1, "photo", "동물", source="user")
        self.assertEqual(self.store.item_tags("flat", "111", 1, "photo"), [{"tag": "동물", "source": "user"}])
        self.store.clear_ai_tags()
        self.assertEqual(self.store.item_tags("flat", "111", 1, "photo"), [{"tag": "동물", "source": "user"}])

    def test_scan_ai_tags_fast_path_and_error_reporting(self):
        from unittest.mock import patch
        from contextlib import closing
        from luvbox.storage import resolve_storage
        from luvbox.qt_library import LibraryRepository, _Request

        paths = resolve_storage(self.temp_dir / "RepoScan", "offline")
        with closing(HistoryStore.for_storage(paths)) as store:
            p1 = paths.output_dir / "u_101_1.jpg"
            Image.new("RGB", (32, 32), color="blue").save(p1)
            store.record_media("flat", {"tweet_id": "101", "num": 1, "type": "photo", "author": "u"}, p1)
            store.save_ai_tags([("flat", "101", 1, "photo", "일러스트")])
            store.save_embeddings([("flat", "101", 1, "photo", b"\0" * 2048, 0.8)])

        repo = LibraryRepository()
        self.addCleanup(repo.configure, None)
        req = _Request(generation=1, account="offline", output_dir=str(paths.output_dir), separate_accounts=False)
        repo.configure(req)

        # 1. Fast path: nothing left to encode -> must not call get_session
        with patch("luvbox.clip_classifier.get_session") as mock_sess:
            self.assertEqual(repo.scan_ai_tags(1), 0)
            mock_sess.assert_not_called()

        # 2. Error reporting: drop the stored feature and the AI tag, then fail to start the engine
        with closing(HistoryStore.for_storage(paths)) as store:
            with store._db:
                store._db.execute("DELETE FROM ai_embeddings")
        repo.clear_ai_tags(1)
        with patch("luvbox.clip_classifier.is_available", return_value=True), \
             patch("luvbox.clip_classifier.get_session", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                repo.scan_ai_tags(1)
            self.assertIn("AI 분류 엔진", str(ctx.exception))

    def test_ai_progress_advances_each_batch_and_labels_are_stored(self):
        from unittest.mock import patch
        from contextlib import closing
        from luvbox.storage import resolve_storage
        from luvbox.qt_library import LibraryRepository, _Request

        paths = resolve_storage(self.temp_dir / "ProgressScan", "offline")
        with closing(HistoryStore.for_storage(paths)) as store:
            for index in range(20):
                image = paths.output_dir / f"u_{200 + index}_1.jpg"
                Image.new("RGB", (16, 16), color="blue").save(image)
                store.record_media(
                    "flat",
                    {"tweet_id": str(200 + index), "num": 1, "type": "photo", "author": "u"},
                    image,
                )

        def fake_detailed(image_paths, threshold=0.45, batch_size=16, session=None):
            vector = np.zeros(512, dtype=np.float32)
            vector[0] = 1
            return [("일러스트", 0.8, vector) for _path in image_paths]

        progress = []
        repo = LibraryRepository()
        self.addCleanup(repo.configure, None)
        repo.configure(_Request(generation=1, account="offline", output_dir=str(paths.output_dir), separate_accounts=False))
        with patch("luvbox.clip_classifier.is_available", return_value=True), \
             patch("luvbox.clip_classifier.get_session", return_value=object()), \
             patch("luvbox.clip_classifier.classify_detailed", side_effect=fake_detailed), \
             patch("luvbox.clip_classifier.release_session"):
            applied = repo.scan_ai_tags(1, progress_callback=lambda cur, tot: progress.append((cur, tot)))
        self.assertIn((16, 20), progress)
        self.assertEqual(progress[-1], (20, 20))
        self.assertEqual(applied, 20)
        with closing(HistoryStore.for_storage(paths)) as store:
            _embeddings, _feedback, estimated = store.learning_state()
            self.assertEqual(len(_embeddings), 20)
            self.assertEqual(sum(1 for tag in estimated.values() if tag == "일러스트"), 20)

    def test_cleared_ai_tags_are_restored_from_stored_embeddings(self):
        from contextlib import closing
        from luvbox.storage import resolve_storage
        from luvbox.qt_library import LibraryRepository, _Request

        paths = resolve_storage(self.temp_dir / "RelabelScan", "offline")
        illustration = np.ascontiguousarray(get_category_embeddings()[0])
        blank = np.zeros(512, dtype=np.float32)
        taught = np.zeros(512, dtype=np.float32)
        taught[1] = 1
        with closing(HistoryStore.for_storage(paths)) as store:
            specs = [
                ("1", illustration.tobytes()),
                ("2", illustration.tobytes()),
                ("3", blank.tobytes()),
                ("4", illustration.tobytes()),
                ("5", taught.tobytes()),
            ]
            with store._db:
                for tweet_id, _blob in specs:
                    store._db.execute(
                        "INSERT INTO media(folder_mode, tweet_id, num, media_type, filename, path, status, "
                        "record_source, updated_at) VALUES ('flat', ?, 1, 'photo', ?, ?, 'saved', 'download', "
                        "'2026-11-10 10:00:00')",
                        (tweet_id, f"p{tweet_id}.jpg", f"C:/test/p{tweet_id}.jpg"),
                    )
            store.save_embeddings([("flat", tweet_id, 1, "photo", blob, 0.8) for tweet_id, blob in specs])
            store.save_ai_tags([("flat", "2", 1, "photo", "풍경")])
            self.assertEqual(store.clear_ai_tags(), 1)
            # A tag the user removed on purpose stays removed. Clearing the
            # library is what makes a stored embedding eligible again.
            store.save_ai_tags([("flat", "4", 1, "photo", "")])
            self.assertEqual(store.set_taught_categories([("flat", "5", 1, "photo")], "인물"), 1)

        progress = []
        repo = LibraryRepository()
        self.addCleanup(repo.configure, None)
        repo.configure(_Request(
            generation=1, account="offline", output_dir=str(paths.output_dir), separate_accounts=False,
        ))
        with patch("luvbox.clip_classifier.get_session", side_effect=AssertionError("vision model")), \
             patch("luvbox.clip_classifier.release_session"):
            applied = repo.scan_ai_tags(1, progress_callback=lambda cur, tot: progress.append((cur, tot)))
        self.assertEqual(applied, 2)
        self.assertIn((0, 3), progress)
        self.assertEqual(progress[-1], (3, 3))
        self.assertLess(progress[0][0], progress[-1][0])
        with closing(HistoryStore.for_storage(paths)) as store:
            self.assertEqual(store.classification_of("flat", "1", 1, "photo")["category"], "일러스트")
            self.assertEqual(store.classification_of("flat", "2", 1, "photo")["category"], "일러스트")
            self.assertFalse(store.classification_of("flat", "2", 1, "photo")["confirmed"])
            self.assertEqual(store.classification_of("flat", "3", 1, "photo")["category"], "")
            self.assertEqual(store.item_tags("flat", "4", 1, "photo"), [])
            self.assertEqual(store.classification_of("flat", "4", 1, "photo")["category"], "")
            taught_label = store.classification_of("flat", "5", 1, "photo")
            self.assertEqual(taught_label["category"], "인물")
            self.assertTrue(taught_label["confirmed"])
            self.assertEqual(len(store.embeddings_without_labels()), 0)

    def test_scan_ai_inference_error_does_not_mark_photos_classified(self):
        from unittest.mock import patch
        from contextlib import closing
        from luvbox.storage import resolve_storage
        from luvbox.qt_library import LibraryRepository, _Request

        paths = resolve_storage(self.temp_dir / "InferFail", "offline")
        with closing(HistoryStore.for_storage(paths)) as store:
            p1 = paths.output_dir / "u_301_1.jpg"
            Image.new("RGB", (32, 32), color="red").save(p1)
            store.record_media("flat", {"tweet_id": "301", "num": 1, "type": "photo", "author": "u"}, p1)

        repo = LibraryRepository()
        self.addCleanup(repo.configure, None)
        req = _Request(generation=1, account="offline", output_dir=str(paths.output_dir), separate_accounts=False)
        repo.configure(req)
        with patch("luvbox.clip_classifier.is_available", return_value=True), \
             patch("luvbox.clip_classifier.get_session", return_value=object()), \
             patch("luvbox.clip_classifier.classify_detailed", side_effect=RuntimeError("inference failed")), \
             patch("luvbox.clip_classifier.release_session") as release:
            with self.assertRaises(RuntimeError) as ctx:
                repo.scan_ai_tags(1)
            self.assertIn("이어서 분류", str(ctx.exception))
            release.assert_called()
        with closing(HistoryStore.for_storage(paths)) as store:
            self.assertEqual(len(store.unclassified_photo_records()), 1)
            self.assertEqual(store.item_tags("flat", "301", 1, "photo"), [])

    def test_tag_text_drops_control_characters(self):
        self.assertTrue(self.store.add_tag("flat", "111", 1, "photo", "배경\n화면\t"))
        self.assertEqual(
            [t["tag"] for t in self.store.item_tags("flat", "111", 1, "photo")],
            ["배경화면"],
        )
        self.assertFalse(self.store.add_tag("flat", "111", 1, "photo", "\n\t"))


class FeedbackLearningTests(unittest.TestCase):
    def test_one_taught_example_relabels_only_a_near_copy(self):
        from luvbox.clip_classifier import neighbor_decision, neighbor_threshold
        from luvbox.qt_library import _propagate_neighbors

        self.assertGreater(neighbor_threshold(1), neighbor_threshold(25))
        person = np.zeros(512, dtype=np.float32)
        person[0] = 1
        other = np.zeros(512, dtype=np.float32)
        other[1] = 1
        groups = {"인물": np.stack([person])}
        self.assertEqual(neighbor_decision(person, groups)[0], "인물")
        self.assertIsNone(neighbor_decision(other, groups))

        with tempfile.TemporaryDirectory() as td:
            store = HistoryStore(Path(td))
            blob = person.tobytes()
            far = other.tobytes()
            store.save_embeddings([
                ("flat", "1", 1, "photo", blob, 0.9),
                ("flat", "2", 1, "photo", blob, 0.4),
                ("flat", "3", 1, "photo", far, 0.4),
            ])
            store.save_ai_tags([
                ("flat", "2", 1, "photo", "풍경"),
                ("flat", "3", 1, "photo", "풍경"),
            ])
            self.assertEqual(store.set_taught_categories([("flat", "1", 1, "photo")], "인물"), 1)
            self.assertEqual(_propagate_neighbors(store), 1)
            taught = store.classification_of("flat", "1", 1, "photo")
            near = store.classification_of("flat", "2", 1, "photo")
            far_photo = store.classification_of("flat", "3", 1, "photo")
            self.assertTrue(taught["confirmed"])
            self.assertEqual(taught["category"], "인물")
            self.assertEqual(near["category"], "인물")
            self.assertFalse(near["confirmed"])
            self.assertEqual(far_photo["category"], "풍경")
            self.assertEqual(store.user_tags(), [])
            store.close()

    def test_neighbor_batch_matches_single_decision(self):
        from luvbox.clip_classifier import neighbor_decision, neighbor_decisions

        person = np.zeros(512, dtype=np.float32)
        person[0] = 1
        other = np.zeros(512, dtype=np.float32)
        other[1] = 1
        groups = {"인물": np.stack([person]), "": np.stack([other])}
        rows = np.stack([person, other, np.zeros(512, dtype=np.float32)])
        batched = neighbor_decisions(rows, groups)
        for row, decision in zip(rows, batched):
            single = neighbor_decision(row, groups)
            if decision is None:
                self.assertIsNone(single)
            else:
                self.assertEqual(decision[0], single[0])
                self.assertAlmostEqual(decision[1], single[1], places=5)
        self.assertEqual(batched[0][0], "인물")
        self.assertEqual(batched[1][0], "")
        self.assertIsNone(batched[2])


class ScanShortcutTests(unittest.TestCase):
    def test_unreadable_file_is_not_opened_again_until_it_changes(self):
        import os
        from contextlib import closing
        from unittest.mock import patch
        from luvbox.qt_library import LibraryRepository, _Request
        from luvbox.storage import resolve_storage

        paths = resolve_storage(Path(tempfile.mkdtemp()) / "Unread", "offline")
        with closing(HistoryStore.for_storage(paths)) as store:
            bad = paths.output_dir / "u_401_1.jpg"
            bad.write_bytes(b"this is not a photo")
            store.record_media("flat", {"tweet_id": "401", "num": 1, "type": "photo", "author": "u"}, bad)
        repo = LibraryRepository()
        self.addCleanup(repo.configure, None)
        repo.configure(_Request(
            generation=1, account="offline", output_dir=str(paths.output_dir), separate_accounts=False,
        ))
        with patch("luvbox.clip_classifier.is_available", return_value=True), \
             patch("luvbox.clip_classifier.get_session", return_value=object()) as session, \
             patch("luvbox.clip_classifier.release_session"):
            self.assertEqual(repo.scan_ai_tags(1), 0)
            self.assertEqual(session.call_count, 1)
            self.assertIn("읽을 수 없는 사진", repo._scan_note)
            self.assertEqual(repo.scan_ai_tags(1), 0)
            self.assertEqual(session.call_count, 1)
            os.utime(bad, ns=(200_000_000_000, 200_000_000_000))
            self.assertEqual(repo.scan_ai_tags(1), 0)
            self.assertEqual(session.call_count, 2)

    def test_locked_file_is_retried(self):
        from contextlib import closing
        from unittest.mock import patch
        from luvbox.qt_library import LibraryRepository, _Request
        from luvbox.storage import resolve_storage

        paths = resolve_storage(Path(tempfile.mkdtemp()) / "Locked", "offline")
        with closing(HistoryStore.for_storage(paths)) as store:
            bad = paths.output_dir / "u_402_1.jpg"
            bad.write_bytes(b"this is not a photo")
            store.record_media("flat", {"tweet_id": "402", "num": 1, "type": "photo", "author": "u"}, bad)
        repo = LibraryRepository()
        self.addCleanup(repo.configure, None)
        repo.configure(_Request(
            generation=1, account="offline", output_dir=str(paths.output_dir), separate_accounts=False,
        ))
        with patch("luvbox.clip_classifier.is_available", return_value=True), \
             patch("luvbox.clip_classifier.get_session", return_value=object()) as session, \
             patch("luvbox.clip_classifier.release_session"), \
             patch("luvbox.clip_classifier.encode_skip_signature", return_value=None):
            repo.scan_ai_tags(1)
            repo.scan_ai_tags(1)
        self.assertEqual(session.call_count, 2)
        with closing(HistoryStore.for_storage(paths)) as store:
            self.assertEqual(store.unreadable_signatures(), {})

    def test_teach_uses_the_selected_rows_and_reports_progress(self):
        from contextlib import closing
        from unittest.mock import patch
        from luvbox.qt_library import LibraryRepository, _Request
        from luvbox.storage import resolve_storage

        paths = resolve_storage(Path(tempfile.mkdtemp()) / "Teach", "offline")
        vector = np.zeros(512, dtype=np.float32)
        vector[0] = 1
        with closing(HistoryStore.for_storage(paths)) as store:
            image = paths.output_dir / "u_403_1.jpg"
            Image.new("RGB", (8, 8), color="red").save(image)
            store.record_media("flat", {"tweet_id": "403", "num": 1, "type": "photo", "author": "u"}, image)
            store.save_embeddings([("flat", "403", 1, "photo", vector.tobytes(), 0.9)])
            for index in range(30):
                store._db.execute(
                    "INSERT INTO media(folder_mode, tweet_id, num, media_type, filename, path, status, "
                    "record_source, updated_at) VALUES ('flat', ?, 1, 'photo', ?, ?, 'saved', 'download', ?)",
                    (str(500 + index), f"extra{index}.jpg", f"C:/unused/{index}.jpg", "2026-11-10 10:00:00"),
                )
            store._db.commit()
        repo = LibraryRepository()
        self.addCleanup(repo.configure, None)
        repo.configure(_Request(
            generation=1, account="offline", output_dir=str(paths.output_dir), separate_accounts=False,
        ))
        progress = []
        with patch.object(HistoryStore, "photo_records_for_scan", side_effect=AssertionError("full photo scan")):
            taught, propagated = repo.teach_labels(
                1, [("flat", "403", 1, "photo")], "인물",
                progress_callback=lambda current, total: progress.append((current, total)),
            )
        self.assertEqual((taught, propagated), (1, 0))
        self.assertEqual(progress[0], (0, 1))
        self.assertEqual(progress[-1], (1, 1))
        with closing(HistoryStore.for_storage(paths)) as store:
            learned = store.classification_of("flat", "403", 1, "photo")
            self.assertEqual(learned["category"], "인물")
            self.assertTrue(learned["confirmed"])

    def test_merge_and_rebuild_keep_embeddings_and_teaching(self):
        import sqlite3
        from contextlib import closing
        from luvbox.recovery import rebuild_history
        from luvbox.storage import _merge_history

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = HistoryStore(root / "src")
            blob = bytes([index % 251 for index in range(2048)])
            source.save_embeddings([("flat", "1", 1, "photo", blob, 0.5)])
            source.set_taught_categories([("flat", "1", 1, "photo")], "동물")
            self.assertEqual(source.remember_unreadable([("flat", "2", 1, "photo", 12, 99)]), 1)
            with source._db:
                source._db.execute(
                    "UPDATE ai_embeddings SET updated_at=? WHERE tweet_id='1'",
                    ("2026-11-10T12:00:00+09:00",),
                )
            source.close()
            target = HistoryStore(root / "dst")
            older = b"\x02" * 2048
            target.save_embeddings([("flat", "1", 1, "photo", older, 0.1)])
            with target._db:
                target._db.execute(
                    "UPDATE ai_embeddings SET updated_at=? WHERE tweet_id='1'",
                    ("2020-01-01T00:00:00+09:00",),
                )
            target.close()
            with closing(sqlite3.connect(root / "dst" / ".luvbox-history.sqlite3")) as dst, \
                 closing(sqlite3.connect(root / "src" / ".luvbox-history.sqlite3")) as src:
                with dst:
                    _merge_history(dst, src)
            with closing(HistoryStore(root / "dst")) as merged:
                embeddings, feedback, _estimated = merged.learning_state()
                self.assertEqual(embeddings[("flat", "1", 1, "photo")][0], blob)
                self.assertEqual(feedback[("flat", "1", 1, "photo")], "동물")
                self.assertEqual(merged.unreadable_signatures()[("flat", "2", 1, "photo")], (12, 99))
            rebuilt_dir = root / "rebuilt"
            rebuilt_dir.mkdir()
            rebuild_history(root / "src" / ".luvbox-history.sqlite3", rebuilt_dir / ".luvbox-history.sqlite3")
            with closing(HistoryStore(rebuilt_dir)) as rebuilt:
                embeddings, feedback, _estimated = rebuilt.learning_state()
                self.assertEqual(embeddings[("flat", "1", 1, "photo")][0], blob)
                self.assertEqual(feedback[("flat", "1", 1, "photo")], "동물")
                self.assertEqual(rebuilt.unreadable_signatures()[("flat", "2", 1, "photo")], (12, 99))

    def test_ai_tag_stats_and_replace_estimated_labels_batch(self):
        from contextlib import closing
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with closing(HistoryStore(root)) as store:
                with store._db:
                    store._db.execute(
                        "INSERT INTO media(folder_mode, tweet_id, num, media_type, filename, path, status, record_source, updated_at) "
                        "VALUES ('flat', '901', 1, 'photo', 'p1.jpg', 'C:/test/p1.jpg', 'saved', 'download', '2026-11-10')"
                    )
                    store._db.execute(
                        "INSERT INTO media(folder_mode, tweet_id, num, media_type, filename, path, status, record_source, updated_at) "
                        "VALUES ('flat', '902', 1, 'photo', 'p2.jpg', 'C:/test/p2.jpg', 'saved', 'download', '2026-11-10')"
                    )
                # Duplicate identity in one batch: last label wins
                changed = store.replace_estimated_labels([
                    ("flat", "901", 1, "photo", "일러스트"),
                    ("flat", "901", 1, "photo", "동물"),
                ])
                self.assertEqual(changed, 2)
                self.assertEqual(store.classification_of("flat", "901", 1, "photo")["category"], "동물")

                # Taught category is included in ai_tag_stats
                store.set_taught_categories([("flat", "902", 1, "photo")], "인물")
                self.assertEqual(store.ai_tag_stats(), {"동물": 1, "인물": 1})

                # Deleted media (path='') is excluded from ai_tag_stats
                with store._db:
                    store._db.execute("UPDATE media SET path='' WHERE tweet_id='901'")
                self.assertEqual(store.ai_tag_stats(), {"인물": 1})

    def test_scan_ai_tags_cancellation(self):
        from unittest.mock import patch
        from contextlib import closing
        from luvbox.storage import resolve_storage
        from luvbox.qt_library import LibraryRepository, _Request
        from luvbox.duplicates import ScanCancelled

        with tempfile.TemporaryDirectory() as td:
            paths = resolve_storage(Path(td) / "RepoCancel", "offline")
            with closing(HistoryStore.for_storage(paths)) as store:
                p1 = paths.output_dir / "u_201_1.jpg"
                Image.new("RGB", (32, 32), color="green").save(p1)
                store.record_media("flat", {"tweet_id": "201", "num": 1, "type": "photo", "author": "u"}, p1)

            repo = LibraryRepository()
            self.addCleanup(repo.configure, None)
            req = _Request(generation=1, account="offline", output_dir=str(paths.output_dir), separate_accounts=False)
            repo.configure(req)

            # Trigger cancel before/during scan
            repo.cancel_ai_scan()
            with self.assertRaises(ScanCancelled) as ctx:
                repo.scan_ai_tags(1)
            self.assertIn("중단", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
