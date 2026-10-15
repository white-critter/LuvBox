"""Files removed outside the app: a moved/disconnected folder, previews kept
on this PC, and what the missing-files list shows. Offline fixtures only."""

import io
import os
import queue
import sqlite3
import string
import tempfile
import time
import threading
import tkinter as tk
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from tests import test_core as core_support
from tests.test_gui import destroy_tk, release_tk
from luvbox import thumbcache
from luvbox.gui import App
from luvbox.history import HistoryStore
from luvbox.history_gui import HistoryWindow, _publish
from luvbox.preview import PreviewLoader, Thumbnail
from luvbox.gallery import _GridDecoder
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
        results = queue.Queue()
        HistoryWindow._load_worker(results, 1, self.paths, "all", "", 0, True)
        kinds = []
        while not results.empty():
            _, kind, result = results.get_nowait()
            kinds.append(kind)
            summary = result[1]
        # No rescan: a rescan would turn every saved file into a missing one.
        self.assertEqual(kinds, ["loaded"])
        self.assertIn("저장 폴더를 찾을 수 없습니다", summary["media_offline"])
        self.assertEqual((summary["files"], summary["missing_files"]), (2, 0))
        self.assertFalse(self.paths.media_dir.exists())
        _publish(self.paths)
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

    def test_starting_a_save_asks_before_making_a_new_empty_folder(self):
        self.saved(1)
        self.paths.media_dir.rename(self.base / "Likes-moved")
        texts = {"status": [], "detail": []}
        fake = SimpleNamespace(root=None, status=SimpleNamespace(set=texts["status"].append),
                               status_detail=SimpleNamespace(set=texts["detail"].append))
        options = SimpleNamespace(output_dir=str(self.paths.output_dir), account="offline", separate_accounts=False)
        with patch("luvbox.gui.dialogs.askyesno", return_value=False) as ask:
            self.assertFalse(App._confirm_missing_media(fake, options))
        self.assertFalse(fake._allow_recreate_media)
        self.assertEqual(ask.call_args.kwargs["default"], "no")
        self.assertIn("시작하지 않았습니다", texts["status"][-1])
        with patch("luvbox.gui.dialogs.askyesno", return_value=True):
            self.assertTrue(App._confirm_missing_media(fake, options))
        self.assertTrue(fake._allow_recreate_media)
        self.paths.media_dir.mkdir()
        with patch("luvbox.gui.dialogs.askyesno") as ask:
            self.assertTrue(App._confirm_missing_media(fake, options))
        self.assertFalse(fake._allow_recreate_media)
        ask.assert_not_called()


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
        self.assertTrue(recorder._disabled)
        recorder.add(("101", 1, "photo"), path)   # silently skipped
        recorder.close()

    def test_gallery_keeps_decoded_previews_and_reuses_them_for_missing_files(self):
        [path] = self.saved(1)
        results = queue.Queue()
        decoder = _GridDecoder(results)
        kept = {}
        decoder.request(1, [("a", str(path), ("100", 1, "photo")),
                            ("b", str(path.with_name("gone_1.jpg")), ("200", 1, "photo"))],
                        lambda paths: set(paths), remember=lambda key, image: kept.setdefault(key, image),
                        recall=lambda key: Image.new("RGBA", (10, 10)) if key == ("200", 1, "photo") else None)
        found = {}
        for _ in range(2):
            token, key, image = results.get(timeout=10)
            found[key] = image
        decoder.close()
        self.assertIn(("100", 1, "photo"), kept)
        self.assertEqual(found["b"].size, (10, 10))

    def test_file_preview_falls_back_to_the_kept_preview(self):
        results = queue.Queue()
        loader = PreviewLoader(results)
        loader.request(1, self.base / "gone.jpg", lambda path: True, recall=lambda: Image.new("RGBA", (12, 9)))
        token, result = results.get(timeout=10)
        loader.close()
        self.assertEqual(result.image.size, (12, 9))
        self.assertTrue(result.note.startswith("저장해 둔 작은 미리보기"))


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


