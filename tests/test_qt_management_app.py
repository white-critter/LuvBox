"""The real Qt shell connects missing-file actions and records complete worker outcomes."""
from contextlib import closing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # Never touch the user's local app data.
from PySide6.QtCore import QCoreApplication
from luvbox import activity, login
from luvbox.app_settings import SettingsRepository
from luvbox.history import HistoryStore
from luvbox.management_store import ManagementSnapshot
from luvbox.qt_app import ApplicationController
from luvbox.qt_download import DownloadController
from luvbox.storage import resolve_storage


class ControlledService:
    """Use the real Qt DownloadController with a deterministic isolated worker."""
    job_id = 7

    def __init__(self):
        self.pending = []
        self.started = []
        self.auto_exit = True
        self.stopped = False

    def start(self, options):
        self.started.append(options)
        return True

    def drain(self, limit):
        rows, self.pending = self.pending[:limit], self.pending[limit:]
        return rows

    def stop(self):
        self.stopped = True

    def set_review_enabled(self, _enabled):
        pass

    def close(self):
        if self.started and self.auto_exit:
            self.finish('stopped', code=0)

    def finish(self, status, *, code=0, forced=False, history_run_id=None, saved=0, skipped=0):
        self.pending.extend([
            {'type': 'finished', 'job_id': self.job_id, 'status': status, 'saved': saved,
             'skipped': skipped, 'history_run_id': history_run_id},
            {'type': 'process_exit', 'job_id': self.job_id, 'code': code, 'forced': forced,
             'stop_requested': self.stopped},
        ])


@unittest.skipUnless(os.name == 'nt', 'The account fixture uses Windows DPAPI')
class ManagementAppIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='luvbox-management-app-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'LOCALAPPDATA': str(self.root), 'APPDATA': str(self.root / 'roaming')})
        env.start()
        self.addCleanup(env.stop)
        self.output = self.root / '사진 폴더'
        self.paths = resolve_storage(self.output, 'offline')
        with closing(HistoryStore.for_storage(self.paths)) as store:
            path = self.paths.media_dir / 'offline_200_1.jpg'
            path.write_bytes(b'synthetic media')
            store.record_media('flat', {'tweet_id': '200', 'num': 1, 'type': 'photo',
                                        'author': {'name': 'offline'}}, path)
            path.unlink()
        SettingsRepository().update({'account': 'offline', 'output_dir': str(self.output),
                                     'record_locations': {'offline': str(self.output)}})
        login.save_authenticated_session('synthetic-cookie', {'user_id': '123', 'handle': 'offline',
                                                               'display_name': 'Offline'})
        self.service = ControlledService()
        for replacement in (
                patch('luvbox.qt_app.DownloadController', side_effect=lambda parent: DownloadController(parent, service=self.service)),
                patch('luvbox.qt_schedule.task_status', return_value={'registered': False}),
                patch('luvbox.qt_browser_setup.available_browsers', return_value=[])):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.controller = ApplicationController()
        self.addCleanup(self.close)
        self.wait(lambda: not self.controller.busy and not self.controller.browserSetup.busy)
        self.assertTrue(self.controller.accounts.connected)

    def wait(self, condition, seconds=10):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents()
            if condition():
                return
            time.sleep(.005)
        self.fail('App integration operation did not settle')

    def close(self):
        if not self.controller.closing:
            self.service.auto_exit = True
            self.controller.requestClose()
        if self.controller.download.running and not self.service.pending:
            self.service.finish('stopped', code=0)
        self.wait(lambda: self.controller.shutdownReady)

    def begin(self):
        self.controller.startSave()
        self.wait(lambda: bool(self.service.started))

    def catalog_run(self):
        with closing(HistoryStore.for_storage(self.paths)) as store:
            ident = store.start_run('flat')
            store.finish_run(ident, 'finished', saved=2, skipped=1)
        return ident

    def ledger(self):
        return activity.outcome_page(0, 100, account='offline')['rows']

    def test_normal_terminal_event_links_activity_and_catalog_without_duplicate(self):
        self.begin()
        ident = self.catalog_run()
        self.service.finish('finished', history_run_id=ident, saved=2, skipped=1)
        self.wait(lambda: not self.controller.download.running and bool(self.ledger()))
        [outcome] = self.ledger()
        self.assertEqual((outcome['origin'], outcome['status'], outcome['history_run_id'], outcome['saved']),
                         ('manual', 'finished', ident, 2))
        with closing(ManagementSnapshot(self.paths)) as snapshot:
            rows = snapshot.run_page()['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['origin'], 'manual')

    def test_forced_interruption_stays_distinct_and_retains_history_link(self):
        self.begin()
        ident = self.catalog_run()
        self.service.finish('stopped', code=1, forced=True, history_run_id=ident, saved=2)
        self.wait(lambda: not self.controller.download.running and bool(self.ledger()))
        [outcome] = self.ledger()
        self.assertEqual((outcome['status'], outcome['history_run_id'], outcome['saved']), ('interrupted', ident, 2))
        self.assertIn('강제로 종료', self.controller.download.status)
        with closing(ManagementSnapshot(self.paths)) as snapshot:
            self.assertEqual(len(snapshot.run_page()['rows']), 1)

    def test_app_close_waits_for_worker_terminal_and_persists_outcome(self):
        self.begin()
        ident = self.catalog_run()
        self.service.auto_exit = False
        self.controller.requestClose()
        self.app.processEvents()
        self.assertFalse(self.controller.shutdownReady)
        self.service.finish('stopped', history_run_id=ident, saved=2)
        self.wait(lambda: self.controller.shutdownReady)
        [outcome] = self.ledger()
        self.assertEqual((outcome['status'], outcome['history_run_id'], outcome['saved']), ('stopped', ident, 2))
        self.assertTrue(self.service.stopped)


if __name__ == '__main__':
    unittest.main()
