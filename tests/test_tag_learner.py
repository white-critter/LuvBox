"""How one AI tag is learned from examples and how stored features are scored with it.

Synthetic 768-d features stand in for SigLIP 2's: a few cluster centres and
photos scattered around them, L2-normalized like the real ones.
"""

import unittest

import numpy as np

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox import tag_learner
from luvbox.tag_learner import TagModel, build_model, score_embeddings_matrix, tag_probabilities, train_tag

DIM = 768


def unit(rows):
    rows = np.asarray(rows, dtype=np.float32)
    return rows / np.linalg.norm(rows, axis=-1, keepdims=True)


class Space:
    """Cluster centres sharing one common direction, as SigLIP features do."""

    def __init__(self, seed=1):
        self.rng = np.random.default_rng(seed)
        self.common = unit(self.rng.normal(size=DIM))
        self.centres = {}

    def centre(self, name):
        if name not in self.centres:
            self.centres[name] = unit(self.common * 2.0 + unit(self.rng.normal(size=DIM)))
        return self.centres[name]

    def photos(self, name, count, spread=0.6):
        noise = self.rng.normal(size=(count, DIM)) / np.sqrt(DIM)
        return unit(self.centre(name) + spread * noise)


class RegimeTests(unittest.TestCase):
    def test_the_way_a_tag_is_decided_follows_its_number_of_examples(self):
        space = Space()
        none = np.zeros((0, DIM), dtype=np.float32)
        self.assertIsNone(train_tag(none, none, none, built_in=False))
        zero = train_tag(none, space.photos("b", 2), none, built_in=True)
        self.assertEqual((zero["regime"], zero["pos_count"], zero["neg_count"]), ("zeroshot", 0, 2))
        for count in range(1, 5):
            self.assertEqual(train_tag(space.photos("a", count), none, none, built_in=False)["regime"], "neighbor")
        learned = train_tag(space.photos("a", 5), none, space.photos("b", 40), built_in=False)
        self.assertEqual(learned["regime"], "linear")
        self.assertEqual(len(learned["weights"]), DIM * 4)
        self.assertEqual(learned["threshold"], tag_learner.DEFAULT_THRESHOLD)
        # Five examples and nothing to tell them apart from: compare with them instead.
        self.assertEqual(train_tag(space.photos("a", 5), none, none, built_in=False)["regime"], "neighbor")

    def test_the_bars_measured_on_real_photos(self):
        self.assertAlmostEqual(tag_learner.neighbor_threshold(1), 0.94)
        self.assertGreater(tag_learner.neighbor_threshold(1), tag_learner.neighbor_threshold(4))
        self.assertGreater(tag_learner.neighbor_threshold(4), tag_learner.NEIGHBOR_BASE)
        self.assertEqual(tag_learner.neighbor_threshold(0), tag_learner.neighbor_threshold(1))
        self.assertEqual([tag_learner.background_size(n) for n in (1, 5, 30, 100)], [64, 64, 300, 512])


class LinearTests(unittest.TestCase):
    def setUp(self):
        self.space = Space(seed=3)
        self.cats = self.space.photos("cat", 60)
        self.dogs = self.space.photos("dog", 60)
        self.others = np.concatenate([self.space.photos(name, 40) for name in ("car", "sky", "food")])

    def model(self, positives, negatives, background):
        learned = train_tag(positives, negatives, background, built_in=False)
        return build_model({"tag": "고양이", "built_in": False, "threshold": None, "weight": learned})

    def test_a_handful_of_examples_finds_the_rest_and_leaves_other_photos(self):
        model = self.model(self.cats[:8], np.zeros((0, DIM), np.float32), self.others[:64])
        _score, cats = tag_probabilities(self.cats[8:], model, None)
        _score, others = tag_probabilities(np.concatenate([self.dogs, self.others[64:]]), model, None)
        self.assertGreater(cats.mean(), 0.9, cats.mean())
        self.assertLess(others.mean(), 0.05, others.mean())

    def test_not_this_examples_teach_the_difference_between_neighbours(self):
        # Cats and dogs share a direction here (cosine about 0.9, as close kinds of photo are in
        # SigLIP's space); without "not this" the dogs come along.
        mixed = Space(seed=5)
        pets = mixed.centre("pet")
        cats = unit(pets + 0.8 * mixed.photos("cat", 60))
        dogs = unit(pets + 0.8 * mixed.photos("dog", 60))
        background = mixed.photos("street", 64)
        blind = self.model(cats[:10], np.zeros((0, DIM), np.float32), background)
        taught = self.model(cats[:10], dogs[:10], background)
        _s, blind_dogs = tag_probabilities(dogs[10:], blind, None)
        _s, taught_dogs = tag_probabilities(dogs[10:], taught, None)
        _s, taught_cats = tag_probabilities(cats[10:], taught, None)
        self.assertGreater(blind_dogs.mean(), 0.9)
        self.assertLess(taught_dogs.mean(), 0.1)
        self.assertGreater(taught_cats.mean(), 0.8)

    def test_a_photo_nearly_identical_to_a_not_this_example_is_never_tagged(self):
        learned = train_tag(self.cats[:10], np.zeros((0, DIM), np.float32), self.others[:64], built_in=False)
        refused = self.cats[10:11]
        model = build_model({"tag": "고양이", "built_in": False, "threshold": None, "weight": learned},
                            negatives=refused)
        copy = unit(refused + 0.01 * self.space.rng.normal(size=(1, DIM)) / np.sqrt(DIM))
        score, accepted = tag_probabilities(np.concatenate([copy, self.cats[20:21]]), model, None)
        self.assertGreater(score[0], tag_learner.DEFAULT_THRESHOLD)    # the probe alone would say yes
        self.assertEqual(list(accepted), [False, True])

    def test_learning_is_deterministic_and_quick(self):
        import time

        first = train_tag(self.cats[:30], self.dogs[:5], self.others, built_in=False)
        start = time.perf_counter()
        second = train_tag(self.cats[:30], self.dogs[:5], self.others, built_in=False)
        self.assertLess(time.perf_counter() - start, 1.0)
        self.assertEqual(first["weights"], second["weights"])
        self.assertEqual(first["bias"], second["bias"])


