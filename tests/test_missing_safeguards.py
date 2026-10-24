"""Files removed outside the app: a moved/disconnected folder, previews kept
on this PC, and what the missing-files list shows. Offline fixtures only."""

import io
import os
import sqlite3
import string
import tempfile
import unittest

from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from tests import test_core as core_support
from luvbox import thumbcache
from luvbox.history import HistoryStore
from luvbox.qt_library import LibraryRepository, _Request
from luvbox.storage import missing_media_help, resolve_storage, saved_media_missing


def jpeg(color=(200, 40, 40), size=(320, 240)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "JPEG")
    return buffer.getvalue()


def free_drive() -> str | None:
    used = set(os.listdrives()) if hasattr(os, "listdrives") else set()
    return next((letter for letter in reversed(string.ascii_uppercase)
                 if f"{letter}:\\" not in used and not Path(f"{letter}:\\").exists()), None)


class StateTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        state = patch("luvbox.protection.state_directory", return_value=self.base / "state" / "protection")
        state.start()
        self.addCleanup(state.stop)
        self.paths = resolve_storage(self.base / "Likes", "offline")

    def saved(self, count=1, *, content=None):
        with closing(HistoryStore.for_storage(self.paths)) as store:
            files = []
            for index in range(count):
                path = self.paths.media_dir / f"{100 + index}_1.jpg"
                path.write_bytes(content or jpeg())
                store.record_media("flat", {"tweet_id": str(100 + index), "num": 1, "type": "photo",
                                            "author": {"name": "alice"}}, path)
                files.append(path)
        return files


class MovedFolderTests(StateTestCase):
    def test_moved_folder_is_reported_and_never_recreated_or_marked_missing(self):
        self.saved(2)
        self.paths.media_dir.rename(self.base / "Likes-moved")
        self.assertTrue(saved_media_missing(self.paths))
        repository = LibraryRepository()
        repository.provider = SimpleNamespace(register=Mock())
        repository.configure(_Request(1, "offline", str(self.paths.output_dir), False))
        count, rows = repository.read("1", 0, 100)
        # Qt browsing reads the catalog without rescanning or publishing into
        # the disconnected folder. The saved paths stay available for recovery.
        self.assertEqual((count, len(rows)), (2, 2))
        self.assertTrue(all("파일 없음" in row["caption"] for row in rows))
        repository.provider.register.assert_not_called()
        self.assertFalse(self.paths.media_dir.exists())
        with closing(HistoryStore(self.paths.record_dir)) as store:
            self.assertTrue(all(row["path"] for row in store.records()))

    def test_new_folder_without_records_and_present_folder_are_not_reported(self):
        self.assertFalse(saved_media_missing(self.paths))
        self.saved(1)
        self.assertFalse(saved_media_missing(self.paths))

    def test_disconnected_drive_is_named(self):
        letter = free_drive()
        if letter is None:
            self.skipTest("no free drive letter")
        paths = resolve_storage(f"{letter}:\\Likes", "offline")
        self.assertIn(f"드라이브({letter}:)", missing_media_help(paths))

    def test_scheduled_save_skips_a_moved_folder(self):
        from luvbox.scheduled import _options_from_settings
        self.saved(1)
        settings = {"auth_mode": "managed", "account": "offline", "output_dir": str(self.paths.output_dir)}
        _options_from_settings(settings, False)
        self.paths.media_dir.rename(self.base / "Likes-moved")
        with self.assertRaises(ValueError) as raised:
            _options_from_settings(settings, False)
        self.assertIn("자동 저장을 건너뛰었습니다", str(raised.exception))
        self.assertFalse(self.paths.media_dir.exists())


class MissingLocationTests(StateTestCase):
    def test_refresh_records_where_and_when_a_file_went_missing(self):
        [path] = self.saved(1)
        path.unlink()
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store.import_legacy(refresh=True)
            [item] = store.missing_media()
        self.assertEqual(item["last_path"], str(path))
        self.assertRegex(item["missing_since"], r"^\d{4}-\d\d-\d\dT")
        path.write_bytes(jpeg())
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store.import_legacy(refresh=True)
            self.assertEqual(store.missing_media(), [])
            self.assertEqual([row["missing_since"] for row in store.records()], [""])

    def test_metadata_job_keeps_the_location_of_a_file_removed_meanwhile(self):
        [path] = self.saved(1)
        with closing(HistoryStore.for_storage(self.paths)) as store:
            [row] = store.records()
            path.unlink()
            store.mark_metadata(row, "failed")
            [row] = store.records()
        self.assertEqual((row["path"], row["last_path"]), ("", str(path)))
        self.assertTrue(row["missing_since"])


