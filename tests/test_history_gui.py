"""Local record window workflows without X requests or opening media apps."""

from pathlib import Path
from contextlib import closing
import json
import gc
import queue
import sys
import tempfile
import time
import tkinter as tk
import threading
import unittest
import weakref
from unittest.mock import patch
from types import SimpleNamespace

from PIL import Image

from xlikesaver.gui import App
from xlikesaver.history import HistoryStore
from xlikesaver.history_gui import HistoryWindow, NO_SELECTION, NO_RUN_SELECTION
from xlikesaver.preview import PreviewLoader, Thumbnail
from xlikesaver import theme
from xlikesaver.protection import AccountGuard, ProtectionError
from xlikesaver.storage import OutputGuard, resolve_storage
from tests.test_gui import destroy_tk, release_tk


class HistoryGuiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        protection = patch("xlikesaver.protection.state_directory", return_value=Path(self.temporary.name) / "protection")
        protection.start()
        self.addCleanup(protection.stop)
        self.account = Path(self.temporary.name) / "offline"
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(release_tk, self, "window", "root")
        self.addCleanup(destroy_tk, self.root)
        with patch("xlikesaver.gui.load_settings", return_value={}), patch("xlikesaver.gui.has_session", return_value=False):
            App(self.root)
        with patch("xlikesaver.history_gui.threading.Thread"):
            self.window = HistoryWindow(self.root, lambda: ("offline", self.account))
        self.window.withdraw()

    def load(self, page=0, query="", kind="all"):
        self.window.page = page
        self.window._load(self.window.generation, self.account, kind, query, page, True)
        self.window._poll()

    def records(self, count=1):
        store = HistoryStore(self.account)
        self.addCleanup(store.close)
        for index in range(count):
            path = self.account / f"{100 + index}_1.jpg"
            path.write_bytes(b"offline-image")
            store.record_media("flat", {"tweet_id": str(100 + index), "num": 1, "type": "photo", "author": {"name": "alice"}}, path)
        return store

    def test_window_loads_statistics_search_and_pagination_locally(self):
        self.records(105)
        with patch("requests.Session.request") as network:
            self.load()
            self.assertEqual(len(self.window.tree.get_children()), 100)
            self.assertIn("105", self.window.summary_text.get())
            self.assertEqual(str(self.window.next_button.cget("state")), "normal")
            self.load(page=1)
            self.assertEqual(len(self.window.tree.get_children()), 5)
            self.assertEqual(str(self.window.previous_button.cget("state")), "normal")
            self.load(query="104")
            self.assertEqual(len(self.window.tree.get_children()), 1)
        network.assert_not_called()

    def test_selected_record_displays_source_and_open_requires_user_action(self):
        self.records()
        with patch("xlikesaver.history_gui.webbrowser.open") as browser:
            self.load()
            browser.assert_not_called()
            self.window.tree.selection_set("0")
            self.window.select_record()
            self.assertIn("https://x.com/i/web/status/100", self.window.selection_text.get())
            self.window.open_tweet()
        browser.assert_called_once_with("https://x.com/i/web/status/100")

    def test_file_selection_follows_media_identity_and_clears_when_absent(self):
        store = self.records(2)
        rows = store.records()
        self.window._display(store.summary(), rows, [])
        self.window.tree.selection_set("0")
        self.window.select_record()
        selected = rows[0]
        # The same tweet can also exist in a different folder mode. It must
        # not take over the selection when new records move the rows around.
        other_mode = dict(selected, folder_mode="author")
        self.window._display(store.summary(), [other_mode, rows[1], selected], [])
        self.assertEqual(self.window.tree.selection(), ("2",))
        self.assertEqual(self.window.selected(), selected)
        with patch("xlikesaver.history_gui.webbrowser.open") as browser:
            self.window.open_tweet()
        browser.assert_called_once_with("https://x.com/i/web/status/" + selected["tweet_id"])
        self.window._display(store.summary(), [other_mode, rows[1]], [])
        self.assertIsNone(self.window.selected())
        self.assertEqual(self.window.selection_text.get(), NO_SELECTION)

    def test_run_selection_follows_run_id_and_clears_old_details(self):
        first = {"id": 1, "status": "running", "message": "still running"}
        second = {"id": 2, "status": "finished", "message": "new success"}
        self.window._display({}, [], [first])
        self.window.run_tree.selection_set("0")
        self.window.select_run()
        finished = dict(first, status="finished", message="selected run finished")
        self.window._display({}, [], [second, finished])
        self.assertEqual(self.window.run_tree.selection(), ("1",))
        self.assertIn("selected run finished", self.window.run_text.get())
        self.window._display({}, [], [second])
        self.assertEqual(self.window.run_tree.selection(), ())
        self.assertEqual(self.window.run_text.get(), NO_RUN_SELECTION)

    def test_location_change_clears_previous_catalog_selection_and_details(self):
        self.records()
        self.load()
        self.window.tree.selection_set("0")
        self.window.select_record()
        next_account = Path(self.temporary.name) / "next-account"
        with closing(HistoryStore(next_account)) as store:
            path = next_account / "100_1.jpg"
            path.write_bytes(b"different-image")
            store.record_media("flat", {"tweet_id": "100", "num": 1, "type": "photo"}, path)
        self.window.get_location = lambda: ("next_account", next_account)
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.refresh()
        self.assertEqual(self.window.tree.get_children(), ())
        self.assertEqual(self.window.selection_text.get(), NO_SELECTION)
        self.window._load(*thread.call_args.kwargs["args"])
        self.window._poll()
        self.assertEqual(len(self.window.rows), 1)
        self.assertIsNone(self.window.selected())

    def test_invalid_location_clears_catalog_and_disables_previous_folder_actions(self):
        self.records()
        self.load()
        self.window.tree.selection_set("0")
        self.window.select_record()
        self.window.get_location = lambda: (_ for _ in ()).throw(ValueError("invalid account"))
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.refresh()
        thread.assert_not_called()
        self.assertEqual(self.window.tree.get_children(), ())
        self.assertEqual(self.window.run_tree.get_children(), ())
        self.assertEqual(self.window.selection_text.get(), NO_SELECTION)
        self.assertIsNone(self.window.current_storage)
        self.assertIsNone(self.window.current_account_dir)
        self.assertEqual(str(self.window.export_button.cget("state")), "disabled")
        self.assertEqual(str(self.window.backfill_button.cget("state")), "disabled")
        with patch("xlikesaver.history_gui.webbrowser.open") as browser:
            self.window.open_tweet()
        browser.assert_not_called()

    def test_catalog_preview_remains_openable_while_csv_and_backfill_are_disabled(self):
        store = self.records()
        self.load()
        with patch("xlikesaver.history_gui.threading.Thread"):
            self.window.refresh(import_legacy=True)
        self.window.results.put((self.window.generation, "preview",
                                 (self.account, store.summary(), store.records(), [], 0)))
        self.window._poll()
        self.assertTrue(self.window.busy)
        self.assertEqual(str(self.window.export_button.cget("state")), "disabled")
        self.assertEqual(str(self.window.backfill_button.cget("state")), "disabled")
        self.window.tree.selection_set("0")
        with patch("xlikesaver.history_gui.os.startfile", create=True) as opened:
            self.window.open_file()
        opened.assert_called_once_with(store.records()[0]["path"])
        self.window.results.put((self.window.generation, "loaded",
                                 (self.account, store.summary(), store.records(), [], 0)))
        self.window._poll()
        self.assertEqual(str(self.window.export_button.cget("state")), "normal")
        self.assertEqual(str(self.window.backfill_button.cget("state")), "normal")

    def test_pagination_ignores_repeated_clicks_until_requested_page_loads(self):
        self.records(105)
        self.load()
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.change_page(1)
            arguments = thread.call_args.kwargs["args"]
            self.window.change_page(1)
            self.window.change_page(-1)
        self.assertEqual(thread.call_count, 1)
        self.assertEqual(self.window.page, 1)
        self.assertEqual(str(self.window.previous_button.cget("state")), "disabled")
        self.assertEqual(str(self.window.next_button.cget("state")), "disabled")
        self.window._load(*arguments)
        self.window._poll()
        self.assertEqual(self.window.page_text.get(), "총 105개 · 2 / 2페이지")
        self.assertEqual(str(self.window.previous_button.cget("state")), "normal")
        self.assertEqual(str(self.window.next_button.cget("state")), "disabled")

    def test_pending_search_does_not_relabel_previous_page_results(self):
        self.records(105)
        self.load()
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.change_page(1)
            arguments = thread.call_args.kwargs["args"]
            self.window.query.set("104")
            self.window.search()
            self.window._load(*arguments)
            self.window._poll()
            pending_arguments = thread.call_args.kwargs["args"]
        # The older page response must not replace the visible snapshot once
        # a newer search is pending, even briefly while its worker starts.
        self.assertEqual(self.window.page_text.get(), "총 105개 · 1 / 2페이지")
        self.assertEqual(self.window.page, 0)
        self.assertEqual(self.window.displayed_page, 0)
        self.assertEqual(pending_arguments[4], 0)
        self.assertEqual(str(self.window.previous_button.cget("state")), "disabled")
        self.window._load(*pending_arguments)
        self.window._poll()
        self.assertEqual(self.window.page_text.get(), "총 1개 · 1 / 1페이지")
        self.assertEqual(len(self.window.rows), 1)

    def test_page_size_last_first_jump_and_empty_filtered_count(self):
        self.records(105)
        self.load()
        self.window.page_size.set("50")
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.change_page_size()
        self.window._load(*thread.call_args.kwargs["args"])
        self.window._poll()
        self.assertEqual(len(self.window.rows), 50)
        self.assertEqual(self.window.page_text.get(), "총 105개 · 1 / 3페이지")
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.last_button.invoke()
        self.window._load(*thread.call_args.kwargs["args"])
        self.window._poll()
        self.assertEqual((self.window.page, len(self.window.rows)), (2, 5))
        self.assertEqual(str(self.window.last_button.cget("state")), "disabled")
        self.window.page_input.set("999")
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.jump_page()
        thread.assert_not_called()
        self.assertEqual(self.window.page_input.get(), "3")
        self.window.page_input.set("2")
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.jump_page()
        self.window._load(*thread.call_args.kwargs["args"])
        self.window._poll()
        self.assertEqual((self.window.page, len(self.window.rows)), (1, 50))
        self.window.page_input.set("invalid")
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.jump_page()
        thread.assert_not_called()
        self.assertEqual(self.window.page_input.get(), "2")
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.first_button.invoke()
        self.window._load(*thread.call_args.kwargs["args"])
        self.window._poll()
        self.assertEqual(self.window.page, 0)
        self.window.query.set("absent")
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.search()
        self.window._load(*thread.call_args.kwargs["args"])
        self.window._poll()
        self.assertEqual(self.window.page_text.get(), "총 0개 · 0 / 0페이지")
        for button in (self.window.first_button, self.window.previous_button, self.window.next_button,
                       self.window.last_button, self.window.jump_button):
            self.assertEqual(str(button.cget("state")), "disabled")

    def test_execution_history_pagination_preserves_selected_run_and_resets_with_size(self):
        store = self.records()
        ids = [store.start_run("flat") for _ in range(105)]
        self.load()
        self.assertEqual(len(self.window.run_rows), 100)
        self.assertEqual(self.window.run_page_text.get(), "총 105회 · 1 / 2페이지")
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.run_last_button.invoke()
        self.window._load(*thread.call_args.kwargs["args"])
        self.window._poll()
        self.assertEqual([row["id"] for row in self.window.run_rows.values()], ids[4::-1])
        self.window.run_tree.selection_set("0")
        self.window.select_run()
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.refresh()
        self.window._load(*thread.call_args.kwargs["args"])
        self.window._poll()
        self.assertEqual(self.window.run_rows[self.window.run_tree.selection()[0]]["id"], ids[4])
        self.window.page_size.set("200")
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.change_page_size()
        self.window._load(*thread.call_args.kwargs["args"])
        self.window._poll()
        self.assertEqual(self.window.run_page, 0)
        self.assertEqual(len(self.window.run_rows), 105)
        self.assertEqual(self.window.run_page_text.get(), "총 105회 · 1 / 1페이지")

    def test_page_size_changed_during_load_discards_older_preview_and_result(self):
        self.records(105)
        self.load()
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.change_page(1)
            old_args = thread.call_args.kwargs["args"]
            self.window.page_size.set("50")
            self.window.change_page_size()
            self.window._load(*old_args)
            self.window._poll()
            new_args = thread.call_args.kwargs["args"]
        self.assertEqual(len(self.window.rows), 100)
        self.assertEqual(self.window.displayed_page_size, 100)
        self.assertEqual(new_args[4], 0)
        self.assertEqual(new_args[6], 50)
        self.window._load(*new_args)
        self.window._poll()
        self.assertEqual((len(self.window.rows), self.window.displayed_page_size), (50, 50))
        # An old generation arriving after the new snapshot is harmless too.
        before = dict(self.window.rows)
        self.window.results.put((self.window.generation - 1, "loaded", (self.account, {}, [], [], 1)))
        self.window._poll()
        self.assertEqual(self.window.rows, before)

    def test_numbered_pages_highlight_current_and_keyboard_respects_editing(self):
        self.records(550)
        self.load(page=3)
        buttons = self.window.page_buttons
        self.assertEqual([button.cget("text") for button in buttons], ["2", "3", "4", "5", "6"])
        self.assertEqual(buttons[2].cget("style"), "CompactPrimary.TButton")
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            buttons[3].invoke()
        self.window._load(*thread.call_args.kwargs["args"])
        self.window._poll()
        self.assertEqual(self.window.displayed_page, 4)
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            result = self.window._page_key(SimpleNamespace(widget=self.window.page_entry, keysym="Home"))
        thread.assert_not_called()
        self.assertIsNone(result)
        # Without Ctrl the table keeps its own row scrolling.
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.assertIsNone(self.window._page_key(SimpleNamespace(widget=self.window.tree, keysym="Home", state=0)))
        thread.assert_not_called()
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.assertEqual(self.window._page_key(SimpleNamespace(widget=self.window.tree, keysym="Home", state=0x4)),
                             "break")
        self.window._load(*thread.call_args.kwargs["args"])
        self.window._poll()
        self.assertEqual(self.window.displayed_page, 0)
        self.window.tabs.select(1)
        with patch.object(self.window, "go_page") as go:
            self.window._page_key(SimpleNamespace(widget=self.window.run_tree, keysym="End", state=0x4))
        go.assert_called_once_with(None, runs=True)
        # A page button needs no modifier.
        with patch.object(self.window, "change_page") as change:
            self.window._page_key(SimpleNamespace(widget=self.window.tabs, keysym="Next", state=0))
        change.assert_called_once_with(1, runs=True)

    def test_preview_updates_only_current_selection_and_clear_releases_image(self):
        self.records(2)
        self.load()
        with patch.object(self.window._preview_loader, "request") as request:
            self.window.tree.selection_set("0")
            self.window.select_record()
            old_token = self.window._preview_token
            first_path = self.window.selected()["path"]
            self.assertEqual(request.call_args.args[1], first_path)
            self.assertIn("불러오는 중", self.window.preview_note.get())
            self.window.tree.selection_set("1")
            self.window.select_record()
            token = self.window._preview_token
        self.assertGreater(token, old_token)
        self.window._preview_results.put((old_token, Thumbnail(Image.new("RGB", (12, 12)), "stale")))
        self.window._preview_results.put((token, Thumbnail(Image.new("RGB", (22, 15)), "사진 미리보기")))
        self.window._poll_previews()
        self.assertEqual(self.window.preview_note.get(), "사진 미리보기")
        self.assertEqual((self.window._preview_image.width(), self.window._preview_image.height()), (22, 15))
        self.assertTrue(self.window.preview_label.cget("image"))
        self.window.tree.selection_remove(*self.window.tree.selection())
        self.window.select_record()
        self.assertIsNone(self.window._preview_image)
        self.assertFalse(self.window.preview_label.cget("image"))
        self.assertIn("고르면", self.window.preview_note.get())

    def test_click_loads_real_local_photo_in_background_without_opening_apps_or_network(self):
        self.records()
        path = self.account / "100_1.jpg"
        Image.new("RGB", (800, 600), "orange").save(path)
        self.load()
        with patch("requests.Session.request") as network, \
                patch("xlikesaver.history_gui.os.startfile", create=True) as opened, \
                patch("xlikesaver.history_gui.webbrowser.open") as browser:
            self.window.tree.selection_set("0")
            self.window.select_record()
            deadline = time.monotonic() + 3
            while self.window._preview_image is None and time.monotonic() < deadline:
                self.window._poll_previews()
                time.sleep(0.01)
            self.assertIsNotNone(self.window._preview_image, self.window.preview_note.get())
            self.assertEqual((self.window._preview_image.width(), self.window._preview_image.height()), (220, 165))
            self.assertEqual(self.window.preview_note.get(), "사진 미리보기")
        network.assert_not_called()
        opened.assert_not_called()
        browser.assert_not_called()

    def test_preview_permission_uses_recorded_locations_and_excludes_unknown_paths(self):
        paths, old_root, media = self.relocated_record()
        self.window.busy = False
        self.window.current_storage = paths
        self.window.current_account_dir = paths.record_dir
        with closing(HistoryStore.for_storage(paths)) as store:
            rows = store.records()
            self.window._display(store.summary(), rows, [])
        with patch.object(self.window._preview_loader, "request") as request:
            self.window.tree.selection_set("0")
            self.window.select_record()
        permitted = request.call_args.args[2]
        self.assertTrue(permitted(media))
        unknown = old_root / "unrecorded.jpg"
        unknown.write_bytes(b"unrelated")
        self.assertFalse(permitted(unknown))

    def test_preview_reflows_below_table_and_paging_buttons_fit_large_fonts(self):
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        self.window.attributes("-alpha", 0)
        self.window.deiconify()
        paging = {"page": 3, "total": 100000, "pages": 1000, "page_size": 100}
        self.window._display({"files": 100000}, [], [], 3, paging, paging)
        self.window.geometry("860x720")
        self.root.update()
        self.assertFalse(self.window._preview_side)
        self.assertEqual(int(self.window.preview_card.grid_info()["row"]), 1)
        self.assertGreaterEqual(self.window.tree.winfo_height(), self.window.tree.winfo_reqheight())
        self.assertLessEqual(self.window.preview_box.winfo_rootx() + self.window.preview_box.winfo_width(),
                             self.window.file_browser.winfo_rootx() + self.window.file_browser.winfo_width())
        self.window.geometry("1400x900")
        self.root.update()
        self.assertTrue(self.window._preview_side)
        self.assertEqual(int(self.window.preview_card.grid_info()["column"]), 1)
        for bar, info, navigation in self.window._pagers:
            if not bar.winfo_ismapped():
                continue
            for group in (navigation._arrows, navigation._jump_box):
                for child in group.winfo_children():
                    if child.winfo_ismapped():
                        self.assertLessEqual(child.winfo_rootx() + child.winfo_width(),
                                             bar.winfo_rootx() + bar.winfo_width())

    def test_narrow_table_hides_save_time_column_instead_of_cutting_file_names(self):
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        self.window.attributes("-alpha", 0)
        self.window.deiconify()
        store = self.records(1)
        self.load()
        need = self.window._column_need("filename")
        for size, hidden in (("1600x900", ()), ("1120x860", ("saved",)), ("860x720", ("saved",))):
            with self.subTest(size=size):
                self.window.geometry(size)
                self.root.update()
                shown = self.window._shown_columns
                self.assertEqual(tuple(key for key in self.window._file_columns if key not in shown), hidden)
                self.assertGreaterEqual(int(self.window.tree.column("filename", "width")), need - 24)
        # The hidden save time is still shown for the selected file.
        self.window.tree.selection_set(self.window.tree.get_children()[0])
        self.window.select_record()
        self.assertIn("저장한 때 " + store.records()[0]["saved_at"][:10], self.window.selection_text.get())

    def test_refresh_clamps_page_after_catalog_is_reduced(self):
        store = self.records(105)
        self.load(page=1)
        store._db.execute("DELETE FROM media WHERE tweet_id!='100'")
        store._db.commit()
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.refresh()
        self.window._load(*thread.call_args.kwargs["args"])
        self.window._poll()
        self.assertEqual((self.window.page, self.window.displayed_page), (0, 0))
        self.assertEqual([row["tweet_id"] for row in self.window.rows.values()], ["100"])
        self.assertEqual(self.window.page_text.get(), "총 1개 · 1 / 1페이지")

    def test_large_totals_and_large_fonts_keep_both_pagers_and_tables_usable(self):
        # Scaling must be set before the interpreter creates its fonts; changing
        # scaling after a theme is built can leave its cached metrics unchanged.
        for scaling in (4 / 3, 2.0):
            with self.subTest(scaling=scaling):
                root = tk.Tk()
                root.attributes("-alpha", 0)
                previous_scaling = root.tk.call("tk", "scaling")
                window = None
                try:
                    root.tk.call("tk", "scaling", scaling)
                    with patch("xlikesaver.history_gui.threading.Thread"):
                        window = HistoryWindow(root, lambda: ("offline", self.account))
                    window.attributes("-alpha", 0)
                    window.geometry("860x720")
                    for total in (5750, 100000):
                        paging = {"page": 0, "total": total, "pages": (total + 99) // 100, "page_size": 100}
                        window._display({"files": total, "posts": total,
                                         "oldest_posted_at": "2022-01-01T00:00:00+09:00",
                                         "newest_posted_at": "2026-10-05T00:00:00+09:00"}, [], [], 0, paging, paging)
                        for tab in (0, 1):
                            window.tabs.select(tab)
                            root.update()
                            bar, info, navigation = window._pagers[tab]
                            self.assertTrue(bar.winfo_ismapped())
                            for frame in (info, navigation):
                                self.assertGreaterEqual(frame.winfo_width(), frame.winfo_reqwidth())
                                self.assertLessEqual(frame.winfo_x() + frame.winfo_width(), bar.winfo_width())
                                for child in frame.winfo_children():
                                    self.assertGreaterEqual(child.winfo_width(), child.winfo_reqwidth())
                                    self.assertLessEqual(child.winfo_rootx() + child.winfo_width(),
                                                         bar.winfo_rootx() + bar.winfo_width())
                            tree = window.run_tree if tab else window.tree
                            self.assertGreaterEqual(tree.winfo_height(), 90)
                            canvas = window.content_view.canvas
                            expected_row = int(info.winfo_reqwidth() + navigation._arrows.winfo_reqwidth() +
                                               navigation._jump_box.winfo_reqwidth() + 24 > bar.winfo_width())
                            self.assertEqual(int(navigation.grid_info()["row"]), expected_row)
                            if scaling == 2.0:
                                self.assertTrue(window.content_view.scrollbar.winfo_manager())
                                canvas.yview_moveto(0)
                                button = window.run_next_button if tab else window.next_button
                                button.event_generate("<MouseWheel>", delta=-120)
                                root.update()
                                self.assertGreater(canvas.yview()[0], 0)
                                canvas.yview_moveto(0)
                                tree.event_generate("<MouseWheel>", delta=-120)
                                root.update()
                                self.assertEqual(canvas.yview()[0], 0)
                                canvas.yview_moveto(1)
                                root.update()
                                bottom = bar.winfo_rooty() - canvas.winfo_rooty() + bar.winfo_height()
                                self.assertLessEqual(bottom, canvas.winfo_height())
                            # A second geometry pass must not undo folding or
                            # continually resize the content and reset scrolling.
                            before = (int(navigation.grid_info()["row"]), bar.winfo_width(), canvas.yview())
                            root.update()
                            after = (int(navigation.grid_info()["row"]), bar.winfo_width(), canvas.yview())
                            self.assertEqual(before, after)
                finally:
                    root.tk.call("tk", "scaling", previous_scaling)
                    if window is not None:
                        window.destroy()
                    destroy_tk(root)

    def test_large_statistics_tiles_reflow_without_clipping_and_restore_six_columns(self):
        for scaling in (4 / 3, 2.0):
            with self.subTest(scaling=scaling):
                root = tk.Tk()
                root.attributes("-alpha", 0)
                previous_scaling = root.tk.call("tk", "scaling")
                window = None
                try:
                    root.tk.call("tk", "scaling", scaling)
                    with patch("xlikesaver.history_gui.threading.Thread"):
                        window = HistoryWindow(root, lambda: ("offline", self.account))
                    window.attributes("-alpha", 0)
                    window.geometry("860x720")
                    paging = {"page": 0, "total": 100000, "pages": 1000, "page_size": 100}
                    window._display({"files": 100000, "posts": 90317, "photo": 97000,
                                     "video": 2400, "animated_gif": 600, "bytes": 1_000_000_000_000,
                                     "oldest_posted_at": "2022-01-01T00:00:00+09:00",
                                     "newest_posted_at": "2026-10-05T00:00:00+09:00"}, [], [], 0, paging, paging)
                    root.update()
                    self.assertEqual(window._tile_columns, 3)
                    for key, tile in zip(window.tile_values, window._tiles):
                        self.assertGreaterEqual(tile.winfo_width(), tile.winfo_reqwidth())
                        self.assertEqual(str(tile.winfo_children()[0].cget("text")), window.tile_values[key].get())
                        for label in tile.winfo_children():
                            self.assertGreaterEqual(label.winfo_width(), label.winfo_reqwidth())
                            self.assertLessEqual(label.winfo_rootx() + label.winfo_width(),
                                                 tile.winfo_rootx() + tile.winfo_width())
                    for column in range(3, 6):
                        self.assertEqual(window._tile_frame.grid_columnconfigure(column)["weight"], 0)
                        self.assertFalse(window._tile_frame.grid_columnconfigure(column)["uniform"])
                    for tab in (0, 1):
                        window.tabs.select(tab)
                        root.update()
                        tree = window.run_tree if tab else window.tree
                        self.assertGreaterEqual(tree.winfo_height(), tree.winfo_reqheight())
                        canvas = window.content_view.canvas
                        canvas.yview_moveto(1)
                        root.update()
                        pager = window._pagers[tab][0]
                        bottom = pager.winfo_rooty() - canvas.winfo_rooty() + pager.winfo_height()
                        self.assertLessEqual(bottom, canvas.winfo_height())
                    # Wide windows keep the original single statistics row,
                    # including a large font and all of the large numbers.
                    window.geometry("1400x900")
                    root.update()
                    self.assertEqual(window._tile_columns, 6)
                    self.assertEqual({int(tile.grid_info()["row"]) for tile in window._tiles}, {0})
                    for tile in window._tiles:
                        for label in tile.winfo_children():
                            self.assertGreaterEqual(label.winfo_width(), label.winfo_reqwidth())
                    # Repeated layout passes with unchanged columns must not
                    # re-grid cards or reset the user's scroll position.
                    with patch.object(window._tiles[0], "grid_configure") as grid:
                        window._layout_tiles()
                    grid.assert_not_called()
                finally:
                    root.tk.call("tk", "scaling", previous_scaling)
                    if window is not None:
                        window.destroy()
                    destroy_tk(root)

    def test_empty_catalog_preview_stays_loading_until_folder_scan_finishes(self):
        self.account.mkdir()
        (self.account / "100_1.jpg").write_bytes(b"offline-image")
        original_import = HistoryStore.import_legacy

        def importing(store, **kwargs):
            self.window._poll()
            self.assertTrue(self.window.busy)
            self.assertIn("확인하는 중", self.window.table_note.get())
            self.assertNotIn("아직 저장한 파일이 없습니다", self.window.table_note.get())
            return original_import(store, **kwargs)

        with patch.object(HistoryStore, "import_legacy", importing):
            self.window._load(self.window.generation, self.account, "all", "", 0, True)
        self.window._poll()
        self.assertEqual(len(self.window.rows), 1)
        self.assertFalse(self.window.table_message.winfo_manager())

    def test_author_handle_with_at_prefix_finds_catalog_records(self):
        self.records()
        self.load()
        self.window.query.set("  @alice  ")
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.search()
        arguments = thread.call_args.kwargs["args"]
        self.assertEqual(arguments[3], "alice")
        self.window._load(*arguments)
        self.window._poll()
        self.assertEqual([row["author"] for row in self.window.rows.values()], ["alice"])
        self.window.query.set("@alice_1.jpg")
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.search()
        self.assertEqual(thread.call_args.kwargs["args"][3], "@alice_1.jpg")

    def test_backfill_is_disabled_while_downloading(self):
        self.window.busy = False
        self.window.is_downloading = lambda: True
        with patch("xlikesaver.history_gui.dialogs.showinfo") as notice, patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.backfill()
        notice.assert_called_once()
        thread.assert_not_called()

    def test_gif_conversion_button_counts_mp4_gifs_and_starts_after_confirmation(self):
        self.load()
        self.assertEqual(str(self.window.convert_button.cget("state")), "disabled")
        store = HistoryStore(self.account)
        self.addCleanup(store.close)
        path = self.account / "100_gif_1.mp4"
        path.write_bytes(b"offline animation")
        store.record_media("flat", {"tweet_id": "100", "num": 1, "type": "animated_gif", "author": {"name": "alice"}}, path)
        self.load()
        self.assertEqual(str(self.window.convert_button.cget("state")), "normal")
        self.assertIn("1개", self.window.convert_button.cget("text"))
        self.window.get_gif_options = lambda: ("720", 50)
        with patch("xlikesaver.history_gui.dialogs.askyesno", return_value=False) as ask, \
                patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.convert_gifs()
        thread.assert_not_called()
        self.assertIn("1개", ask.call_args.args[1])
        self.assertIn("최대 720px", ask.call_args.args[1])
        self.assertIn("휴지통", ask.call_args.args[1])
        self.window.is_downloading = lambda: True
        with patch("xlikesaver.history_gui.dialogs.showinfo") as notice, \
                patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.convert_gifs()
        notice.assert_called_once()
        thread.assert_not_called()
        self.window.is_downloading = lambda: False
        with patch("xlikesaver.history_gui.dialogs.askyesno", return_value=True), \
                patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.convert_gifs()
        self.assertEqual(thread.call_args.kwargs["args"][2:], ("720", 50))
        self.assertTrue(self.window.metadata_active)
        self.assertEqual(self.window.source_stop_button.cget("text"), "GIF 변환 중단")
        self.assertEqual(str(self.window.convert_button.cget("state")), "disabled")
        self.window.results.put((self.window.generation, "converted", "GIF 변환 끝 · 바꿈 1개"))
        with patch("xlikesaver.history_gui.threading.Thread") as refresh:
            self.window._poll()
        refresh.assert_called_once()
        self.assertFalse(self.window.metadata_active)
        self.assertIn("GIF 변환 끝", self.window.completion_text)

    def test_refresh_skips_whole_folder_rescan_while_downloading(self):
        # The worker records every file itself; a rescan would only compete
        # with it for the catalog's write lock.
        for downloading, expected in ((True, False), (False, True)):
            with self.subTest(downloading=downloading):
                self.window.busy = False
                self.window.is_downloading = lambda: downloading
                with patch("xlikesaver.history_gui.threading.Thread") as thread:
                    self.window.refresh(import_legacy=True)
                self.assertIs(thread.call_args.kwargs["args"][5], expected)
                if downloading:
                    self.assertIn("재확인을 건너뛰고", self.window.status_text.get())

    def test_closing_history_cancels_poll_without_closing_main_window(self):
        scheduled = self.window._poll_timer
        self.window._queue_pager_layout()
        layout = self.window._pager_layout_timer
        self.assertIn(scheduled, self.root.tk.call("after", "info"))
        self.assertIn(layout, self.root.tk.call("after", "info"))
        selection_text = self.window.selection_text
        self.window.close()
        self.assertNotIn(scheduled, self.root.tk.call("after", "info"))
        self.assertNotIn(layout, self.root.tk.call("after", "info"))
        self.assertIsNone(self.window._pager_layout_timer)
        self.assertEqual(self.root.winfo_exists(), 1)
        self.window._poll()  # A late callback is harmless after closing.
        selection_text.set("late selection text")

    def test_closed_history_releases_ui_variables_and_widget_cycles_on_main_thread(self):
        with patch("xlikesaver.history_gui.threading.Thread") as thread:
            window = HistoryWindow(self.root, lambda: ("offline", self.account))
        worker = thread.call_args.kwargs["target"]
        self.assertIs(worker.func, HistoryWindow._load_worker)
        self.assertEqual(worker.args, (window.results,))
        variables = [weakref.ref(value) for value in window.__dict__.values() if isinstance(value, tk.Variable)]
        variable_refs = variables + [weakref.ref(value) for value in window.tile_values.values()]
        window_ref = weakref.ref(window)
        enabled = gc.isenabled()
        gc.disable()
        try:
            window.destroy()
            self.assertTrue(all(ref() is None for ref in variable_refs))
            del window
            # No cyclic GC is required: worker target owns only a queue,
            # and stored widget parents no longer retain the closed window.
            self.assertIsNone(window_ref())
        finally:
            if enabled:
                gc.enable()

    def test_closing_during_backfill_waits_for_result_then_cancels_poll(self):
        self.window.metadata_active = True
        scheduled = self.window._poll_timer
        self.window.close()
        self.assertTrue(self.window.metadata_stop.is_set())
        self.assertEqual(self.window.winfo_exists(), 1)
        self.assertIn(scheduled, self.root.tk.call("after", "info"))
        self.window.results.put((self.window.generation, "backfilled", "출처 기록 중단"))
        self.window._poll()
        self.assertEqual(self.window.winfo_exists(), 0)
        self.assertIsNone(self.window._poll_timer)
        self.assertEqual(self.root.winfo_exists(), 1)

    def test_shared_media_folder_still_shows_only_selected_account_records(self):
        output = Path(self.temporary.name) / "shared"
        first = resolve_storage(output, "first")
        second = resolve_storage(output, "second")
        for paths, tweet_id in ((first, "100"), (second, "200")):
            with closing(HistoryStore.for_storage(paths)) as store:
                media = paths.media_dir / f"{tweet_id}_1.jpg"
                media.write_bytes(b"offline-image")
                store.record_media("flat", {"tweet_id": tweet_id, "num": 1, "type": "photo"}, media)
        self.window.busy = False
        self.window.get_location = lambda: ("first", first)
        with patch("xlikesaver.history_gui.threading.Thread"):
            self.window.refresh()
        with patch("requests.Session.request") as network:
            self.window._load(self.window.generation, first, "all", "", 0, True)
            self.window._poll()
        network.assert_not_called()
        self.assertEqual(self.window.current_account_dir, first.record_dir)
        self.assertEqual(self.window.current_storage, first)
        self.assertEqual([row["tweet_id"] for row in self.window.rows.values()], ["100"])
        self.assertIn(str(first.media_dir), self.window.location_text.get())

    def test_backfill_respects_other_process_using_same_output_folder(self):
        paths = resolve_storage(Path(self.temporary.name) / "shared", "offline")
        with patch("xlikesaver.history_gui.OutputGuard") as guard, patch("xlikesaver.metadata.embed_source") as embed:
            guard.return_value.acquire.side_effect = ProtectionError("다른 저장 작업이 실행 중입니다.")
            self.window._backfill(self.window.generation, paths)
        embed.assert_not_called()
        guard.return_value.close.assert_called_once()
        generation, kind, message = self.window.results.get_nowait()
        self.assertEqual(kind, "error")
        self.assertIn("다른 저장 작업", message)

    def relocated_record(self):
        paths = resolve_storage(Path(self.temporary.name) / "new", "offline")
        old_root = Path(self.temporary.name) / "previous"
        old_root.mkdir()
        media = old_root / "100_1.jpg"
        media.write_bytes(b"offline-image")
        with closing(HistoryStore.for_storage(paths)) as store:
            current = paths.media_dir / media.name
            current.write_bytes(b"offline-image")
            store.record_media("flat", {"tweet_id": "100", "num": 1, "type": "photo"}, current)
            store._db.execute("UPDATE media SET path=?", (str(media),))
            store._db.execute("INSERT INTO catalog_meta VALUES ('allowed_media_roots',?)",
                              (json.dumps([str(old_root)]),))
            store._db.commit()
        current.unlink()
        return paths, old_root, media

    def test_backfill_locks_previous_media_root_before_changing_any_file(self):
        paths, old_root, media = self.relocated_record()
        guard = OutputGuard(old_root)
        guard.acquire()
        try:
            with patch("xlikesaver.metadata.embed_source") as embed:
                self.window._backfill(self.window.generation, paths)
            embed.assert_not_called()
            self.assertEqual(media.read_bytes(), b"offline-image")
            self.assertEqual(self.window.results.get_nowait()[1], "error")
        finally:
            guard.close()
        # A failed root lock must release both the other roots and account lock.
        account = AccountGuard("offline", sleep=lambda *_: None)
        try:
            account.acquire(respect_cooldown=False)
        finally:
            account.close()
        current = OutputGuard(paths.output_dir)
        try:
            current.acquire()
        finally:
            current.close()

    def test_backfill_preserves_previous_path_and_can_run_during_request_cooldown(self):
        paths, _old_root, media = self.relocated_record()
        account = AccountGuard("offline", sleep=lambda *_: None)
        account.root.mkdir(parents=True, exist_ok=True)
        cooldown = json.dumps({"until": time.time() + 3600})
        account.state_path.write_text(cooldown, encoding="utf-8")
        with patch("xlikesaver.metadata.embed_source", return_value={"status": "embedded", "author_status": "embedded"}) as embed, patch("requests.Session.request") as network:
            self.window._backfill(self.window.generation, paths)
        embed.assert_called_once_with(media, "100", author="", author_id="")
        network.assert_not_called()
        self.assertEqual(account.state_path.read_text(encoding="utf-8"), cooldown)
        with closing(HistoryStore.for_storage(paths)) as store:
            row = store.records()[0]
            self.assertEqual(row["path"], str(media))
            self.assertEqual(row["metadata_status"], "embedded")

    def test_backfill_cannot_start_while_same_account_records_are_relocating(self):
        paths, _old_root, _media = self.relocated_record()
        account = AccountGuard("offline", sleep=lambda *_: None)
        account.acquire(respect_cooldown=False)
        try:
            with patch("xlikesaver.metadata.embed_source") as embed:
                self.window._backfill(self.window.generation, paths)
            embed.assert_not_called()
            self.assertEqual(self.window.results.get_nowait()[1], "error")
        finally:
            account.close()

    def test_open_file_accepts_recorded_previous_path_and_rejects_unknown_old_file(self):
        paths, old_root, media = self.relocated_record()
        self.window.current_storage = paths
        self.window.rows["0"] = {"path": str(media)}
        self.window.tree.insert("", "end", iid="0")
        self.window.tree.selection_set("0")
        with patch("xlikesaver.history_gui.os.startfile", create=True) as opened:
            self.window.open_file()
        opened.assert_called_once_with(str(media))
        unknown = old_root / "999_1.jpg"
        unknown.write_bytes(b"unrelated-file")
        self.window.rows["0"]["path"] = str(unknown)
        with patch("xlikesaver.history_gui.os.startfile", create=True) as opened, patch("xlikesaver.history_gui.dialogs.showinfo") as notice:
            self.window.open_file()
        opened.assert_not_called()
        notice.assert_called_once()

    def test_minimum_history_window_keeps_selection_and_status_visible(self):
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        self.window.attributes("-alpha", 0)
        width, height = self.window.minsize()
        self.window.geometry(f"{width}x{height}")
        self.window.deiconify()
        self.window._display({"files": 384, "posts": 342, "photo": 372, "video": 7,
                              "animated_gif": 5, "bytes": 198700000,
                              "oldest_posted_at": "2022-06-03T00:23:02+09:00",
                              "newest_posted_at": "2026-10-04T20:00:00+09:00",
                              "estimated_dates": 364}, [], [])
        self.window.selection_text.set("트윗 100 · 파일에 출처 기록됨\n출처: https://x.com/i/web/status/100\n파일: C:/offline/100_1.jpg")
        self.root.update()
        self.assertGreaterEqual(self.window.tree.winfo_height(), 90)
        # Each table is laid out inside its own parent. A table once gridded
        # into a sibling frame created after it was drawn underneath that
        # frame, so saved files looked like an empty table.
        for tree in (self.window.tree, self.window.run_tree):
            self.assertEqual(str(tree.grid_info()["in"]), str(tree.master))
        self.assertTrue(self.window.tree.winfo_ismapped())
        status_label = next(widget for widget in self.window.wrapping_labels
                            if str(widget.cget("textvariable")) == str(self.window.status_text))
        for widget in (self.window.selection_detail, status_label):
            self.assertTrue(widget.winfo_ismapped())
            # Readable statistics may occupy two rows in a narrow window.
            # The outer scrollbar then keeps both the selected file and
            # status reachable while preserving the table's useful height.
            self.window.content_view._ensure_visible(widget)
            self.root.update()
            self.assertGreaterEqual(widget.winfo_rooty(), self.window.winfo_rooty())
            bottom = widget.winfo_rooty() - self.window.winfo_rooty() + widget.winfo_height()
            self.assertLessEqual(bottom, self.window.winfo_height())

    def test_long_selected_path_is_scrollable_and_does_not_hide_table(self):
        store = self.records()
        row = dict(store.records()[0], path="C:/Google Drive/" + "long folder/" * 70 + "100_1.jpg")
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        self.window.attributes("-alpha", 0)
        width, height = self.window.minsize()
        self.window.geometry(f"{width}x{height}")
        self.window.deiconify()
        self.window._display(store.summary(), [row], [])
        self.window.tree.selection_set("0")
        self.window.select_record()
        self.root.update()
        detail = self.window.selection_detail
        self.assertGreaterEqual(self.window.tree.winfo_height(), 90)
        self.assertEqual(detail.get("1.0", "end-1c"), self.window.selection_text.get())
        self.assertEqual(str(detail.cget("state")), "disabled")
        original = detail.get("1.0", "end-1c")
        detail.insert("1.0", "unwanted edit")
        self.assertEqual(detail.get("1.0", "end-1c"), original)
        self.assertLess(detail.yview()[1], 1)
        detail.yview_moveto(1)
        self.root.update()
        self.assertAlmostEqual(detail.yview()[1], 1)
        self.assertIn("파일 안 기록", detail.get("@0,0", "end-1c"))
        # Auto-refresh of the same selected file retains the user's scroll.
        before = detail.yview()
        self.window._display(store.summary(), [row], [])
        self.root.update()
        self.assertEqual(detail.yview(), before)

    def test_backfill_writes_only_within_current_account_and_preserves_save_times(self):
        store = self.records()
        original = store.records()[0]
        self.window.busy = False
        with patch("xlikesaver.metadata.embed_source", return_value={"status": "embedded", "url": "https://x.com/i/web/status/100", "message": "ok", "author_status": "embedded"}) as embed, patch("requests.Session.request") as network:
            self.window._backfill(self.window.generation, self.account)
        embed.assert_called_once_with(Path(original["path"]), "100", author="alice", author_id="")
        network.assert_not_called()
        self.assertEqual(store.records()[0]["metadata_status"], "embedded")
        self.assertEqual(store.records()[0]["saved_at"], original["saved_at"])
        outside = Path(self.temporary.name) / "outside.jpg"
        outside.write_bytes(b"untouched-outside-file")
        store._db.execute("UPDATE media SET path=?,metadata_status='pending'", (str(outside),))
        store._db.commit()
        with patch("xlikesaver.metadata.embed_source") as embed:
            self.window._backfill(self.window.generation, self.account)
        embed.assert_not_called()
        self.assertEqual(outside.read_bytes(), b"untouched-outside-file")

    def test_backfill_adds_known_author_to_already_source_tagged_files_once(self):
        store = self.records()
        original = store.records()[0]
        store.mark_metadata(original, "embedded")
        store._db.execute("UPDATE media SET author_id='123456789'")
        store._db.commit()
        with patch("xlikesaver.metadata.embed_source", return_value={"status": "embedded", "author_status": "embedded"}) as embed, patch("requests.Session.request") as network:
            self.window._backfill(self.window.generation, self.account)
            embed.assert_called_once_with(Path(original["path"]), "100", author="alice", author_id="123456789")
            self.window._backfill(self.window.generation, self.account)
            embed.assert_called_once()
        network.assert_not_called()
        row = store.records()[0]
        self.assertEqual(row["author_metadata_status"], "embedded")
        self.assertEqual(row["saved_at"], original["saved_at"])
        self.window._display(store.summary(), store.records(), store.runs())
        self.window.tree.selection_set("0")
        self.window.select_record()
        self.assertIn("@alice (ID 123456789)", self.window.selection_text.get())
        self.assertIn("작성자 기록됨", self.window.selection_text.get())

    def test_backfill_drops_catalog_author_text_that_is_not_a_handle(self):
        store = self.records()
        store._db.execute("UPDATE media SET author='고양이 폴더'")
        store._db.commit()
        original = store.records()[0]
        with patch("xlikesaver.metadata.embed_source", return_value={"status": "embedded", "author_status": "unknown"}) as embed, \
                patch("requests.Session.request") as network:
            self.window._backfill(self.window.generation, self.account)
            embed.assert_called_once_with(Path(original["path"]), "100", author="", author_id="")
            # Source recorded, author unknown: the next pass leaves the file alone.
            self.window._backfill(self.window.generation, self.account)
            embed.assert_called_once()
        network.assert_not_called()
        self.assertEqual((store.records()[0]["metadata_status"], store.records()[0]["author_metadata_status"]),
                         ("embedded", "unknown"))

    def test_backfill_does_not_guess_author_for_source_tagged_unknown_record(self):
        store = self.records()
        store._db.execute("UPDATE media SET author='',metadata_status='embedded'")
        store._db.commit()
        with patch("xlikesaver.metadata.embed_source") as embed, patch("requests.Session.request") as network:
            self.window._backfill(self.window.generation, self.account)
        embed.assert_not_called()
        network.assert_not_called()
        self.assertEqual(store.records()[0]["author"], "")

    def test_author_backfill_failure_preserves_previous_source_and_original_save_time(self):
        store = self.records()
        original = store.records()[0]
        store.mark_metadata(original, "embedded")
        with patch("xlikesaver.metadata.embed_source", return_value={"status": "failed", "author_status": "error"}), patch("requests.Session.request") as network:
            self.window._backfill(self.window.generation, self.account)
        network.assert_not_called()
        row = store.records()[0]
        self.assertEqual((row["metadata_status"], row["author_metadata_status"]), ("embedded", "error"))
        self.assertEqual(row["saved_at"], original["saved_at"])


class HistoryLifecycleTests(unittest.TestCase):
    def test_root_history_preview_theme_resources_die_before_background_gc(self):
        # A decoder's allocations can trigger Python's cyclic GC. Tk resources
        # must already be released on the main thread before that can happen.
        root = app = window = loader = None
        automatic_gc = gc.isenabled()
        old_hook = sys.unraisablehook
        errors = []
        sys.unraisablehook = lambda error: errors.append((str(error.exc_value), threading.current_thread().name))
        gc.disable()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = tk.Tk()
                root.withdraw()
                with patch("xlikesaver.gui.load_settings", return_value={"output_dir": directory}), \
                        patch("xlikesaver.gui.has_session", return_value=False):
                    app = App(root)
                with patch("xlikesaver.history_gui.threading.Thread"):
                    window = HistoryWindow(root, lambda: ("offline", Path(directory)))
                app.history_window = window
                window._preview_token = 1
                window._preview_results.put((1, Thumbnail(Image.new("RGB", (30, 20), "red"), "ready")))
                window._poll_previews()
                refs = [weakref.ref(value) for value in
                        (root, app, window, window.selection_text, app.status, window._preview_image)]
                theme.apply(root, "dark")
                self.assertEqual(theme.mode(root), "dark")
                window.destroy()
                root.destroy()
                root = app = window = None
                self.assertTrue(all(ref() is None for ref in refs), "Closed UI retained a Python cycle")
                path = Path(directory) / "local.jpg"
                path.write_bytes(b"local file for cache key")
                results = queue.Queue(maxsize=2)
                loader = PreviewLoader(results, decoder=lambda _: (gc.collect(), Thumbnail(None, "done"))[-1])
                loader.request(1, path, lambda _: True)
                self.assertEqual(results.get(timeout=3)[0], 1)
                loader.close()
                loader.thread.join(1)
                self.assertFalse(loader.thread.is_alive())
                self.assertEqual(errors, [])
                self.assertTrue(all(ref() is None for ref in refs))
        finally:
            if loader is not None:
                loader.close()
            if root is not None:
                root.destroy()
            root = app = window = None
            sys.unraisablehook = old_hook
            if automatic_gc:
                gc.enable()
            gc.collect()


if __name__ == "__main__":
    unittest.main()
