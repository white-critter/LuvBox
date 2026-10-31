"""Run history merges the app-wide ledger with runs only a catalog remembers."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # Isolate all app data from the user's PC.
from luvbox import activity
from luvbox.history import HistoryStore
from luvbox.run_history import PAGE_SIZE, run_page
from luvbox.storage import resolve_storage


class RunHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='luvbox-run-history-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / '사진 보관'
        self.output.mkdir()
        for name, value in (('luvbox.storage.records_root', self.root / 'records'),
                            ('luvbox.qt_library.records_root', self.root / 'records'),
                            ('luvbox.activity.activity_path', self.root / 'activity.sqlite3')):
            patcher = patch(name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        legacy = patch('luvbox.activity._legacy_row', return_value=None)
        legacy.start()
        self.addCleanup(legacy.stop)
        self.paths = resolve_storage(self.output, 'alice')
        self.store = HistoryStore.for_storage(self.paths)
        self.addCleanup(self.store.close)

    def record(self, run_id, account='alice', **fields):
        activity.record_outcome({'run_id': run_id, 'status': 'finished', **fields}, origin='manual',
                                account=account, output_dir=str(self.output),
                                started_at=fields.get('started_at', '2026-10-28T10:00:00+09:00'))

    def test_linked_manual_outcome_and_catalog_run_are_not_shown_twice(self):
        run_id = self.store.start_run('flat')
        self.store.finish_run(run_id, 'finished', saved=3)
        self.store.start_run('flat')  # Older/legacy records remain visible separately.
        self.record('run-1', history_run_id=run_id, saved=3)
        rows = run_page(self.paths)['rows']
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(row['origin'] == 'manual' for row in rows), 1)
        self.assertEqual(sum(row['origin'] == 'legacy' for row in rows), 1)
        self.assertEqual(next(row for row in rows if row['origin'] == 'manual')['saved'], 3)

    def test_linked_run_deduplicates_equivalent_output_path_with_trailing_separator(self):
        run_id = self.store.start_run('flat')
        self.store.finish_run(run_id, 'finished', saved=1)
        activity.record_outcome({'run_id': 'alternate-path', 'status': 'finished', 'history_run_id': run_id},
                                origin='manual', account='alice', output_dir=str(self.output) + os.sep,
                                started_at='2026-10-28T10:00:00+09:00')
        rows = run_page(self.paths)['rows']
        self.assertEqual([row['origin'] for row in rows], ['manual'])

    def test_every_account_is_listed_without_choosing_a_scope(self):
        self.record('mine', account='alice', started_at='2026-10-28T10:00:00+09:00')
        self.record('theirs', account='bob', started_at='2026-10-29T10:00:00+09:00')
        rows = run_page(self.paths)['rows']
        self.assertEqual([(row['run_id'], row['account']) for row in rows], [('theirs', 'bob'), ('mine', 'alice')])
        self.assertEqual({row['account'] for row in run_page(None)['rows']}, {'alice', 'bob'})

    def test_other_accounts_never_hide_the_current_catalog_runs(self):
        run_id = self.store.start_run('flat')
        self.store.finish_run(run_id, 'finished', saved=1)
        self.record('other', account='bob', history_run_id=run_id)
        origins = sorted(row['origin'] for row in run_page(self.paths)['rows'])
        self.assertEqual(origins, ['legacy', 'manual'])

    def test_an_unreadable_catalog_keeps_the_ledger_and_says_so(self):
        self.record('mine')
        with patch('luvbox.run_history._ReadOnlyHistory', side_effect=ValueError('unsafe catalog')):
            result = run_page(self.paths)
        self.assertEqual([row['run_id'] for row in result['rows']], ['mine'])
        self.assertTrue(result['legacy_unreadable'])

    def test_a_catalog_that_does_not_exist_yet_is_not_an_error(self):
        other = resolve_storage(self.root / '새 폴더', 'newcomer')
        self.record('mine', account='newcomer')
        result = run_page(other)
        self.assertEqual([row['run_id'] for row in result['rows']], ['mine'])
        self.assertFalse(result['legacy_unreadable'])

    def test_pages_are_newest_first_and_clamped(self):
        for index in range(PAGE_SIZE + 3):
            self.record(f'run-{index:03d}', started_at=f'2026-10-{1 + index // 24:02d}T{index % 24:02d}:00:00+09:00')
        first, last = run_page(self.paths, 0), run_page(self.paths, 99)
        self.assertEqual((first['total'], first['pages'], len(first['rows'])), (PAGE_SIZE + 3, 2, PAGE_SIZE))
        self.assertEqual((last['page'], len(last['rows'])), (1, 3))
        self.assertGreater(first['rows'][0]['started_at'], last['rows'][-1]['started_at'])


if __name__ == '__main__':
    unittest.main()
