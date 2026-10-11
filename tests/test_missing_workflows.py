"""Missing-file context, paging and serialized local choices; all data is temporary."""

from contextlib import closing
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests import test_gui, test_history_gui
from xlikesaver.history import HistoryStore
from xlikesaver.history_gui import HistoryWindow
from xlikesaver.protection import AccountGuard, ProtectionError, _BUSY_HELP
from xlikesaver.recovery import latest_backup
from xlikesaver.storage import OutputGuard, publish_snapshot, resolve_storage


def missing_item(index, ignored=False):
    tweet = str(index + 100)
    return {"tweet_id": tweet, "num": 1, "media_type": "photo", "archive_key": tweet + "_1",
            "author": "alice", "filename": tweet + "_1.jpg", "ignored": ignored}


class MissingWorkflowTests(unittest.TestCase):
    setUp = test_history_gui.HistoryGuiTests.setUp
    load = test_history_gui.HistoryGuiTests.load

    def show_items(self, count=1):
        self.load()
        self.window.missing_items = [missing_item(index) for index in range(count)]
        self.window._show_missing()

    def test_paging_reaches_every_item_and_selects_only_current_page(self):
        self.show_items(5001)
        self.assertEqual(len(self.window.missing_rows), 100)
        self.assertEqual(self.window.missing_total_pages, 51)
        self.window.select_all_missing()
        self.assertEqual(len(self.window._missing_selection()), 100)
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.go_page(None, missing=True)
        thread.assert_not_called()  # Page turns use the loaded snapshot.
        self.assertEqual(list(self.window.missing_rows), ["5100_1"])
        self.assertEqual(self.window._missing_selection(), [])
        self.assertIn("51 / 51", self.window.missing_page_text.get())
        self.window.missing_page_input.set("2")
        self.window.jump_page(missing=True)
        self.assertEqual(list(self.window.missing_rows)[0], "200_1")
        self.window.missing_page_input.set("bad")
        self.window.jump_page(missing=True)
        self.assertEqual(self.window.missing_page_input.get(), "2")
        self.window.change_page(-1, missing=True)
        self.assertEqual(list(self.window.missing_rows)[0], "100_1")

    def test_hidden_filter_is_applied_before_paging_and_resets_selection(self):
        self.show_items(5001)
        for item in self.window.missing_items[:5000]:
            item["ignored"] = True
        self.window._show_missing()
        self.assertEqual(list(self.window.missing_rows), ["5100_1"])
        self.assertEqual(self.window.missing_total_pages, 1)
        self.assertIn("숨긴 파일 5,000개", self.window.missing_text.get())
        self.window.select_all_missing()
        self.window.show_ignored.set(True)
        self.window._filter_missing()
        self.assertEqual(self.window._missing_selection(), [])
        self.assertEqual(self.window.missing_total_pages, 51)
        self.window.go_page(None, missing=True)
        self.assertEqual(list(self.window.missing_rows), ["5100_1"])
        self.window.show_ignored.set(False)
        self.window._filter_missing()
        self.assertEqual(self.window.missing_page, 0)

    def test_hiding_last_page_clamps_to_remaining_page(self):
        self.show_items(201)
        self.window.go_page(None, missing=True)
        self.assertEqual(self.window.missing_page, 2)
        self.window.missing_items[-1]["ignored"] = True
        self.window._show_missing()
        self.assertEqual(self.window.missing_page, 1)
        self.assertEqual(len(self.window.missing_rows), 100)
        self.assertEqual(str(self.window.missing_next_button.cget("state")), "disabled")

    def test_download_disables_and_guards_both_choice_actions(self):
        self.show_items(2)
        self.window.missing_items[1]["ignored"] = True
        self.window.show_ignored.set(True)
        self.window._show_missing()
        self.window.select_all_missing()
        self.window.is_downloading = lambda: True
        self.window._update_missing_actions()
        self.assertEqual(str(self.window.ignore_button.cget("state")), "disabled")
        self.assertEqual(str(self.window.unignore_button.cget("state")), "disabled")
        with patch("xlikesaver.history_gui.threading.Thread") as thread, \
                patch("xlikesaver.history_gui.dialogs.showinfo") as notice:
            self.window.choose_missing(True)
            self.window.choose_missing(False)
        thread.assert_not_called()
        self.assertEqual(notice.call_count, 2)
        self.assertFalse(self.window.choice_active)
        self.window.is_downloading = lambda: False
        self.window._update_missing_actions()
        self.assertEqual(str(self.window.ignore_button.cget("state")), "normal")
        self.assertEqual(str(self.window.unignore_button.cget("state")), "normal")

    def test_modal_confirmation_cannot_apply_old_selection_after_refresh_or_save(self):
        self.show_items()
        self.window.select_all_missing()

        def changed_catalog(*args, **kwargs):
            self.window.generation += 1
            return True

        with patch("xlikesaver.history_gui.dialogs.askyesno", side_effect=changed_catalog), \
                patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.choose_missing(True)
        thread.assert_not_called()
        self.window.retry_missing = Mock(return_value=True)
        with patch("xlikesaver.history_gui.dialogs.askyesno", side_effect=changed_catalog):
            self.window.retry_missing_files()
        self.window.retry_missing.assert_not_called()

        def began_saving(*args, **kwargs):
            self.window.is_downloading = lambda: True
            return True

        with patch("xlikesaver.history_gui.dialogs.askyesno", side_effect=began_saving), \
                patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.choose_missing(True)
        thread.assert_not_called()
        self.assertFalse(self.window.choice_active)

    def test_choice_flag_remains_active_until_worker_result_and_close_waits(self):
        self.show_items()
        self.window.select_all_missing()
        with patch("xlikesaver.history_gui.dialogs.askyesno", return_value=True), \
                patch("xlikesaver.history_gui.threading.Thread"):
            self.window.choose_missing(True)
        self.assertTrue(self.window.choice_active)
        self.window.close()
        self.assertTrue(self.window.winfo_exists())
        self.assertTrue(self.window.closing)
        self.window.results.put((self.window.generation, "missing-choice", "끝"))
        self.window._poll()
        self.assertFalse(self.window.winfo_exists())

    def test_record_repair_finishes_before_the_library_window_closes(self):
        self.load()
        with patch("xlikesaver.history_gui.dialogs.askyesno", return_value=True), \
                patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.repair_records()
        thread.assert_called_once()
        self.assertTrue(self.window.repair_active)
        self.window.close()
        self.assertTrue(self.window.winfo_exists())
        self.assertIn("기록 복구", self.window.status_text.get())
        self.window.results.put((self.window.generation, "damaged", "복구하지 못했습니다"))
        self.window._poll()
        self.assertFalse(self.window.winfo_exists())

    def test_choice_flag_clears_on_failure_without_closing(self):
        self.show_items()
        self.window.busy = self.window.choice_active = True
        self.window.results.put((self.window.generation, "error", "다른 작업 중"))
        self.window._poll()
        self.assertFalse(self.window.choice_active)
        self.assertFalse(self.window.busy)
        self.assertEqual(self.window.status_text.get(), "다른 작업 중")

    def finish_choice_with_cleanup_error(self, *, closing=False):
        self.show_items()
        self.window.current_storage = resolve_storage(self.account, "offline")
        self.window.select_all_missing()
        with patch("xlikesaver.history_gui.dialogs.askyesno", return_value=True), \
                patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.choose_missing(True)
        self.assertTrue(self.window.busy)
        self.assertTrue(self.window.choice_active)
        if closing:
            self.window.close()
            self.assertTrue(self.window.winfo_exists())
            self.assertTrue(self.window.closing)
        close_output = OutputGuard.close

        def fail_close(guard):
            close_output(guard)
            raise OSError("offline output cleanup failure")

        with patch.object(OutputGuard, "close", fail_close):
            thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
        self.window._poll()

    def test_choice_cleanup_failure_releases_ui_and_allows_close(self):
        self.finish_choice_with_cleanup_error()
        self.assertFalse(self.window.busy)
        self.assertFalse(self.window.choice_active)
        self.assertTrue(self.window.winfo_exists())
        self.assertIn("선택 기록 정리", self.window.status_text.get())
        self.window.close()
        self.assertFalse(self.window.winfo_exists())

    def test_choice_cleanup_failure_finishes_an_already_requested_close(self):
        self.finish_choice_with_cleanup_error(closing=True)
        self.assertFalse(self.window.busy)
        self.assertFalse(self.window.choice_active)
        self.assertFalse(self.window.winfo_exists())


