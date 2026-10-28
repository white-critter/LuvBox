"""Management views must reflect current files without altering their catalog."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # Isolate all app data from the user's PC.
from luvbox import activity
from luvbox.history import HistoryStore
from luvbox.management_store import ManagementSnapshot, PAGE_SIZE
from luvbox.recycling import file_signature
from luvbox.storage import resolve_storage


class ManagementSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='luvbox-management-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / '사진 보관'
        self.output.mkdir()
        self.records = self.root / 'records'
        self.paths = resolve_storage(self.output, 'alice')
        records = patch('luvbox.storage.records_root', return_value=self.records)
        records.start()
        self.addCleanup(records.stop)
        records2 = patch('luvbox.qt_library.records_root', return_value=self.records)
        records2.start()
        self.addCleanup(records2.stop)
        self.paths = resolve_storage(self.output, 'alice')
        ledger = patch('luvbox.activity.activity_path', return_value=self.root / 'activity.sqlite3')
        ledger.start()
        self.addCleanup(ledger.stop)
        legacy = patch('luvbox.activity._legacy_row', return_value=None)
        legacy.start()
        self.addCleanup(legacy.stop)
        self.store = HistoryStore.for_storage(self.paths)
        self.addCleanup(self.store.close)

    def add(self, tweet, *, mode='flat', author='alice', path=None, payload=b'image'):
        path = path or self.output / f'{tweet}_1.jpg'
        if not path.exists():
            path.write_bytes(payload)
        self.store.record_media(mode, {'tweet_id': str(tweet), 'num': 1, 'type': 'photo',
                                       'author': {'name': author},
                                       'date': datetime(2024, 2, 1, tzinfo=timezone.utc)}, path)
        return path

    def snapshot(self):
        result = ManagementSnapshot(self.paths)
        self.addCleanup(result.close)
        return result

    def test_missing_and_cloud_unavailable_are_distinct_and_sources_remain_unchanged(self):
        present, missing, unavailable = (self.add('100'), self.add('200'), self.add('300'))
        missing.unlink()
        self.store.close()
        before = hashlib.sha256(self.store.path.read_bytes()).digest()
        with patch('luvbox.management_store.cloud_only', side_effect=lambda path: path == unavailable):
            snapshot = self.snapshot()
        rows = {row['tweet_id']: row for row in snapshot.missing_page()['rows']}
        self.assertEqual(set(rows), {'200', '300'})
        self.assertEqual(rows['200']['availability'], 'missing')
        self.assertEqual(rows['300']['availability'], 'unavailable')
        self.assertEqual([row['tweet_id'] for row in snapshot.missing_page(mode='missing')['rows']], ['200'])
        self.assertEqual([row['tweet_id'] for row in snapshot.missing_page(mode='unavailable')['rows']], ['300'])
        self.assertEqual(hashlib.sha256(self.store.path.read_bytes()).digest(), before)
        self.assertTrue(present.is_file())
        self.assertTrue(unavailable.is_file())

    def test_many_missing_items_are_paged_without_losing_or_repeating_identities(self):
        for number in range(PAGE_SIZE + 5):
            self.add(str(1000 + number)).unlink()
        snapshot = self.snapshot()
        first, second = snapshot.missing_page(0), snapshot.missing_page(1)
        self.assertEqual((first['total'], first['pages'], len(first['rows']), len(second['rows'])),
                         (PAGE_SIZE + 5, 2, PAGE_SIZE, 5))
        ids = [row['archive_key'] for row in first['rows'] + second['rows']]
        self.assertEqual(len(set(ids)), PAGE_SIZE + 5)
        self.add('4000').unlink()
        self.assertEqual(snapshot.missing_page()['total'], PAGE_SIZE + 5)
        self.assertEqual(self.snapshot().missing_page()['total'], PAGE_SIZE + 6)

    def test_pending_recycle_is_unavailable_and_retains_the_requested_exclusion(self):
        for ident, keep in (('100', True), ('200', False)):
            path = self.add(ident)
            row = self.store.media_record('flat', {'tweet_id': ident, 'num': 1, 'type': 'photo'})
            self.store.begin_deletion('test-batch', row, keep, file_signature(path))
            path.rename(self.root / path.name)
        snapshot = self.snapshot()
        rows = {row['tweet_id']: row for row in snapshot.missing_page()['rows']}
        self.assertEqual(set(rows), {'100', '200'})
        self.assertEqual([(rows[key]['availability'], rows[key]['ignored']) for key in ('100', '200')],
                         [('unavailable', 1), ('unavailable', 0)])
        self.assertEqual(snapshot.missing_page(mode='missing')['total'], 0)
        self.assertEqual(snapshot.missing_page(mode='unavailable')['total'], 2)

    def test_statistics_preserve_existing_deduped_metadata_and_earliest_save_time(self):
        path = self.add('100', mode='flat', author='alice')
        self.add('100', mode='author', author='', path=path)
        with self.store._db:
            self.store._db.execute("UPDATE media SET author='alice',saved_at='2024-01-05T00:00:00+09:00', "
                                   "posted_at='2024-02-05T00:00:00+09:00',posted_source='metadata', "
                                   "updated_at='2024-02-05T00:00:00+09:00' WHERE folder_mode='flat'")
            self.store._db.execute("UPDATE media SET author='',saved_at='2025-11-05T00:00:00+09:00', "
                                   "posted_at='2025-12-05T00:00:00+09:00',posted_source='estimated', "
                                   "updated_at='2025-12-05T00:00:00+09:00' WHERE folder_mode='author'")
        expected = self.store.statistics(top=None)
        result = self.snapshot().statistics()['statistics']
        self.assertEqual(result['summary']['files'], 1)
        self.assertEqual([(x['author'], x['files'], x['posts']) for x in result['authors']],
                         [(x['author'].lower(), x['files'], x['posts']) for x in expected['authors']])
        months = {(row['kind'], row['month']): row['files'] for row in result['months']}
        for kind, source in (('게시월', 'posted'), ('저장월', 'saved'), ('확인된 좋아요 관측월', 'liked')):
            self.assertEqual({month: count for (label, month), count in months.items() if label == kind},
                             dict(expected['months'][source]))

    def test_statistics_keep_author_spelling_and_exclude_invalid_months(self):
        self.add('100', author='Alice')
        self.add('200', author='ALICE')
        with self.store._db:
            self.store._db.execute("UPDATE media SET posted_at='not-a-date',saved_at='bad' WHERE tweet_id='200'")
            self.store._db.execute("INSERT INTO likes(tweet_id,like_key,liked_after,liked_before) VALUES('200',1,'old','invalid')")
        expected = self.store.statistics(top=None)
        actual = self.snapshot().statistics()['statistics']
        self.assertEqual([(row['author'], row['files']) for row in actual['authors']],
                         [(row['author'], row['files']) for row in expected['authors']])
        for row in actual['months']:
            self.assertRegex(row['month'], r'^\d{4}-\d{2}$')
        for kind, source in (('게시월', 'posted'), ('저장월', 'saved'), ('확인된 좋아요 관측월', 'liked')):
            self.assertEqual({row['month']: row['files'] for row in actual['months'] if row['kind'] == kind},
                             dict(expected['months'][source]))

    def test_linked_manual_outcome_and_catalog_run_are_not_shown_twice(self):
        run_id = self.store.start_run('flat')
        self.store.finish_run(run_id, 'finished', saved=3)
        self.store.start_run('flat')  # Older/legacy records remain visible separately.
        activity.record_outcome({'run_id': 'run-1', 'status': 'finished', 'history_run_id': run_id, 'saved': 3},
                                origin='manual', account='alice', output_dir=str(self.output),
                                started_at='2026-10-28T10:00:00+09:00')
        rows = self.snapshot().run_page()['rows']
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
        rows = self.snapshot().run_page()['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['origin'], 'manual')

    def test_existing_regular_hardlink_paths_remain_visible_in_statistics(self):
        first = self.add('100')
        second = self.output / '200_1.jpg'
        try:
            os.link(first, second)
        except OSError:
            self.skipTest('hardlink fixtures unavailable')
        self.add('200', path=second)
        expected = self.store.statistics(top=None)
        snapshot = self.snapshot()
        self.assertEqual(snapshot.statistics()['statistics']['summary']['files'],
                         sum(item['files'] for item in expected['authors']))
        self.assertEqual(snapshot.missing_page()['total'], 0)


if __name__ == '__main__':
    unittest.main()
