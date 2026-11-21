"""The shared AI profile: which AI tags exist, what they were taught, and what they learned.

One small SQLite file in the app data folder, read by the window at start (so it
must not load numpy or onnxruntime) and shared by every library and the folder tab.
"""

from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox import ai_profile
from luvbox.ai_profile import AiProfileError, AiProfileStore
from luvbox.clip_model import ACTIVE_MODEL_ID, CATEGORY_NAMES, LEGACY_MODEL_ID

ROOT = Path(__file__).resolve().parents[1]
FEATURE = b"\x01" * 3072


def example(example_id, tag, label=1, blob=FEATURE, kind="x", scope="lib/a"):
    return (example_id, kind, scope, tag, label, ACTIVE_MODEL_ID, blob)


class ProfileTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="luvbox-ai-profile-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)

    def open(self):
        store = AiProfileStore()
        self.addCleanup(store.close)
        return store


class TagTests(ProfileTestCase):
    def test_it_lives_in_the_app_data_folder_and_starts_with_the_five_defaults(self):
        store = self.open()
        self.assertEqual(store.path, self.root / "LuvBox" / "ai-profile" / "profile.sqlite3")
        self.assertEqual(ai_profile.profile_path(), store.path)
        self.assertEqual(store.active_tag_names(), CATEGORY_NAMES)
        rows = store.active_tags()
        self.assertTrue(all(row["built_in"] and row["enabled"] for row in rows))
        self.assertEqual([row["regime"] for row in rows], ["zeroshot"] * 5)
        self.assertEqual(store.meta("schema_version"), "1")
        self.assertEqual(store.meta("active_model_id"), ACTIVE_MODEL_ID)

    def test_a_deleted_default_stays_deleted_after_every_start_until_restored(self):
        store = self.open()
        self.assertTrue(store.delete_tag("동물"))
        self.assertNotIn("동물", store.active_tag_names())
        store.close()
        for _start in range(3):
            with closing(AiProfileStore()) as again:
                self.assertNotIn("동물", again.active_tag_names())
                self.assertEqual(again.deleted_builtin_count(), 1)
        with closing(AiProfileStore()) as again:
            self.assertEqual(again.restore_builtin_tags(), 1)
            self.assertEqual(again.active_tag_names(), CATEGORY_NAMES)
            self.assertEqual(again.restore_builtin_tags(), 0)

    def test_adding_a_deleted_default_by_name_brings_it_back(self):
        store = self.open()
        store.delete_tag("인물")
        row = store.add_tag("#인물")
        self.assertEqual((row["tag"], row["built_in"], row["deleted"], row["enabled"]), ("인물", 1, 0, 1))
        self.assertEqual(store.active_tag_names(), CATEGORY_NAMES)

    def test_tags_of_ones_own_are_cleaned_like_catalog_tags_and_listed_after_the_defaults(self):
        store = self.open()
        self.assertEqual(store.add_tag("  #고양이\t")["tag"], "고양이")
        self.assertEqual(store.add_tag("퍼슈트")["tag"], "퍼슈트")
        for bad in ("", "#", "   ", "x" * 51, None, 7):
            self.assertIsNone(store.add_tag(bad), bad)
        self.assertEqual(store.active_tag_names(), CATEGORY_NAMES + ["고양이", "퍼슈트"])
        custom = store.active_tags()[-1]
        self.assertFalse(custom["built_in"])
        self.assertTrue(custom["needs_examples"])
        self.assertEqual(custom["regime"], "none")

    def test_switching_off_keeps_the_tag_and_its_examples(self):
        store = self.open()
        store.add_tag("고양이")
        store.upsert_examples([example("x:1", "고양이")])
        self.assertTrue(store.set_tag_enabled("고양이", False))
        self.assertNotIn("고양이", store.active_tag_names())
        self.assertIn("고양이", store.known_tag_names())
        self.assertFalse(next(r for r in store.active_tags(include_disabled=True) if r["tag"] == "고양이")["enabled"])
        self.assertEqual(store.example_counts()["고양이"], (1, 0))
        self.assertFalse(store.set_tag_enabled("없는 태그", True))

    def test_deleting_ones_own_tag_removes_it_and_optionally_its_examples(self):
        store = self.open()
        for name in ("고양이", "강아지"):
            store.add_tag(name)
            store.upsert_examples([example("x:1", name)])
            store.save_tag_weight(name, ACTIVE_MODEL_ID, "neighbor", b"", 0.0, 0.65, 1, 0)
        self.assertTrue(store.delete_tag("고양이"))
        self.assertTrue(store.delete_tag("강아지", purge_examples=True))
        self.assertFalse(store.delete_tag("고양이"))
        self.assertEqual(store.known_tag_names(), CATEGORY_NAMES)
        self.assertEqual(store.load_tag_weights(), {})
        self.assertEqual(store.example_counts(), {"고양이": (1, 0)})
        # Added again, it finds its examples where they were.
        store.add_tag("고양이")
        self.assertEqual(next(r for r in store.active_tags() if r["tag"] == "고양이")["pos_count"], 1)

    def test_a_threshold_of_ones_own_is_kept_within_bounds(self):
        store = self.open()
        self.assertTrue(store.set_tag_threshold("인물", 0.8))
        self.assertEqual(next(r for r in store.active_tags() if r["tag"] == "인물")["threshold"], 0.8)
        for bad in (0.01, 0.99, "x"):
            self.assertFalse(store.set_tag_threshold("인물", bad))
        self.assertTrue(store.set_tag_threshold("인물", None))


