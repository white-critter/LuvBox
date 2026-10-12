"""Merge dialog context and worker completion, with isolated account records."""

import queue
import unittest
from contextlib import closing
from tkinter import ttk
from unittest.mock import patch

from tests import test_account_merge as merge_support
from xlikesaver.history import HistoryStore
from xlikesaver.history_gui import HistoryWindow
from xlikesaver.gallery import TILE
from xlikesaver.storage import MigrationError


class AccountMergeGuiStabilityTests(unittest.TestCase):
    setUp = merge_support.AccountMergeWindowTests.setUp
    write_cookies = merge_support.AccountMergeWindowTests.write_cookies
    load = merge_support.AccountMergeWindowTests.load

    def start_merge(self):
        self.load()
        with patch("xlikesaver.history_gui.dialogs.askyesno", return_value=True), \
                patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.merge_accounts()
        self.assertTrue(self.window.choice_active)
        return thread.call_args.kwargs["args"]

    def test_worker_publishes_one_batch_result_for_multiple_accounts(self):
        results = queue.Queue()
        accounts = ["old_account", "another"]
        with patch("xlikesaver.history_gui.merge_accounts_records",
                   return_value={"records": 4, "added": 3, "keys": 4}) as merge:
            HistoryWindow._merge_worker(results, 1, self.paths, accounts)
        merge.assert_called_once_with(self.paths, accounts)
        generation, kind, message = results.get_nowait()
        self.assertEqual((generation, kind), (1, "accounts-merged"))
        self.assertIn("새로 더함 3개", message)
        self.assertTrue(results.empty())

    def test_failed_batch_releases_busy_state_and_leaves_current_records_unchanged(self):
        args = self.start_merge()
        with patch("xlikesaver.history_gui.merge_accounts_records", side_effect=MigrationError("offline fixture")):
            HistoryWindow._merge_worker(self.window.results, *args)
        self.window._poll()
        self.assertFalse(self.window.busy)
        self.assertFalse(self.window.choice_active)
        self.assertFalse(self.window._destroyed)
        self.assertIn("지금 계정의 기록은 그대로입니다", self.window.status_text.get())
        with closing(HistoryStore.for_storage(self.paths, create_media=False)) as store:
            self.assertEqual(store.records(), [])
            self.assertEqual(store.merged_accounts(), set())
        self.window.close()
        self.assertTrue(self.window._destroyed)

    def test_pending_close_completes_after_a_failed_batch(self):
        args = self.start_merge()
        self.window.close()
        self.assertTrue(self.window.closing)
        self.assertFalse(self.window._destroyed)
        with patch("xlikesaver.history_gui.merge_accounts_records", side_effect=MigrationError("offline fixture")):
            HistoryWindow._merge_worker(self.window.results, *args)
        self.window._poll()
        self.assertFalse(self.window.busy)
        self.assertFalse(self.window.choice_active)
        self.assertTrue(self.window._destroyed)

    def test_dialog_context_change_prevents_starting_a_stale_merge(self):
        self.load()

        def changed(*args, **kwargs):
            self.window.generation += 1
            return True

        with patch("xlikesaver.history_gui.dialogs.askyesno", side_effect=changed), \
                patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.merge_accounts()
        thread.assert_not_called()
        self.assertFalse(self.window.busy)
        self.assertFalse(self.window.choice_active)

    def test_missing_detail_keeps_its_small_width_when_the_window_resizes(self):
        self.load()
        path = self.paths.media_dir / "100_1.jpg"
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store.record_media("flat", {"tweet_id": "100", "num": 1, "type": "photo"}, path)
        path.unlink()
        with patch("xlikesaver.history_gui.threading.Thread"):
            self.window.refresh(import_legacy=True)
            self.load()
        self.window.attributes("-alpha", 0)
        self.window.geometry("820x700")
        self.window.deiconify()
        self.window.tabs.select(3)
        [key] = self.window.missing_rows
        self.window.missing_tree.selection_set(key)
        self.window.missing_tree.focus(key)
        self.window._missing_selected()
        self.window.update()
        label = next(child for child in self.window.missing_card.winfo_children()
                     if isinstance(child, ttk.Label) and str(child.cget("textvariable")) == str(self.window.missing_detail))
        self.assertLessEqual(int(label.cget("wraplength")), TILE[0] + 10)
        self.assertLess(self.window.missing_card.winfo_width(), TILE[0] + 100)
        self.assertGreater(self.window.missing_tree.master.winfo_width(), 400)


if __name__ == "__main__":
    unittest.main()
