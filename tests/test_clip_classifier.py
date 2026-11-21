"""The SigLIP 2 image encoder, its zero-shot scores, and the catalog side of AI tags."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox import clip_classifier
from luvbox.clip_classifier import (
    CATEGORY_NAMES,
    categories_from_embeddings,
    classify_batch,
    classify_image,
    encode_batch,
    feature_output,
    get_category_embeddings,
    is_available,
    is_model_ready,
    preprocess_image,
    release_session,
    resolve_model_path,
    zero_shot_probabilities,
)
from luvbox.clip_model import ACTIVE_MODEL_ID, LEGACY_MODEL_ID
from luvbox.history import HistoryStore

DIM = 768


class Output:
    def __init__(self, name, shape):
        self.name, self.shape = name, shape


class FakeSession:
    """An ONNX session whose first output is the patch tokens, as the real export's is."""

    def __init__(self, outputs=None, vector=None):
        self.outputs = outputs or [Output("last_hidden_state", ["batch", 196, DIM]),
                                   Output("pooler_output", ["batch", DIM])]
        self.vector = vector
        self.calls = []

    def get_outputs(self):
        return self.outputs

    def run(self, names, feeds):
        self.calls.append((names, feeds["pixel_values"].shape))
        batch = feeds["pixel_values"].shape[0]
        values = {"last_hidden_state": np.ones((batch, 196, DIM), np.float32)}
        base = self.vector if self.vector is not None else np.arange(DIM, dtype=np.float32) + 1
        for output in self.outputs:
            if output.name != "last_hidden_state":
                values[output.name] = np.stack([base * (index + 2) for index in range(batch)])
        return [values[name] for name in names]


