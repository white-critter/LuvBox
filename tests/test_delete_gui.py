"""Library deletion UI with temporary catalogs and a mocked native worker."""

from pathlib import Path
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from xlikesaver import theme
from xlikesaver.history_gui import HistoryWindow
from tests import test_history_gui as history_tests


class DeleteGuiTests(unittest.TestCase):
    setUp = history_tests.HistoryGuiTests.setUp
    load = history_tests.HistoryGuiTests.load
    records = history_tests.HistoryGuiTests.records

    def prepare(self, count=3):
        store = self.records(count)
        self.load()
        return store

    def select(self, *tweets, focus=None):
        keys = tuple(key for key, row in self.window.rows.items() if row["tweet_id"] in set(map(str, tweets)))
        self.window.tree.selection_set(keys)
        focus = str(focus) if focus is not None else str(tweets[-1]) if tweets else None
        self.window.tree.focus(next((key for key in keys if self.window.rows[key]["tweet_id"] == focus), ""))
        self.window.select_record()
        return keys

    def test_list_and_gallery_keep_multiple_selection_and_preview_focus_in_sync(self):
        self.prepare()
        keys = self.select(100, 102, focus=102)
        self.assertEqual(self.window.selected()["tweet_id"], "102")
        self.window.set_view("grid")
        self.assertEqual(self.window.grid_view.selected_keys, keys)
        self.assertEqual(self.window.rows[self.window.grid_view.selected]["tweet_id"], "102")
        other = next(key for key, row in self.window.rows.items() if row["tweet_id"] == "101")
        self.window._select_gallery(keys + (other,), other)
        self.assertEqual(set(self.window.tree.selection()), set(keys) | {other})
        self.assertEqual(self.window.selected()["tweet_id"], "101")
        self.window.set_view("table")
        self.assertEqual(len(self.window.tree.selection()), 3)
        self.window.set_view("grid")
        self.assertEqual(len(self.window.grid_view.selected_keys), 3)
        self.window._select_gallery((), None)
        self.assertEqual(self.window.tree.selection(), ())
        self.assertIsNone(self.window.selected())

    def test_selected_count_and_confirmation_deduplicate_physical_files(self):
        store = self.prepare(1)
        source = self.account / "100_1.jpg"
        store.record_media("author", {"tweet_id": "100", "num": 1, "type": "photo",
                                       "author": {"name": "alice"}}, source)
        self.load()
        self.window.select_all_files()
        self.assertEqual(len(self.window.tree.selection()), 2)
        self.assertEqual(len(self.window._selected_files()), 1)
        self.assertIn("파일 1개 선택", self.window.file_selection_text.get())
        with patch("xlikesaver.history_gui.dialogs.confirm_delete", return_value=None) as confirm:
            self.window.start_deletion()
        self.assertEqual(confirm.call_args.args, (1, len(b"offline-image"), [source.name]))

    def test_select_all_and_delete_keys_leave_text_editing_and_other_tabs_alone(self):
        self.prepare()
        event = SimpleNamespace(widget=self.window.tree)
        self.assertEqual(self.window._file_select_all_key(event), "break")
        self.assertEqual(len(self.window.tree.selection()), 3)
        self.assertIn("3개", self.window.file_selection_text.get())
        with patch.object(self.window, "start_deletion") as start:
            self.assertEqual(self.window._file_delete_key(event), "break")
            start.assert_called_once_with()
        entry_event = SimpleNamespace(widget=self.window.search_entry)
        self.assertIsNone(self.window._file_select_all_key(entry_event))
        with patch.object(self.window, "start_deletion") as start:
            self.assertIsNone(self.window._file_delete_key(entry_event))
            start.assert_not_called()
        self.window.tabs.select(4)
        with patch.object(self.window, "start_deletion") as start:
            self.assertIsNone(self.window._file_delete_key(event))
            self.assertIsNone(self.window._file_select_all_key(event))
            start.assert_not_called()

    def test_approved_request_starts_one_ui_free_worker_and_local_stop_control(self):
        self.prepare()
        self.select(100, 101)
        previous = self.window.generation
        with patch("xlikesaver.history_gui.dialogs.confirm_delete", return_value=False), \
                patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.start_deletion()
        self.assertTrue(self.window.busy)
        self.assertTrue(self.window.delete_active)
        self.assertFalse(self.window.delete_stop.is_set())
        self.assertFalse(self.window.metadata_active)
        self.assertEqual(self.window.generation, previous + 1)
        worker = thread.call_args.kwargs["target"]
        self.assertIs(worker.func, HistoryWindow._delete_worker)
        self.assertIs(worker.args[0], self.window.results)
        self.assertIs(worker.args[1], self.window.delete_stop)
        generation, location, rows, keep = thread.call_args.kwargs["args"]
        self.assertEqual((generation, location, keep), (self.window.generation, self.account, False))
        self.assertEqual({row["tweet_id"] for row in rows}, {"100", "101"})
        self.assertEqual(str(self.window.delete_button.cget("state")), "disabled")
        self.assertEqual(str(self.window.file_all_button.cget("state")), "disabled")
        self.assertIn("휴지통", self.window.source_stop_button.cget("text"))
        self.window.stop_metadata()
        self.assertTrue(self.window.delete_stop.is_set())
        self.assertEqual(str(self.window.source_stop_button.cget("state")), "disabled")

    def test_cancel_busy_and_download_never_start_deletion(self):
        self.prepare()
        self.select(100)
        with patch("xlikesaver.history_gui.threading.Thread") as thread, \
                patch("xlikesaver.history_gui.dialogs.confirm_delete", return_value=None) as confirm:
            self.window.start_deletion()
            confirm.assert_called_once()
            self.window.busy = True
            self.window.start_deletion()
            self.window.busy = False
            self.window.is_downloading = lambda: True
            with patch("xlikesaver.history_gui.dialogs.showinfo"):
                self.window.start_deletion()
            thread.assert_not_called()
        self.window._update_file_actions()
        self.assertEqual(str(self.window.delete_button.cget("state")), "disabled")
        self.assertFalse(self.window.delete_active)

    def test_modal_revalidates_generation_account_folder_busy_and_download(self):
        self.prepare()
        self.select(100)
        changes = (
            lambda: setattr(self.window, "generation", self.window.generation + 1),
            lambda: setattr(self.window, "current_account", "other"),
            lambda: setattr(self.window, "current_storage", self.account / "other"),
            lambda: setattr(self.window, "busy", True),
            lambda: setattr(self.window, "is_downloading", lambda: True),
            lambda: setattr(self.window, "closing", True),
        )
        for change in changes:
            with self.subTest(change=change):
                self.window.current_account, self.window.current_storage = "offline", self.account
                self.window.busy = self.window.closing = False
                self.window.is_downloading = lambda: False

                def approve(*args, **kwargs):
                    change()
                    return True

                with patch("xlikesaver.history_gui.dialogs.confirm_delete", side_effect=approve), \
                        patch("xlikesaver.history_gui.threading.Thread") as thread:
                    self.window.start_deletion()
                thread.assert_not_called()
                self.assertFalse(self.window.delete_active)
        self.window.busy = self.window.closing = False

    def test_modal_rejects_stale_main_account_and_folder_even_without_refresh(self):
        self.prepare()
        self.select(100)
        for new_location in (("other", self.account), ("offline", self.account / "other")):
            with self.subTest(location=new_location):
                self.window.get_location = lambda: new_location
                with patch("xlikesaver.history_gui.dialogs.confirm_delete", return_value=True), \
                        patch("xlikesaver.history_gui.threading.Thread") as thread, \
                        patch("xlikesaver.history_gui.dialogs.showinfo") as info:
                    self.window.start_deletion()
                thread.assert_not_called()
                info.assert_called_once()
                self.assertFalse(self.window.delete_active)

    def test_worker_progress_and_completion_release_ui_and_refresh_all_records(self):
        self.prepare()
        self.select(101)
        self.window.delete_active = self.window.busy = True
        self.window.results.put((self.window.generation, "delete-progress", {"message": "fixture progress"}))
        with patch.object(self.window, "refresh") as refresh:
            self.window._poll()
            self.assertEqual(self.window.status_text.get(), "fixture progress")
            self.assertTrue(self.window.delete_active)
            refresh.assert_not_called()
            self.window.results.put((self.window.generation, "deleted", {
                "paths": [str(self.account / "101_1.jpg")], "message": "fixture done", "recycled": 1,
            }))
            self.window._poll()
        self.assertFalse(self.window.busy)
        self.assertFalse(self.window.delete_active)
        self.assertEqual(self.window.completion_text, "fixture done")
        refresh.assert_called_once_with()
        remaining = [row for row in self.window.rows.values() if row["tweet_id"] != "101"]
        self.window._display({"files": 2}, remaining, [])
        self.assertEqual(self.window.selected()["tweet_id"], "100")
        self.assertEqual(self.window.tile_values["files"].get(), "2")

    def test_worker_completion_uses_previous_neighbor_at_end_and_first_of_clamped_page(self):
        self.prepare()
        self.select(100)
        removed = str(self.account / "100_1.jpg")
        self.window._selection_after_deletion([removed])
        remaining = [row for row in self.window.rows.values() if row["tweet_id"] != "100"]
        self.window._display({"files": 2}, remaining, [])
        self.assertEqual(self.window.selected()["tweet_id"], "101")
        self.window.select_all_files()
        self.window._selection_after_deletion([row["path"] for row in remaining])
        other = dict(remaining[0], tweet_id="999", path=str(self.account / "999_1.jpg"))
        self.window._display({"files": 1}, [other], [])
        self.assertEqual(self.window.selected()["tweet_id"], "999")

    def test_close_waits_for_worker_and_requests_stop_before_destroy(self):
        self.prepare()
        self.window.delete_active = self.window.busy = True
        self.window.close()
        self.assertTrue(self.window.delete_stop.is_set())
        self.assertTrue(self.window.closing)
        self.assertFalse(self.window._destroyed)
        self.window.results.put((self.window.generation, "deleted", {"paths": [], "message": "stopped"}))
        self.window._poll()
        self.assertTrue(self.window._destroyed)

    def test_worker_reports_unexpected_error_without_tk_or_real_deletion(self):
        results = queue.Queue()
        stop = threading.Event()
        rows = [{"path": "offline"}]
        with patch("xlikesaver.deletion.run_deletion", side_effect=OSError("fixture")):
            HistoryWindow._delete_worker(results, stop, 11, self.account, rows, True)
        generation, kind, result = results.get_nowait()
        self.assertEqual((generation, kind, result["failed"]), (11, "deleted", 1))
        self.assertEqual(result["paths"], [])
        self.assertTrue(results.empty())

    def test_other_local_job_modals_cannot_start_over_a_delete_job(self):
        self.prepare()

        def deletion_started(*args, **kwargs):
            self.window.busy = self.window.delete_active = True
            return True

        for method in (self.window.backfill, self.window.convert_gifs, self.window.repair_records):
            with self.subTest(method=method):
                self.window.busy = self.window.delete_active = False
                self.window.current_summary["mp4_gifs"] = 1
                with patch("xlikesaver.metadata.available", return_value=True), \
                        patch("xlikesaver.media.ffmpeg_path", return_value=Path("offline-ffmpeg.exe")), \
                        patch("xlikesaver.history_gui.dialogs.askyesno", side_effect=deletion_started), \
                        patch("xlikesaver.history_gui.threading.Thread") as thread:
                    method()
                thread.assert_not_called()
                self.assertFalse(self.window.metadata_active)
        self.window.busy = self.window.delete_active = False

    def test_deletion_policy_is_only_reported_applied_after_verified_recycle(self):
        self.prepare(1)
        statuses = ("recycled", "failed", "cancelled", "pending", "interrupted")
        rows = [dict(id=str(index), filename="offline.jpg", path=str(self.account / "100_1.jpg"),
                     tweet_id="100", status=status, keep_deleted=True, filesize=4)
                for index, status in enumerate(statuses)]
        self.window._show_deletions({"rows": rows, "total": 5, "pages": 1, "page": 0})
        policies = [self.window.deletion_tree.item(str(index), "values")[-1] for index in range(5)]
        self.assertEqual(policies, ["다시 받지 않음", "변경 없음", "변경 없음", "확인 전", "확인 필요"])
        self.window.deletion_tree.selection_set("1")
        self.window.select_deletion()
        self.assertIn("요청한 설정  이 계정에서 다시 받지 않기", self.window.deletion_detail.get())

    def journal(self, store, row, status="failed", *, remove=False, keep=True):
        status_file = Path(row["path"]).stat()
        signature = tuple(getattr(status_file, name) for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns"))
        ident = store.begin_deletion("offline_test", row, keep, signature)
        if remove:
            Path(row["path"]).unlink()
        store.finish_deletion(ident, status, "offline result")
        return ident

    def test_loader_omits_recycled_files_and_shows_source_and_policy_in_deletion_tab(self):
        store = self.prepare(2)
        recycled = next(row for row in store.records() if row["tweet_id"] == "100")
        self.journal(store, recycled, "recycled", remove=True)
        self.load()
        self.assertEqual(len(self.window.rows), 1)
        self.assertEqual(self.window.tile_values["files"].get(), "1")
        self.assertEqual(len(self.window.deletion_rows), 1)
        self.assertIn("1개", self.window.deletion_page_text.get())
        self.window.tabs.select(4)
        self.window.deletion_tree.selection_set("0")
        self.window.select_deletion()
        self.assertIn(recycled["path"], self.window.deletion_detail.get())
        self.assertIn("https://x.com/i/web/status/100", self.window.deletion_detail.get())
        self.assertIn("휴지통으로 보냄", self.window.deletion_detail.get())
        self.assertEqual(self.window.deletion_tree.item("0", "values")[-1], "다시 받지 않음")
        with patch("xlikesaver.history_gui.webbrowser.open") as browser:
            self.window.open_deletion_tweet()
        browser.assert_called_once_with("https://x.com/i/web/status/100")

    def test_deletion_history_has_independent_paging_and_does_not_recheck_files(self):
        store = self.prepare(1)
        row = store.records()[0]
        for _ in range(105):
            self.journal(store, row)
        self.window._load(self.window.generation, self.account, "all", "", 0, False)
        self.window._poll()
        self.assertEqual(len(self.window.deletion_rows), 100)
        self.assertEqual(self.window.deletion_total_pages, 2)
        self.assertEqual(str(self.window.deletion_next_button.cget("state")), "normal")
        with patch.object(self.window, "refresh") as refresh:
            self.window.go_page(None, deletions=True)
        self.assertEqual(self.window.deletion_page, 1)
        self.assertEqual(self.window.page, 0)
        refresh.assert_called_once_with(view_only=True)
        self.window._load(self.window.generation, self.account, "all", "", 0, False, deletion_page=1)
        self.window._poll()
        self.assertEqual(len(self.window.deletion_rows), 5)
        self.assertEqual(self.window.displayed_deletion_page, 1)
        self.assertIn("2 / 2", self.window.deletion_page_text.get())
        self.window.deletion_page_input.set("invalid")
        self.window.jump_page(deletions=True)
        self.assertEqual(self.window.deletion_page_input.get(), "2")
        self.window.tabs.select(4)
        with patch.object(self.window, "change_page") as change:
            self.window._page_key(SimpleNamespace(widget=self.window.deletion_tree, keysym="Prior", state=4))
        change.assert_called_once_with(-1, runs=False, missing=False, deletions=True)

    def test_new_controls_and_deletion_table_fit_small_light_and_dark_windows(self):
        self.prepare()
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        self.window.attributes("-alpha", 0)
        self.window.deiconify()
        for mode in ("light", "dark"):
            theme.apply(self.root, mode)
            for geometry in ("760x640", "1200x780"):
                with self.subTest(mode=mode, geometry=geometry):
                    self.window.geometry(geometry)
                    for tab in (0, 4):
                        self.window.tabs.select(tab)
                        self.root.update()
                        self.window._layout_pagers()
                        self.root.update()
                        if tab == 0:
                            actions, bar = self.window.file_selection_actions, self.window.file_selection_bar
                            self.assertLessEqual(actions.winfo_x() + actions.winfo_width(), bar.winfo_width() + 2)
                            self.assertGreater(self.window.tree.winfo_height(), 50)
                        else:
                            self.assertGreater(self.window.deletion_tree.winfo_height(), 50)
                            self.assertTrue(self.window.deletion_tree.winfo_ismapped())


if __name__ == "__main__":
    unittest.main()
