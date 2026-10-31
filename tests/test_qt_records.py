"""The records controller lists runs and unsaved media off the UI thread and gates retries."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # Never open the user's app data.
from PySide6.QtCore import QCoreApplication
from luvbox import activity, unsaved
from luvbox.qt_records import RecordsController
from luvbox.storage import resolve_storage

MIB = 1048576


class RecordsControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='luvbox-records-qt-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / '사진 보관'
        self.output.mkdir()
        for name, value in (('luvbox.unsaved.unsaved_path', self.root / 'unsaved.sqlite3'),
                            ('luvbox.activity.activity_path', self.root / 'activity.sqlite3'),
                            ('luvbox.storage.records_root', self.root / 'records'),
                            ('luvbox.qt_library.records_root', self.root / 'records')):
            patcher = patch(name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        legacy = patch('luvbox.activity._legacy_row', return_value=None)
        legacy.start()
        self.addCleanup(legacy.stop)
        self.records = RecordsController()
        self.addCleanup(self.close)
        self.retries = []
        self.records.retryRequested.connect(self.retries.append)

    def close(self):
        self.records.shutdown()
        self.wait(lambda: not self.records._worker.is_alive())

    def wait(self, predicate, seconds=8):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail('Records operation did not settle')

    def add(self, items, account='alice'):
        unsaved.record(items, account=account, output_dir=str(self.output))

    @staticmethod
    def item(tweet, reason=unsaved.FAILED, num=1, media_type='photo', **fields):
        return {'tweet_id': tweet, 'num': num, 'media_type': media_type, 'reason': reason, 'author': 'carol',
                'posted_at': '2026-10-02T09:30:00+09:00', 'detail': '이유', **fields}

    def open_unsaved(self, view='unsaved'):
        self.records.setLibrary('alice', str(self.output), False)
        self.records.setActive('unsaved', True)
        self.records.setUnsavedView(view)
        self.wait(lambda: not self.records.unsavedBusy)

    def test_unsaved_rows_describe_each_media_and_count_what_needs_attention(self):
        self.add([self.item('100'),
                  self.item('200', unsaved.SIZE, 1, 'video', width=1280, height=720, duration=133,
                            size_bytes=340 * MIB, limit_mb=200),
                  self.item('300', unsaved.EXCLUDED)])
        self.open_unsaved()
        self.wait(lambda: self.records.unsavedCount == 2)
        self.assertEqual((self.records.unsavedCount, self.records.excludedCount, self.records.unsavedTotal), (2, 1, 2))
        rows = {row['reasonLabel']: row for row in self.records.unsavedRows}
        self.assertEqual(set(rows), {'저장 실패', '용량 제한'})
        self.assertEqual(rows['용량 제한']['info'], '영상 · 1280×720 · 2분 13초 · 약 340MB')
        self.assertEqual((rows['저장 실패']['author'], rows['저장 실패']['posted']), ('@carol', '2026-10-02'))
        self.records.setUnsavedView('excluded')
        self.wait(lambda: not self.records.unsavedBusy and self.records.unsavedTotal == 1)
        self.assertEqual(self.records.unsavedRows[0]['reasonLabel'], '직접 제외')

    def test_size_limited_media_retries_only_after_the_limit_was_raised(self):
        self.add([self.item('100'), self.item('200', unsaved.SIZE, 1, 'video', limit_mb=200)])
        self.records.setVideoLimit(200)
        self.open_unsaved()
        by_reason = {row['reason']: row for row in self.records.unsavedRows}
        self.assertTrue(by_reason['failed']['canRetry'])
        self.assertFalse(by_reason['size']['canRetry'])
        self.assertIn('크기 제한', by_reason['size']['hint'])
        self.assertEqual(self.records.retryableCount, 1)
        self.records.retry(by_reason['size']['token'])
        self.assertEqual(self.retries, [])
        self.records.setVideoLimit(500)
        self.assertEqual(self.records.retryableCount, 2)
        self.records.retry({row['reason']: row for row in self.records.unsavedRows}['size']['token'])
        self.assertEqual(self.retries, [('200_video_1',)])

    def test_retry_all_collects_every_page_and_skips_what_cannot_succeed(self):
        self.add([self.item(str(1000 + number)) for number in range(60)]
                 + [self.item('2000', unsaved.SIZE, 3, 'animated_gif', limit_mb=50)])
        self.records.setVideoLimit(50)
        self.open_unsaved()
        self.assertEqual((self.records.unsavedPages, len(self.records.unsavedRows)), (2, 50))
        self.records.retryAll()
        self.wait(lambda: bool(self.retries))
        keys = self.retries[0]
        self.assertEqual(len(keys), 60)
        self.assertNotIn('2000_animated_gif_3', keys)
        self.assertIn('1000_1', keys)

    def test_dismissing_forgets_the_row_without_touching_other_lists(self):
        self.add([self.item('100'), self.item('200'), self.item('300', unsaved.EXCLUDED)])
        self.open_unsaved()
        self.records.dismiss(self.records.unsavedRows[0]['token'])
        self.wait(lambda: self.records.unsavedTotal == 1 and not self.records.unsavedBusy)
        self.assertEqual((self.records.unsavedCount, self.records.excludedCount), (1, 1))
        self.assertIn('저장된 파일에는 영향이 없습니다', self.records.unsavedStatus)
        self.records.dismissAll()
        self.wait(lambda: self.records.unsavedTotal == 0 and not self.records.unsavedBusy)
        self.assertEqual(self.records.excludedCount, 1)

    def test_changing_account_or_folder_drops_the_old_list(self):
        self.add([self.item('100')])
        self.open_unsaved()
        self.assertEqual(self.records.unsavedTotal, 1)
        self.records.setLibrary('bob', str(self.output), False)
        self.assertEqual((self.records.unsavedTotal, self.records.unsavedCount, self.records.unsavedRows), (0, 0, []))
        self.wait(lambda: not self.records.unsavedBusy)
        self.assertEqual(self.records.unsavedTotal, 0)

    def test_run_rows_are_short_and_keep_unusual_results_visible(self):
        activity.record_outcome({'run_id': 'a', 'status': 'finished', 'saved': 12, 'skipped': 340, 'size_skipped': 1,
                                 'failed_media': 2, 'message': 'X가 보여 주는 좋아요 목록을 모두 확인했습니다. 삭제된 게시물은 빠질 수 있습니다.'},
                                origin='manual', account='alice', output_dir=str(self.output),
                                started_at='2026-10-30T21:05:12+09:00')
        activity.record_outcome({'run_id': 'b', 'status': 'error', 'message': 'X 연결이 만료됐습니다.',
                                 'log': str(self.root / 'logs' / 'scheduled-1.log')},
                                origin='scheduled', account='bob', output_dir=str(self.output),
                                started_at='2026-10-31T09:00:00+09:00')
        self.records.setLibrary('alice', str(self.output), False)
        self.records.setActive('runs', True)
        self.wait(lambda: not self.records.runBusy and self.records.runTotal == 2)
        newest, oldest = self.records.runRows
        self.assertEqual((newest['status'], newest['tone'], newest['account'], newest['origin'], newest['hasLog']),
                         ('실패', 'error', '@bob', '자동', True))
        self.assertEqual(newest['message'], 'X 연결이 만료됐습니다.')
        self.assertEqual((oldest['when'], oldest['summary'], oldest['message']),
                         ('2026-10-30 21:05', '저장 12개 · 이미 저장됨 340개 · 저장 못 함 3개', ''))

    def test_an_unreadable_ledger_is_reported_as_an_error_not_an_empty_list(self):
        (self.root / 'unsaved.sqlite3').write_bytes(b'broken' * 200)
        self.open_unsaved()
        self.assertTrue(self.records.unsavedError)
        self.assertEqual(self.records.unsavedRows, [])


if __name__ == '__main__':
    unittest.main()
