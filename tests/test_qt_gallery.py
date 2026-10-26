from importlib.util import find_spec
import os
from pathlib import Path
import tempfile
from threading import Event, Thread, get_ident
import time
import unittest
from unittest.mock import patch

from PIL import Image


_QT_AVAILABLE = find_spec("PySide6") is not None
if _QT_AVAILABLE:
    from PySide6.QtCore import QCoreApplication, QModelIndex, QObject, QSize, Signal
    from PySide6.QtQuick import QQuickImageProvider
    from luvbox.preview import thumbnail
    from luvbox.qt_gallery import GalleryModel, SafeThumbnailProvider


@unittest.skipUnless(_QT_AVAILABLE, "optional Qt experiment dependencies are not installed")
class GalleryModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def wait_for(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            time.sleep(0.002)
        self.fail("Qt gallery condition did not settle")

    def model(self, reader, **kwargs):
        model = GalleryModel(reader, **kwargs)
        self.addCleanup(model.shutdown)
        return model

    @staticmethod
    def rows(count, prefix="item"):
        return [{"key": f"{prefix}{index}", "caption": f"사진 {index}"} for index in range(count)]

    def test_batches_keep_order_roles_and_reader_off_ui_thread(self):
        rows = self.rows(5)
        calls = []
        inserted_threads = []

        def reader(query, offset, limit):
            calls.append((query, offset, limit, get_ident()))
            return len(rows), rows[offset:offset + limit]

        model = self.model(reader, batch_size=2)
        model.rowsInserted.connect(lambda *_: inserted_threads.append(get_ident()))
        self.wait_for(lambda: not model.busy)
        self.assertEqual((model.count, model.total), (2, 5))
        self.assertEqual(model.roleNames()[model.ThumbUrlRole], b"thumbUrl")
        self.assertEqual(model.data(model.index(0), model.ThumbUrlRole),
                         "image://thumbs/item0?generation=0")
        model.loadMore()
        model.loadMore()
        self.wait_for(lambda: not model.busy)
        model.fetchMore()
        self.wait_for(lambda: not model.busy)
        self.assertEqual([model.data(model.index(index), model.KeyRole) for index in range(5)],
                         [row["key"] for row in rows])
        self.assertEqual([call[:3] for call in calls], [("", 0, 2), ("", 2, 2), ("", 4, 2)])
        self.assertTrue(all(call[3] != get_ident() for call in calls))
        self.assertTrue(all(thread == get_ident() for thread in inserted_threads))
        self.assertFalse(model.canFetchMore())
        self.assertEqual(model.rowCount(model.index(0)), 0)
        self.assertIsNone(model.data(QModelIndex()))

    def test_search_coalesces_pending_queries_and_discards_old_result(self):
        entered, release = Event(), Event()
        calls = []
        self.addCleanup(release.set)

        def reader(query, offset, limit):
            calls.append(query)
            if not query:
                entered.set()
                release.wait(3)
            return 1, [{"key": query or "old", "caption": query}]

        model = self.model(reader)
        self.assertTrue(entered.wait(1))
        model.search("intermediate")
        model.search("latest")
        release.set()
        self.wait_for(lambda: not model.busy)
        self.assertEqual(calls, ["", "latest"])
        self.assertEqual(model.count, 1)
        self.assertEqual(model.data(model.index(0), model.KeyRole), "latest")
        self.assertIn("generation=2", model.data(model.index(0), model.ThumbUrlRole))

    def test_queued_stale_batch_does_not_enter_new_search(self):
        complete = Event()

        def reader(query, offset, limit):
            complete.set()
            return 1, [{"key": query or "old", "caption": query}]

        model = self.model(reader)
        self.assertTrue(complete.wait(1))
        model.search("new")
        self.wait_for(lambda: not model.busy)
        self.assertEqual(model.data(model.index(0), model.KeyRole), "new")
        self.assertEqual(model.count, 1)

    def test_reader_error_is_fixed_and_does_not_expose_paths_or_tokens(self):
        def reader(*_):
            raise RuntimeError("C:/private/path auth_token=not-public")

        model = self.model(reader)
        self.wait_for(lambda: not model.busy)
        self.assertEqual(model.error, "사진 목록을 불러오지 못했습니다.")
        self.assertEqual(model.count, 0)
        self.assertFalse(model.canFetchMore())

    def test_invalid_batches_fail_atomically_without_infinite_fetch(self):
        for result in ((True, []), (2, []), (1, self.rows(2)),
                       (2, [{"key": "same"}, {"key": "same"}]),
                       (1, [{"key": "../file"}]), (1, [{"key": "one", "caption": None}])):
            with self.subTest(result=result):
                model = self.model(lambda *_: result, batch_size=2)
                self.wait_for(lambda: not model.busy)
                self.assertEqual(model.count, 0)
                self.assertTrue(model.error)
                self.assertFalse(model.canFetchMore())

    def test_search_with_no_matches_resets_count_and_keeps_empty_state(self):
        model = self.model(lambda query, *_: (0, []) if query else (1, self.rows(1)))
        self.wait_for(lambda: not model.busy)
        model.search("absent")
        self.wait_for(lambda: not model.busy)
        self.assertEqual((model.count, model.total, model.error), (0, 0, ""))
        self.assertFalse(model.canFetchMore())

    def test_shutdown_is_bounded_and_discards_inflight_work(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)

        def reader(*_):
            entered.set()
            release.wait(3)
            return 1, self.rows(1)

        model = self.model(reader)
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        self.assertFalse(model.shutdown(timeout=0.01))
        self.assertLess(time.monotonic() - started, 0.5)
        release.set()
        self.assertTrue(model.shutdown())
        self.application.processEvents()
        self.assertEqual(model.count, 0)
        self.assertFalse(model.canFetchMore())

    def test_duplicate_key_in_later_batch_preserves_existing_rows_and_stops(self):
        model = self.model(lambda query, offset, limit: (2, self.rows(1)), batch_size=1)
        self.wait_for(lambda: not model.busy)
        model.loadMore()
        self.wait_for(lambda: not model.busy)
        self.assertEqual(model.count, 1)
        self.assertTrue(model.error)
        self.assertFalse(model.canFetchMore())

    def test_focus_keeps_all_visible_keys_before_bounded_lookahead(self):
        class RecordingProvider(QObject):
            ready = Signal(str)

            def __init__(self):
                super().__init__()
                self.calls = []

            def schedule(self, keys, width, height):
                self.calls.append((list(keys), width, height))

            def reset_notifications(self):
                self.calls.append("reset")

        provider = RecordingProvider()
        model = self.model(lambda *_: (200, self.rows(200)), provider=provider, batch_size=200)
        self.wait_for(lambda: model.count == 200)
        model.focusViewport(10, 12, 4, 80, 60)
        self.assertEqual(provider.calls[-1], (["item10", "item11", "item12", "item13", "item14", "item15", "item16"], 80, 60))
        model.focusViewport(4, 6, -3, 80, 60)
        self.assertEqual(provider.calls[-1][0], ["item4", "item5", "item6", "item3", "item2", "item1"])
        model.focusViewport(0, 59, 10, 20, 16)
        self.assertEqual(provider.calls[-1][0], [f"item{index}" for index in range(70)])
        model.focusViewport(50, 109, 1000, 20, 16)
        self.assertEqual(provider.calls[-1][0], [f"item{index}" for index in range(50, 158)])
        model.focusViewport(50, 109, -1000, 20, 16)
        self.assertEqual(provider.calls[-1][0], [f"item{index}" for index in range(50, 110)]
                         + [f"item{index}" for index in range(49, 1, -1)])
        before = len(provider.calls)
        model.focusViewport(0, 1, 0, 0, 16)
        self.assertEqual(len(provider.calls), before)
        model.search("again")
        self.assertIn("reset", provider.calls)

    def test_large_visible_range_becomes_ready_with_bounded_cache_and_lookahead(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name).resolve() / "photo.png"
        with Image.new("RGBA", (64, 48), (10, 20, 30, 255)) as image:
            image.save(path)
        provider = SafeThumbnailProvider(workers=2)
        self.addCleanup(provider.close)
        rows = self.rows(320)
        for row in rows:
            provider.register(row["key"], path)
        model = self.model(lambda *_: (len(rows), rows), provider=provider, batch_size=320)
        self.wait_for(lambda: model.count == len(rows))
        model.focusViewport(0, 259, 1000, 16, 12)
        self.wait_for(lambda: all(model.data(model.index(index), model.ThumbReadyRole)
                                 for index in range(260)))
        self.assertTrue(provider.wait_for_idle(2))
        snapshot = provider.snapshot()
        self.assertEqual(snapshot["focus_count"], 260 + 48)
        self.assertEqual(snapshot["decoded"], 260 + 48)
        self.assertEqual(snapshot["cache_count"], 240)
        self.assertLessEqual(snapshot["cache_bytes"], 240 * 16 * 12 * 4)
        self.assertEqual(snapshot["decode_workers"], 2)

    def test_cached_focus_publishes_ready_without_a_second_decode(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name).resolve() / "photo.png"
        image = Image.new("RGBA", (32, 32), (10, 20, 30, 255))
        image.save(path)
        image.close()
        provider = SafeThumbnailProvider(workers=1)
        self.addCleanup(provider.close)
        provider.register("photo", path)
        size = QSize()
        self.assertFalse(provider.requestImage("photo", size, QSize(24, 18)).isNull())
        decoded = []
        original = provider._decoder

        def counting(file, dimensions):
            decoded.append(file)
            return original(file, dimensions)

        provider._decoder = counting
        model = self.model(lambda *_: (1, [{"key": "photo", "caption": "사진"}]), provider=provider)
        self.wait_for(lambda: not model.busy and model.count == 1)
        model.focusViewport(0, 0, 0, 24, 18)
        self.wait_for(lambda: model.data(model.index(0), model.ThumbReadyRole) is True)
        self.assertEqual(decoded, [])
        self.assertEqual(provider.snapshot()["decoded"], 1)

    def test_density_round_trip_republishes_cached_images_without_decoding_again(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name).resolve() / "photo.png"
        with Image.new("RGBA", (64, 48), (10, 20, 30, 255)) as image:
            image.save(path)
        provider = SafeThumbnailProvider(workers=1)
        self.addCleanup(provider.close)
        provider.register("photo", path)
        for dimensions in ((24, 18), (40, 30)):
            self.assertFalse(provider.requestImage("photo", QSize(), QSize(*dimensions)).isNull())
        decoded = provider.snapshot()["decoded"]
        model = self.model(lambda *_: (1, [{"key": "photo", "caption": "사진"}]), provider=provider)
        self.wait_for(lambda: not model.busy and model.count == 1)
        for dimensions in ((24, 18), (40, 30), (24, 18), (40, 30)):
            with self.subTest(dimensions=dimensions):
                model.focusViewport(0, 0, 0, *dimensions)
                self.wait_for(lambda: model.data(model.index(0), model.ThumbReadyRole) is True)
        self.assertEqual(provider.snapshot()["decoded"], decoded)


@unittest.skipUnless(_QT_AVAILABLE, "optional Qt experiment dependencies are not installed")
class SafeThumbnailProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QCoreApplication.instance() or QCoreApplication([])
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name).resolve()
        self.path = self.folder / "사진.png"
        image = Image.new("RGBA", (640, 320), (40, 80, 120, 100))
        image.save(self.path)
        image.close()
        self.provider = SafeThumbnailProvider(cache_size=2)
        self.addCleanup(self.provider.close)
        self.provider.register("photo", self.path)

    def request(self, key="photo", dimensions=(220, 168), provider=None):
        size = QSize()
        image = (provider or self.provider).requestImage(key, size, QSize(*dimensions))
        self.assertEqual(size, image.size())
        return image

    def test_unknown_ids_encoded_traversal_and_paths_never_reach_decoder(self):
        with patch.object(self.provider, "_decoder") as decoder:
            for key in ("unknown", "../photo", "%2e%2e%2fphoto", "%2fphoto", str(self.path), "file:///photo"):
                self.assertTrue(self.request(key).isNull())
            decoder.assert_not_called()
        with self.assertRaises(ValueError):
            self.provider.register("../photo", self.path)
        with self.assertRaises(ValueError):
            self.provider.register("relative", Path("photo.png"))
        with self.assertRaises(ValueError):
            self.provider.register("photo", self.folder / "other.png")

    def test_image_copy_preserves_alpha_source_and_bounded_size(self):
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        image = self.request(dimensions=(5000, 5000))
        self.assertEqual((image.width(), image.height()), (384, 192))
        self.assertEqual(image.pixelColor(0, 0).alpha(), 100)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)
        self.assertTrue(self.provider.flags() & QQuickImageProvider.Flag.ForceAsynchronousImageLoading)
        self.provider.close()
        self.assertEqual(image.pixelColor(0, 0).alpha(), 100)

    def test_cache_reuses_stamp_but_not_other_sizes_and_is_bounded(self):
        first = self.request()
        second = self.request("photo?generation=5")
        self.assertEqual(first, second)
        self.assertEqual(self.provider.snapshot()["decoded"], 1)
        self.request(dimensions=(100, 80))
        self.request(dimensions=(80, 60))
        stats = self.provider.snapshot()
        self.assertEqual((stats["decoded"], stats["cache_count"]), (3, 2))
        self.assertLessEqual(stats["cache_bytes"], 2 * 384 * 384 * 4)
        self.assertGreaterEqual(stats["decode_ms"]["samples"], 1)
        self.assertGreaterEqual(stats["delivery_ms"]["samples"], 1)
        self.assertIsNotNone(stats["decode_ms"]["max_ms"])
        self.assertEqual(stats["cancelled"], 0)

    def test_modified_file_invalidates_image_cache(self):
        first = self.request()
        image = Image.new("RGBA", (100, 100), (200, 10, 20, 255))
        image.save(self.path)
        image.close()
        second = self.request()
        self.assertNotEqual(first.size(), second.size())
        self.assertEqual(second.pixelColor(0, 0).red(), 200)
        self.assertEqual(self.provider.snapshot()["decoded"], 2)

    def test_replaced_file_with_same_size_and_timestamp_invalidates_image_cache(self):
        with Image.new("RGBA", (24, 16), "red") as picture:
            picture.save(self.path, compress_level=0)
        first = self.request()
        before = self.path.stat()
        replacement = self.folder / "replacement.png"
        with Image.new("RGBA", (24, 16), "blue") as picture:
            picture.save(replacement, compress_level=0)
        self.assertEqual(replacement.stat().st_size, before.st_size)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(self.path)
        after = self.path.stat()
        self.assertEqual((after.st_size, after.st_mtime_ns), (before.st_size, before.st_mtime_ns))
        self.assertNotEqual(after.st_ino, before.st_ino)
        second = self.request()
        self.assertEqual(first.pixelColor(0, 0).red(), 255)
        self.assertEqual(second.pixelColor(0, 0).blue(), 255)
        self.assertEqual(self.provider.snapshot()["decoded"], 2)

    def test_cloud_and_redirected_file_do_not_decode(self):
        with patch("luvbox.qt_gallery.cloud_only", return_value=True), patch.object(self.provider, "_decoder") as decoder:
            self.assertTrue(self.request().isNull())
            decoder.assert_not_called()
        with patch.object(Path, "is_symlink", return_value=True), patch.object(self.provider, "_decoder") as decoder:
            self.assertTrue(self.request().isNull())
            decoder.assert_not_called()

    def test_close_discards_inflight_image_and_clears_registry_and_cache(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)

        def decoder(path, dimensions):
            entered.set()
            release.wait(3)
            return thumbnail(path, dimensions)

        provider = SafeThumbnailProvider(decoder=decoder)
        self.addCleanup(provider.close)
        provider.register("photo", self.path)
        results = []
        worker = Thread(target=lambda: results.append(self.request(provider=provider)))
        worker.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(provider.snapshot()["active"], 1)
        provider.close()
        started = time.monotonic()
        self.assertFalse(provider.wait_for_idle(0.01))
        self.assertLess(time.monotonic() - started, 0.5)
        release.set()
        self.assertTrue(provider.wait_for_idle(2))
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0].isNull())
        stats = provider.snapshot()
        self.assertEqual((stats["active"], stats["cache_count"], stats["registered"]), (0, 0, 0))
        self.assertTrue(self.request(provider=provider).isNull())

    def test_slow_scheduled_decode_is_shared_with_a_waiting_image_request(self):
        entered, duplicate, release = Event(), Event(), Event()
        self.addCleanup(release.set)
        calls = []

        def decoder(path, dimensions):
            calls.append(path)
            (entered if len(calls) == 1 else duplicate).set()
            release.wait(4)
            return thumbnail(path, dimensions)

        provider = SafeThumbnailProvider(decoder=decoder, workers=1)
        self.addCleanup(lambda: provider.join_workers(2))
        self.addCleanup(provider.close)
        provider.register("photo", self.path)
        provider.schedule(["photo"], 220, 168)
        self.assertTrue(entered.wait(1))
        results = []
        request = Thread(target=lambda: results.append(self.request(provider=provider)))
        request.start()
        try:
            # requestImage used to launch a duplicate after its one-second
            # cache wait, exceeding the configured decode worker count.
            self.assertFalse(duplicate.wait(1.2))
            self.assertEqual(provider.snapshot()["active"], 1)
        finally:
            release.set()
            request.join(2)
        self.assertFalse(request.is_alive())
        self.assertTrue(provider.wait_for_idle(2))
        self.assertEqual(len(calls), 1)
        self.assertEqual(provider.snapshot()["decoded"], 1)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].isNull())

    def test_file_redirected_during_decode_is_not_returned_or_cached(self):
        original = self.path.with_name("original.png")
        supported = self.folder / "link-probe.png"
        try:
            supported.symlink_to(self.path)
        except OSError:
            self.skipTest("Cannot create private symbolic-link fixture")
        supported.unlink()

        def decoder(path, dimensions):
            result = thumbnail(path, dimensions)
            path.rename(original)
            path.symlink_to(original)
            return result

        provider = SafeThumbnailProvider(decoder=decoder)
        self.addCleanup(provider.close)
        provider.register("photo", self.path)
        self.assertTrue(self.request(provider=provider).isNull())
        self.assertEqual(provider.snapshot()["cache_count"], 0)

    def test_registry_reset_discards_decode_from_the_previous_library(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)

        def decoder(path, dimensions):
            entered.set()
            release.wait(3)
            return thumbnail(path, dimensions)

        provider = SafeThumbnailProvider(decoder=decoder, workers=1)
        self.addCleanup(lambda: provider.join_workers(2))
        self.addCleanup(provider.close)
        provider.register("photo", self.path)
        provider.schedule(["photo"], 220, 168)
        self.assertTrue(entered.wait(1))
        provider.clear_registry()
        # A refresh may reuse an identity, even when its file is unchanged.
        provider.register("photo", self.path)
        release.set()
        self.assertTrue(provider.wait_for_idle(2))
        self.assertEqual(provider.snapshot()["cache_count"], 0)
        provider.schedule(["photo"], 220, 168)
        self.assertTrue(provider.wait_for_idle(2))
        self.assertEqual(provider.snapshot()["cache_count"], 1)
        self.assertFalse(self.request(provider=provider).isNull())

    def test_wait_for_idle_observes_worker_completion_without_closing_provider(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)

        def decoder(path, dimensions):
            entered.set()
            release.wait(3)
            return thumbnail(path, dimensions)

        provider = SafeThumbnailProvider(decoder=decoder)
        self.addCleanup(provider.close)
        provider.register("photo", self.path)
        self.assertTrue(provider.wait_for_idle(0))
        worker = Thread(target=lambda: self.request(provider=provider))
        worker.start()
        self.assertTrue(entered.wait(1))
        self.assertFalse(provider.wait_for_idle(0))
        release.set()
        self.assertTrue(provider.wait_for_idle(2))
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(provider.snapshot()["closed"])
        self.assertEqual(provider.snapshot()["cache_count"], 1)

    def test_decoder_error_remains_retryable_without_caching_failure(self):
        with patch.object(self.provider, "_decoder", side_effect=OSError("private fixture path")):
            self.assertTrue(self.request().isNull())
        self.assertEqual(self.provider.snapshot()["cache_count"], 0)
        self.assertFalse(self.request().isNull())

    def test_schedule_decodes_focus_order_off_the_caller_thread(self):
        folder = self.folder
        keys = []
        for name in ("a", "b", "c"):
            path = folder / f"{name}.png"
            image = Image.new("RGBA", (24, 16), (20, 40, 60, 255))
            image.save(path)
            image.close()
            self.provider.register(name, path)
            keys.append(name)
        threads = []
        original = self.provider._decoder

        def ordered(path, dimensions):
            threads.append(get_ident())
            keys_seen.append(path.stem)
            return original(path, dimensions)

        keys_seen = []
        provider = SafeThumbnailProvider(decoder=ordered, workers=1)
        self.addCleanup(lambda: provider.join_workers(2))
        self.addCleanup(provider.close)
        for name in keys:
            provider.register(name, folder / f"{name}.png")
        provider.schedule(keys, 20, 12)
        self.assertTrue(provider.wait_for_idle(2))
        self.assertEqual(keys_seen, keys)
        self.assertTrue(threads)
        self.assertNotEqual(threads[0], get_ident())
        stats = provider.snapshot()
        self.assertGreaterEqual(stats["queue_wait_ms"]["samples"], 1)
        self.assertGreaterEqual(stats["decode_ms"]["samples"], 1)
        self.assertEqual(stats["cancelled"], 0)

    def test_reschedule_cancels_queued_work_before_decode(self):
        folder = self.folder
        for name in ("one", "two", "three"):
            path = folder / f"{name}.png"
            image = Image.new("RGBA", (24, 16), (30, 60, 90, 255))
            image.save(path)
            image.close()
        started, release = Event(), Event()
        self.addCleanup(release.set)
        seen = []

        def decoder(path, dimensions):
            seen.append(path.stem)
            if len(seen) == 1:
                started.set()
                release.wait(3)
            return thumbnail(path, dimensions)

        provider = SafeThumbnailProvider(decoder=decoder, workers=1)
        self.addCleanup(lambda: provider.join_workers(2))
        self.addCleanup(provider.close)
        for name in ("one", "two", "three"):
            provider.register(name, folder / f"{name}.png")
        provider.schedule(["one", "two", "three"], 20, 12)
        self.assertTrue(started.wait(2))
        provider.schedule(["three"], 20, 12)
        release.set()
        self.assertTrue(provider.wait_for_idle(3))
        self.assertIn("three", seen)
        self.assertNotIn("two", seen)
        self.assertGreaterEqual(provider.snapshot()["cancelled"], 1)


if __name__ == "__main__":
    unittest.main()
