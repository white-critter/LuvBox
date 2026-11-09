"""Gallery catalog reads check each folder once and decode without reopening the catalog."""

from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox.history import HistoryStore
from luvbox.qt_library import LibraryRepository, _Request
from luvbox.storage import resolve_storage


class LibraryRepositoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        state = patch("luvbox.protection.state_directory", return_value=self.base / "state" / "protection")
        state.start()
        self.addCleanup(state.stop)
        self.paths = resolve_storage(self.base / "Likes", "offline")
        self.files = []
        with closing(HistoryStore.for_storage(self.paths)) as store:
            for index in range(6):
                path = self.paths.media_dir / f"{100 + index}_1.jpg"
                Image.new("RGB", (64, 48), (200, 40, 40)).save(path, "JPEG")
                store.record_media("flat", {"tweet_id": str(100 + index), "num": 1, "type": "photo",
                                            "author": {"name": "alice"}}, path)
                self.files.append(path)
        self.registered = {}
        self.repository = LibraryRepository()
        self.repository.provider = self
        self.repository.configure(_Request(1, "offline", str(self.paths.output_dir), False))

    def register(self, key, path):
        self.registered[key] = path

    def test_a_page_checks_each_folder_once_and_registers_every_file(self):
        original = HistoryStore._real_folders
        calls = []

        def counted(store, folder):
            calls.append(folder)
            return original(store, folder)

        with patch.object(HistoryStore, "_real_folders", counted):
            total, rows = self.repository.read("1", 0, 100)
        self.assertEqual((total, len(rows)), (6, 6))
        # The catalog's own folder and the one media folder, not every file.
        self.assertLessEqual(len(calls), 2)
        self.assertEqual(sorted(self.registered.values()), sorted(path.resolve() for path in self.files))
        self.assertFalse(any("파일 없음" in row["caption"] for row in rows))

    def test_a_linked_media_folder_is_still_refused(self):
        media = self.paths.media_dir
        is_symlink = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: path == media or is_symlink(path)):
            total, rows = self.repository.read("1", 0, 100)
        self.assertEqual((total, len(rows)), (6, 6))
        self.assertEqual(self.registered, {})
        self.assertTrue(all("파일 없음" in row["caption"] for row in rows))

    def test_captions_show_the_post_date_and_fall_back_to_the_file_name(self):
        with closing(HistoryStore.for_storage(self.paths)) as store:
            path = self.paths.media_dir / "artist_200_1.jpg"
            Image.new("RGB", (8, 8)).save(path, "JPEG")
            store.record_media("flat", {"tweet_id": "200", "num": 1, "type": "photo", "author": {"name": "artist"},
                                        "date": datetime(2026, 9, 30, 15, 30, tzinfo=timezone.utc)}, path)
        _total, rows = self.repository.read("1", 0, 100)
        captions = {row["key"].split("_")[2]: row["caption"] for row in rows}
        # 15:30 UTC is the next day in Korea, where the stored times are kept.
        self.assertEqual(captions["200"], "@artist · 2026-10-01")
        self.assertEqual(captions["100"], "@alice · 100_1.jpg")
        key = next(row["key"] for row in rows if row["key"].split("_")[2] == "200")
        selected = self.repository.selected(key, 1)
        self.assertTrue(selected["public_info"].startswith("artist_200_1.jpg · 2026-10-01 00:30"))
        self.assertIn("8×8", selected["public_info"])
        self.assertIn("631 B", selected["public_info"])

    def test_gif_selection_reports_the_screen_without_decoding_it(self):
        import struct
        path = self.paths.media_dir / "alice_300_1.gif"
        path.write_bytes(b"GIF89a" + struct.pack("<HH", 4000, 1000) + bytes(3))
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store.record_media("flat", {"tweet_id": "300", "num": 1, "type": "animated_gif",
                                        "author": {"name": "alice"}}, path)
        _total, rows = self.repository.read("1", 0, 100)
        key = next(row["key"] for row in rows if "_300_" in row["key"])
        with patch("PIL.Image.open", side_effect=AssertionError("decoded pixels")):
            selected = self.repository.selected(key, 1)
        self.assertEqual(path.read_bytes()[6:10], struct.pack("<HH", 4000, 1000))
        self.assertEqual((selected["source_width"], selected["source_height"]), (4000, 1000))
        self.assertEqual((selected["pixel_width"], selected["pixel_height"]), (4000, 1000))
        self.assertFalse(selected["missing"])

    def test_a_missing_file_the_person_hid_stays_out_of_the_gallery(self):
        self.files[0].unlink()
        self.files[1].unlink()
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store.import_legacy(refresh=True)
            store.set_missing_ignored([("100", 1, "photo")], True)
        total, rows = self.repository.read("1", 0, 100)
        self.assertEqual(total, 5)
        self.assertNotIn("100", {row["key"].split("_")[2] for row in rows})
        # A file that is merely missing is still shown, marked as such.
        self.assertTrue(any(row["key"].split("_")[2] == "101" and "파일 없음" in row["caption"] for row in rows))

    def test_decode_uses_the_registered_file_without_reopening_the_catalog(self):
        self.repository.read("1", 0, 100)
        path = next(iter(self.registered.values()))
        with patch("luvbox.qt_library._ReadOnlyHistory", side_effect=AssertionError("catalog reopened")):
            result = self.repository.decode(path, (32, 24))
            self.assertIsNotNone(result.image)
            result.image.close()
            self.repository.configure(None)
            self.assertIsNone(self.repository.decode(path, (32, 24)).image)

    def test_exact_author_and_inclusive_dates_apply_to_every_page(self):
        korea = timezone(timedelta(hours=9))
        with closing(HistoryStore.for_storage(self.paths)) as store:
            for ident, author, day in (("200", "alice", 1), ("201", "alice2", 1),
                                       ("202", "alice", 2), ("203", "ALICE", 1)):
                path = self.paths.media_dir / f"{ident}_1.jpg"
                Image.new("RGB", (8, 8)).save(path, "JPEG")
                store.record_media("flat", {"tweet_id": ident, "num": 1, "type": "photo",
                                            "author": {"name": author},
                                            "date": datetime(2026, 10, day, 23, 59, tzinfo=korea)}, path)
        self.repository.configure(_Request(2, "offline", str(self.paths.output_dir), False,
                                           author="alice", date_from="2026-10-01", date_to="2026-10-01"))
        first_total, first = self.repository.read("2", 0, 1)
        second_total, second = self.repository.read("2", 1, 1)
        self.assertEqual((first_total, second_total), (2, 2))
        self.assertEqual({row["key"].split("_")[2] for row in first + second}, {"200", "203"})
        self.assertNotEqual(first[0]["key"], second[0]["key"])

    def test_saved_date_filter_uses_saved_time_independently_of_posted_time(self):
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store._db.execute("UPDATE media SET saved_at='2026-10-05T12:00:00+09:00' WHERE tweet_id='100'")
            store._db.commit()
        self.repository.configure(_Request(2, "offline", str(self.paths.output_dir), False,
                                           date_field="saved", date_from="2026-10-05", date_to="2026-10-05"))
        total, rows = self.repository.read("2", 0, 100)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["key"].split("_")[2], "100")

    def test_deletion_manifest_requires_displayed_keys_and_preserves_native_signature(self):
        key = "g1_flat_100_1_photo"
        with self.assertRaisesRegex(ValueError, "다시 선택"):
            self.repository.deletion_manifest([key])
        self.repository.read("1", 0, 100)
        result = self.repository.deletion_manifest([key, key])
        self.assertEqual(result["location"], self.paths)
        self.assertEqual(result["generation"], 1)
        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        info = self.files[0].stat()
        self.assertEqual(row["_delete_signature"], (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
        self.assertTrue(all(type(part) is int for part in row["_delete_signature"]))
        self.assertEqual(row["path"], str(self.files[0]))

    def test_recycle_selection_excludes_missing_and_unserved_items(self):
        self.files[0].unlink()
        _, rows = self.repository.read("1", 0, 100)
        keys = [row["key"] for row in rows]
        selectable, unavailable, overflow = self.repository.selectable_keys(keys + ["g1_flat_999_1_photo"])
        self.assertEqual(unavailable, 2)
        self.assertFalse(overflow)
        self.assertEqual(len(selectable), 5)
        self.assertNotIn("g1_flat_100_1_photo", selectable)
        self.assertNotIn("g1_flat_999_1_photo", selectable)
        result = self.repository.deletion_manifest(selectable)
        self.assertEqual(len(result["rows"]), 5)

    def test_deletion_manifest_rejects_file_changes_after_display_and_context_changes(self):
        self.repository.read("1", 0, 100)
        with self.files[0].open("ab") as stream:
            stream.write(b"changed after selection")
        with self.assertRaisesRegex(ValueError, "파일이 바뀌"):
            self.repository.deletion_manifest(["g1_flat_100_1_photo"])
        self.repository.configure(_Request(2, "offline", str(self.paths.output_dir), False))
        with self.assertRaisesRegex(ValueError, "다시 선택"):
            self.repository.deletion_manifest(["g1_flat_101_1_photo"])

    def test_deletion_manifest_rechecks_context_after_file_inspection(self):
        from luvbox.recycling import file_signature

        self.repository.read("1", 0, 100)
        def changed_context(path):
            result = file_signature(path)
            self.repository.configure(_Request(2, "offline", str(self.paths.output_dir), False))
            return result
        with patch("luvbox.qt_library.file_signature", side_effect=changed_context):
            with self.assertRaisesRegex(ValueError, "다시 선택"):
                self.repository.deletion_manifest(["g1_flat_100_1_photo"])

    def test_deletion_manifest_does_not_redirect_a_selected_identity_to_a_new_path(self):
        self.repository.read("1", 0, 100)
        replacement = self.paths.media_dir / "replacement_100_1.jpg"
        Image.new("RGB", (8, 8)).save(replacement, "JPEG")
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store._db.execute("UPDATE media SET path=? WHERE tweet_id='100'", (str(replacement),))
            store._db.commit()
        with self.assertRaisesRegex(ValueError, "새로 고침"):
            self.repository.deletion_manifest(["g1_flat_100_1_photo"])

    def test_scrolling_keeps_its_place_when_a_newer_file_is_saved(self):
        _total, first = self.repository.read("1", 0, 2)
        first_ids = [row["key"].split("_")[2] for row in first]
        with closing(HistoryStore.for_storage(self.paths)) as store:
            path = self.paths.media_dir / "999_1.jpg"
            Image.new("RGB", (8, 8)).save(path, "JPEG")
            store.record_media("flat", {"tweet_id": "999", "num": 1, "type": "photo",
                                        "author": {"name": "alice"}}, path)
        _total, second = self.repository.read("1", 2, 2)
        second_ids = [row["key"].split("_")[2] for row in second]
        self.assertEqual(first_ids, ["105", "104"])
        self.assertEqual(second_ids, ["103", "102"])
        self.assertNotIn("999", second_ids)

    def test_cloud_only_files_are_not_described_as_deleted(self):
        with patch("luvbox.qt_library.cloud_only", return_value=True):
            _total, rows = self.repository.read("1", 0, 100)
            selected = self.repository.selected(rows[0]["key"], 1)
        self.assertTrue(rows)
        self.assertTrue(all("클라우드에만 있음" in row["caption"] for row in rows))
        self.assertFalse(any("파일 없음" in row["caption"] for row in rows))
        self.assertIn("클라우드", selected["missing_note"])
        self.assertNotIn("지우거나", selected["missing_note"])
        self.assertEqual(selected["preview"], "")


if __name__ == "__main__":
    unittest.main()