class NeighbourTests(unittest.TestCase):
    def test_only_a_near_copy_of_a_single_example_gets_the_tag(self):
        space = Space(seed=7)
        example = space.photos("fursuit", 1, spread=0.0)
        near = unit(example + 0.1 * space.rng.normal(size=(1, DIM)) / np.sqrt(DIM))
        same_kind = space.photos("fursuit", 5, spread=0.9)
        model = TagModel("퍼슈트", regime="neighbor", positives=example)
        score, accepted = tag_probabilities(np.concatenate([near, same_kind]), model, None)
        self.assertGreater(score[0], tag_learner.neighbor_threshold(1))
        self.assertEqual(list(accepted), [True] + [False] * 5)

    def test_a_photo_as_close_to_a_not_this_example_is_left_alone(self):
        space = Space(seed=8)
        base = space.photos("x", 1, spread=0.0)
        yes = unit(base + 0.05 * space.rng.normal(size=(1, DIM)) / np.sqrt(DIM))
        no = unit(base + 0.05 * space.rng.normal(size=(1, DIM)) / np.sqrt(DIM))
        model = TagModel("t", regime="neighbor", positives=yes, negatives=no)
        _score, accepted = tag_probabilities(base, model, None)
        self.assertFalse(accepted[0])

    def test_a_default_tag_undecided_by_its_examples_falls_back_to_its_zero_shot_score(self):
        space = Space(seed=9)
        model = TagModel("인물", built_in=True, regime="neighbor", positives=space.photos("a", 2))
        photos = space.photos("b", 3)
        zero_shot = np.zeros((3, 5), dtype=np.float32)
        zero_shot[:, 1] = [0.9, 0.2, 0.7]
        score, accepted = tag_probabilities(photos, model, zero_shot)
        self.assertEqual(list(accepted), [True, False, True])
        self.assertAlmostEqual(float(score[0]), 0.9, places=5)


class ZeroShotAndBlendTests(unittest.TestCase):
    def test_a_default_tag_without_examples_uses_its_anchor_column(self):
        model = build_model({"tag": "동물", "built_in": True, "threshold": None, "weight": None})
        self.assertEqual((model.regime, model.anchor), ("zeroshot", 2))
        zero_shot = np.array([[0, 0, 0.66, 0, 0], [0, 0, 0.64, 0, 0]], dtype=np.float32)
        _score, accepted = tag_probabilities(np.zeros((2, DIM), np.float32), model, zero_shot)
        self.assertEqual(list(accepted), [True, False])
        # A person's own threshold replaces the default one.
        strict = build_model({"tag": "동물", "built_in": True, "threshold": 0.9, "weight": None})
        _score, accepted = tag_probabilities(np.zeros((2, DIM), np.float32), strict, zero_shot)
        self.assertEqual(list(accepted), [False, False])

    def test_a_custom_tag_without_a_classifier_is_not_scored(self):
        self.assertFalse(build_model({"tag": "고양이", "built_in": False, "threshold": None, "weight": None}).usable)

    def test_a_default_tag_blends_its_probe_with_zero_shot_until_fifteen_examples(self):
        space = Space(seed=11)
        positives = space.photos("a", 15)
        learned = train_tag(positives[:5], np.zeros((0, DIM), np.float32), space.photos("b", 64), built_in=True)
        photo = positives[14:15]
        zero_shot = np.zeros((1, 5), dtype=np.float32)
        scores = {}
        for count in (5, 10, 15, 40):
            model = build_model({"tag": "일러스트", "built_in": True, "threshold": None,
                                 "weight": {**learned, "pos_count": count}})
            scores[count] = float(tag_probabilities(photo, model, zero_shot)[0][0])
        probe = float(tag_probabilities(photo, build_model(
            {"tag": "x", "built_in": False, "threshold": None, "weight": learned}), None)[0][0])
        self.assertAlmostEqual(scores[5], probe / 11, places=5)
        self.assertAlmostEqual(scores[10], probe * 6 / 11, places=5)
        self.assertAlmostEqual(scores[15], probe, places=5)
        self.assertAlmostEqual(scores[40], probe, places=5)


