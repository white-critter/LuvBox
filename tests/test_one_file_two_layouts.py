"""One file recorded under two folder layouts is one photo, everywhere.

The shape comes from a real library: photos first saved by author folder were
later moved into one folder, the old records were reconnected to the new place,
and every later save in the flat layout added a second record for the same
path. A duplicate scan paired each file with itself, the gallery showed it
twice, and sending the "copy" to the Recycle Bin removed the only file.
"""

from contextlib import closing
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # noqa: F401  Never open the user's app data.
from luvbox import deletion
from luvbox.history import HistoryStore
from luvbox.qt_library import LibraryRepository, _Request
from luvbox.recycling import file_signature
from luvbox.storage import resolve_storage

ACCOUNT = "offline"
TWICE, ONCE = 40, 15           # files with two records, files with one
GONE = 6                       # media whose file is gone: a record in each layout, no path


class TwoLayoutCatalog(unittest.TestCase):
    """A small library with the same proportions as the real one."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="luvbox-two-layouts-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "appdata")})
        environment.start()
        self.addCleanup(environment.stop)
        self.paths = resolve_storage(self.root / "Likes", ACCOUNT)
        self.files = {}
        with closing(HistoryStore.for_storage(self.paths)) as store:
            for index in range(TWICE + ONCE):
                tweet = str(1000 + index)
                path = self.paths.output_dir / f"writer{index % 7}_{tweet}_1.jpg"
                path.write_bytes(f"picture {tweet}".encode())
                self.files[tweet] = path
                details = {"tweet_id": tweet, "num": 1, "type": "photo", "author": {"name": f"writer{index % 7}"},
                           "date": f"2026-09-{1 + index % 28:02d}T12:00:00+09:00"}
                if index < TWICE:
                    # Saved by author folder, moved to one folder, then met again by a flat save.
                    store.record_media("author", details, path)
                    store.record_media("flat", details, path, status="existing")
                else:
                    store.record_media("flat", details, path)
            # A real second copy: another tweet with exactly the same bytes as tweet 1000.
            copy = self.paths.output_dir / "writer0_2000_1.jpg"
            copy.write_bytes(self.files["1000"].read_bytes())
            self.files["2000"] = copy
            store.record_media("flat", {"tweet_id": "2000", "num": 1, "type": "photo", "author": {"name": "writer0"},
                                        "date": "2026-10-01T12:00:00+09:00"}, copy)
            for index in range(GONE):
                tweet = str(3000 + index)
                for mode in ("author", "flat"):
                    store._upsert(store._legacy_row(mode, tweet, 1), replace_path=False)
            store._db.commit()
            # An old download record of the other layout for a photo that is right here.
            store._upsert(store._legacy_row("author", str(1000 + TWICE), 1), replace_path=False)
            store._db.commit()
        self.present = TWICE + ONCE + 1
        self.kept = self.root / "recycled"
        self.kept.mkdir()

    def store(self):
        return closing(HistoryStore.for_storage(self.paths, create_media=False))

    def repository(self, **filters):
        repository = LibraryRepository()
        self.addCleanup(repository.configure, None)
        repository.provider = self
        repository.configure(_Request(generation=1, account=ACCOUNT, output_dir=str(self.paths.output_dir),
                                      separate_accounts=False, **filters))
        return repository

    def register(self, key, path):
        pass

    def stale_groups(self, store):
        """What versions up to 1.9.3 stored: both records of a file hashed and grouped together."""
        hashes, groups = [], []
        for index in range(TWICE):
            tweet = str(1000 + index)
            size = self.files[tweet].stat().st_size
            for mode in ("author", "flat"):
                hashes.append((mode, tweet, 1, "photo", size, f"digest-{tweet}", "", ""))
            groups.append([("author", tweet, 1, "photo"), ("flat", tweet, 1, "photo")])
        size = self.files["2000"].stat().st_size
        hashes.append(("flat", "2000", 1, "photo", size, "digest-1000", "", ""))
        groups[0].append(("flat", "2000", 1, "photo"))
        store.save_image_hashes(hashes)
        store.save_clusters(groups)
        store.set_duplicate_rule("exact")

    def recycle(self, path, expected=None):
        path = Path(path)
        self.assertEqual(file_signature(path), tuple(expected))
        destination = self.kept / path.name
        path.rename(destination)
        return str(destination)

    def delete(self, manifest):
        with patch.object(deletion, "recycle_file", side_effect=self.recycle):
            return deletion.run_deletion(manifest["location"], manifest["rows"], True, threading.Event(), lambda _: None)


class GalleryTests(TwoLayoutCatalog):
    def test_the_gallery_lists_each_file_once(self):
        with self.store() as store:
            page = store.record_page(0, 1000, include_deleted=False)
            paths = [row["path"] for row in page["rows"] if row["path"]]
            self.assertEqual(len(paths), self.present)
            self.assertEqual(len(set(paths)), self.present)
            # A photo missing everywhere is one entry; a photo that is here has no "missing" entry.
            missing = [row["tweet_id"] for row in page["rows"] if not row["path"]]
            self.assertEqual(sorted(missing), [str(3000 + index) for index in range(GONE)])
            self.assertEqual(page["total"], self.present + GONE)

    def test_pages_through_the_cursor_show_every_file_exactly_once(self):
        repository = self.repository()
        seen, offset = [], 0
        while True:
            total, rows = repository.read("1", offset, 10)
            if not rows:
                break
            seen.extend(row["key"] for row in rows)
            offset += len(rows)
        self.assertEqual(total, self.present + GONE)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), self.present + GONE)

    def test_the_record_shown_carries_the_save_time(self):
        with self.store() as store:
            rows = {row["tweet_id"]: row for row in store.record_page(0, 1000, include_deleted=False)["rows"]}
            self.assertEqual(rows["1000"]["folder_mode"], "author")
            self.assertIsNotNone(rows["1000"]["saved_at"])

    def test_author_and_statistics_counts_agree_with_the_files(self):
        with self.store() as store:
            self.assertEqual(sum(item["count"] for item in store.authors()), self.present)
            self.assertEqual(store.summary()["files"], self.present)
            self.assertEqual(len(store.photo_records_for_scan()), self.present)
            self.assertEqual(len(store.unclassified_photo_records()), self.present)
            self.assertEqual(len(store.photos_without_embeddings()), self.present)

    def test_a_favorite_or_tag_set_on_either_record_belongs_to_the_file(self):
        with self.store() as store:
            # Set through the record an earlier version listed.
            store.toggle_favorite("flat", "1001", 1, "photo")
            store.add_tag("flat", "1002", 1, "photo", "바다")
            page = store.record_page(0, 1000, include_deleted=False, favorite_only=True)
            self.assertEqual([row["tweet_id"] for row in page["rows"]], ["1001"])
            shown = page["rows"][0]
            self.assertTrue(store.is_favorite(shown["folder_mode"], "1001", 1, "photo"))
            self.assertEqual(store.favorite_identities_among([(shown["folder_mode"], "1001", 1, "photo")]),
                             {(shown["folder_mode"], "1001", 1, "photo")})
            tagged = store.record_page(0, 1000, include_deleted=False, user_tag="바다")
            self.assertEqual([row["tweet_id"] for row in tagged["rows"]], ["1002"])
            self.assertEqual(store.item_tags(tagged["rows"][0]["folder_mode"], "1002", 1, "photo"),
                             [{"tag": "바다", "source": "user"}])
            self.assertEqual(store.user_tags(), [{"tag": "바다", "count": 1}])
            # Taking them away through the shown record leaves nothing behind on the other one.
            self.assertFalse(store.toggle_favorite(shown["folder_mode"], "1001", 1, "photo"))
            self.assertEqual(store.record_page(0, 10, include_deleted=False, favorite_only=True)["total"], 0)
            self.assertTrue(store.remove_tag(tagged["rows"][0]["folder_mode"], "1002", 1, "photo", "바다"))
            self.assertEqual(store.record_page(0, 10, include_deleted=False, user_tag="바다")["total"], 0)

    def test_ai_counts_are_per_file(self):
        with self.store() as store:
            store.save_ai_tags([(mode, "1003", 1, "photo", "인물") for mode in ("author", "flat")])
            self.assertEqual(store.ai_tag_stats(), {"인물": 1})
            self.assertEqual(store.record_page(0, 10, include_deleted=False, ai_category="인물")["total"], 1)


class DuplicateTests(TwoLayoutCatalog):
    def test_a_scan_never_pairs_a_file_with_itself(self):
        repository = self.repository(duplicate_only=True)
        self.assertEqual(repository.scan_duplicates(1), 2)
        with self.store() as store:
            page = store.duplicate_page(0, 100)
            self.assertEqual(sorted(row["tweet_id"] for row in page["rows"]), ["1000", "2000"])
            self.assertEqual(len({row["path"] for row in page["rows"]}), 2)
            self.assertEqual(len(store.duplicate_copy_identities()), 1)

    def test_groups_stored_by_an_earlier_version_no_longer_pair_a_file_with_itself(self):
        with self.store() as store:
            self.stale_groups(store)
            self.assertEqual(store.duplicate_counts(), 2)
            page = store.duplicate_page(0, 100)
            self.assertEqual(sorted(row["tweet_id"] for row in page["rows"]), ["1000", "2000"])
            self.assertEqual({row["cluster_size"] for row in page["rows"]}, {2})
            self.assertEqual(len(store.duplicate_copy_identities()), 1)
            self.assertEqual(store.record_page(0, 100, include_deleted=False, duplicate_only=True)["total"], 2)
            self.assertEqual(store.regroup_exact_duplicates(), 2)

    def test_a_second_name_for_the_same_file_is_not_a_copy(self):
        # A hard link is the same file under another name, recorded by another tweet's record.
        linked = self.paths.output_dir / "writer1_4000_1.jpg"
        try:
            os.link(self.files["1001"], linked)
        except OSError:
            self.skipTest("this folder cannot hold hard links")
        with self.store() as store:
            store.record_media("flat", {"tweet_id": "4000", "num": 1, "type": "photo", "author": {"name": "writer1"},
                                        "date": "2026-10-02T12:00:00+09:00"}, linked)
        repository = self.repository(duplicate_only=True)
        self.assertEqual(repository.scan_duplicates(1), 2)   # only tweets 1000 and 2000
        with self.store() as store:
            self.assertNotIn("4000", {row["tweet_id"] for row in store.duplicate_page(0, 100)["rows"]})

    def test_sending_the_offered_copies_to_the_recycle_bin_keeps_every_only_file(self):
        """The incident, replayed: groups from 1.9.3, the copies the view offers, one confirmation."""
        with self.store() as store:
            self.stale_groups(store)
        repository = self.repository(duplicate_only=True)
        _total, rows = repository.read("1", 0, 1000)
        offered = [key for key in repository.duplicate_copy_keys(1) if key in {row["key"] for row in rows}]
        if offered:
            self.delete(repository.deletion_manifest(offered))
        remaining = {tweet for tweet, path in self.files.items() if path.is_file()}
        # Every photo is still there under at least one name; only the real copy may have gone.
        self.assertTrue(all(str(1000 + index) in remaining for index in range(1, TWICE + ONCE)))
        self.assertTrue({"1000", "2000"} & remaining)
        self.assertLessEqual(len(list(self.kept.iterdir())), 1)
        with self.store() as store:
            self.assertEqual(store.summary()["files"], len(remaining))


class CopyDeletionGuardTests(TwoLayoutCatalog):
    def row(self, tweet, mode="flat"):
        with self.store() as store:
            return store.media_record(mode, {"tweet_id": tweet, "num": 1, "type": "photo"})

    def run_copy_deletion(self, tweet, keepers, rule="exact"):
        row = dict(self.row(tweet), _copy_keepers=[str(path) for path in keepers], _copy_rule=rule)
        return self.delete({"location": self.paths, "rows": [row]})

    def test_a_copy_goes_only_while_another_file_with_the_same_bytes_stays(self):
        result = self.run_copy_deletion("2000", [self.files["1000"]])
        self.assertEqual((result["recycled"], result["protected"]), (1, 0))
        self.assertFalse(self.files["2000"].exists())
        self.assertTrue(self.files["1000"].is_file())

    def test_nothing_else_to_keep_means_the_file_stays(self):
        result = self.run_copy_deletion("1005", [])
        self.assertEqual((result["recycled"], result["protected"], result["failed"]), (0, 1, 0))
        self.assertTrue(self.files["1005"].is_file())
        self.assertIn("그대로 두었습니다", result["message"])
        with self.store() as store:
            self.assertEqual(store.media_record("flat", {"tweet_id": "1005", "num": 1, "type": "photo"})["path"],
                             str(self.files["1005"]))
            self.assertEqual(store.ignored_media(), set())

    def test_the_same_file_under_its_own_or_another_name_is_no_proof(self):
        result = self.run_copy_deletion("1006", [self.files["1006"], str(self.files["1006"]).upper()])
        self.assertEqual((result["recycled"], result["protected"]), (0, 1))
        self.assertTrue(self.files["1006"].is_file())
        linked = self.paths.output_dir / "writer1_4001_1.jpg"
        try:
            os.link(self.files["1007"], linked)
        except OSError:
            self.skipTest("this folder cannot hold hard links")
        result = self.run_copy_deletion("1007", [linked])
        self.assertEqual((result["recycled"], result["protected"]), (0, 1))
        self.assertTrue(self.files["1007"].is_file())

    def test_a_file_with_other_content_or_a_missing_one_is_no_proof(self):
        gone = self.paths.output_dir / "writer0_5000_1.jpg"
        result = self.run_copy_deletion("1008", [self.files["1009"], gone])
        self.assertEqual((result["recycled"], result["protected"]), (0, 1))
        self.assertTrue(self.files["1008"].is_file())

    def test_two_copies_of_each_other_cannot_both_go(self):
        first = dict(self.row("1000", "author"), _copy_keepers=[str(self.files["2000"])], _copy_rule="exact")
        second = dict(self.row("2000"), _copy_keepers=[str(self.files["1000"])], _copy_rule="exact")
        result = self.delete({"location": self.paths, "rows": [first, second]})
        self.assertEqual((result["recycled"], result["protected"]), (1, 1))
        self.assertEqual(sum(self.files[tweet].is_file() for tweet in ("1000", "2000")), 1)

    def test_an_ordinary_deletion_is_not_held_back(self):
        result = self.delete({"location": self.paths, "rows": [self.row("1010")]})
        self.assertEqual((result["recycled"], result["protected"]), (1, 0))
        self.assertFalse(self.files["1010"].exists())

    def test_the_duplicate_view_marks_what_it_sends_and_keeps_one_of_each_group(self):
        repository = self.repository(duplicate_only=True)
        self.assertEqual(repository.scan_duplicates(1), 2)
        _total, rows = repository.read("1", 0, 100)
        manifest = repository.deletion_manifest([row["key"] for row in rows])     # both photos of the group
        self.assertEqual(len(manifest["rows"]), 1)
        self.assertEqual(manifest["kept"], 1)
        self.assertEqual(len(manifest["rows"][0]["_copy_keepers"]), 1)
        result = self.delete(manifest)
        self.assertEqual((result["recycled"], result["protected"]), (1, 0))
        self.assertEqual(sum(self.files[tweet].is_file() for tweet in ("1000", "2000")), 1)

    def test_the_ordinary_gallery_sends_no_copy_mark(self):
        repository = self.repository()
        _total, rows = repository.read("1", 0, 100)
        manifest = repository.deletion_manifest([rows[0]["key"]])
        self.assertNotIn("_copy_keepers", manifest["rows"][0])


if __name__ == "__main__":
    unittest.main()
