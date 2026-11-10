"""Unit tests for AI CLIP image classification engine and UI/DB integration."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from luvbox.clip_classifier import (
    CATEGORY_NAMES,
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

        # 6. user_tags (combines all tags for gallery dropdown)
        all_tags = self.store.user_tags()
        tag_names = {t["tag"] for t in all_tags}
        self.assertIn("내최애", tag_names)
        self.assertIn("일러스트", tag_names)

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
        self.assertEqual([t["tag"] for t in self.store.user_tags()], ["동물"])

        # User manually removes AI tag '동물' from p1 -> sentinel preserved so incremental scan won't re-add it
        self.assertTrue(self.store.remove_tag("flat", "111", 1, "photo", "동물"))
        self.assertEqual(self.store.item_tags("flat", "111", 1, "photo"), [])
        self.assertEqual(self.store.unclassified_photo_records(), [])

        # clear_ai_tags removes all AI tags and sentinels so full re-classification is possible
        cleared = self.store.clear_ai_tags()
        self.assertEqual(cleared, 0)
        self.assertEqual(len(self.store.unclassified_photo_records()), 2)

    def test_add_tags_batch_and_model_cache(self):
        added = self.store.add_tags_batch([
            ("flat", "111", 1, "photo"),
            ("flat", "222", 1, "photo"),
        ], "#배치태그", source="user")
        self.assertEqual(added, 2)
        self.assertEqual(self.store.user_tags(), [{"tag": "배치태그", "count": 2}])


if __name__ == "__main__":
    unittest.main()