class ModelTests(unittest.TestCase):
    def test_the_weights_are_pinned_to_one_upstream_commit(self):
        self.assertTrue(clip_classifier.MODEL_URL.startswith("https://huggingface.co/onnx-community/"
                                                             "siglip2-base-patch16-224-ONNX/resolve/"))
        self.assertIn(clip_classifier.MODEL_COMMIT, clip_classifier.MODEL_URL)
        self.assertTrue(clip_classifier.MODEL_URL.endswith("/onnx/vision_model_int8.onnx"))
        self.assertEqual(clip_classifier.MODEL_SIZE, 94553333)
        self.assertEqual(len(clip_classifier.MODEL_SHA256), 64)
        self.assertEqual(clip_classifier.MODEL_FILENAME, "siglip2_base_patch16_224_vision.onnx")
        self.assertEqual(clip_classifier.MAX_DOWNLOAD, 120 * 1024 * 1024)
        self.assertEqual(clip_classifier.default_cache_dir().name, "siglip2")

    def test_the_anchors_are_768_wide_unit_vectors_each_scoring_its_own_tag_best(self):
        anchors = get_category_embeddings()
        self.assertEqual((anchors.shape, anchors.dtype), ((5, DIM), np.float32))
        for norm in np.linalg.norm(anchors, axis=1):
            self.assertAlmostEqual(float(norm), 1.0, places=4)
        self.assertEqual(hashlib.sha256(anchors.tobytes()).hexdigest()[:16], "e8ca3c6b91c5e60f")
        self.assertEqual(CATEGORY_NAMES, ["일러스트", "인물", "동물", "짤·텍스트", "풍경·배경"])
        scored = categories_from_embeddings(anchors)
        self.assertEqual([name for name, _confidence in scored], CATEGORY_NAMES)

    def test_zero_shot_scores_are_independent_per_tag_and_land_on_the_threshold_at_each_cut(self):
        anchors = get_category_embeddings().astype(np.float64)
        probabilities = zero_shot_probabilities(anchors)
        self.assertEqual(probabilities.shape, (5, 5))
        # A photo exactly at the cut of a tag scores the common threshold for it.
        rng = np.random.default_rng(1)
        photo = rng.normal(size=DIM)
        photo -= anchors.T @ np.linalg.lstsq(anchors.T, photo, rcond=None)[0]
        photo /= np.linalg.norm(photo)
        for index, cut in enumerate(clip_classifier.ZERO_SHOT_CUTS):
            # Move the photo along the anchor until its relative similarity is the cut.
            relative = lambda v: float((anchors @ v)[index] - (anchors @ v).mean())
            low, high = -1.0, 1.0
            for _step in range(60):
                middle = (low + high) / 2
                v = photo + middle * anchors[index]
                v /= np.linalg.norm(v)
                low, high = (middle, high) if relative(v) < cut else (low, middle)
            score = float(zero_shot_probabilities(v)[0, index])
            self.assertAlmostEqual(score, clip_classifier.ZERO_SHOT_THRESHOLD, places=3)
        self.assertEqual(zero_shot_probabilities(np.zeros((2, 512), np.float32)).shape, (0, 5))
        blank = categories_from_embeddings(np.zeros(DIM, dtype=np.float32))
        self.assertIsNone(blank[0][0])
        self.assertEqual(categories_from_embeddings(np.zeros(512, dtype=np.float32)), [])

    def test_model_resolution_and_signature_cache(self):
        path = resolve_model_path()
        self.assertIsInstance(path, Path)
        if path.is_file():
            self.assertTrue(is_model_ready(path))
            with patch("builtins.open", side_effect=AssertionError("Should not re-read model on cached signature")):
                self.assertTrue(is_model_ready(path))
            self.assertTrue(is_available())

    def test_the_pooled_feature_is_chosen_by_name_not_position(self):
        self.assertEqual(feature_output(FakeSession()), "pooler_output")
        self.assertEqual(feature_output(FakeSession([Output("last_hidden_state", [1, 196, DIM]),
                                                     Output("image_embeds", [1, DIM])])), "image_embeds")
        self.assertEqual(feature_output(FakeSession([Output("tokens", [1, 196, DIM]),
                                                     Output("features", ["batch", DIM])])), "features")
        with self.assertRaises(RuntimeError):
            feature_output(FakeSession([Output("tokens", [1, 196, DIM])]))

    def test_a_batch_comes_back_normalized_and_only_the_pooled_output_is_asked_for(self):
        session = FakeSession()
        tensors = np.zeros((3, 3, 224, 224), dtype=np.float32)
        embeds = encode_batch(tensors, session)
        self.assertEqual(embeds.shape, (3, DIM))
        np.testing.assert_allclose(np.linalg.norm(embeds, axis=1), 1.0, rtol=1e-5)
        self.assertEqual(session.calls, [(["pooler_output"], (3, 3, 224, 224))])
        wrong = FakeSession([Output("pooler_output", ["batch", DIM])], vector=np.ones(512, np.float32))
        with self.assertRaises(RuntimeError):
            encode_batch(tensors, wrong)


