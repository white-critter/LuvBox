"""AI tags of one's own, taught by the person's own tags and answers, on a real catalog.

The vision model is replaced by a table of synthetic 768-d features (one per file
name); everything else is the product's code: the catalog, the shared AI profile,
learning and scoring, snapshot merges and the rebuild of a damaged catalog.
"""

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox import clip_classifier
from luvbox.ai_profile import AiProfileStore
from luvbox.clip_model import ACTIVE_MODEL_ID, CATEGORY_NAMES, LEGACY_MODEL_ID
from luvbox.history import HistoryStore
from luvbox.storage import resolve_storage

DIM = 768
ACCOUNT = "offline"


def unit(rows):
    rows = np.asarray(rows, dtype=np.float32)
    return rows / np.linalg.norm(rows, axis=-1, keepdims=True)


class Features:
    """Clusters of features that the five default anchors do not see (their span is projected out)."""

    def __init__(self, seed=21):
        self.rng = np.random.default_rng(seed)
        basis, _ = np.linalg.qr(clip_classifier.get_category_embeddings().T.astype(np.float64))
        self.basis = basis                      # (768, 5): the anchors' span
        self.common = self.clean(self.rng.normal(size=DIM))
        self.centres = {}

    def clean(self, vector):
        vector = np.asarray(vector, dtype=np.float64)
        vector = vector - self.basis @ (self.basis.T @ vector)
        return vector / np.linalg.norm(vector)

    def photo(self, cluster, spread=0.5):
        if cluster not in self.centres:
            self.centres[cluster] = self.clean(self.common * 2.0 + self.clean(self.rng.normal(size=DIM)))
        noise = self.clean(self.rng.normal(size=DIM)) * spread
        return unit(self.clean(self.centres[cluster] + noise)).astype(np.float32)

    def with_anchor(self, vector, name, amount=0.4):
        """The same picture, plus a little of one default tag (so its zero-shot score is high)."""
        anchor = clip_classifier.get_category_embeddings()[CATEGORY_NAMES.index(name)].astype(np.float64)
        return unit(np.asarray(vector, dtype=np.float64) + amount * anchor).astype(np.float32)