class VersionTests(ProfileTestCase):
    def test_every_change_moves_the_rules_version_and_a_repeat_does_not(self):
        store = self.open()
        versions = [store.rules_version()]

        def moved(action):
            action()
            versions.append(store.rules_version())
            return versions[-1] > versions[-2]

        self.assertTrue(moved(lambda: store.add_tag("고양이")))
        self.assertFalse(moved(lambda: store.add_tag("고양이")))
        self.assertTrue(moved(lambda: store.set_tag_enabled("고양이", False)))
        self.assertFalse(moved(lambda: store.set_tag_enabled("고양이", False)))
        self.assertTrue(moved(lambda: store.upsert_examples([example("x:1", "고양이")])))
        self.assertFalse(moved(lambda: store.upsert_examples([example("x:1", "고양이")])))
        self.assertTrue(moved(lambda: store.upsert_examples([example("x:1", "고양이", label=-1)])))
        self.assertTrue(moved(lambda: store.remove_examples([("x:1", "고양이")])))
        self.assertFalse(moved(lambda: store.remove_examples([("x:1", "고양이")])))
        self.assertTrue(moved(lambda: store.delete_tag("고양이")))
        self.assertTrue(moved(lambda: store.restore_builtin_tags() or store.delete_tag("동물")))
        self.assertTrue(moved(lambda: store.restore_builtin_tags()))
        self.assertFalse(moved(lambda: store.restore_builtin_tags()))
        self.assertTrue(moved(lambda: store.save_tag_weight("인물", ACTIVE_MODEL_ID, "zeroshot", b"", 0, 0.65, 0, 0)))
        # The version is shared: another open of the file reads the same number.
        with closing(AiProfileStore()) as again:
            self.assertEqual(again.rules_version(), versions[-1])


class ExampleTests(ProfileTestCase):
    def test_only_well_formed_examples_of_the_current_model_size_are_kept(self):
        store = self.open()
        rows = [example("x:1", "인물"), example("x:2", "인물", blob=b"\0" * 2048), example("x:3", "인물", label=0),
                example("", "인물"), example("x:4", ""), ("x:5", "elsewhere", "s", "인물", 1, ACTIVE_MODEL_ID, FEATURE),
                ("x:6", "x", "s", "인물", 1, LEGACY_MODEL_ID, b"\0" * 2048)]
        self.assertEqual(store.upsert_examples(rows), 2)
        self.assertEqual(store.example_counts(), {"인물": (1, 0)})
        self.assertEqual(store.example_counts(LEGACY_MODEL_ID), {"인물": (1, 0)})
        positives, negatives = store.training_data_for_tag("인물")
        self.assertEqual((positives, negatives), ([FEATURE], []))

    def test_one_scope_is_made_to_match_and_the_others_are_left_alone(self):
        store = self.open()
        store.add_tag("고양이")
        store.upsert_examples([example("x:a1", "인물"), example("x:a2", "고양이"),
                               example("x:b1", "인물", scope="lib/b"), example("x:a3", "동물")])
        version = store.rules_version()
        written, removed = store.sync_scope("x", "lib/a", ["인물", "고양이"], [
            ("x:a1", "인물", -1, ACTIVE_MODEL_ID, FEATURE),         # changed label
            ("x:a4", "고양이", 1, ACTIVE_MODEL_ID, FEATURE),        # new
            ("x:a5", "풍경·배경", 1, ACTIVE_MODEL_ID, FEATURE),     # a tag outside this sync: ignored
        ])
        self.assertEqual((written, removed), (2, 1))                # x:a2 is no longer an example
        self.assertGreater(store.rules_version(), version)
        found = store.examples_for(["x:a1", "x:a2", "x:a3", "x:a4", "x:a5", "x:b1"])
        self.assertEqual(found, {"x:a1": {"인물": -1}, "x:a3": {"동물": 1}, "x:a4": {"고양이": 1},
                                 "x:b1": {"인물": 1}})
        version = store.rules_version()
        self.assertEqual(store.sync_scope("x", "lib/a", ["인물", "고양이"], [
            ("x:a1", "인물", -1, ACTIVE_MODEL_ID, FEATURE), ("x:a4", "고양이", 1, ACTIVE_MODEL_ID, FEATURE)]), (0, 0))
        self.assertEqual(store.rules_version(), version)
        self.assertEqual(store.remove_scope_examples("x", "lib/b"), 1)

    def test_other_tags_positives_are_background_unless_taught_for_this_tag(self):
        store = self.open()
        other = b"\x02" * 3072
        store.upsert_examples([example("x:1", "인물", blob=other), example("x:1", "동물", blob=other),
                               example("x:2", "동물", blob=FEATURE), example("x:3", "일러스트", label=-1)])
        self.assertEqual(store.other_examples("인물"), [FEATURE])
        self.assertEqual(store.other_examples("인물", limit=0), [])


