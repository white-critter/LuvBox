"""Local record window workflows without X requests or opening media apps."""

from pathlib import Path
from contextlib import closing
import json
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from xlikesaver.gui import App
from xlikesaver.history import HistoryStore
from xlikesaver.history_gui import HistoryWindow
from xlikesaver.protection import AccountGuard, ProtectionError
from xlikesaver.storage import OutputGuard, resolve_storage
from tests.test_gui import destroy_tk


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

    def test_backfill_is_disabled_while_downloading(self):
        self.window.busy = False
        self.window.is_downloading = lambda: True
        with patch("xlikesaver.history_gui.messagebox.showinfo") as notice, patch("xlikesaver.history_gui.threading.Thread") as thread:
            self.window.backfill()
        notice.assert_called_once()
        thread.assert_not_called()

    def test_refresh_skips_whole_folder_rescan_while_downloading(self):
        # The worker records every file itself; a rescan would only compete
        # with it for the catalog's write lock.
        for downloading, expected in ((True, False), (False, True)):
            with self.subTest(downloading=downloading):
                self.window.busy = False
                self.window.is_downloading = lambda: downloading
                with patch("xlikesaver.history_gui.threading.Thread") as thread:
                    self.window.refresh(import_legacy=True)
                self.assertIs(thread.call_args.kwargs["args"][-1], expected)
                if downloading:
                    self.assertIn("재확인을 건너뛰고", self.window.status_text.get())

    def test_closing_history_cancels_poll_without_closing_main_window(self):
        scheduled = self.window._poll_timer
        self.assertIn(scheduled, self.root.tk.call("after", "info"))
        self.window.close()
        self.assertNotIn(scheduled, self.root.tk.call("after", "info"))
        self.assertEqual(self.root.winfo_exists(), 1)
        self.window._poll()  # A late callback is harmless after closing.

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
        with patch("xlikesaver.metadata.embed_source", return_value={"status": "embedded"}) as embed, patch("requests.Session.request") as network:
            self.window._backfill(self.window.generation, paths)
        embed.assert_called_once_with(media, "100")
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
        with patch("xlikesaver.history_gui.os.startfile", create=True) as opened, patch("xlikesaver.history_gui.messagebox.showinfo") as notice:
            self.window.open_file()
        opened.assert_not_called()
        notice.assert_called_once()

    def test_minimum_history_window_keeps_selection_and_status_visible(self):
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        self.window.attributes("-alpha", 0)
        self.window.geometry("780x600")
        self.window.deiconify()
        self.window._display({"files": 384, "posts": 342, "photo": 372, "video": 7,
                              "animated_gif": 5, "bytes": 198700000,
                              "oldest_posted_at": "2022-06-03T00:23:02+09:00",
                              "newest_posted_at": "2026-10-04T20:00:00+09:00",
                              "estimated_dates": 364}, [], [])
        self.window.selection_text.set("트윗 100 · 파일에 출처 기록됨\n출처: https://x.com/i/web/status/100\n파일: C:/offline/100_1.jpg")
        self.root.update()
        self.assertGreaterEqual(self.window.tree.winfo_height(), 90)
        for variable in (self.window.selection_text, self.window.status_text):
            label = next(widget for widget in self.window.wrapping_labels if str(widget.cget("textvariable")) == str(variable))
            self.assertTrue(label.winfo_ismapped())
            bottom = label.winfo_rooty() - self.window.winfo_rooty() + label.winfo_height()
            self.assertLessEqual(bottom, self.window.winfo_height())

    def test_backfill_writes_only_within_current_account_and_preserves_save_times(self):
        store = self.records()
        original = store.records()[0]
        self.window.busy = False
        with patch("xlikesaver.metadata.embed_source", return_value={"status": "embedded", "url": "https://x.com/i/web/status/100", "message": "ok"}) as embed, patch("requests.Session.request") as network:
            self.window._backfill(self.window.generation, self.account)
        embed.assert_called_once_with(Path(original["path"]), "100")
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


if __name__ == "__main__":
    unittest.main()