class LibraryCase(unittest.TestCase):
    """A saved library of photos whose features the fake encoder hands out by file name."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="luvbox-custom-ai-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "appdata")})
        environment.start()
        self.addCleanup(environment.stop)
        self.paths = resolve_storage(self.root / "Likes", ACCOUNT)
        self.features = Features()
        self.vectors = {}
        self.encoded = []
        from luvbox.qt_library import LibraryRepository, _Request

        self.repo = LibraryRepository()
        self.addCleanup(self.repo.configure, None)
        self.repo.configure(_Request(generation=1, account=ACCOUNT, output_dir=str(self.paths.output_dir),
                                     separate_accounts=False))
        for target, value in (("luvbox.clip_classifier.is_available", True),
                              ("luvbox.clip_classifier.get_session", object()),
                              ("luvbox.clip_classifier.release_session", None),
                              ("luvbox.clip_model.model_file_present", True)):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        encoder = patch("luvbox.clip_classifier.classify_detailed", side_effect=self.fake_encode)
        encoder.start()
        self.addCleanup(encoder.stop)

    def fake_encode(self, paths, threshold=None, batch_size=16, session=None):
        found = []
        for path in paths:
            self.encoded.append(Path(path).name)
            vector = self.vectors.get(Path(path).name)
            found.append((None, 0.5, None if vector is None else np.ascontiguousarray(vector, dtype=np.float32)))
        return found

    def add_photos(self, cluster, tweets, *, anchor=None, spread=0.5):
        with closing(self.store()) as store:
            for tweet in tweets:
                name = f"writer_{tweet}_1.png"
                path = self.paths.output_dir / name
                Image.new("RGB", (8, 8), (int(tweet) % 250, 40, 90)).save(path)
                vector = self.features.photo(cluster, spread)
                if anchor:
                    vector = self.features.with_anchor(vector, anchor)
                self.vectors[name] = vector
                store.record_media("flat", {"tweet_id": str(tweet), "num": 1, "type": "photo",
                                            "author": {"name": "writer"}, "date": "2026-10-01T12:00:00+09:00"}, path)

    def store(self):
        return HistoryStore.for_storage(self.paths, create_media=not self.paths.output_dir.exists())

    def profile(self):
        return closing(AiProfileStore())

    def ai_tags(self, tweet):
        with closing(self.store()) as store:
            return sorted(row["tag"] for row in store.ai_tags_of("flat", str(tweet), 1, "photo")
                          if row["source"] == "ai")

    def tagged(self, tag, origin=""):
        with closing(self.store()) as store:
            return sorted(int(row["tweet_id"]) for row in store.records(limit=1000, ai_category=tag, ai_origin=origin,
                                                                         include_deleted=False))

    def only_custom(self):
        """Switch the five defaults off so only the tags under test decide."""
        with self.profile() as profile:
            for name in CATEGORY_NAMES:
                profile.set_tag_enabled(name, False)


class OwnTagTests(LibraryCase):
    def setUp(self):
        super().setUp()
        self.add_photos("cat", range(101, 111), spread=0.35)      # ten cat photos
        self.add_photos("dog", range(201, 211))                   # ten dog photos
        self.add_photos("sky", range(301, 331))                   # and photos of everything else
        with closing(self.store()) as store:
            for tweet in range(101, 107):                 # the person tagged six of the cats
                store.add_tag("flat", str(tweet), 1, "photo", "고양이")
        self.only_custom()

    def test_a_typed_tag_turned_into_an_ai_tag_teaches_it_and_finds_the_other_photos(self):
        with self.profile() as profile:
            profile.add_tag("고양이")
        changed = self.repo.scan_ai_tags(1)
        self.assertEqual(changed, 4)                      # the four cats nobody tagged
        self.assertEqual(self.tagged("고양이"), list(range(101, 111)))
        self.assertEqual(self.tagged("고양이", "ai"), list(range(107, 111)))
        with closing(self.store()) as store:
            self.assertEqual([row["tag"] for row in store.user_tags()], ["고양이"])     # still only six typed
            self.assertEqual(store.ai_tag_counts(["고양이"]), {"고양이": 10})
        with self.profile() as profile:
            rule = next(row for row in profile.active_tags() if row["tag"] == "고양이")
            self.assertEqual((rule["pos_count"], rule["neg_count"], rule["regime"]), (6, 0, "linear"))
        for tweet in list(range(201, 211)) + list(range(301, 331)):
            self.assertEqual(self.ai_tags(tweet), [], tweet)
        self.assertEqual(self.repo.ai_counts(1), [{"category": "고양이", "count": 10}])
        # Nothing new: a second run encodes nothing and changes nothing.
        before = len(self.encoded)
        self.assertEqual(self.repo.scan_ai_tags(1), 0)
        self.assertEqual(len(self.encoded), before)

    def test_rules_changed_while_the_rest_is_encoded_are_the_rules_it_is_scored_with(self):
        """Another tab (or a scheduled save) switches an AI tag off during a long classification.

        A default tag needs no learning, so nothing else rebuilds the scoring
        rules on the way: the last stage must notice the change itself.
        """
        self.add_photos("pet", range(401, 411), anchor="인물")      # last in the catalog
        with self.profile() as profile:
            profile.set_tag_enabled("인물", True)
        calls = []

        def encode_and_change(paths, threshold=None, batch_size=16, session=None):
            calls.append(len(paths))
            if len(calls) == 1:
                with self.profile() as profile:
                    profile.set_tag_enabled("인물", False)
            return self.fake_encode(paths, threshold, batch_size, session)

        with patch("luvbox.clip_classifier.classify_detailed", side_effect=encode_and_change):
            self.repo.scan_ai_tags(1)
        self.assertGreater(len(calls), 2)
        for tweet in range(401, 411):
            self.assertEqual(self.ai_tags(tweet), [], tweet)
        with closing(self.store()) as store, self.profile() as profile:
            self.assertEqual(store.ai_scored_state(), (str(profile.rules_version()), ACTIVE_MODEL_ID))

    def test_a_tag_switched_off_elsewhere_no_longer_shows_its_guesses(self):
        """Another tab switches #고양이 off: its AI guesses leave the preview at once; typed tags stay."""
        with self.profile() as profile:
            profile.add_tag("고양이")
        self.repo.scan_ai_tags(1)
        self.assertEqual(self.ai_tags(108), ["고양이"])
        with self.profile() as profile:
            profile.set_tag_enabled("고양이", False)
        shown = self.repo.selected("g1_flat_108_1_photo", 1)
        self.assertEqual(shown["ai_tags"], [])
        self.assertNotIn("고양이", [tag["tag"] for tag in shown["tags"] if tag["source"] == "ai"])
        typed = self.repo.selected("g1_flat_101_1_photo", 1)
        self.assertIn({"tag": "고양이", "source": "user"}, typed["tags"])

    def test_photos_the_person_taught_are_encoded_first(self):
        with self.profile() as profile:
            profile.add_tag("고양이")
        self.repo.scan_ai_tags(1)
        self.assertEqual(sorted(self.encoded[:6]), [f"writer_{t}_1.png" for t in range(101, 107)])

    def test_taking_an_ai_tag_away_is_a_not_this_that_stays_and_is_learned(self):
        with self.profile() as profile:
            profile.add_tag("고양이")
        self.repo.scan_ai_tags(1)
        self.assertTrue(self.repo.remove_tag("g1_flat_107_1_photo", 1, "고양이"))
        with closing(self.store()) as store:
            self.assertEqual(store.tag_feedback([("flat", "107", 1, "photo")]),
                             {("flat", "107", 1, "photo"): {"고양이": -1}})
            self.assertEqual(store.ai_tags_of("flat", "107", 1, "photo"), [])
        # The profile learned of it at once.
        with self.profile() as profile:
            self.assertEqual(profile.example_counts()["고양이"], (6, 1))
        self.repo.reclassify(1)
        self.assertEqual(self.ai_tags(107), [])           # the AI does not put it back
        self.assertEqual(self.ai_tags(108), ["고양이"])
        self.assertNotIn(107, self.tagged("고양이"))

    def test_a_typed_tag_outweighs_an_earlier_not_this(self):
        with self.profile() as profile:
            profile.add_tag("고양이")
        self.repo.scan_ai_tags(1)
        self.repo.remove_tag("g1_flat_108_1_photo", 1, "고양이")
        self.assertTrue(self.repo.add_tag("g1_flat_108_1_photo", 1, "고양이"))
        with closing(self.store()) as store:
            self.assertEqual(store.tag_feedback([("flat", "108", 1, "photo")]), {})
        with self.profile() as profile:
            self.assertEqual(profile.example_counts()["고양이"], (7, 0))

    def test_removing_a_typed_tag_takes_its_example_back_without_saying_not_this(self):
        with self.profile() as profile:
            profile.add_tag("고양이")
        self.repo.scan_ai_tags(1)
        self.assertTrue(self.repo.remove_tag("g1_flat_101_1_photo", 1, "고양이"))
        with self.profile() as profile:
            self.assertEqual(profile.example_counts()["고양이"], (5, 0))
        with closing(self.store()) as store:
            self.assertEqual(store.tag_feedback(), {})

    def test_teaching_one_photo_spreads_to_the_photos_like_it(self):
        # "고양이" has no example yet; one "yes" (✓) makes the near copies follow.
        with closing(self.store()) as store:
            for tweet in range(101, 107):
                store.remove_tag("flat", str(tweet), 1, "photo", "고양이")
        self.add_photos("twins", range(401, 404), spread=0.02)
        self.repo.scan_ai_tags(1)           # every photo has its feature, as in a library in use
        taught, propagated = self.repo.teach_tag(1, [("flat", "401", 1, "photo")], "고양이", True)
        self.assertEqual(taught, 1)
        self.assertEqual(propagated, 2)
        self.assertEqual(self.tagged("고양이", "confirmed"), [401])
        self.assertEqual(self.tagged("고양이", "ai"), [402, 403])
        with self.profile() as profile:
            self.assertIn("고양이", profile.active_tag_names())      # taught, so it became an AI tag
            self.assertEqual(next(r for r in profile.active_tags() if r["tag"] == "고양이")["regime"], "neighbor")
        # Not this, for the same photo: the confirmed label goes and so do its copies' estimates.
        self.repo.teach_tag(1, [("flat", "401", 1, "photo")], "고양이", False)
        self.assertEqual(self.tagged("고양이"), [])

    def test_switching_a_tag_off_or_deleting_it_takes_its_estimates_away(self):
        with self.profile() as profile:
            profile.add_tag("고양이")
        self.repo.scan_ai_tags(1)
        self.repo.teach_tag(1, [("flat", "107", 1, "photo")], "고양이", True)
        with self.profile() as profile:
            profile.set_tag_enabled("고양이", False)
        self.repo.reclassify(1)
        self.assertEqual(self.tagged("고양이", "ai"), [])
        self.assertEqual(self.tagged("고양이", "confirmed"), [107])     # what the person said stays
        self.assertEqual(self.repo.ai_counts(1), [])
        with self.profile() as profile:
            profile.set_tag_enabled("고양이", True)
        self.repo.reclassify(1)
        self.assertEqual(self.tagged("고양이", "ai"), [108, 109, 110])
        with self.profile() as profile:
            profile.delete_tag("고양이")
        self.repo.reclassify(1)
        self.assertEqual(self.tagged("고양이", "ai"), [])
        with closing(self.store()) as store:
            self.assertEqual(len(store.user_tags()), 1)                     # typed tags are never touched

    def test_one_photo_carries_several_ai_tags(self):
        self.add_photos("cat", [501], anchor="인물", spread=0.25)
        with self.profile() as profile:
            profile.add_tag("고양이")
            profile.set_tag_enabled("인물", True)
        self.repo.scan_ai_tags(1)
        self.assertEqual(self.ai_tags(501), ["고양이", "인물"])
        self.assertEqual(self.ai_tags(108), ["고양이"])
        with closing(self.store()) as store:
            self.assertEqual(store.count_embeddings_without_labels(), 0)
            # A photo given no AI tag still counts as looked at.
            row = store._db.execute("SELECT tag FROM media_tags WHERE tweet_id='301' AND source='ai'").fetchall()
            self.assertEqual([tuple(r) for r in row], [("",)])

    def test_a_custom_tag_without_any_example_is_not_guessed(self):
        with self.profile() as profile:
            profile.add_tag("강아지")
        self.repo.scan_ai_tags(1)
        self.assertEqual(self.tagged("강아지"), [])
        with self.profile() as profile:
            self.assertTrue(next(r for r in profile.active_tags() if r["tag"] == "강아지")["needs_examples"])

    def test_the_profile_is_shared_with_another_library(self):
        with self.profile() as profile:
            profile.add_tag("고양이")
        self.repo.scan_ai_tags(1)
        other = resolve_storage(self.root / "Other", ACCOUNT)
        from luvbox.qt_library import LibraryRepository, _Request
        with closing(HistoryStore.for_storage(other)) as store:
            for tweet in (901, 902):
                path = other.output_dir / f"writer_{tweet}_1.png"
                Image.new("RGB", (8, 8), (10, 10, 10)).save(path)
                self.vectors[path.name] = self.features.photo("cat" if tweet == 901 else "dog")
                store.record_media("flat", {"tweet_id": str(tweet), "num": 1, "type": "photo",
                                            "author": {"name": "writer"}}, path)
        second = LibraryRepository()
        self.addCleanup(second.configure, None)
        second.configure(_Request(generation=1, account=ACCOUNT, output_dir=str(other.output_dir),
                                  separate_accounts=False))
        second.scan_ai_tags(1)
        with closing(HistoryStore.for_storage(other)) as store:
            self.assertEqual([r["tag"] for r in store.ai_tags_of("flat", "901", 1, "photo")], ["고양이"])

    def test_neighbor_promotes_to_linear_when_backgrounds_arrive_and_private_teach_stays_local(self):
        from luvbox import ai_pipeline
        from luvbox.ai_profile import example_id_for_x, output_key

        with self.profile() as profile:
            profile.add_tag("고양이")
        with closing(self.store()) as store, self.profile() as profile:
            cat_idents = [("flat", str(t), 1, "photo") for t in range(101, 107)]
            store.save_embeddings([(ident[0], ident[1], ident[2], ident[3],
                                    self.vectors[f"writer_{ident[1]}_1.png"].tobytes(), 0.9)
                                   for ident in cat_idents])
            scope = output_key(self.paths.output_dir)
            example_id = lambda ident: example_id_for_x(self.paths.output_dir, ACCOUNT, *ident)
            ai_pipeline.sync_examples(store, profile, "x", scope, example_id)
            ai_pipeline.train_dirty(store, profile)
            rule = next(r for r in profile.active_tags() if r["tag"] == "고양이")
            # 6 positives, 0 negatives, 0 backgrounds -> trained in "neighbor" regime.
            self.assertEqual(rule["regime"], "neighbor")
            # Now background embeddings arrive; train_dirty must promote "neighbor" -> "linear".
            bg_idents = [("flat", str(t), 1, "photo") for t in range(301, 315)]
            store.save_embeddings([(ident[0], ident[1], ident[2], ident[3],
                                    self.vectors[f"writer_{ident[1]}_1.png"].tobytes(), 0.9)
                                   for ident in bg_idents])
            ai_pipeline.train_dirty(store, profile)
            rule = next(r for r in profile.active_tags() if r["tag"] == "고양이")
            self.assertEqual(rule["regime"], "linear")
            # Private teaching (shared=False) updates only the target photo without touching shared profile or scored_state.
            before_state = store.ai_scored_state()
            before_counts = dict(profile.example_counts())
            taught, _prop = ai_pipeline.teach(
                store, profile, kind="x", scope=scope, example_id=example_id,
                idents=[("flat", "201", 1, "photo")], tag="고양이", positive=True, shared=False,
            )
            self.assertEqual(taught, 1)
            self.assertEqual(store.ai_scored_state(), before_state)
            self.assertEqual(dict(profile.example_counts()), before_counts)
            self.assertEqual(store.ai_tags_of("flat", "902", 1, "photo"), [])


