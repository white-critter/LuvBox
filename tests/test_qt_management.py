"""Management actions use displayed identities and guard existing files."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # Never open the user's app data.
from PySide6.QtCore import QCoreApplication
from luvbox.history import HistoryStore
from luvbox.qt_library import LibraryController
from luvbox.qt_management import ManagementController
from luvbox.recycling import RecyclingError, file_signature
from luvbox.storage import OutputGuard, resolve_storage


class ManagementControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='luvbox-management-qt-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / '사진 보관'
        self.output.mkdir()
        self.kept = self.root / 'simulated-recycle'
        self.kept.mkdir()
        for name in ('luvbox.storage.records_root', 'luvbox.qt_library.records_root'):
            replacement = patch(name, return_value=self.root / 'records')
            replacement.start()
            self.addCleanup(replacement.stop)
        state = patch('luvbox.protection.state_directory', return_value=self.root / 'protection')
        state.start()
        self.addCleanup(state.stop)
        self.paths = resolve_storage(self.output, 'alice')
        self.present = self.output / 'alice_100_1.jpg'
        self.missing = self.output / 'alice_200_1.jpg'
        with closing(HistoryStore.for_storage(self.paths)) as store:
            for tweet, path in (('100', self.present), ('200', self.missing)):
                path.write_bytes(('fixture-' + tweet).encode())
                store.record_media('flat', {'tweet_id': tweet, 'num': 1, 'type': 'photo',
                                            'author': {'name': 'alice'}}, path)
        self.missing.unlink()
        self.library = LibraryController()
        self.management = ManagementController(self.library)
        self.addCleanup(self.close)
        self.library.setLibrary('alice', str(self.output), False)
        self.management.setLibrary('alice', str(self.output), False)
        self.wait(lambda: not self.library.model.busy and self.library.model.count == 2)

    def wait(self, predicate, seconds=8):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail('Management operation did not settle')

    def close(self):
        self.management.shutdown()
        self.library.shutdown()
        self.wait(lambda: not self.management._worker.is_alive() and self.library.shutdownComplete)
        self.app.processEvents()

    def missing_page(self):
        self.management.setSection('missing')
        self.wait(lambda: not self.management.busy)
        self.assertFalse(self.management.error, self.management.error)
        self.assertEqual(len(self.management.rows), 1)
        return self.management.rows[0]['token']

    def gallery_key(self):
        return next(row['key'] for row in self.library.model._rows if '_100_' in row['key'])

    def ignored(self):
        with closing(HistoryStore.for_storage(self.paths)) as store:
            return store.ignored_media()

    def test_ignore_and_unignore_use_current_snapshot_and_reject_old_or_unknown_tokens(self):
        token = self.missing_page()
        self.management.toggleRow('unknown-token')
        self.assertEqual(self.management.selectedCount, 0)
        self.management.toggleRow(token)
        self.assertEqual(self.management.selectedCount, 1)
        self.management.setIgnored(True)
        self.wait(lambda: not self.management.mutating and not self.management.busy)
        self.assertFalse(self.management.error, self.management.error)
        self.assertEqual(self.ignored(), {('200', 1, 'photo')})
        self.management.toggleRow(token)
        self.assertEqual(self.management.selectedCount, 0)
        new_token = self.management.rows[0]['token']
        self.assertNotEqual(new_token, token)
        self.management.toggleRow(new_token)
        self.management.setIgnored(False)
        self.wait(lambda: not self.management.mutating and not self.management.busy)
        self.assertFalse(self.management.error, self.management.error)
        self.assertEqual(self.ignored(), set())

    def test_file_restored_after_selection_cannot_be_marked_as_missing(self):
        token = self.missing_page()
        self.management.toggleRow(token)
        self.missing.write_bytes(b'restored outside the application')
        self.management.setIgnored(True)
        self.wait(lambda: not self.management.mutating and not self.management.busy)
        self.assertTrue(self.management.error)
        self.assertEqual(self.ignored(), set())
        self.assertEqual(self.missing.read_bytes(), b'restored outside the application')

    def test_file_restored_after_selection_is_not_requested_for_download(self):
        token = self.missing_page()
        self.management.toggleRow(token)
        self.missing.write_bytes(b'restored before retry')
        requests = []
        self.management.retryRequested.connect(requests.append)
        self.management.retrySelected()
        self.wait(lambda: not self.management.mutating and not self.management.busy)
        self.app.processEvents()
        self.assertEqual(requests, [])
        self.assertEqual(self.missing.read_bytes(), b'restored before retry')

    def test_new_pending_recycle_after_selection_blocks_retry(self):
        token = self.missing_page()
        self.management.toggleRow(token)
        self.missing.write_bytes(b'restored and then moved')
        with closing(HistoryStore.for_storage(self.paths)) as store:
            row = store.media_record('flat', {'tweet_id': '200', 'num': 1, 'type': 'photo'})
            store.begin_deletion('test-batch', row, True, file_signature(self.missing))
        self.missing.rename(self.kept / self.missing.name)
        requests = []
        self.management.retryRequested.connect(requests.append)
        self.management.retrySelected()
        self.wait(lambda: not self.management.mutating and not self.management.busy)
        self.assertEqual(requests, [])
        self.assertTrue(self.management.error)
        self.assertEqual((self.kept / self.missing.name).read_bytes(), b'restored and then moved')

    def test_missing_choice_waits_for_other_job_using_approved_former_root(self):
        previous = self.root / 'previous-output'
        previous.mkdir()
        old_path = previous / self.missing.name
        with closing(HistoryStore.for_storage(self.paths)) as store, store._db:
            store._db.execute("UPDATE media SET path=?,last_path='' WHERE tweet_id='200'", (str(old_path),))
            store._db.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES('allowed_media_roots',?)",
                              (json.dumps([str(self.output), str(previous)]),))
        guard = OutputGuard(previous)
        guard.acquire()
        self.addCleanup(guard.close)
        token = self.missing_page()
        self.management.toggleRow(token)
        self.management.setIgnored(True)
        self.wait(lambda: not self.management.mutating and not self.management.busy)
        self.assertTrue(self.management.error)
        self.assertEqual(self.ignored(), set())

    def test_cancel_and_stale_generation_never_move_a_file(self):
        original = self.present.read_bytes()
        self.management.prepareDelete([self.gallery_key()])
        self.wait(lambda: self.management.deleteConfirmationPending)
        self.assertEqual(self.management.deleteCount, 1)
        self.management.cancelDelete()
        self.assertFalse(self.management.mutating)
        self.assertEqual(self.present.read_bytes(), original)
        self.management.prepareDelete([self.gallery_key()])
        self.wait(lambda: self.management.deleteConfirmationPending)
        self.library.refresh()
        self.management.confirmDelete(False)
        self.assertFalse(self.management.mutating)
        self.assertIn('바뀌었습니다', self.management.error)
        self.assertEqual(self.present.read_bytes(), original)

    def test_stop_during_delete_preparation_does_not_open_confirmation(self):
        started, release = threading.Event(), threading.Event()
        original = self.library.deletionManifest
        def slow_manifest(keys):
            started.set()
            if not release.wait(5):
                raise TimeoutError('test fixture was not released')
            return original(keys)
        with patch.object(self.library, 'deletionManifest', side_effect=slow_manifest):
            self.management.prepareDelete([self.gallery_key()])
            self.wait(started.is_set)
            try:
                self.management.stop()
            finally:
                release.set()
            self.wait(lambda: self.management.deleteConfirmationPending or not self.management.mutating)
        self.assertFalse(self.management.deleteConfirmationPending)
        self.assertFalse(self.management.mutating)
        self.assertEqual(self.present.read_bytes(), b'fixture-100')

    def test_failed_native_move_is_reported_in_history_and_preserves_file(self):
        self.management.prepareDelete([self.gallery_key()])
        self.wait(lambda: self.management.deleteConfirmationPending)
        with patch('luvbox.deletion.recycle_file', side_effect=RecyclingError('fixture failure')) as recycle:
            self.management.confirmDelete(True)
            self.wait(lambda: not self.management.mutating)
        recycle.assert_called_once()
        self.assertEqual(self.present.read_bytes(), b'fixture-100')
        with closing(HistoryStore.for_storage(self.paths)) as store:
            [row] = store.deletion_page()['rows']
            self.assertEqual(row['status'], 'failed')
            self.assertFalse(store.ignored_media())

    def test_confirmed_move_records_exact_selected_file_and_keep_choice(self):
        self.management.prepareDelete([self.gallery_key()])
        self.wait(lambda: self.management.deleteConfirmationPending)
        def fake_recycle(path, expected):
            path = Path(path)
            self.assertEqual(file_signature(path), tuple(expected))
            target = self.kept / path.name
            path.rename(target)
            return str(target)
        with patch('luvbox.deletion.recycle_file', side_effect=fake_recycle) as recycle:
            self.management.confirmDelete(True)
            self.wait(lambda: not self.management.mutating)
        recycle.assert_called_once()
        self.assertFalse(self.present.exists())
        self.assertEqual((self.kept / self.present.name).read_bytes(), b'fixture-100')
        with closing(HistoryStore.for_storage(self.paths)) as store:
            [row] = store.deletion_page()['rows']
            self.assertEqual(row['status'], 'recycled')
            self.assertEqual(store.ignored_media(), {('100', 1, 'photo')})


if __name__ == '__main__':
    unittest.main()
