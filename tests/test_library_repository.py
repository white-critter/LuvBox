"""Gallery catalog reads check each folder once and decode without reopening the catalog."""

from contextlib import closing
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

    def test_decode_uses_the_registered_file_without_reopening_the_catalog(self):
        self.repository.read("1", 0, 100)
        path = next(iter(self.registered.values()))
        with patch("luvbox.qt_library._ReadOnlyHistory", side_effect=AssertionError("catalog reopened")):
            result = self.repository.decode(path, (32, 24))
            self.assertIsNotNone(result.image)
            result.image.close()
            self.repository.configure(None)
            self.assertIsNone(self.repository.decode(path, (32, 24)).image)


if __name__ == "__main__":
    unittest.main()