class MigrationTests(LibraryCase):
    """A catalog of 1.11.9: CLIP features, single-label teaching, AI tags from CLIP."""

    def setUp(self):
        super().setUp()
        self.add_photos("person", range(101, 108))
        self.add_photos("drawing", range(201, 204))
        legacy = self.paths.record_dir / ".luvbox-history.sqlite3"
        with closing(sqlite3.connect(legacy)) as db, db:
            # What 1.11.9 left: no model column, no answers per tag.
            db.execute("ALTER TABLE ai_embeddings DROP COLUMN model_id")
            db.execute("DROP TABLE ai_tag_feedback")
            db.execute("DELETE FROM catalog_meta WHERE key='ai_feedback_migrated_v2'")
            for tweet in range(101, 108):
                db.execute("INSERT INTO ai_embeddings(folder_mode,tweet_id,num,media_type,embedding,confidence,"
                           "updated_at) VALUES ('flat',?,1,'photo',?,0.9,'2026-11-01')", (str(tweet), b"\x01" * 2048))
            for tweet in range(101, 106):
                db.execute("INSERT INTO ai_feedback VALUES ('flat',?,1,'photo','인물','2026-11-02T00:00:00+09:00')",
                           (str(tweet),))
                db.execute("INSERT INTO media_tags VALUES ('flat',?,1,'photo','인물','confirmed','2026-11-02')",
                           (str(tweet),))
            db.execute("INSERT INTO ai_feedback VALUES ('flat','201',1,'photo','','2026-11-02T00:00:00+09:00')")
            db.execute("INSERT INTO media_tags VALUES ('flat','201',1,'photo','','confirmed','2026-11-02')")
            db.execute("INSERT INTO media_tags VALUES ('flat','106',1,'photo','일러스트','ai','2026-11-02')")
            db.execute("INSERT INTO media_tags VALUES ('flat','107',1,'photo','내 사진','user','2026-11-02')")

    def test_a_read_only_look_before_the_first_open_treats_every_feature_as_clips(self):
        from luvbox.qt_library import _ReadOnlyHistory
        with closing(_ReadOnlyHistory(self.paths)) as store:
            self.assertFalse(store._has_column("ai_embeddings", "model_id"))
            self.assertEqual(len(store.photos_without_embeddings()), 10)
            self.assertEqual(store.count_embeddings(), 0)
            self.assertEqual(store.count_embeddings(LEGACY_MODEL_ID), 7)
            self.assertEqual(store.embeddings_for([("flat", "101", 1, "photo")]), {})
        self.assertTrue(self.repo.auto_ai_pending(1))

    def test_answers_and_tags_survive_and_the_photos_are_encoded_once_more(self):
        with closing(self.store()) as store:
            self.assertEqual(store.count_embeddings(LEGACY_MODEL_ID), 7)
            feedback = store.tag_feedback()
            self.assertEqual(feedback[("flat", "101", 1, "photo")], {"인물": 1})
            self.assertEqual(feedback[("flat", "201", 1, "photo")], {name: -1 for name in CATEGORY_NAMES})
            self.assertEqual(len(store.photos_without_embeddings()), 10)
            # The old single answers are kept for older versions reading a merged snapshot.
            self.assertEqual(store._db.execute("SELECT COUNT(*) FROM ai_feedback").fetchone()[0], 6)
        with closing(self.store()) as store:
            # 1.11.9's "none of these" row is the empty AI row now, so the photo can be scored.
            rows = store._db.execute("SELECT tag, source FROM media_tags WHERE tweet_id='201'").fetchall()
            self.assertEqual([tuple(r) for r in rows], [("", "ai")])
        self.repo.scan_ai_tags(1)
        with closing(self.store()) as store:
            self.assertEqual(store.count_embeddings(), 10)
            self.assertEqual(store.count_embeddings(unlabeled=True), 0)
            self.assertEqual(store.count_embeddings(LEGACY_MODEL_ID), 0)
            self.assertEqual(self.tagged("인물", "confirmed"), [101, 102, 103, 104, 105])
            self.assertIn({"tag": "내 사진", "source": "user"}, store.item_tags("flat", "107", 1, "photo"))
            # The CLIP estimate is replaced by what SigLIP 2 and the teaching say.
            self.assertNotIn("일러스트", [r["tag"] for r in store.ai_tags_of("flat", "106", 1, "photo")])
        with self.profile() as profile:
            counts = profile.example_counts()
            self.assertEqual(counts["인물"], (5, 1))
            self.assertEqual(counts["일러스트"], (0, 1))
        # The migration ran once: opening again does not add the answers twice or bring old ones back.
        with closing(self.store()) as store:
            store.set_tag_feedback([("flat", "201", 1, "photo")], "인물", True)
        with closing(self.store()) as store:
            self.assertEqual(store.tag_feedback([("flat", "201", 1, "photo")])[("flat", "201", 1, "photo")]["인물"], 1)


class MergeAndRebuildTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="luvbox-ai-merge-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def catalog(self, name):
        return HistoryStore(self.root / name)

    def merge(self, target, source):
        from luvbox.storage import _merge_history

        with closing(sqlite3.connect(self.root / target / ".luvbox-history.sqlite3")) as dst, \
                closing(sqlite3.connect(self.root / source / ".luvbox-history.sqlite3")) as src:
            with dst:
                _merge_history(dst, src)

    def test_several_ai_tags_of_a_photo_survive_a_merge(self):
        """Up to 1.11.9 a merge kept one AI row per photo and dropped the rest."""
        with closing(self.catalog("a")) as store:
            store.replace_ai_tags([("flat", "1", 1, "photo", ["일러스트", "짤·텍스트"])])
        with closing(self.catalog("b")) as store:
            store.replace_ai_tags([("flat", "1", 1, "photo", ["고양이"]), ("flat", "2", 1, "photo", [])])
            store.set_tag_feedback([("flat", "1", 1, "photo")], "인물", True)
        self.merge("a", "b")
        with closing(self.catalog("a")) as store:
            self.assertEqual(sorted((t["tag"], t["source"]) for t in store.item_tags("flat", "1", 1, "photo")),
                             [("고양이", "ai"), ("인물", "confirmed"), ("일러스트", "ai"), ("짤·텍스트", "ai")])
            rows = store._db.execute("SELECT tag FROM media_tags WHERE tweet_id='2' AND source='ai'").fetchall()
            self.assertEqual([tuple(r) for r in rows], [("",)])

    def test_a_later_not_this_wins_and_takes_the_label_away(self):
        with closing(self.catalog("a")) as store:
            store.set_tag_feedback([("flat", "1", 1, "photo")], "인물", True)
        with closing(self.catalog("b")) as store:
            store.set_tag_feedback([("flat", "1", 1, "photo")], "인물", False)
        self.merge("a", "b")
        with closing(self.catalog("a")) as store:
            self.assertEqual(store.tag_feedback()[("flat", "1", 1, "photo")], {"인물": -1})
            self.assertEqual(store.ai_tags_of("flat", "1", 1, "photo"), [])
        # The older answer coming back the other way does not undo it.
        self.merge("b", "a")
        with closing(self.catalog("b")) as store:
            self.assertEqual(store.tag_feedback()[("flat", "1", 1, "photo")], {"인물": -1})

    def test_a_current_feature_is_never_replaced_by_an_old_models(self):
        new = np.full(DIM, 0.5, dtype=np.float32).tobytes()
        with closing(self.catalog("a")) as store:
            store.save_embeddings([("flat", "1", 1, "photo", new, 0.7)])
            store._db.execute("UPDATE ai_embeddings SET updated_at='2020-01-01'")
            store._db.commit()
        with closing(self.catalog("b")) as store:
            store.save_embeddings([("flat", "1", 1, "photo", b"\x02" * 2048, 0.2), ("flat", "2", 1, "photo", b"\x03" * 2048, 0.3)],
                                  LEGACY_MODEL_ID)
            store._db.execute("UPDATE ai_embeddings SET updated_at='2030-01-01'")
            store._db.commit()
        self.merge("a", "b")
        with closing(self.catalog("a")) as store:
            self.assertEqual(store.embeddings_for([("flat", "1", 1, "photo")])[("flat", "1", 1, "photo")][0], new)
            self.assertEqual(store.count_embeddings(LEGACY_MODEL_ID), 0)    # only listed photos count; check rows
            rows = dict(store._db.execute("SELECT tweet_id, model_id FROM ai_embeddings").fetchall())
            self.assertEqual(rows, {"1": ACTIVE_MODEL_ID, "2": LEGACY_MODEL_ID})
        # The other way round, the old feature gives way to the current one.
        self.merge("b", "a")
        with closing(self.catalog("b")) as store:
            self.assertEqual(dict(store._db.execute("SELECT tweet_id, model_id FROM ai_embeddings").fetchall())["1"],
                             ACTIVE_MODEL_ID)

    def test_a_typed_tag_is_not_turned_down_again_by_an_old_snapshot(self):
        """Typing a tag clears the photo's "not this" for it; an old single-label answer merged later must not
        bring that refusal back (the typed tag outweighs an earlier "not this")."""
        with closing(self.catalog("a")) as store:
            store.add_tag("flat", "2", 1, "photo", "인물")
        old = self.root / "old"
        old.mkdir()
        with closing(sqlite3.connect(old / ".luvbox-history.sqlite3")) as db, db:
            db.execute("CREATE TABLE ai_feedback (folder_mode TEXT NOT NULL, tweet_id TEXT NOT NULL, num INTEGER "
                       "NOT NULL, media_type TEXT NOT NULL, category TEXT NOT NULL, updated_at TEXT NOT NULL, "
                       "PRIMARY KEY(folder_mode, tweet_id, num, media_type))")
            db.execute("INSERT INTO ai_feedback VALUES ('flat','2',1,'photo','','2026-11-03')")
        self.merge("a", "old")
        with closing(self.catalog("a")) as store:
            answers = store.tag_feedback()[("flat", "2", 1, "photo")]
            self.assertNotIn("인물", answers)
            self.assertEqual(answers, {name: -1 for name in CATEGORY_NAMES if name != "인물"})
            self.assertIn({"tag": "인물", "source": "user"}, store.item_tags("flat", "2", 1, "photo"))

    def test_a_snapshot_of_a_pc_still_on_single_label_teaching_counts_per_tag(self):
        with closing(self.catalog("a")) as store:
            pass
        old = self.root / "old"
        old.mkdir()
        with closing(sqlite3.connect(old / ".luvbox-history.sqlite3")) as db, db:
            db.execute("CREATE TABLE ai_feedback (folder_mode TEXT NOT NULL, tweet_id TEXT NOT NULL, num INTEGER "
                       "NOT NULL, media_type TEXT NOT NULL, category TEXT NOT NULL, updated_at TEXT NOT NULL, "
                       "PRIMARY KEY(folder_mode, tweet_id, num, media_type))")
            db.execute("CREATE TABLE media_tags (folder_mode TEXT NOT NULL, tweet_id TEXT NOT NULL, num INTEGER "
                       "NOT NULL, media_type TEXT NOT NULL, tag TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'user', "
                       "created_at TEXT NOT NULL, PRIMARY KEY(folder_mode, tweet_id, num, media_type, tag))")
            db.execute("INSERT INTO ai_feedback VALUES ('flat','1',1,'photo','동물','2026-11-03')")
            db.execute("INSERT INTO ai_feedback VALUES ('flat','2',1,'photo','','2026-11-03')")
        self.merge("a", "old")
        with closing(self.catalog("a")) as store:
            feedback = store.tag_feedback()
            self.assertEqual(feedback[("flat", "1", 1, "photo")], {"동물": 1})
            self.assertEqual(feedback[("flat", "2", 1, "photo")], {name: -1 for name in CATEGORY_NAMES})
            self.assertEqual([t["tag"] for t in store.ai_tags_of("flat", "1", 1, "photo")], ["동물"])

    def test_a_rebuilt_catalog_keeps_the_answers_per_tag_and_converts_old_ones(self):
        from luvbox.recovery import rebuild_history

        with closing(self.catalog("src")) as store:
            store.set_tag_feedback([("flat", "1", 1, "photo")], "고양이", False)
            store.set_tag_feedback([("flat", "3", 1, "photo")], "인물", True)
            store._db.execute("INSERT INTO ai_feedback VALUES ('flat','2',1,'photo','일러스트','2026-11-03')")
            store._db.commit()
        built = self.root / "built"
        built.mkdir()
        rebuild_history(self.root / "src" / ".luvbox-history.sqlite3", built / ".luvbox-history.sqlite3")
        with closing(HistoryStore(built)) as store:
            feedback = store.tag_feedback()
            self.assertEqual(feedback[("flat", "1", 1, "photo")], {"고양이": -1})
            self.assertEqual(feedback[("flat", "2", 1, "photo")], {"일러스트": 1})
            self.assertEqual(feedback[("flat", "3", 1, "photo")], {"인물": 1})

    def test_multi_select_refuse_ai_tags_isolates_negative_feedback_per_photo(self):
        with closing(self.catalog("multi")) as store:
            p1 = ("flat", "1", 1, "photo")
            p2 = ("flat", "2", 1, "photo")
            store.replace_ai_tags([(*p1, ["고양이"]), (*p2, ["인물"])])
            store.refuse_ai_tags([p1, p2])
            feedback = store.tag_feedback([p1, p2])
            self.assertEqual(feedback[p1], {"고양이": -1})
            self.assertEqual(feedback[p2], {"인물": -1})


if __name__ == "__main__":
    unittest.main()
