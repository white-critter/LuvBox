"""File selection and read-only previous deletion records, using temporary media."""

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from xlikesaver import theme
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

    def test_selected_files_have_no_delete_action_and_delete_key_preserves_media(self):
        store = self.prepare()
        self.select(100, 101)
        before = {Path(row["path"]): (Path(row["path"]).read_bytes(), Path(row["path"]).stat().st_mtime_ns)
                  for row in store.records()}
        records = store.records()
        self.assertFalse(self.window.bind("<Delete>"))
        labels = {button.cget("text") for button in self.window.file_selection_actions.winfo_children()}
        self.assertFalse(any("삭제" in label for label in labels))
        commands = {label: callback for label, callback, _enabled in self.window._file_menu_commands()}
        self.assertEqual(set(commands), {"파일 열기", "트윗 열기", "이 작성자 파일 보기",
                                         "선택한 파일 경로 복사", "이 페이지 모두 선택", "선택 해제"})
        self.show()
        for view in ("table", "grid"):
            with self.subTest(view=view):
                self.window.set_view(view)
                self.root.update()
                target = self.window.tree if view == "table" else self.window.grid_view.canvas
                target.focus_force()
                with patch("xlikesaver.history_gui.dialogs.askyesno") as confirmation, \
                        patch("xlikesaver.history_gui.threading.Thread") as thread:
                    target.event_generate("<Delete>")
                    self.root.update()
                confirmation.assert_not_called()
                thread.assert_not_called()
                self.assertEqual(store.records(), records)
                self.assertEqual(store.deletion_page()["total"], 0)
                self.assertEqual(store.ignored_media(), set())
                for path, expected in before.items():
                    self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), expected)

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
        self.assertEqual(self.window.tabs.tab(self.window.deletion_view, "text"), "이전 삭제 기록 5")

    def journal(self, store, row, status="failed", *, remove=False, keep=True):
        """Represent an existing journal, without using the retired deletion worker."""
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
