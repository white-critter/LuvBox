"""File selection, deletion confirmation and journals, using temporary media."""

import json
import queue
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from xlikesaver import theme
from xlikesaver.history_gui import HistoryWindow
from xlikesaver.recycling import file_signature
from tests import test_history_gui as history_tests


class FileSelectionGuiTests(unittest.TestCase):
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

    def show(self):
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        self.window.attributes("-alpha", 0)
        self.window.deiconify()
        self.root.update()

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

    def test_selected_count_and_path_copy_deduplicate_physical_files(self):
        store = self.prepare(1)
        source = self.account / "100_1.jpg"
        store.record_media("author", {"tweet_id": "100", "num": 1, "type": "photo",
                                     "author": {"name": "alice"}}, source)
        self.load()
        self.window.select_all_files()
        self.assertEqual(len(self.window.tree.selection()), 2)
        self.assertEqual(len(self.window._selected_files()), 1)
        self.assertIn("파일 1개 선택", self.window.file_selection_text.get())
        self.window._copy_file_paths()
        self.assertEqual(self.window.clipboard_get(), str(source))

    def test_mouse_only_multiple_selection_toggles_rows_and_synchronizes_gallery(self):
        self.prepare()
        self.show()
        self.window.toggle_button.invoke()
        order = self.window.tree.get_children()

        def click(key):
            x, y, width, height = self.window.tree.bbox(key, "posted")
            self.window.tree.event_generate("<ButtonPress-1>", x=x + width // 2, y=y + height // 2)
            self.window.tree.event_generate("<ButtonRelease-1>", x=x + width // 2, y=y + height // 2)
            self.root.update()

        click(order[0])
        click(order[2])
        self.assertEqual(set(self.window.tree.selection()), {order[0], order[2]})
        click(order[0])
        self.assertEqual(self.window.tree.selection(), (order[2],))
        self.window.set_view("grid")
        self.assertTrue(self.window.grid_view.toggle_mode)
        self.assertEqual(self.window.grid_view.selected_keys, (order[2],))

    def test_select_all_key_leaves_text_editing_and_other_tabs_alone(self):
        self.prepare()
        event = SimpleNamespace(widget=self.window.tree)
        self.assertEqual(self.window._file_select_all_key(event), "break")
        self.assertEqual(len(self.window.tree.selection()), 3)
        self.assertIn("3개", self.window.file_selection_text.get())
        self.assertIsNone(self.window._file_select_all_key(SimpleNamespace(widget=self.window.search_entry)))
        self.window.tabs.select(4)
        self.assertIsNone(self.window._file_select_all_key(event))

    def test_delete_action_asks_in_list_and_gallery_and_cancel_preserves_media(self):
        store = self.prepare()
        self.select(100, 101)
        before = {Path(row["path"]): (Path(row["path"]).read_bytes(), Path(row["path"]).stat().st_mtime_ns)
                  for row in store.records()}
        records = store.records()
        self.assertTrue(self.window.bind("<Delete>"))
        labels = {button.cget("text") for button in self.window.file_selection_actions.winfo_children()}
        self.assertIn("휴지통으로 보내기", labels)
        commands = {label: callback for label, callback, _enabled in self.window._file_menu_commands()}
        self.assertEqual(set(commands), {"파일 열기", "트윗 열기", "이 작성자 파일 보기",
                                         "선택한 파일 경로 복사", "이 페이지 모두 선택", "선택 해제", "휴지통으로 보내기"})
        self.show()
        for view in ("table", "grid"):
            with self.subTest(view=view):
                self.window.set_view(view)
                self.root.update()
                target = self.window.tree if view == "table" else self.window.grid_view.canvas
                target.focus_force()
                with patch("xlikesaver.history_gui.dialogs.confirm_delete", return_value=None) as confirmation, \
                        patch("xlikesaver.history_gui.threading.Thread") as thread:
                    target.event_generate("<Delete>")
                    self.root.update()
                confirmation.assert_called_once()
                thread.assert_not_called()
                self.assertEqual(store.records(), records)
                self.assertEqual(store.deletion_page()["total"], 0)
                self.assertEqual(store.ignored_media(), set())
                for path, expected in before.items():
                    self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), expected)

    def test_delete_confirmation_deduplicates_files_and_keeps_unchecked_choice(self):
        store = self.prepare(1)
        source = self.account / "100_1.jpg"
        store.record_media("author", {"tweet_id": "100", "num": 1, "type": "photo"}, source)
        self.load()
        self.window.select_all_files()
        expected = file_signature(source)
        with patch("xlikesaver.history_gui.dialogs.confirm_delete", return_value=False) as confirm, \
                patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.start_deletion()
        confirm.assert_called_once_with(1, source.stat().st_size, [source.name], parent=self.window)
        generation, location, files, keep = thread.call_args.kwargs["args"]
        self.assertEqual((generation, location, keep), (self.window.generation, self.account, False))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["_delete_signature"], expected)
        self.assertNotIn("_delete_signature", next(iter(self.window.rows.values())))
        self.assertTrue(self.window.busy and self.window.delete_active)
        self.assertTrue(self.window.delete_button.instate(["disabled"]))
        self.assertEqual(thread.call_args.kwargs["target"].func, HistoryWindow._delete_worker)
        self.assertEqual(thread.call_args.kwargs["target"].args, (self.window.results, self.window.delete_stop))
        thread.return_value.start.assert_called_once()

    def test_delete_key_leaves_entries_and_other_tabs_alone(self):
        self.prepare()
        self.select(100)
        with patch.object(self.window, "start_deletion") as start:
            self.assertIsNone(self.window._file_delete_key(SimpleNamespace(widget=self.window.search_entry)))
            self.window.tabs.select(1)
            self.assertIsNone(self.window._file_delete_key(SimpleNamespace(widget=self.window.tree)))
            start.assert_not_called()
            self.window.tabs.select(0)
            self.assertEqual(self.window._file_delete_key(SimpleNamespace(widget=self.window.tree)), "break")
            start.assert_called_once()

    def test_delete_waits_during_download_or_media_folder_disconnect(self):
        self.prepare()
        self.select(100)
        for offline, downloading in ((False, True), (True, False)):
            with self.subTest(offline=offline, downloading=downloading), \
                    patch("xlikesaver.history_gui.dialogs.showinfo") as notice, \
                    patch("xlikesaver.history_gui.dialogs.confirm_delete") as confirm, \
                    patch("xlikesaver.history_gui.threading.Thread") as thread:
                self.window.media_offline = offline
                self.window.is_downloading = lambda: downloading
                self.window._update_file_actions()
                self.assertTrue(self.window.delete_button.instate(["disabled"]))
                self.window.start_deletion()
                notice.assert_called_once()
                confirm.assert_not_called()
                thread.assert_not_called()
        self.window.media_offline = False
        self.window.is_downloading = lambda: False

    def test_delete_rejects_main_form_change_before_confirmation(self):
        self.prepare()
        self.select(100)
        with patch.object(self.window, "get_location", return_value=("other", self.account)), \
                patch("xlikesaver.history_gui.dialogs.showinfo") as notice, \
                patch("xlikesaver.history_gui.dialogs.confirm_delete") as confirm, \
                patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.start_deletion()
        notice.assert_called_once()
        confirm.assert_not_called()
        thread.assert_not_called()

    def test_delete_rechecks_generation_account_folder_and_busy_state_after_confirmation(self):
        self.prepare()
        self.select(100)
        old_account, old_storage = self.window.current_account, self.window.current_storage
        cases = ("generation", "account", "folder", "download", "busy", "closing", "offline", "main form")
        for case in cases:
            def confirm(*_args, **_kwargs):
                if case == "generation":
                    self.window.generation += 1
                elif case == "account":
                    self.window.current_account = "other"
                elif case == "folder":
                    self.window.current_storage = self.account / "elsewhere"
                elif case == "download":
                    self.window.is_downloading = lambda: True
                elif case == "busy":
                    self.window.busy = True
                elif case == "closing":
                    self.window.closing = True
                elif case == "offline":
                    self.window.media_offline = True
                else:
                    self.window.get_location = lambda: ("other", self.account)
                return True
            with self.subTest(case=case), patch("xlikesaver.history_gui.dialogs.confirm_delete", side_effect=confirm), \
                    patch("xlikesaver.history_gui.dialogs.showinfo"), \
                    patch("xlikesaver.history_gui.threading.Thread") as thread:
                self.window.start_deletion()
                thread.assert_not_called()
                self.assertFalse(self.window.delete_active)
            self.window.current_account, self.window.current_storage = old_account, old_storage
            self.window.get_location = lambda: ("offline", self.account)
            self.window.is_downloading = lambda: False
            self.window.busy = self.window.closing = self.window.media_offline = False

    def test_delete_captures_original_file_identity_before_confirmation(self):
        self.prepare(1)
        self.select(100)
        source = self.account / "100_1.jpg"
        expected = file_signature(source)
        def changed(*_args, **_kwargs):
            source.write_bytes(b"changed while the confirmation is open")
            return True
        with patch("xlikesaver.history_gui.dialogs.confirm_delete", side_effect=changed), \
                patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.start_deletion()
        [row] = thread.call_args.kwargs["args"][2]
        self.assertEqual(row["_delete_signature"], expected)
        self.assertNotEqual(file_signature(source), expected)

    def test_missing_file_that_reappears_during_confirmation_is_not_a_new_target(self):
        self.prepare(1)
        self.select(100)
        source = self.account / "100_1.jpg"
        source.unlink()  # Only this test's temporary fixture.
        def appeared(*_args, **_kwargs):
            source.write_bytes(b"replacement file")
            return True
        with patch("xlikesaver.history_gui.dialogs.confirm_delete", side_effect=appeared), \
                patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.start_deletion()
        [row] = thread.call_args.kwargs["args"][2]
        self.assertEqual(row["_delete_signature"], (0, 0, 0, 0))
        self.assertNotEqual(file_signature(source), row["_delete_signature"])

    def test_delete_progress_does_not_release_controls_and_done_keeps_next_selection(self):
        self.prepare(3)
        selected = self.select(100)
        removed_path = self.window.rows[selected[0]]["path"]
        self.window.busy = self.window.delete_active = True
        self.window.source_stop_button.configure(text="휴지통 보내기 중단", state="normal")
        self.window.results.put((self.window.generation - 1, "deleted", {"paths": [removed_path]}))
        self.window.results.put((self.window.generation, "delete-progress", {"message": "완료 1개"}))
        self.window._poll()
        self.assertTrue(self.window.busy and self.window.delete_active)
        self.assertEqual(self.window.status_text.get(), "완료 1개")
        self.window.results.put((self.window.generation, "deleted", {"paths": [removed_path], "message": "처리 완료"}))
        with patch.object(self.window, "refresh") as refresh:
            self.window._poll()
        self.assertFalse(self.window.busy or self.window.delete_active)
        self.assertEqual(self.window.completion_text, "처리 완료")
        self.assertEqual(self.window.deletion_page, 0)
        self.assertIsNotNone(self.window._delete_next_identity)
        self.assertEqual(self.window._delete_next_identity[1], "101")
        refresh.assert_called_once_with()

    def test_delete_close_waits_for_worker_completion_and_signals_stop(self):
        self.prepare()
        self.window.busy = self.window.delete_active = True
        with patch.object(self.window, "destroy") as destroy:
            self.window.close()
            self.assertTrue(self.window.closing and self.window.delete_stop.is_set())
            destroy.assert_not_called()
            self.window.results.put((self.window.generation, "deleted", {"paths": []}))
            self.window._poll()
            destroy.assert_called_once()
        self.window.closing = False

    def test_previous_deletion_policy_is_only_reported_applied_after_verified_recycle(self):
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
        self.assertEqual(self.window.tabs.tab(self.window.deletion_view, "text"), "삭제 기록 5")

    def journal(self, store, row, status="failed", *, remove=False, keep=True):
        """Represent an existing journal without touching the Windows Recycle Bin."""
        source = Path(row["path"])
        stat = source.stat()
        signature = tuple(getattr(stat, name) for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns"))
        ident, stamp = uuid.uuid4().hex, "2026-10-01T12:00:00+09:00"
        with store._db:
            store._db.execute(
                "INSERT INTO deletion_items(id,batch_id,tweet_id,num,media_type,folder_mode,path,filename,"
                "filesize,signature,posted_at,author,created_at,finished_at,status,message,keep_deleted) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ident, "previous_version", row["tweet_id"], row["num"], row["media_type"], row["folder_mode"],
                 row["path"], row["filename"], stat.st_size, json.dumps(signature), row.get("posted_at"),
                 row.get("author") or "", stamp, stamp, status, "offline previous result", int(keep)))
            if remove:
                source.unlink()  # Only this test's temporary fixture.
                store._db.execute(
                    "UPDATE media SET last_path=path,path='',filesize=NULL WHERE folder_mode=? "
                    "AND tweet_id=? AND num=? AND media_type=?",
                    (row["folder_mode"], row["tweet_id"], row["num"], row["media_type"]))
                store._db.execute(
                    "INSERT INTO missing_choices(tweet_id,num,media_type,ignored,updated_at) VALUES (?,?,?,?,?)",
                    (row["tweet_id"], row["num"], row["media_type"], int(keep), stamp))
        return ident

    def test_loader_shows_previous_recycle_source_and_policy_without_current_file(self):
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

    def test_previous_deletion_history_has_independent_paging_without_file_rescan(self):
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

    def test_selection_controls_and_previous_history_fit_small_light_and_dark_windows(self):
        self.prepare()
        self.show()
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