class RetryContextTests(unittest.TestCase):
    setUp = test_gui.GuiTests.setUp

    def source(self):
        self.app.account.set("offline_test")
        self.app.folder.set(self.temp.name)
        return self.app._history_location()

    def test_changed_account_or_folder_is_rejected_before_auth_and_storage(self):
        account, paths = self.source()
        for new_account, folder in (("different", self.temp.name),
                                    (account, str(Path(self.temp.name) / "other"))):
            with self.subTest(account=new_account, folder=folder):
                self.app.account.set(new_account)
                self.app.folder.set(folder)
                with patch.object(self.app, "start") as start, \
                        patch("xlikesaver.gui.dialogs.showinfo") as notice:
                    self.assertFalse(self.app.retry_missing(["100_1"], account, paths))
                start.assert_not_called()
                notice.assert_called_once()
                self.assertIn("새로고침", notice.call_args.args[1])

    def test_same_catalog_allows_current_layout_and_account_folder_options(self):
        account, paths = self.source()
        self.session_mock.return_value = True
        self.app.account.set("@OFFLINE_TEST")
        self.app.folder_mode.set("작성자별 폴더")
        self.app.separate_accounts.set(True)
        with patch("xlikesaver.gui.threading.Thread") as thread:
            self.assertTrue(self.app.retry_missing(["100_1"], account, paths))
        options = thread.call_args.kwargs["args"][0]
        self.assertEqual((options.account, options.folder_mode, options.separate_accounts),
                         (account, "author", True))

    def test_choice_write_blocks_main_start_but_ordinary_history_loading_does_not(self):
        self.source()
        self.session_mock.return_value = True
        self.app.history_window = Mock(metadata_active=False, choice_active=True, repair_active=False)
        self.app.history_window.winfo_exists.return_value = True
        with patch("xlikesaver.gui.threading.Thread") as thread, \
                patch("xlikesaver.gui.dialogs.showinfo") as notice:
            self.assertFalse(self.app.start())
        thread.assert_not_called()
        notice.assert_called_once()
        self.app.history_window.choice_active = False
        self.app.history_window.busy = True  # A read-only load is not a mutation.
        with patch("xlikesaver.gui.threading.Thread"):
            self.assertTrue(self.app.start())
        self.app.history_window._update_missing_actions.assert_called_once()

    def test_record_repair_blocks_saving_and_delays_app_exit(self):
        self.source()
        self.session_mock.return_value = True
        self.app.history_window = Mock(metadata_active=False, choice_active=False,
                                       repair_active=True)
        self.app.history_window.winfo_exists.return_value = True
        with patch("xlikesaver.gui.threading.Thread") as thread, \
                patch("xlikesaver.gui.dialogs.showinfo") as notice:
            self.assertFalse(self.app.start())
        thread.assert_not_called()
        notice.assert_called_once()
        with patch.object(self.app.root, "after") as later, patch.object(self.app.root, "destroy") as destroy:
            self.app.close()
        destroy.assert_not_called()
        later.assert_called_once()
        self.app.history_window.stop_metadata.assert_not_called()
        self.assertIn("기록 복구", self.app.status.get())

    def test_save_refreshes_the_open_library_at_its_own_pace(self):
        self.app.history_window = Mock(metadata_active=False, choice_active=False,
                                       repair_active=False)
        self.app.history_window.winfo_exists.return_value = True
        self.app.history_window.auto_refresh_delay.return_value = 4500
        with patch.object(self.app.root, "after") as later:
            self.app._schedule_history_refresh()
            self.app._schedule_history_refresh()
        later.assert_called_once_with(4500, self.app._refresh_history)

    def test_running_state_refreshes_missing_action_buttons(self):
        self.app.history_window = Mock(metadata_active=False, choice_active=False, repair_active=False)
        self.app.history_window.winfo_exists.return_value = True
        self.app.running = True
        self.app._set_running(True)
        self.app.running = False
        self.app._set_running(False)
        self.assertEqual(self.app.history_window._update_missing_actions.call_count, 2)


class ChoiceCopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        state = patch("xlikesaver.protection.state_directory", return_value=Path(self.temp.name) / "state")
        state.start()
        self.addCleanup(state.stop)
        self.paths = resolve_storage(Path(self.temp.name) / "media", "offline")
        with closing(HistoryStore.for_storage(self.paths)) as store:
            path = self.paths.media_dir / "100_1.jpg"
            path.write_bytes(b"offline-image")
            store.record_media("flat", {"tweet_id": "100", "num": 1, "type": "photo"}, path)
            path.unlink()
        publish_snapshot(self.paths)

    def choose(self, ignored=True):
        results = queue.Queue()
        HistoryWindow._choice_worker(results, 1, self.paths, [("100", 1, "photo")], ignored)
        return results.get_nowait()

    def ignored(self, directory=None):
        store = HistoryStore(directory) if directory is not None else HistoryStore.for_storage(self.paths)
        with closing(store):
            return store.ignored_media()

    def test_choice_and_unhide_are_immediately_in_snapshot_and_local_backup(self):
        for ignored in (True, False):
            with self.subTest(ignored=ignored):
                result = self.choose(ignored)
                self.assertEqual(result[1], "missing-choice")
                expected = {("100", 1, "photo")} if ignored else set()
                self.assertEqual(self.ignored(), expected)
                self.assertEqual(self.ignored(self.paths.snapshot_dir), expected)
                backup = latest_backup(self.paths.account, self.paths.record_dir)
                self.assertIsNotNone(backup)
                import sqlite3
                with closing(sqlite3.connect(backup)) as copy:
                    self.assertEqual(copy.execute("SELECT ignored FROM missing_choices").fetchone()[0], int(ignored))

    def test_account_and_output_locks_reject_overlap_and_release_after_failure(self):
        for guard in (AccountGuard(self.paths.account, sleep=lambda *_: None), OutputGuard(self.paths.output_dir)):
            with self.subTest(guard=type(guard).__name__):
                guard.acquire()
                try:
                    self.assertEqual(self.choose()[1], "error")
                    self.assertEqual(self.ignored(), set())
                finally:
                    guard.close()
        self.assertEqual(self.choose()[1], "missing-choice")

    def test_copy_failure_keeps_local_choice_and_reports_warning(self):
        with patch("xlikesaver.history_gui.publish_snapshot", side_effect=OSError), \
                patch("xlikesaver.recovery.backup_catalog", side_effect=OSError):
            result = self.choose()
        self.assertEqual(result[1], "missing-choice")
        self.assertIn("선택은 이 PC에 저장", result[2])
        self.assertIn("갱신하지 못했습니다", result[2])
        self.assertEqual(self.ignored(), {("100", 1, "photo")})
        self.assertEqual(self.ignored(self.paths.snapshot_dir), set())

    def test_guard_cleanup_errors_still_release_other_guard_and_report_one_result(self):
        close_output, close_account = OutputGuard.close, AccountGuard.close
        for failed in ({"output"}, {"account"}, {"output", "account"}):
            with self.subTest(failed=failed):
                closed = []

                def finish_output(guard):
                    closed.append("output")
                    close_output(guard)
                    if "output" in failed:
                        raise OSError("offline output cleanup failure")

                def finish_account(guard):
                    closed.append("account")
                    close_account(guard)
                    if "account" in failed:
                        raise OSError("offline account cleanup failure")

                results = queue.Queue()
                with patch.object(OutputGuard, "close", finish_output), \
                        patch.object(AccountGuard, "close", finish_account):
                    HistoryWindow._choice_worker(results, 7, self.paths, [("100", 1, "photo")], True)
                generation, kind, message = results.get_nowait()
                self.assertEqual((generation, kind), (7, "error"))
                self.assertIn("선택 기록 정리", message)
                self.assertEqual(closed, ["output", "account"])
                self.assertTrue(results.empty())
                self.assertEqual(self.ignored(), {("100", 1, "photo")})
                for guard in (AccountGuard(self.paths.account, sleep=lambda *_: None), OutputGuard(self.paths.output_dir)):
                    try:
                        guard.acquire()
                    finally:
                        guard.close()

    def test_cleanup_error_preserves_an_existing_protection_failure(self):
        guard = Mock()
        guard.acquire.side_effect = ProtectionError(_BUSY_HELP)
        guard.close.side_effect = OSError("offline account cleanup failure")
        with patch("xlikesaver.protection.AccountGuard", return_value=guard):
            generation, kind, message = self.choose()
        self.assertEqual((generation, kind, message), (1, "error", _BUSY_HELP))
        guard.close.assert_called_once_with()
        self.assertEqual(self.ignored(), set())


if __name__ == "__main__":
    unittest.main()