class OldModelTests(unittest.TestCase):
    """The CLIP model of 1.11 and earlier (about 85 MB) goes once the SigLIP 2 model is ready."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="luvbox-old-model-")
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.base)})
        environment.start()
        self.addCleanup(environment.stop)
        self.old = self.base / "LuvBox" / "models" / "clip"

    def ready(self):
        with patch("luvbox.clip_classifier.is_model_ready", return_value=True):
            clip_classifier.ensure_model(self.base / "LuvBox" / "models" / "siglip2" / "model.onnx")

    def test_the_old_model_and_its_unfinished_downloads_go_with_the_empty_folder(self):
        self.old.mkdir(parents=True)
        (self.old / "clip_vit_b32_vision.onnx").write_bytes(b"x" * 64)
        (self.old / "clip_download_1a2b.tmp").write_bytes(b"x")
        self.ready()
        self.assertFalse(self.old.exists())

    def test_anything_else_in_that_folder_stays(self):
        self.old.mkdir(parents=True)
        (self.old / "clip_vit_b32_vision.onnx").write_bytes(b"x" * 64)
        (self.old / "notes.txt").write_text("mine", encoding="utf-8")
        self.ready()
        self.assertEqual(sorted(path.name for path in self.old.iterdir()), ["notes.txt"])

    @unittest.skipUnless(os.name == "nt", "Windows junctions")
    def test_a_folder_reached_through_a_junction_is_left_alone(self):
        import _winapi

        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "clip_vit_b32_vision.onnx").write_bytes(b"x" * 64)
        self.old.parent.mkdir(parents=True)
        _winapi.CreateJunction(str(elsewhere), str(self.old))
        self.ready()
        self.assertTrue((elsewhere / "clip_vit_b32_vision.onnx").exists())


class PreprocessTests(unittest.TestCase):
    def test_a_picture_is_squeezed_whole_to_224_and_scaled_to_minus_one_one(self):
        with tempfile.TemporaryDirectory() as td:
            tall = Path(td) / "tall.png"
            picture = Image.new("RGB", (100, 1000), (255, 0, 0))
            picture.paste((0, 0, 255), (0, 900, 100, 1000))        # the bottom tenth is blue
            picture.save(tall)
            tensor = preprocess_image(tall)
            self.assertEqual((tensor.shape, tensor.dtype), ((3, 224, 224), np.float32))
            self.assertAlmostEqual(float(tensor.max()), 1.0, places=5)
            self.assertAlmostEqual(float(tensor.min()), -1.0, places=5)
            # Nothing is cut: the blue band at the bottom is still there (a square crop drops it).
            self.assertAlmostEqual(float(tensor[2, 220, 112]), 1.0, places=5)
            self.assertAlmostEqual(float(tensor[0, 220, 112]), -1.0, places=5)
            self.assertAlmostEqual(float(tensor[0, 10, 112]), 1.0, places=5)

    def test_preprocess_security_and_bounds(self):
        with tempfile.TemporaryDirectory() as td:
            temp_dir = Path(td)
            self.assertIsNone(preprocess_image(temp_dir / "missing.jpg"))
            img_path = temp_dir / "sample.jpg"
            Image.new("RGB", (300, 200), color=(128, 64, 32)).save(img_path)
            self.assertEqual(preprocess_image(img_path).shape, (3, 224, 224))
            rgba = temp_dir / "tall_rgba.png"
            Image.new("RGBA", (40, 2000), color=(10, 120, 240, 200)).save(rgba)
            self.assertEqual(preprocess_image(rgba).shape, (3, 224, 224))
            with patch("luvbox.clip_classifier.MAX_PIXELS", 50):
                self.assertIsNone(preprocess_image(img_path))
            with patch("luvbox.clip_classifier.MAX_RASTER_BYTES", 10):
                self.assertIsNone(preprocess_image(img_path))
            bad = temp_dir / "bad.jpg"
            bad.write_bytes(b"this is not a photo")
            self.assertIsNone(preprocess_image(bad))

    def test_encode_skip_signature_keeps_real_photos(self):
        from luvbox.clip_classifier import encode_skip_signature

        with tempfile.TemporaryDirectory() as td:
            good = Path(td) / "ok.jpg"
            bad = Path(td) / "bad.jpg"
            Image.new("RGB", (8, 8), color="blue").save(good)
            bad.write_bytes(b"this is not a photo")
            self.assertIsNone(encode_skip_signature(good))
            self.assertEqual(encode_skip_signature(bad), (bad.stat().st_size, bad.stat().st_mtime_ns))

    def test_classify_with_a_session_encodes_and_names_the_best_default_tag(self):
        anchor = get_category_embeddings()[1].astype(np.float32)        # 인물
        session = FakeSession(vector=anchor)
        with tempfile.TemporaryDirectory() as td:
            one, two = Path(td) / "1.png", Path(td) / "2.png"
            Image.new("RGB", (64, 48), (200, 100, 50)).save(one)
            two.write_bytes(b"broken")
            detailed = clip_classifier.classify_detailed([one, two], session=session)
            self.assertEqual(detailed[0][0], "인물")
            self.assertGreater(detailed[0][1], clip_classifier.ZERO_SHOT_THRESHOLD)
            self.assertEqual(detailed[0][2].shape, (DIM,))
            self.assertEqual(detailed[1], (None, 0.0, None))
            self.assertEqual(classify_batch([one], session=session)[0][0], "인물")
            self.assertEqual(classify_image(one, threshold=0.999, session=session)[0], None)

    @unittest.skipUnless(os.environ.get("LUVBOX_SIGLIP_MODEL"), "set LUVBOX_SIGLIP_MODEL to the real ONNX file")
    def test_the_real_model_encodes_photos(self):
        session = clip_classifier.get_session(Path(os.environ["LUVBOX_SIGLIP_MODEL"]))
        self.assertIsNotNone(session)
        with tempfile.TemporaryDirectory() as td:
            first, second = Path(td) / "a.png", Path(td) / "b.png"
            Image.new("RGB", (256, 256), (255, 200, 200)).save(first)
            Image.new("RGB", (320, 240), (50, 150, 50)).save(second)
            detailed = clip_classifier.classify_detailed([first, second], batch_size=2, session=session)
            for _tag, confidence, vector in detailed:
                self.assertEqual(vector.shape, (DIM,))
                self.assertAlmostEqual(float(np.linalg.norm(vector)), 1.0, places=4)
                self.assertTrue(0.0 <= confidence <= 1.0)
        release_session()


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.store = HistoryStore(Path(self.td.name))
        self.addCleanup(self.store.close)
        with self.store._db:
            for tweet in ("111", "222", "333"):
                self.store._db.execute(
                    "INSERT INTO media(folder_mode, tweet_id, num, media_type, filename, path, status, record_source, "
                    "updated_at) VALUES ('flat', ?, 1, 'photo', ?, ?, 'saved', 'download', '2026-11-10 10:00:00')",
                    (tweet, f"p{tweet}.jpg", f"C:/test/p{tweet}.jpg"))

    def test_ai_tags_crud_keeps_typed_tags_apart(self):
        self.assertEqual(len(self.store.unclassified_photo_records()), 3)
        self.store.add_tag("flat", "111", 1, "photo", "내최애", source="user")
        self.assertEqual(len(self.store.unclassified_photo_records()), 3)
        self.assertEqual(self.store.save_ai_tags([("flat", "111", 1, "photo", "일러스트")]), 1)
        self.assertEqual([r["tweet_id"] for r in self.store.unclassified_photo_records()], ["222", "333"])
        sources = {t["source"]: t["tag"] for t in self.store.item_tags("flat", "111", 1, "photo")}
        self.assertEqual(sources, {"user": "내최애", "ai": "일러스트"})
        self.assertEqual(self.store.ai_tag_stats(), {"일러스트": 1})
        self.assertNotIn("일러스트", {t["tag"] for t in self.store.user_tags()})
        self.assertEqual(self.store.clear_ai_tags(), 1)
        self.assertEqual(self.store.item_tags("flat", "111", 1, "photo"), [{"tag": "내최애", "source": "user"}])

    def test_features_of_the_old_and_the_new_model_never_mix(self):
        new = np.ones(DIM, np.float32).tobytes()
        self.assertEqual(self.store.save_embeddings([("flat", "111", 1, "photo", new, 0.8),
                                                     ("flat", "222", 1, "photo", b"\0" * 2048, 0.5)]), 1)
        self.assertEqual(self.store.save_embeddings([("flat", "222", 1, "photo", b"\0" * 2048, 0.5)],
                                                    LEGACY_MODEL_ID), 1)
        self.assertEqual(self.store.save_embeddings([("flat", "333", 1, "photo", new, 0.5)], "unknown"), 0)
        # Whether the file is there is the scan's business; the catalog lists what has no current feature.
        self.assertEqual([r["tweet_id"] for r in self.store.photos_without_embeddings()], ["222", "333"])
        self.assertEqual(set(self.store.embeddings_for([("flat", t, 1, "photo") for t in ("111", "222")])),
                         {("flat", "111", 1, "photo")})
        self.assertEqual(set(self.store.embeddings_for([("flat", "222", 1, "photo")], LEGACY_MODEL_ID)),
                         {("flat", "222", 1, "photo")})
        self.assertEqual(self.store.count_embeddings(), 1)
        self.assertEqual(self.store.count_embeddings(LEGACY_MODEL_ID), 1)
        rows = [row for batch in self.store.iter_embeddings(ACTIVE_MODEL_ID) for row in batch]
        self.assertEqual([row["tweet_id"] for row in rows], ["111"])
        # Encoding the old photo again replaces its feature and its model.
        self.store.save_embeddings([("flat", "222", 1, "photo", new, 0.9)])
        self.assertEqual(self.store.count_embeddings(LEGACY_MODEL_ID), 0)
        self.assertEqual(self.store.count_embeddings(), 2)

    def test_the_empty_ai_row_marks_a_photo_as_looked_at(self):
        self.store.save_embeddings([("flat", t, 1, "photo", np.ones(DIM, np.float32).tobytes(), 0.5)
                                    for t in ("111", "222")])
        self.assertEqual(self.store.count_embeddings_without_labels(), 2)
        self.assertEqual(self.store.replace_ai_tags([("flat", "111", 1, "photo", ["동물"]),
                                                     ("flat", "222", 1, "photo", [])]), (2, 1))
        self.assertEqual(self.store.count_embeddings_without_labels(), 0)
        self.assertEqual(self.store.item_tags("flat", "222", 1, "photo"), [])
        self.assertEqual(self.store.ai_tag_stats(), {"동물": 1})
        # Taking the AI tag away says "not this"; the photo stays looked at.
        self.assertTrue(self.store.remove_tag("flat", "111", 1, "photo", "동물"))
        self.assertEqual(self.store.tag_feedback(), {("flat", "111", 1, "photo"): {"동물": -1}})
        self.assertEqual(self.store.count_embeddings_without_labels(), 0)
        # Clearing AI tags makes every stored feature eligible again.
        self.assertEqual(self.store.clear_ai_tags(), 0)
        self.assertEqual(self.store.count_embeddings_without_labels(), 2)
        self.assertEqual(self.store.ai_scored_state(), ("", ""))

    def test_an_estimate_never_overrides_what_the_person_decided(self):
        ident = ("flat", "111", 1, "photo")
        self.store.set_tag_feedback([ident], "인물", True)
        self.store.set_tag_feedback([ident], "동물", False)
        self.store.add_tag(*ident, "고양이")
        changed, tagged = self.store.replace_ai_tags([(*ident, ["인물", "동물", "고양이", "일러스트"])])
        self.assertEqual((changed, tagged), (1, 1))
        self.assertCountEqual(self.store.item_tags(*ident), [{"tag": "인물", "source": "confirmed"},
                                                             {"tag": "고양이", "source": "user"},
                                                             {"tag": "일러스트", "source": "ai"}])
        self.assertEqual(self.store.replace_ai_tags([(*ident, ["일러스트"])]), (0, 0))
        self.assertEqual(self.store.replace_ai_tags([(*ident, [])]), (1, 0))
        self.assertEqual(self.store.classification_of(*ident),
                         {"category": "인물", "confirmed": True, "confidence": None})

    def test_none_of_these_turns_down_every_ai_tag_shown(self):
        ident = ("flat", "222", 1, "photo")
        self.store.replace_ai_tags([(*ident, ["일러스트", "짤·텍스트"])])
        self.assertEqual(self.store.set_taught_categories([ident], ""), 1)
        self.assertEqual(self.store.tag_feedback()[ident], {"일러스트": -1, "짤·텍스트": -1})
        self.assertEqual(self.store.ai_tags_of(*ident), [])
        self.assertTrue(self.store.classification_of(*ident)["confirmed"])
        self.assertEqual(self.store.set_taught_categories([ident], "인물"), 1)
        self.assertEqual(self.store.tag_feedback()[ident]["인물"], 1)

    def test_one_label_per_photo_from_older_callers_still_works(self):
        changed = self.store.replace_estimated_labels([("flat", "111", 1, "photo", "일러스트"),
                                                      ("flat", "111", 1, "photo", "동물")])
        self.assertEqual(changed, 2)
        self.assertEqual(self.store.classification_of("flat", "111", 1, "photo")["category"], "동물")

    def test_the_menu_counts_and_the_list_agree(self):
        self.store.replace_ai_tags([("flat", "111", 1, "photo", ["고양이"])])
        self.store.add_tag("flat", "222", 1, "photo", "고양이")
        self.store.set_tag_feedback([("flat", "333", 1, "photo")], "고양이", True)
        self.assertEqual(self.store.ai_tag_counts(["#고양이", "동물"]), {"고양이": 3})
        listed = [row["tweet_id"] for row in self.store.records(ai_category="고양이")]
        self.assertEqual(sorted(listed), ["111", "222", "333"])
        self.assertEqual([row["tweet_id"] for row in self.store.records(user_tag="고양이")], ["222"])
        self.assertEqual(self.store.records(ai_category="x" * 51), [])

    def test_tag_text_drops_control_characters(self):
        self.assertTrue(self.store.add_tag("flat", "111", 1, "photo", "배경\n화면\t"))
        self.assertEqual([t["tag"] for t in self.store.item_tags("flat", "111", 1, "photo")], ["배경화면"])
        self.assertFalse(self.store.add_tag("flat", "111", 1, "photo", "\n\t"))


class RepositoryScanTests(unittest.TestCase):
    """The scan with the vision model stubbed: fast paths, errors and progress."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "appdata")})
        environment.start()
        self.addCleanup(environment.stop)

    def library(self, name, count):
        from contextlib import closing
        from luvbox.qt_library import LibraryRepository, _Request
        from luvbox.storage import resolve_storage

        paths = resolve_storage(self.root / name, "offline")
        with closing(HistoryStore.for_storage(paths)) as store:
            for index in range(count):
                image = paths.output_dir / f"u_{200 + index}_1.jpg"
                Image.new("RGB", (16, 16), color="blue").save(image)
                store.record_media("flat", {"tweet_id": str(200 + index), "num": 1, "type": "photo", "author": "u"},
                                   image)
        repo = LibraryRepository()
        self.addCleanup(repo.configure, None)
        repo.configure(_Request(generation=1, account="offline", output_dir=str(paths.output_dir),
                                separate_accounts=False))
        return paths, repo

    def test_nothing_to_encode_or_score_never_loads_the_model(self):
        from contextlib import closing

        paths, repo = self.library("Fast", 1)
        with closing(HistoryStore.for_storage(paths)) as store:
            store.save_embeddings([("flat", "200", 1, "photo", np.ones(DIM, np.float32).tobytes(), 0.8)])
            store.replace_ai_tags([("flat", "200", 1, "photo", [])])
        repo.scan_ai_tags(1, download=False)               # scores once with the shared rules
        with patch("luvbox.clip_classifier.get_session") as session:
            self.assertEqual(repo.scan_ai_tags(1), 0)
            session.assert_not_called()

    def test_an_engine_that_does_not_start_is_reported(self):
        _paths, repo = self.library("Engine", 1)
        with patch("luvbox.clip_classifier.is_available", return_value=True), \
             patch("luvbox.clip_classifier.get_session", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                repo.scan_ai_tags(1)
            self.assertIn("AI 분류 기능을 시작하지 못했습니다", str(ctx.exception))

    def test_progress_advances_each_batch_and_features_are_stored(self):
        from contextlib import closing

        paths, repo = self.library("Progress", 20)
        vector = get_category_embeddings()[0].copy()

        def fake_detailed(image_paths, threshold=None, batch_size=16, session=None):
            return [("일러스트", 0.8, vector) for _path in image_paths]

        progress = []
        with patch("luvbox.clip_classifier.is_available", return_value=True), \
             patch("luvbox.clip_classifier.get_session", return_value=object()), \
             patch("luvbox.clip_classifier.classify_detailed", side_effect=fake_detailed), \
             patch("luvbox.clip_classifier.release_session"):
            applied = repo.scan_ai_tags(1, progress_callback=lambda cur, tot: progress.append((cur, tot)))
        self.assertIn((16, 20), progress)                  # scored as they are encoded
        self.assertEqual(progress[-1], (20, 20))
        self.assertEqual([total for _done, total in progress], [20] * len(progress))
        self.assertEqual(applied, 20)
        with closing(HistoryStore.for_storage(paths)) as store:
            embeddings, _feedback, estimated = store.learning_state()
            self.assertEqual(len(embeddings), 20)
            self.assertEqual(sum(1 for tags in estimated.values() if "일러스트" in tags), 20)
            self.assertEqual(store.ai_scored_state()[1], ACTIVE_MODEL_ID)

    def test_an_inference_error_does_not_mark_photos_classified(self):
        from contextlib import closing

        paths, repo = self.library("InferFail", 1)
        with patch("luvbox.clip_classifier.is_available", return_value=True), \
             patch("luvbox.clip_classifier.get_session", return_value=object()), \
             patch("luvbox.clip_classifier.classify_detailed", side_effect=RuntimeError("inference failed")), \
             patch("luvbox.clip_classifier.release_session") as release:
            with self.assertRaises(RuntimeError) as ctx:
                repo.scan_ai_tags(1)
            self.assertIn("다시 실행해 주세요", str(ctx.exception))
            release.assert_called()
        with closing(HistoryStore.for_storage(paths)) as store:
            self.assertEqual(len(store.unclassified_photo_records()), 1)
            self.assertEqual(store.item_tags("flat", "200", 1, "photo"), [])

    def test_an_unreadable_file_is_not_opened_again_until_it_changes(self):
        from contextlib import closing
        from luvbox.qt_library import LibraryRepository, _Request
        from luvbox.storage import resolve_storage

        paths = resolve_storage(self.root / "Unread", "offline")
        with closing(HistoryStore.for_storage(paths)) as store:
            bad = paths.output_dir / "u_401_1.jpg"
            bad.write_bytes(b"this is not a photo")
            store.record_media("flat", {"tweet_id": "401", "num": 1, "type": "photo", "author": "u"}, bad)
        repo = LibraryRepository()
        self.addCleanup(repo.configure, None)
        repo.configure(_Request(generation=1, account="offline", output_dir=str(paths.output_dir),
                                separate_accounts=False))
        with patch("luvbox.clip_classifier.is_available", return_value=True), \
             patch("luvbox.clip_classifier.get_session", return_value=FakeSession()) as session, \
             patch("luvbox.clip_classifier.release_session"):
            self.assertEqual(repo.scan_ai_tags(1), 0)
            self.assertEqual(session.call_count, 1)
            self.assertIn("읽을 수 없는 사진", repo._scan_note)
            self.assertEqual(repo.scan_ai_tags(1), 0)
            self.assertEqual(session.call_count, 1)
            os.utime(bad, ns=(200_000_000_000, 200_000_000_000))
            self.assertEqual(repo.scan_ai_tags(1), 0)
            self.assertEqual(session.call_count, 2)

    def test_a_locked_file_is_retried(self):
        from contextlib import closing

        paths, repo = self.library("Locked", 0)
        with closing(HistoryStore.for_storage(paths)) as store:
            bad = paths.output_dir / "u_402_1.jpg"
            bad.write_bytes(b"this is not a photo")
            store.record_media("flat", {"tweet_id": "402", "num": 1, "type": "photo", "author": "u"}, bad)
        with patch("luvbox.clip_classifier.is_available", return_value=True), \
             patch("luvbox.clip_classifier.get_session", return_value=FakeSession()) as session, \
             patch("luvbox.clip_classifier.release_session"), \
             patch("luvbox.clip_classifier.encode_skip_signature", return_value=None):
            repo.scan_ai_tags(1)
            repo.scan_ai_tags(1)
        self.assertEqual(session.call_count, 2)
        with closing(HistoryStore.for_storage(paths)) as store:
            self.assertEqual(store.unreadable_signatures(), {})

    def test_teaching_uses_the_selected_rows_and_reports_progress(self):
        from contextlib import closing

        paths, repo = self.library("Teach", 1)
        vector = get_category_embeddings()[1].copy()
        with closing(HistoryStore.for_storage(paths)) as store:
            store.save_embeddings([("flat", "200", 1, "photo", vector.tobytes(), 0.9)])
            for index in range(30):
                store._db.execute(
                    "INSERT INTO media(folder_mode, tweet_id, num, media_type, filename, path, status, "
                    "record_source, updated_at) VALUES ('flat', ?, 1, 'photo', ?, ?, 'saved', 'download', ?)",
                    (str(500 + index), f"extra{index}.jpg", f"C:/unused/{index}.jpg", "2026-11-10 10:00:00"))
            store._db.commit()
        progress = []
        with patch.object(HistoryStore, "photo_records_for_scan", side_effect=AssertionError("full photo scan")):
            taught, propagated = repo.teach_labels(1, [("flat", "200", 1, "photo")], "인물",
                                                   progress_callback=lambda current, total: progress.append(
                                                       (current, total)))
        self.assertEqual((taught, propagated), (1, 0))
        self.assertEqual((progress[0], progress[-1]), ((0, 1), (1, 1)))
        with closing(HistoryStore.for_storage(paths)) as store:
            learned = store.classification_of("flat", "200", 1, "photo")
            self.assertEqual((learned["category"], learned["confirmed"]), ("인물", True))

    def test_a_stop_asked_before_the_scan_ends_it(self):
        from luvbox.duplicates import ScanCancelled

        _paths, repo = self.library("Cancel", 1)
        repo.cancel_ai_scan()
        with self.assertRaises(ScanCancelled) as ctx:
            repo.scan_ai_tags(1)
        self.assertIn("중단", str(ctx.exception))


class MergeKeepsLearningTests(unittest.TestCase):
    def test_merge_and_rebuild_keep_features_and_teaching(self):
        import sqlite3
        from contextlib import closing
        from luvbox.recovery import rebuild_history
        from luvbox.storage import _merge_history

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = HistoryStore(root / "src")
            blob = np.arange(DIM, dtype=np.float32).tobytes()
            source.save_embeddings([("flat", "1", 1, "photo", blob, 0.5)])
            source.set_taught_categories([("flat", "1", 1, "photo")], "동물")
            self.assertEqual(source.remember_unreadable([("flat", "2", 1, "photo", 12, 99)]), 1)
            with source._db:
                source._db.execute("UPDATE ai_embeddings SET updated_at='2026-11-10T12:00:00+09:00' WHERE tweet_id='1'")
            source.close()
            target = HistoryStore(root / "dst")
            target.save_embeddings([("flat", "1", 1, "photo", np.zeros(DIM, np.float32).tobytes(), 0.1)])
            with target._db:
                target._db.execute("UPDATE ai_embeddings SET updated_at='2020-01-01T00:00:00+09:00' WHERE tweet_id='1'")
            target.close()
            with closing(sqlite3.connect(root / "dst" / ".luvbox-history.sqlite3")) as dst, \
                 closing(sqlite3.connect(root / "src" / ".luvbox-history.sqlite3")) as src:
                with dst:
                    _merge_history(dst, src)
            with closing(HistoryStore(root / "dst")) as merged:
                self.assertEqual(merged.embeddings_for([("flat", "1", 1, "photo")])[("flat", "1", 1, "photo")][0], blob)
                self.assertEqual(merged.tag_feedback()[("flat", "1", 1, "photo")], {"동물": 1})
                self.assertEqual(merged.unreadable_signatures()[("flat", "2", 1, "photo")], (12, 99))
            rebuilt_dir = root / "rebuilt"
            rebuilt_dir.mkdir()
            rebuild_history(root / "src" / ".luvbox-history.sqlite3", rebuilt_dir / ".luvbox-history.sqlite3")
            with closing(HistoryStore(rebuilt_dir)) as rebuilt:
                self.assertEqual(rebuilt.embeddings_for([("flat", "1", 1, "photo")])[("flat", "1", 1, "photo")][0], blob)
                self.assertEqual(rebuilt.tag_feedback()[("flat", "1", 1, "photo")], {"동물": 1})
                self.assertEqual(rebuilt.unreadable_signatures()[("flat", "2", 1, "photo")], (12, 99))


if __name__ == "__main__":
    unittest.main()
