"""Externally removed/copied files in known and current output locations."""

import json
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from unittest.mock import patch

from tests import test_core
from tests.test_missing_files import FakeTweetExtractor
from luvbox.core import run_worker
from luvbox.history import HistoryStore
from luvbox.storage import migrate_records, resolve_storage


class ExternalMissingRetryTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    set_session = test_core.CoreTests.set_session
    worker = test_core.CoreTests.worker

    def migrated_file(self, *, remove_original):
        options = replace(self.options, folder_mode="flat", separate_accounts=False)
        code, _ = self.worker([("100", 1, "photo", "alice")], options=options)
        self.assertEqual(code, 0)
        old = resolve_storage(self.output, "myaccount")
        new = resolve_storage(self.base / "new-output", "myaccount")
        migrate_records(old, new)
        with closing(HistoryStore.for_storage(new)) as store:
            store.import_legacy(refresh=True)
            [before] = store.records()
            if remove_original:
                (self.output / "alice_100_1.jpg").unlink()  # Only this private test file.
                store.import_legacy(refresh=True)
                [missing] = store.records()
                self.assertEqual((missing["path"], missing["filesize"]), (before["path"], None))
            store.set_missing_ignored([("100", 1, "photo")], True)
        current = new.media_dir / "100_1.jpg"
        current.write_bytes(b"externally copied existing photo")
        self.embed_source.reset_mock()
        test_core.FakeHttpDownloader.downloaded = []
        return replace(options, output_dir=str(new.output_dir)), new, before, current

    def retry(self, options, kind="photo"):
        events = []
        key = "100_1" if kind == "photo" else "100_animated_gif_1"
        with patch("gallery_dl.extractor.find", side_effect=lambda url: FakeTweetExtractor(url, {"100": [(1, kind)]})), \
                patch("gallery_dl.downloader.find", return_value=test_core.FakeHttpDownloader):
            code = run_worker(replace(options, targets=(key,)), events.append)
        self.assertNotIn(test_core.SECRET, json.dumps(events))
        return code, events

    def test_selected_retry_recognizes_current_copy_when_former_file_is_missing(self):
        options, location, before, current = self.migrated_file(remove_original=True)
        payload = current.read_bytes()
        code, events = self.retry(options)
        self.assertEqual((code, events[-1]["saved"], events[-1]["recovered"]), (0, 0, 1))
        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
        self.assertEqual(next(event["path"] for event in events if event["type"] == "skipped"), str(current))
        self.assertEqual(current.read_bytes(), payload)
        with closing(HistoryStore.for_storage(location)) as store:
            [after] = store.records()
            self.assertEqual(after["path"], str(current))
            self.assertEqual(after["saved_at"], before["saved_at"])
            self.assertEqual((store.summary()["files"], store.summary()["missing_files"]), (1, 0))
            self.assertEqual(store.ignored_media(), set())
        # The vanished file's embedded status must not certify a different
        # physical copy. The new existing file is checked/embedded itself.
        self.embed_source.assert_called_once()
        self.assertEqual(str(self.embed_source.call_args.args[0]), str(current))

    def test_selected_retry_keeps_present_former_copy_and_its_verified_metadata(self):
        options, location, before, current = self.migrated_file(remove_original=False)
        payload = current.read_bytes()
        code, events = self.retry(options)
        self.assertEqual((code, events[-1]["saved"], events[-1]["recovered"]), (0, 0, 1))
        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
        self.assertEqual(next(event["path"] for event in events if event["type"] == "skipped"), before["path"])
        self.assertEqual(current.read_bytes(), payload)
        with closing(HistoryStore.for_storage(location)) as store:
            [after] = store.records()
            self.assertEqual((after["path"], after["saved_at"]), (before["path"], before["saved_at"]))
        self.embed_source.assert_not_called()

    def test_new_existing_copy_does_not_inherit_old_embedded_status_after_tag_failure(self):
        options, location, before, current = self.migrated_file(remove_original=True)
        self.embed_source.side_effect = None
        self.embed_source.return_value = {"status": "failed", "author_status": "error"}
        code, events = self.retry(options)
        self.assertEqual((code, events[-1]["recovered"]), (0, 1))
        with closing(HistoryStore.for_storage(location)) as store:
            [after] = store.records()
            self.assertEqual((after["path"], after["saved_at"]), (str(current), before["saved_at"]))
            self.assertEqual((after["metadata_status"], after["author_metadata_status"]), ("failed", "error"))
            self.assertEqual(store.ignored_media(), set())
        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])

    def test_selected_retry_recognizes_current_gif_when_former_gif_is_missing(self):
        old = resolve_storage(self.output, "myaccount")
        self.output.mkdir()
        original = self.output / "100_gif_1.gif"
        original.write_bytes(b"GIF89aoffline original")
        with closing(HistoryStore.for_storage(old)) as store:
            store.record_media("flat", {"tweet_id": "100", "num": 1, "type": "animated_gif",
                                       "author": {"name": "alice"}, "_luvbox_metadata_status": "embedded",
                                       "_luvbox_author_metadata_status": "embedded"}, original)
            [before] = store.records()
        with closing(sqlite3.connect(old.record_dir / ".luvbox-archive.flat.sqlite3")) as db:
            db.execute("CREATE TABLE archive(entry TEXT PRIMARY KEY) WITHOUT ROWID")
            db.execute("INSERT INTO archive VALUES ('twitter100_animated_gif_1')")
            db.commit()
        location = resolve_storage(self.base / "new-output", "myaccount")
        migrate_records(old, location)
        with closing(HistoryStore.for_storage(location)) as store:
            store.import_legacy(refresh=True)
            original.unlink()  # Only this private completed GIF.
            store.import_legacy(refresh=True)
            store.set_missing_ignored([("100", 1, "animated_gif")], True)
        current = location.media_dir / original.name
        current.write_bytes(b"GIF89aexternally copied GIF")
        payload = current.read_bytes()
        options = replace(self.options, folder_mode="flat", separate_accounts=False,
                          output_dir=str(location.output_dir))
        code, events = self.retry(options, "animated_gif")
        self.assertEqual((code, events[-1]["saved"], events[-1]["recovered"]), (0, 0, 1))
        self.assertEqual(test_core.FakeHttpDownloader.downloaded, [])
        self.assertEqual(next(event["path"] for event in events if event["type"] == "skipped"), str(current))
        self.assertEqual(current.read_bytes(), payload)
        self.assertFalse(list(location.media_dir.glob("*.mp4")))
        with closing(HistoryStore.for_storage(location)) as store:
            [after] = store.records()
            self.assertEqual((after["path"], after["media_type"], after["saved_at"]),
                             (str(current), "animated_gif", before["saved_at"]))
            self.assertEqual((store.summary()["files"], store.summary()["missing_files"]), (1, 0))
            self.assertEqual(store.ignored_media(), set())


if __name__ == "__main__":
    unittest.main()
