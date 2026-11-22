"""Regression checks for the 1.12.1 full inspection fixes."""

from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from tests import app_data_isolation  # noqa: E402,F401  (never this PC's real app data)
except ModuleNotFoundError:
    import app_data_isolation  # noqa: E402,F401

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from luvbox import __version__, ai_pipeline, clip_classifier, settings, storage  # noqa: E402
from luvbox.ai_profile import AiProfileStore  # noqa: E402
from luvbox.clip_model import ACTIVE_MODEL_ID, CATEGORY_NAMES  # noqa: E402
from luvbox.duplicates import ScanCancelled  # noqa: E402
from luvbox.history import HistoryStore  # noqa: E402
from luvbox.local_store import LocalFolderStore  # noqa: E402
from luvbox.qt_local_folder import _FolderRequest  # noqa: E402
from luvbox.storage import resolve_storage  # noqa: E402


class ClipSessionDeadlockRegressionTests(unittest.TestCase):
    def test_get_session_does_not_deadlock_calling_is_model_ready(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-clip-lock-") as tmp:
            fake_model = Path(tmp) / "model.onnx"
            payload = b"onnx" * 32
            fake_model.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            fake_ort = MagicMock()
            fake_ort.get_available_providers.return_value = ["CPUExecutionProvider"]
            fake_session = MagicMock()
            fake_ort.InferenceSession.return_value = fake_session
            with patch.object(clip_classifier, "_SESSION", None), \
                 patch.object(clip_classifier, "_VERIFIED_MODEL_SIG", None), \
                 patch.object(clip_classifier, "bundled_model_path", return_value=fake_model), \
                 patch.object(clip_classifier, "MODEL_SIZE", len(payload)), \
                 patch.object(clip_classifier, "MODEL_SHA256", digest), \
                 patch.object(clip_classifier, "ort", fake_ort), \
                 patch.object(clip_classifier, "_ONNXRUNTIME_AVAILABLE", True):
                result = []

                def worker():
                    result.append(clip_classifier.get_session(fake_model))

                t = threading.Thread(target=worker, daemon=True)
                t.start()
                t.join(timeout=3.0)
                self.assertFalse(t.is_alive(), "get_session() deadlocked on _SESSION_LOCK")
                self.assertEqual(result, [fake_session])


class AiPipelineTeachAndRescoreRegressionTests(unittest.TestCase):
    def test_encode_missing_for_teach_encodes_unembedded_target_and_teach_honors_cancel(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-teach-enc-") as tmp:
            base = Path(tmp)
            paths = resolve_storage(base / "media", "tester")
            ident = ("flat", "1", 1, "photo")
            with closing(HistoryStore.for_storage(paths)) as store, \
                 closing(AiProfileStore(base / "profile.sqlite3")) as profile:
                photo = paths.media_dir / "author_1_1.png"
                Image.new("RGB", (32, 32), (120, 80, 200)).save(photo)
                store.record_media("flat", {"tweet_id": "1", "num": 1, "type": "photo",
                                            "author": {"name": "author"}, "date": "2026-11-20T00:00:00+09:00"}, photo)
                self.assertEqual(store.embeddings_for([ident]), {})
                vec = np.zeros(768, dtype=np.float32)
                vec[0] = 1.0
                with patch("luvbox.clip_classifier.is_available", return_value=True), \
                     patch("luvbox.clip_classifier.get_session", return_value=object()), \
                     patch("luvbox.clip_classifier.release_session", return_value=None), \
                     patch("luvbox.clip_classifier.classify_detailed",
                           side_effect=lambda batch, **_k: [(None, 0.5, vec) for _ in batch]):
                    ai_pipeline.encode_missing_for_teach(store, [(ident, photo)])
                self.assertIn(ident, store.embeddings_for([ident]))

                with self.assertRaises(ScanCancelled):
                    ai_pipeline.teach(
                        store, profile, kind="catalog", scope="scope-a",
                        example_id=lambda i: ":".join(str(x) for x in i),
                        idents=[ident], tag="고양이", positive=True,
                        shared=True, check_stop=lambda: True,
                    )

    def test_classify_scores_encoded_first_when_not_rescore_all(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-scan-first-") as tmp:
            base = Path(tmp)
            paths = resolve_storage(base / "media", "tester")
            id1 = ("flat", "1", 1, "photo")
            id2 = ("flat", "2", 1, "photo")
            with closing(HistoryStore.for_storage(paths)) as store, \
                 closing(AiProfileStore(base / "profile.sqlite3")) as profile:
                p1 = paths.media_dir / "author_1_1.png"
                p2 = paths.media_dir / "author_2_1.png"
                Image.new("RGB", (32, 32), (10, 20, 30)).save(p1)
                Image.new("RGB", (32, 32), (40, 50, 60)).save(p2)
                store.record_media("flat", {"tweet_id": "1", "num": 1, "type": "photo",
                                            "author": {"name": "author"}, "date": "2026-11-20T00:00:00+09:00"}, p1)
                store.record_media("flat", {"tweet_id": "2", "num": 1, "type": "photo",
                                            "author": {"name": "author"}, "date": "2026-11-20T00:00:00+09:00"}, p2)
                anchor_vec = np.asarray(
                    clip_classifier.get_category_embeddings()[CATEGORY_NAMES.index("인물")],
                    dtype=np.float32,
                )
                anchor_vec = anchor_vec / np.linalg.norm(anchor_vec)
                store.add_tag("flat", "1", 1, "photo", "동물")
                version = profile.rules_version()
                store.set_ai_scored_state(version, ACTIVE_MODEL_ID)
                with patch("luvbox.clip_classifier.is_available", return_value=True), \
                     patch("luvbox.clip_classifier.get_session", return_value=object()), \
                     patch("luvbox.clip_classifier.release_session", return_value=None), \
                     patch("luvbox.clip_classifier.classify_detailed",
                           side_effect=lambda batch, **_k: [(None, 0.5, anchor_vec) for _ in batch]):
                    ai_pipeline.classify(
                        store, profile, kind="catalog", scope="scope-a",
                        example_id=lambda i: ":".join(str(x) for x in i),
                        items=[(id1, p1), (id2, p2)],
                        shared=False,
                    )
                self.assertIn("인물", [r["tag"] for r in store.item_tags(*id1) if r["source"] == "ai"])
                self.assertIn("인물", [r["tag"] for r in store.item_tags(*id2) if r["source"] == "ai"])


class StorageAndHistoryIntegrityRegressionTests(unittest.TestCase):
    def test_remove_tag_detailed_restores_confirmed_ai_tag_and_records_negative_feedback(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-storage-edits-") as tmp:
            base = Path(tmp)
            paths = resolve_storage(base / "media", "tester")
            with closing(HistoryStore.for_storage(paths)) as store:
                photo = paths.media_dir / "author_10_1.png"
                Image.new("RGB", (32, 32), (70, 80, 90)).save(photo)
                store.record_media("flat", {"tweet_id": "10", "num": 1, "type": "photo",
                                            "author": {"name": "author"}, "date": "2026-11-20T00:00:00+09:00"}, photo)
                store.set_tag_feedback([("flat", "10", 1, "photo")], "인물", True)
                store.add_tag("flat", "10", 1, "photo", "인물")
                store.replace_ai_tags([("flat", "10", 1, "photo", ["동물"])])
                self.assertEqual(store.remove_tag_detailed("flat", "10", 1, "photo", "인물"), (True, True))
                self.assertEqual(store.remove_tag_detailed("flat", "10", 1, "photo", "동물"), (True, False))
                self.assertEqual(
                    store.tag_feedback([("flat", "10", 1, "photo")])[("flat", "10", 1, "photo")].get("동물"),
                    -1,
                )

    def test_moved_snapshot_edits_do_not_delete_updated_tags(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-moved-snap-") as tmp:
            base = Path(tmp)
            p_before = resolve_storage(base / "before", "tester")
            p_after = resolve_storage(base / "after", "tester")
            p_target = resolve_storage(base / "target", "tester")
            for sp in (p_before, p_after, p_target):
                with closing(HistoryStore.for_storage(sp)) as st:
                    db_file = st.path
                if sp is p_before:
                    before = db_file
                elif sp is p_after:
                    after = db_file
                else:
                    target = db_file
            with sqlite3.connect(before) as conn:
                conn.execute(
                    "INSERT INTO media_tags(folder_mode, tweet_id, num, media_type, tag, created_at, source) "
                    "VALUES ('flat', '1', 1, 'photo', '풍경', '2026-11-20T00:00:00+09:00', 'user')"
                )
                conn.commit()
            with sqlite3.connect(after) as conn:
                conn.execute(
                    "INSERT INTO media_tags(folder_mode, tweet_id, num, media_type, tag, created_at, source) "
                    "VALUES ('flat', '1', 1, 'photo', '풍경', '2026-11-21T00:00:00+09:00', 'user')"
                )
                conn.commit()
            with sqlite3.connect(target) as conn:
                conn.execute(
                    "INSERT INTO media_tags(folder_mode, tweet_id, num, media_type, tag, created_at, source) "
                    "VALUES ('flat', '1', 1, 'photo', '풍경', '2026-11-20T00:00:00+09:00', 'user')"
                )
                conn.commit()
            edits = storage._moved_snapshot_edits(after, before)
            self.assertIsNotNone(edits)
            deletes, inserts = edits["media_tags"]
            self.assertEqual(deletes, [])
            self.assertEqual(len(inserts), 1)
            storage._apply_moved_mark_edits(target, edits)
            with sqlite3.connect(target) as conn:
                rows = conn.execute("SELECT tag, created_at, source FROM media_tags").fetchall()
            self.assertEqual(rows, [("풍경", "2026-11-21T00:00:00+09:00", "user")])


class LocalFolderAndSettingsRegressionTests(unittest.TestCase):
    def test_local_store_remove_tag_result_and_save_hashes_6tuple_and_duplicate_filter(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-local-1121-") as tmp:
            root = (Path(tmp) / "photos").resolve()
            (root / "sub").mkdir(parents=True)
            img = root / "a.png"
            Image.new("RGB", (24, 24), (10, 100, 200)).save(img)
            with closing(LocalFolderStore(root)) as store:
                store.sync_folder(include_subfolders=True)
                store.set_tag_feedback(["a.png"], "일러스트", True)
                store.add_tag(["a.png"], "일러스트")
                self.assertEqual(store.remove_tag_result("a.png", "일러스트"), ("user", True))
                self.assertEqual(store.remove_tag_result("a.png", "일러스트"), ("confirmed", False))
                store.save_hashes([("a.png", "aa" * 32, "bb" * 8, "cc" * 8, 555, 777)])
                tracked = store._read("SELECT filesize, mtime_ns FROM local_files WHERE rel_path='a.png'")[0]
                self.assertEqual((tracked["filesize"], tracked["mtime_ns"]), (555, 777))
                store._folder_cache = {}
                self.assertEqual(store.path_of("sub/b.png"), root / "sub" / "b.png")
                self.assertIn(root / "sub", store._folder_cache)

            req = _FolderRequest(
                generation=1, root=str(root), media_type="gif", duplicate_only=True,
            )
            self.assertEqual(req.filters()["media_type"], "all")

    def test_load_settings_with_status_reports_damaged_without_second_read(self):
        with tempfile.TemporaryDirectory(prefix="luvbox-settings-1121-") as tmp, \
             patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
            bad = settings.settings_path()
            bad.parent.mkdir(parents=True, exist_ok=True)
            bad.write_text("{corrupted-json", encoding="utf-8")
            loaded, damaged = settings.load_settings_with_status()
            self.assertTrue(damaged)
            self.assertEqual(loaded, {})


class DocumentationAndQmlComponentRegressionTests(unittest.TestCase):
    def test_browser_login_doc_is_plain_user_facing_and_tile_components_exist(self):
        self.assertEqual(__version__, "1.12.1")
        doc = (ROOT / "docs" / "browser-login.md").read_text(encoding="utf-8")
        for forbidden in (
            "BrowserBridge",
            "BrowserSession",
            "save_authenticated_session",
            "SO_EXCLUSIVEADDRUSE",
            "AES-256-GCM",
            "HMAC-SHA-256",
            "gallery_dl.aes",
            "%LOCALAPPDATA%",
            "127.0.0.1",
            "InstalledBrowserExtensionTests",
            "토큰 보관",
        ):
            self.assertNotIn(forbidden, doc)
        self.assertIn("토큰 저장", doc)

        for name in ("TileSelectionCheck.qml", "TileMissingPlaceholder.qml"):
            self.assertTrue((ROOT / "assets" / "qt" / name).is_file(), name)
        for view in ("MainWindow.qml", "LocalFolderView.qml"):
            content = (ROOT / "assets" / "qt" / view).read_text(encoding="utf-8")
            self.assertIn("TileSelectionCheck", content)
            self.assertIn("TileMissingPlaceholder", content)


if __name__ == "__main__":
    unittest.main()