class MatrixTests(unittest.TestCase):
    def test_several_tags_per_photo_best_first_and_at_most_the_limit(self):
        space = Space(seed=13)
        photos = space.photos("p", 4)
        always = [TagModel(f"t{index}", regime="neighbor", positives=photos) for index in range(10)]
        found = score_embeddings_matrix(photos, always, None)
        self.assertEqual(len(found), 4)
        self.assertTrue(all(len(row) == tag_learner.MAX_TAGS_PER_PHOTO for row in found))
        two = score_embeddings_matrix(photos[:1], always[:2], None)[0]
        self.assertEqual([tag for tag, _score in two], ["t0", "t1"])
        self.assertEqual(score_embeddings_matrix(photos, [], None), [[], [], [], []])
        self.assertEqual(score_embeddings_matrix(np.zeros((2, 512), np.float32), always, None), [])
        single = score_embeddings_matrix(photos[0], always[:1], None)
        self.assertEqual(len(single), 1)

    def test_stored_bytes_become_a_matrix_and_bad_ones_are_skipped(self):
        good = np.ones(DIM, dtype=np.float32).tobytes()
        matrix = tag_learner.as_matrix([good, b"\0" * 2048, memoryview(good), None])
        self.assertEqual(matrix.shape, (2, DIM))
        self.assertEqual(tag_learner.as_matrix([]).shape, (0, DIM))

    def test_a_sample_is_the_same_for_the_same_seed(self):
        first = tag_learner.sample_rows(100, 10, "고양이")
        self.assertEqual(list(first), list(tag_learner.sample_rows(100, 10, "고양이")))
        self.assertEqual(len(set(first)), 10)
        self.assertEqual(list(tag_learner.sample_rows(5, 10, "x")), [0, 1, 2, 3, 4])

    def test_narrow_cone_normalized_embeddings_converge_without_oscillation(self):
        rng = np.random.default_rng(2026)
        shared = rng.standard_normal(DIM)
        shared /= np.linalg.norm(shared)
        pos_dir = rng.standard_normal(DIM)
        pos_dir /= np.linalg.norm(pos_dir)
        neg_dir = -pos_dir

        def make_cluster(direction, n):
            raw = 0.90 * shared + 0.35 * direction + 0.12 * rng.standard_normal((n, DIM))
            norms = np.linalg.norm(raw, axis=1, keepdims=True)
            return (raw / norms).astype(np.float32)

        positives = make_cluster(pos_dir, 12)
        backgrounds = make_cluster(neg_dir, 80)
        learned = train_tag(positives, np.zeros((0, DIM), np.float32), backgrounds, built_in=False)
        self.assertEqual(learned["regime"], "linear")
        model = build_model({"tag": "고양이", "built_in": False, "threshold": None, "weight": learned})
        self.assertTrue(model.usable)
        self.assertTrue(np.all(np.isfinite(np.frombuffer(learned["weights"], dtype=np.float32))))
        self.assertTrue(np.isfinite(learned["bias"]))
        pos_scores, _ = tag_probabilities(positives, model, None)
        bg_scores, _ = tag_probabilities(backgrounds, model, None)
        self.assertGreater(float(np.mean(pos_scores)), float(np.mean(bg_scores)))

    def test_non_finite_embeddings_are_skipped_and_rejected(self):
        good = np.ones(DIM, dtype=np.float32)
        good /= np.linalg.norm(good)
        bad_nan = good.copy()
        bad_nan[0] = np.nan
        bad_inf = good.copy()
        bad_inf[1] = np.inf
        matrix = tag_learner.as_matrix([good.tobytes(), bad_nan.tobytes(), bad_inf.tobytes()])
        self.assertEqual(matrix.shape, (1, DIM))
        nan_model = TagModel("고양이", regime="linear", weights=bad_nan.tobytes(), bias=0.0)
        self.assertFalse(nan_model.usable)
        inf_bias_model = TagModel("고양이", regime="linear", weights=good.tobytes(), bias=float("inf"))
        self.assertFalse(inf_bias_model.usable)


if __name__ == "__main__":
    unittest.main()