class PreviewCacheTests(StateTestCase):
    def image(self):
        return Image.new("RGB", (400, 300), (10, 120, 200))

    def test_preview_round_trip_is_small_and_known(self):
        key = ("100", 1, "photo")
        with closing(thumbcache.Cache("offline")) as cache:
            self.assertEqual(cache.stored([key]), set())
            cache.put(key, self.image())
            self.assertEqual(cache.stored([key]), {key})
            data = cache.get(key)
        self.assertLess(len(data), 8 * 1024)
        picture = thumbcache.recall("offline", key)
        self.assertLessEqual(picture.width, thumbcache.SIZE[0])
        self.assertLessEqual(picture.height, thumbcache.SIZE[1])
        self.assertTrue(thumbcache.known("offline", key))
        self.assertTrue(str(thumbcache.directory()).startswith(str(self.base)))

    def test_oldest_previews_are_dropped_above_the_limit(self):
        with closing(thumbcache.Cache("offline")) as cache:
            for index in range(6):
                cache.put((str(100 + index), 1, "photo"), Image.new("RGB", (150, 112), (index * 40, 0, 0)))
            size = cache.total_bytes()
            removed = cache.prune(limit=size // 2)
            self.assertGreater(removed, 0)
            kept = cache.stored([(str(100 + index), 1, "photo") for index in range(6)])
        self.assertNotIn(("100", 1, "photo"), kept)
        self.assertIn(("105", 1, "photo"), kept)

    def test_cloud_only_files_are_never_read(self):
        [path] = self.saved(1)
        placeholder = SimpleNamespace(st_file_attributes=0x400000)
        with patch("luvbox.thumbcache.Path.stat", return_value=placeholder), \
                patch("luvbox.preview.thumbnail") as decode:
            self.assertTrue(thumbcache.cloud_only(path))
            self.assertIsNone(thumbcache.make(path))
        decode.assert_not_called()
        self.assertIsNotNone(thumbcache.make(path))

    def test_invalid_input_and_cache_failures_never_raise(self):
        thumbcache.remember("Bad Account!", ("100", 1, "photo"), self.image())
        thumbcache.remember("offline", ("not-a-tweet", 1, "photo"), self.image())
        self.assertIsNone(thumbcache.recall("offline", ("999", 1, "photo")))
        self.assertIsNone(thumbcache.recall("nobody", ("100", 1, "photo")))
        recorder = thumbcache.Recorder("offline")
        with patch("luvbox.thumbcache.Cache", side_effect=sqlite3.OperationalError("locked")):
            [path] = self.saved(1)
            recorder.add(("100", 1, "photo"), path)
        self.assertEqual(recorder.failed, 1)
        recorder.add(("101", 1, "photo"), path)
        recorder.close()
        self.assertEqual(recorder.failed, 0)
        for tweet in ("100", "101"):
            image = thumbcache.recall("offline", (tweet, 1, "photo"))
            self.assertIsNotNone(image)
            image.close()




class RealPhotoDownloader(core_support.FakeHttpDownloader):
    def download(self, url, pathfmt):
        self.downloaded.append(url)
        with pathfmt.open() as fp:
            fp.write(jpeg((30, 160, 90)))
        return True


class WorkerPreviewTests(unittest.TestCase):
    write_cookies = core_support.CoreTests.write_cookies

    def setUp(self):
        core_support.CoreTests.setUp(self)
        self.options = replace(self.options, folder_mode="flat", separate_accounts=False)

    def tearDown(self):
        core_support.CoreTests.tearDown(self)

    def test_each_newly_saved_file_gets_a_preview_on_this_pc(self):
        from luvbox.core import run_worker
        rows = [("123", 1, "photo", "alice")]
        events = []
        with patch("gallery_dl.extractor.find", side_effect=lambda url: core_support.FakeLikesExtractor(url, rows, None, None)), \
                patch("gallery_dl.downloader.find", return_value=RealPhotoDownloader):
            self.assertEqual(run_worker(self.options, events.append), 0)
        saved = [event for event in events if event.get("type") == "saved"]
        self.assertEqual(len(saved), 1)
        self.assertIsNotNone(thumbcache.recall("myaccount", ("123", 1, "photo")))


if __name__ == "__main__":
    unittest.main()
