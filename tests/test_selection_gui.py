"""File selection, deletion confirmation and journals, using temporary media."""

import json
import queue
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from luvbox import theme
from luvbox.history_gui import HistoryWindow
from luvbox.recycling import file_signature
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
        self.window.tabs.select(3)
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
                with patch("luvbox.history_gui.dialogs.confirm_delete", return_value=None) as confirmation, \
                        patch("luvbox.history_gui.threading.Thread") as thread:
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
        with patch("luvbox.history_gui.dialogs.confirm_delete", return_value=False) as confirm, \
                patch("luvbox.history_gui.threading.Thread") as thread:
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
                    patch("luvbox.history_gui.dialogs.showinfo") as notice, \
                    patch("luvbox.history_gui.dialogs.confirm_delete") as confirm, \
                    patch("luvbox.history_gui.threading.Thread") as thread:
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
                patch("luvbox.history_gui.dialogs.showinfo") as notice, \
                patch("luvbox.history_gui.dialogs.confirm_delete") as confirm, \
                patch("luvbox.history_gui.threading.Thread") as thread:
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
            with self.subTest(case=case), patch("luvbox.history_gui.dialogs.confirm_delete", side_effect=confirm), \
                    patch("luvbox.history_gui.dialogs.showinfo"), \
                    patch("luvbox.history_gui.threading.Thread") as thread:
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
        with patch("luvbox.history_gui.dialogs.confirm_delete", side_effect=changed), \
                patch("luvbox.history_gui.threading.Thread") as thread:
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
        with patch("luvbox.history_gui.dialogs.confirm_delete", side_effect=appeared), \
                patch("luvbox.history_gui.threading.Thread") as thread:
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
                # A recycle marks the file missing in the same commit.
                store._db.execute(
                    "UPDATE media SET last_path=path,path='',filesize=NULL,missing_since=? WHERE folder_mode=? "
                    "AND tweet_id=? AND num=? AND media_type=?",
                    (stamp, row["folder_mode"], row["tweet_id"], row["num"], row["media_type"]))
                store._db.execute(
                    "INSERT INTO missing_choices(tweet_id,num,media_type,ignored,updated_at) VALUES (?,?,?,?,?)",
                    (row["tweet_id"], row["num"], row["media_type"], int(keep), stamp))
        return ident

    def test_recycled_file_is_shown_with_missing_files_and_marked_as_recycled(self):
        store = self.prepare(3)
        rows = {row["tweet_id"]: row for row in store.records()}
        self.journal(store, rows["100"], "recycled", remove=True)
        # Removed in Explorer: no recycle journal of its own.
        Path(rows["101"]["path"]).unlink()  # Only this test's temporary fixture.
        self.load()
        items = {item["tweet_id"]: item for item in self.window.missing_items}
        self.assertEqual(set(items), {"100", "101"})
        self.assertEqual((items["100"]["removed"], items["100"]["ignored"]), ("recycled", True))
        self.assertEqual((items["101"]["removed"], items["101"]["ignored"]), ("", False))
        self.window.show_ignored.set(True)
        self.window._filter_missing()
        key = items["100"]["archive_key"]
        self.assertEqual(self.window.missing_tree.item(key, "values")[1], "사진 · 휴지통 · 숨김")
        self.window.missing_tree.selection_set(key)
        self.window.missing_tree.focus(key)
        self.window._missing_selected()
        self.assertIn("휴지통으로 보낸 때", self.window.missing_detail.get())
        self.assertIn("Windows 휴지통에서 복원", self.window.missing_detail.get())
        other = items["101"]["archive_key"]
        self.window.missing_tree.selection_set(other)
        self.window.missing_tree.focus(other)
        self.window._missing_selected()
        self.assertIn("사라진 것을 안 때", self.window.missing_detail.get())
        self.assertNotIn("휴지통", self.window.missing_detail.get())

    def test_file_not_saved_on_purpose_is_listed_with_hidden_missing_files(self):
        store = self.records(2)
        store.record_declined("flat", {"tweet_id": "300", "num": 1, "type": "photo", "author": {"name": "carol"}},
                              self.account / "300_1.jpg")
        self.load()
        self.assertEqual({row["tweet_id"] for row in self.window.rows.values()}, {"100", "101"})
        self.assertEqual(self.window.tile_values["files"].get(), "2")
        items = {item["tweet_id"]: item for item in self.window.missing_items}
        self.assertEqual((items["300"]["removed"], items["300"]["ignored"]), ("declined", True))
        self.assertEqual(self.window.tabs.tab(self.window.missing_view, "text"), "사라진 파일")
        self.window.show_ignored.set(True)
        self.window._filter_missing()
        key = items["300"]["archive_key"]
        self.assertEqual(self.window.missing_tree.item(key, "values")[1], "사진 · 저장 안 함 · 숨김")
        self.window.missing_tree.selection_set(key)
        self.window.missing_tree.focus(key)
        self.window._missing_selected()
        detail = self.window.missing_detail.get()
        self.assertIn("저장하지 않기로 한 때", detail)
        self.assertIn("저장하려던 위치", detail)
        self.assertIn("‘다시 받기’로 받을 수 있습니다", detail)
        self.window._update_missing_actions()
        self.assertEqual(str(self.window.unignore_button.cget("state")), "normal")

    def test_selection_controls_and_missing_files_fit_small_light_and_dark_windows(self):
        self.prepare()
        self.show()
        for mode in ("light", "dark"):
            theme.apply(self.root, mode)
            for geometry in ("760x640", "1200x780"):
                with self.subTest(mode=mode, geometry=geometry):
                    self.window.geometry(geometry)
                    for tab in (0, 3):
                        self.window.tabs.select(tab)
                        self.root.update()
                        self.window._layout_pagers()
                        self.root.update()
                        if tab == 0:
                            actions, bar = self.window.file_selection_actions, self.window.file_selection_bar
                            self.assertLessEqual(actions.winfo_x() + actions.winfo_width(), bar.winfo_width() + 2)
                            self.assertGreater(self.window.tree.winfo_height(), 50)
                        else:
                            self.assertGreater(self.window.missing_tree.winfo_height(), 50)
                            self.assertTrue(self.window.missing_tree.winfo_ismapped())


if __name__ == "__main__":
    unittest.main()