class MissingTabTests(StateTestCase):
    def setUp(self):
        super().setUp()
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(release_tk, self, "window", "root")
        self.addCleanup(destroy_tk, self.root)
        with patch("luvbox.history_gui.threading.Thread"):
            self.window = HistoryWindow(self.root, lambda: ("offline", self.paths))
        self.window.withdraw()

    def load(self, do_import=True):
        # The window's own refresh started this load; run it here in order.
        self.window._load(self.window.generation, self.paths, "all", "", 0, do_import)
        self.window._poll()

    def test_detail_shows_kept_preview_location_and_time_then_opens_folder(self):
        [path] = self.saved(1)
        thumbcache.remember("offline", ("100", 1, "photo"), Image.open(path))
        path.unlink()
        with patch("luvbox.history_gui.threading.Thread"):
            self.load()
        [key] = self.window.missing_rows
        self.window.missing_tree.selection_set(key)
        self.window.missing_tree.focus(key)
        self.window._missing_selected()
        deadline = time.monotonic() + 3
        while self.window._missing_image is None and time.monotonic() < deadline:
            self.window._poll_missing_previews()
            time.sleep(.01)
        detail = self.window.missing_detail.get()
        self.assertIn("저장해 둔 미리보기입니다", detail)
        self.assertIn(str(self.paths.media_dir), detail.replace("\n", ""))   # wrapped at separators
        self.assertNotIn("알 수 없음", detail)
        self.assertIsNotNone(self.window._missing_image)
        with patch("luvbox.history_gui.os.startfile") as start:
            self.window.open_missing_folder()
        start.assert_called_once_with(str(self.paths.media_dir))

    def test_every_page_can_be_chosen_and_a_click_ends_it(self):
        files = self.saved(130)
        for path in files:
            path.unlink()
        with patch("luvbox.history_gui.threading.Thread"):
            self.load()
        self.assertEqual(len(self.window.missing_rows), 100)
        self.window.select_every_missing()
        self.assertEqual(len(self.window._missing_selection()), 130)
        self.assertIn("모든 페이지의 130개", self.window.missing_text.get())
        self.window.change_page(1, missing=True)
        self.assertEqual(set(self.window.missing_tree.selection()), set(self.window.missing_rows))
        self.assertEqual(len(self.window._missing_selection()), 130)
        with patch("luvbox.history_gui.dialogs.askyesno", return_value=True) as ask, \
                patch("luvbox.history_gui.threading.Thread") as thread:
            self.window.choose_missing(True)
        self.assertIn("130개", ask.call_args.args[1])
        self.assertEqual(len(thread.call_args.kwargs["args"][2]), 130)
        self.window.busy = False
        first = next(iter(self.window.missing_rows))
        self.window.missing_tree.selection_set(first)
        self.window._missing_selected()
        self.assertFalse(self.window.missing_all)
        self.assertEqual(len(self.window._missing_selection()), 1)

    def test_too_many_files_to_download_again_are_explained(self):
        files = self.saved(3)
        for path in files:
            path.unlink()
        self.window.retry_missing = lambda *args: True
        with patch("luvbox.history_gui.threading.Thread"):
            self.load()
        self.window.select_every_missing()
        with patch("luvbox.core.RETRY_LIMIT", 2), \
                patch("luvbox.history_gui.dialogs.showinfo") as info, \
                patch("luvbox.history_gui.dialogs.askyesno") as ask:
            self.window.retry_missing_files()
        self.assertIn("2개까지", info.call_args.args[1])
        ask.assert_not_called()

    def test_moved_folder_banner_blocks_downloads_and_file_jobs(self):
        self.saved(2)
        self.paths.media_dir.rename(self.base / "Likes-moved")
        with patch("luvbox.history_gui.threading.Thread"):
            self.load()
        self.assertTrue(self.window.media_offline)
        self.assertIn("저장 폴더를 찾을 수 없습니다", self.window.offline_text.get())
        self.assertTrue(self.window.offline_label.winfo_manager())
        self.assertEqual(self.window.tile_values["files"].get(), "2")
        for start in (self.window.backfill, self.window.save_previews):
            with self.subTest(job=start.__name__), \
                    patch("luvbox.history_gui.dialogs.showinfo") as info, \
                    patch("luvbox.history_gui.threading.Thread") as thread:
                start()
            info.assert_called_once()
            thread.assert_not_called()
        self.assertFalse(self.paths.media_dir.exists())

    def test_library_basics_for_new_users(self):
        from luvbox.history_gui import format_bytes
        self.assertEqual(format_bytes(1536), "1.5 KB")
        self.assertEqual(format_bytes(5 * 1024 * 1024), "5.0 MB")
        self.saved(1)
        with patch("luvbox.history_gui.threading.Thread"):
            self.load()
        # Files that are gone, however they went, have one tab.
        self.assertEqual([self.window.tabs.tab(tab, "text").split(" ")[0] for tab in self.window.tabs.tabs()],
                         ["파일", "실행", "통계", "사라진"])
        self.assertEqual(self.window.tile_values["bytes"].get().split()[-1], "KB")
        with patch("luvbox.history_gui.os.startfile") as opened:
            self.window.open_media_folder()
        opened.assert_called_once_with(str(self.paths.media_dir))
        self.paths.media_dir.rename(self.base / "Likes-moved")
        with patch("luvbox.history_gui.os.startfile") as opened, \
                patch("luvbox.history_gui.dialogs.showinfo") as info:
            self.window.open_media_folder()
        opened.assert_not_called()
        info.assert_called_once()
        self.assertFalse(self.paths.media_dir.exists())

    def test_status_suggests_recording_sources_only_for_old_files(self):
        self.saved(2)
        with patch("luvbox.history_gui.threading.Thread"):
            self.load()
        self.assertIn("출처가 기록되지 않은 예전 파일 2개", self.window.status_text.get())
        with closing(HistoryStore.for_storage(self.paths)) as store:
            for row in store.records():
                store.mark_metadata(row, "embedded")
        with patch("luvbox.history_gui.threading.Thread"):
            self.load()
        self.assertNotIn("출처가 기록되지 않은", self.window.status_text.get())

    def test_preview_job_keeps_local_files_and_skips_cloud_only_ones(self):
        files = self.saved(3)
        results = queue.Queue()
        stop = threading.Event()
        cloud = str(files[2])
        with patch("luvbox.history_gui.thumbcache.cloud_only", side_effect=lambda path: str(path) == cloud):
            HistoryWindow._previews_worker(results, stop, 1, self.paths, "offline")
        _, kind, message = results.get_nowait()
        self.assertEqual(kind, "previews-saved")
        self.assertIn("새로 저장 2개", message)
        self.assertIn("클라우드 전용이라 건너뜀 1개", message)
        HistoryWindow._previews_worker(results, stop, 1, self.paths, "offline")
        _, kind, message = results.get_nowait()
        self.assertIn("이미 있음 2개", message)
        self.assertIsNotNone(thumbcache.recall("offline", ("100", 1, "photo")))


if __name__ == "__main__":
    unittest.main()
