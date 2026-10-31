"""The unsaved-media ledger stays bounded, per account and folder, and never creates itself on read."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # Isolate all app data from the user's PC.
from luvbox import unsaved


def item(tweet='100', num=1, media_type='photo', reason=unsaved.FAILED, **fields):
    return {'tweet_id': tweet, 'num': num, 'media_type': media_type, 'reason': reason, 'author': 'alice',
            'posted_at': '2026-10-01T09:00:00+09:00', 'detail': 'reason text', **fields}


class UnsavedLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='luvbox-unsaved-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'unsaved.sqlite3'
        patcher = patch('luvbox.unsaved.unsaved_path', return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = str(Path(self.temp.name) / 'media')

    def record(self, items, **fields):
        unsaved.record(items, account='Alice', output_dir=self.folder, **fields)

    def rows(self, kinds=(unsaved.FAILED, unsaved.SIZE)):
        return unsaved.page('alice', self.folder, kinds)['rows']

    def test_reading_never_creates_the_ledger(self):
        self.assertEqual(unsaved.counts('alice', self.folder), {'failed': 0, 'size': 0, 'excluded': 0})
        self.assertEqual(unsaved.page('alice', self.folder)['total'], 0)
        self.assertEqual(unsaved.identities('alice', self.folder), set())
        self.assertFalse(self.path.exists())

    def test_repeating_a_failure_updates_one_row_and_counts_the_attempts(self):
        self.record([item(reason=unsaved.FAILED)])
        self.record([item(reason=unsaved.SIZE, size_bytes=500 * 1048576, limit_mb=200, author='', media_type='photo')])
        [row] = self.rows()
        self.assertEqual((row['reason'], row['attempts'], row['size_bytes'], row['limit_mb']),
                         (unsaved.SIZE, 2, 500 * 1048576, 200))
        # Facts learned once are kept when a later try does not know them.
        self.assertEqual((row['author'], row['posted_at'][:10]), ('alice', '2026-10-01'))

    def test_saving_the_media_later_removes_its_row_only(self):
        self.record([item('100'), item('200', 2, 'video', unsaved.SIZE)])
        self.record([], resolved=[('100', 1, 'photo')])
        self.assertEqual([(row['tweet_id'], row['media_type']) for row in self.rows()], [('200', 'video')])

    def test_rows_belong_to_one_account_and_folder_spelling(self):
        self.record([item()])
        unsaved.record([item('300')], account='bob', output_dir=self.folder)
        unsaved.record([item('400')], account='alice', output_dir=str(Path(self.temp.name) / 'other'))
        self.assertEqual([row['tweet_id'] for row in self.rows()], ['100'])
        again = unsaved.page('@ALICE', self.folder + os.sep)['rows']
        self.assertEqual([row['tweet_id'] for row in again], ['100'])

    def test_excluded_rows_have_their_own_view(self):
        self.record([item('100'), item('200', reason=unsaved.EXCLUDED)])
        self.assertEqual(unsaved.counts('alice', self.folder), {'failed': 1, 'size': 0, 'excluded': 1})
        self.assertEqual([row['tweet_id'] for row in self.rows((unsaved.EXCLUDED,))], ['200'])

    def test_only_the_newest_rows_are_kept(self):
        with patch('luvbox.unsaved.MAX_ITEMS', 3):
            for number in range(5):
                self.record([item(str(100 + number))])
        self.assertEqual(sorted(row['tweet_id'] for row in self.rows()), ['102', '103', '104'])

    def test_invalid_items_are_ignored_and_text_is_bounded(self):
        self.record([item('not-a-number'), item(media_type='audio'), item(num=0), item(reason='other'),
                     item('500', detail='x' * 1000, author='bad name!')])
        [row] = self.rows()
        self.assertEqual((row['tweet_id'], len(row['detail']), row['author']), ('500', 300, ''))

    def test_pages_and_removal(self):
        self.record([item(str(100 + number)) for number in range(7)])
        first = unsaved.page('alice', self.folder, page=0, page_size=3)
        self.assertEqual((first['total'], first['pages'], len(first['rows'])), (7, 3, 3))
        self.assertEqual(len(unsaved.page('alice', self.folder, page=9, page_size=3)['rows']), 1)
        self.assertEqual(unsaved.remove('alice', self.folder, items=[('100', 1, 'photo')]), 1)
        self.assertEqual(unsaved.remove('alice', self.folder, kinds=(unsaved.FAILED,)), 6)
        self.assertEqual(unsaved.counts('alice', self.folder)['failed'], 0)

    def test_a_ledger_that_cannot_be_written_is_reported_not_crashed(self):
        self.path.write_bytes(b'not a database' * 100)
        with self.assertRaises(unsaved.UnsavedError):
            self.record([item()])
        with self.assertRaises(unsaved.UnsavedError):
            unsaved.counts('alice', self.folder)

    def test_reading_an_older_empty_database_is_an_empty_ledger(self):
        sqlite3.connect(self.path).close()
        self.assertEqual(unsaved.page('alice', self.folder)['total'], 0)


if __name__ == '__main__':
    unittest.main()