class LearnedTests(ProfileTestCase):
    def test_a_tag_is_dirty_until_learned_and_again_once_its_examples_change(self):
        store = self.open()
        store.add_tag("고양이")
        # Defaults need a zero-shot row once; a tag of ones own with no example waits.
        self.assertEqual(store.dirty_tags(), CATEGORY_NAMES)
        for name in CATEGORY_NAMES:
            store.save_tag_weight(name, ACTIVE_MODEL_ID, "zeroshot", b"", 0.0, 0.65, 0, 0)
        self.assertEqual(store.dirty_tags(), [])
        store.upsert_examples([example("x:1", "고양이")])
        self.assertEqual(store.dirty_tags(), ["고양이"])
        store.save_tag_weight("고양이", ACTIVE_MODEL_ID, "neighbor", b"", 0.0, 0.65, 1, 0)
        self.assertEqual(store.dirty_tags(), [])
        # A removed example leaves no newer time behind, only a different count.
        store.remove_examples([("x:1", "고양이")])
        self.assertEqual(store.dirty_tags(), ["고양이"])
        store.save_tag_weight("고양이", ACTIVE_MODEL_ID, "neighbor", b"", 0.0, 0.65, 0, 0)
        store.upsert_examples([example("x:2", "인물")])
        self.assertEqual(store.dirty_tags(), ["인물"])
        # A switched-off tag is not learned.
        store.set_tag_enabled("인물", False)
        self.assertEqual(store.dirty_tags(), [])

    def test_learned_weights_round_trip_and_a_linear_one_must_have_its_size(self):
        store = self.open()
        weights = bytes(range(256)) * 12
        self.assertTrue(store.save_tag_weight("인물", ACTIVE_MODEL_ID, "linear", weights, -1.5, 0.7, 9, 2))
        self.assertFalse(store.save_tag_weight("인물", ACTIVE_MODEL_ID, "linear", b"\0" * 10, 0, 0.7, 9, 2))
        self.assertFalse(store.save_tag_weight("인물", ACTIVE_MODEL_ID, "unknown", b"", 0, 0.7, 9, 2))
        loaded = store.load_tag_weights()["인물"]
        self.assertEqual((loaded["weights"], loaded["bias"], loaded["threshold"], loaded["pos_count"]),
                         (weights, -1.5, 0.7, 9))
        self.assertEqual(store.load_tag_weights(LEGACY_MODEL_ID), {})
        rules = {rule["tag"]: rule for rule in store.scoring_rules()}
        self.assertEqual(list(rules), CATEGORY_NAMES)
        self.assertEqual(rules["인물"]["weight"]["regime"], "linear")
        self.assertIsNone(rules["동물"]["weight"])
        self.assertTrue(store.delete_tag_weight("인물"))
        self.assertIn("인물", json.loads(store.summary())["tags"][1])


@unittest.skipUnless(os.name == "nt", "Windows junctions")
class LinkTests(ProfileTestCase):
    def test_a_junctioned_profile_folder_is_refused(self):
        import _winapi

        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.root / "LuvBox").mkdir()
        _winapi.CreateJunction(str(elsewhere), str(self.root / "LuvBox" / "ai-profile"))
        with self.assertRaises(AiProfileError):
            AiProfileStore()
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_hard_linked_database_is_refused(self):
        store = AiProfileStore()
        path = store.path
        store.close()
        os.link(path, self.root / "copy.sqlite3")
        with self.assertRaises(AiProfileError):
            AiProfileStore()


class LightImportTests(unittest.TestCase):
    def test_the_profile_is_read_without_numpy_or_onnxruntime(self):
        code = ("import json, sys, os, tempfile\n"
                "os.environ['LOCALAPPDATA'] = tempfile.mkdtemp()\n"
                "from luvbox.ai_profile import AiProfileStore\n"
                "with AiProfileStore() as store:\n"
                "    names = store.active_tag_names(); store.add_tag('고양이'); store.active_tags()\n"
                "print(json.dumps([n for n in ('numpy', 'onnxruntime', 'luvbox.clip_classifier',\n"
                "                              'luvbox.tag_learner') if n in sys.modules]))\n")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1]), [])


if __name__ == "__main__":
    unittest.main()
