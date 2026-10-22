"""Native UI workflow checks without account or browser access."""

import gc
import json
import os
from pathlib import Path
import tempfile
import threading
import tkinter as tk
from types import SimpleNamespace
import unittest
import weakref
from unittest.mock import Mock, patch

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox.gui import App, AUTH_MODES, worker_command


def destroy_tk(root):
    """Cancel scheduled callbacks before a test destroys its Tcl interpreter."""
    root.update_idletasks()
    for timer in root.tk.call("after", "info"):
        root.tk.call("after", "cancel", timer)
    root.destroy()


def release_tk(test, *names):
    """Free a test's Tk objects on the main thread right after destroy.

    Otherwise a later automatic collection may run on a worker thread of
    another test, and freeing a Tcl interpreter there aborts the process.
    """
    for name in names:
        test.__dict__.pop(name, None)
    gc.collect()


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        app_data = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        app_data.start()
        self.addCleanup(app_data.stop)
        self.guards = patch("luvbox.protection.state_directory", return_value=Path(self.temp.name) / "locks")
        self.guards.start()
        self.addCleanup(self.guards.stop)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(release_tk, self, "app", "root")
        self.addCleanup(destroy_tk, self.root)
        self.session = patch("luvbox.gui.has_session", return_value=False)
        self.session_mock = self.session.start()
        self.addCleanup(self.session.stop)
        self.settings = patch("luvbox.gui.load_settings", return_value={})
        self.settings.start()
        self.addCleanup(self.settings.stop)
        preferences = patch("luvbox.gui.settings_path", return_value=Path(self.temp.name) / "settings.json")
        preferences.start()
        self.addCleanup(preferences.stop)
        self.app = App(self.root)

    def _check_help_taskbar_cleanup(self, close_with_button):
        from luvbox import app_icon

        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        self.app.help()
        window = self.app.help_window
        window.attributes("-alpha", 0)
        self.root.update()
        handle = int(window.wm_frame(), 16)
        properties = app_icon._taskbar_properties
        self.assertEqual(properties(handle, None)[5], app_icon.APP_ID)
        cleared = []

        def tracked(current, values):
            result = properties(current, values)
            if current == handle and values == {}:
                cleared.append((bool(window.winfo_exists()), properties(current, None)))
            return result

        with patch.object(app_icon, "_taskbar_properties", side_effect=tracked):
            if close_with_button:
                pending = [window]
                while pending:
                    widget = pending.pop()
                    if widget.winfo_class() == "TButton" and widget.cget("text") == "닫기":
                        widget.invoke()
                        break
                    pending.extend(widget.winfo_children())
                else:
                    self.fail("Help close button is missing")
            else:
                self.root.tk.call(window.protocol("WM_DELETE_WINDOW"))
        self.assertEqual(cleared, [(True, {2: "", 3: "", 4: "", 5: ""})])
        self.assertFalse(window.winfo_exists())
        self.assertTrue(self.root.winfo_exists())

    @unittest.skipUnless(os.name == "nt", "Windows taskbar properties")
    def test_help_close_button_clears_taskbar_properties_before_destroy(self):
        self._check_help_taskbar_cleanup(close_with_button=True)

    @unittest.skipUnless(os.name == "nt", "Windows taskbar properties")
    def test_help_window_close_clears_taskbar_properties_before_destroy(self):
        self._check_help_taskbar_cleanup(close_with_button=False)

    def test_help_window_close_button_and_title_bar_use_the_current_destroy(self):
        from tkinter import ttk

        for close in ("button", "title"):
            with self.subTest(close=close):
                self.app.help()
                popup = self.app.help_window
                closed = []
                # The icon hook replaces destroy after the window is created.
                original = popup.destroy
                popup.destroy = lambda: (closed.append(True), original())
                if close == "button":
                    stack = list(popup.winfo_children())
                    while stack:
                        widget = stack.pop()
                        stack.extend(widget.winfo_children())
                        if isinstance(widget, ttk.Button) and widget.cget("text") == "닫기":
                            widget.invoke()
                            break
                else:
                    popup.tk.call(popup.wm_protocol("WM_DELETE_WINDOW"))
                self.assertEqual(closed, [True])
                self.assertFalse(popup.winfo_exists())

    def test_unconnected_token_mode_requests_connection_before_download(self):
        with patch("luvbox.gui.threading.Thread") as thread:
            self.app.start()
        # The login window opens with the reason instead of a separate message box.
        self.assertTrue(self.app.connect_window.winfo_exists())
        self.assertIn("로그인", self.app.status.get())
        thread.assert_not_called()
        self.assertFalse(self.app.running)
        self.assertIn("연결 필요", self.app.login_text.get())

    def test_connection_status_can_grow_after_a_short_disconnected_label(self):
        import tkinter.font as tkfont
        from luvbox import theme
        self.root.geometry("1180x880")
        self.root.deiconify()
        self.root.update()
        self.assertIn("연결 필요", self.app.login_text.get())
        self.session_mock.return_value = True
        self.app._auth_changed()
        theme.apply(self.root, "dark")
        self.root.update()
        label = self.app.login_label
        measured = tkfont.Font(root=self.root, font=theme.SMALL).measure(self.app.login_text.get())
        self.assertGreaterEqual(int(label.cget("wraplength")), measured)
        self.assertEqual(str(label.cget("anchor")), "w")

    def relocation_source(self):
        from contextlib import closing
        import sqlite3
        from luvbox.history import HistoryStore
        from luvbox.storage import resolve_storage
        source = resolve_storage(Path(self.temp.name) / "이전 폴더", "offline_test")
        source.output_dir.mkdir()
        media = source.output_dir / "100_1.jpg"
        media.write_bytes(b"original-media")
        store = HistoryStore.for_storage(source)
        try:
            store.record_media("flat", {"tweet_id": "100", "num": 1, "type": "photo",
                                        "_luvbox_metadata_status": "embedded"}, media)
        finally:
            store.close()
        with closing(sqlite3.connect(source.record_dir / ".luvbox-archive.flat.sqlite3")) as db:
            db.execute("CREATE TABLE archive(entry TEXT PRIMARY KEY)")
            db.execute("INSERT INTO archive VALUES ('twitter100_1')")
            db.commit()
        self.app.account.set("offline_test")
        self.app.folder.set(str(source.output_dir))
        self.app.record_locations["offline_test"] = str(source.output_dir)
        return source, Path(self.temp.name) / "새 폴더"

    def test_folder_choice_carries_records_and_remembers_new_location(self):
        from luvbox.history import HistoryStore
        from luvbox.storage import resolve_storage
        source, target = self.relocation_source()
        with patch("luvbox.gui.filedialog.askdirectory", return_value=str(target)), \
                patch("luvbox.gui.threading.Thread") as thread, \
                patch("luvbox.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
            self.app.choose_folder()
            self.assertTrue(self.app.relocating)
            self.assertFalse(self.app.running)
            self.assertEqual(str(self.app.start_button.cget("state")), "disabled")
            self.app._relocate_records(*thread.call_args.kwargs["args"])
            self.app._poll()
        self.assertFalse(self.app.relocating)
        self.assertEqual(self.app.record_locations["offline_test"], str(target))
        prefs = json.loads((Path(self.temp.name) / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(prefs["output_dir"], str(target))
        self.assertEqual(prefs["record_locations"]["offline_test"], str(target))
        store = HistoryStore.for_storage(resolve_storage(target, "offline_test"))
        try:
            store.import_legacy(refresh=True)
            self.assertEqual(store.summary()["files"], 1)
            self.assertEqual(store.records()[0]["path"], str(source.output_dir / "100_1.jpg"))
        finally:
            store.close()
        self.assertEqual((source.output_dir / "100_1.jpg").read_bytes(), b"original-media")

    def test_start_waits_for_typed_folder_relocation_before_worker(self):
        source, target = self.relocation_source()
        self.app.folder.set(str(target))
        self.session_mock.return_value = True
        with patch("luvbox.gui.threading.Thread") as thread, \
                patch("luvbox.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
            self.app.start()
            self.assertTrue(self.app.relocating)
            self.assertFalse(self.app.running)
            self.assertEqual(thread.call_args.kwargs["target"], self.app._relocate_records)
            self.app._relocate_records(*thread.call_args.kwargs["args"])
            self.app._poll()
            self.assertTrue(self.app.running)
            self.assertEqual(thread.call_args.kwargs["target"], self.app._run_process)
            self.assertEqual(thread.call_args.kwargs["args"][0].output_dir, str(target))

    def test_relocation_failure_keeps_old_location_without_starting_download(self):
        source, target = self.relocation_source()
        self.app.folder.set(str(target))
        with patch("luvbox.gui.threading.Thread") as thread, \
                patch("luvbox.storage.migrate_records", side_effect=OSError("private-error")), \
                patch("luvbox.gui.dialogs.showerror") as error:
            self.assertFalse(self.app._ensure_storage("start"))
            self.app._relocate_records(*thread.call_args.kwargs["args"])
            self.app._poll()
        self.assertEqual(self.app.folder.get(), str(source.output_dir))
        self.assertEqual(self.app.record_locations["offline_test"], str(source.output_dir))
        self.assertFalse(self.app.running)
        self.assertFalse(self.app.relocating)
        self.assertNotIn("private-error", self.app.status.get())
        error.assert_called_once()

    def test_source_validation_failure_cannot_silently_use_an_empty_new_catalog(self):
        source, target = self.relocation_source()
        self.app.folder.set(str(target))
        from luvbox.storage import resolve_storage
        def resolve(directory, account, split):
            if str(directory) == str(source.output_dir):
                raise ValueError("linked-source")
            return resolve_storage(directory, account, split)
        with patch("luvbox.gui.resolve_storage", side_effect=resolve), \
                patch("luvbox.gui.threading.Thread") as thread:
            self.assertFalse(self.app._ensure_storage("start"))
        thread.assert_not_called()
        self.assertEqual(self.app.folder.get(), str(source.output_dir))

    def test_typing_a_folder_does_not_apply_it_on_focus_change(self):
        folder = next(widget for widget in self.app.inputs
                      if isinstance(widget, tk.ttk.Entry) and
                      str(widget.cget("textvariable")) == str(self.app.folder))
        self.assertEqual(folder.bind("<FocusOut>"), "")
        self.assertTrue(folder.bind("<Return>"))

    def test_unavailable_previous_folder_requires_explicit_fresh_start(self):
        source, target = self.relocation_source()
        offline = source.output_dir.with_name("연결되지 않은 드라이브")
        self.app.record_locations[source.account] = str(offline)
        self.app.folder.set(str(target))
        with patch("luvbox.gui.dialogs.askyesno", return_value=False) as notice, \
                patch("luvbox.gui.threading.Thread") as thread:
            self.assertFalse(self.app._ensure_storage("start"))
        notice.assert_called_once()
        thread.assert_not_called()
        self.assertEqual(self.app.folder.get(), str(offline))
        self.assertFalse(target.exists())

    def test_explicit_fresh_start_preserves_destination_records_and_can_merge_old_records_later(self):
        from luvbox.history import HistoryStore
        from luvbox.storage import migrate_records, resolve_storage
        source, target = self.relocation_source()
        destination = resolve_storage(target, source.account)
        store = HistoryStore.for_storage(destination)
        file = target / "101_1.jpg"
        file.write_bytes(b"new-media")
        store.record_media("flat", {"tweet_id": "101", "num": 1, "type": "photo"}, file)
        store.close()
        self.app.record_locations[source.account] = str(source.output_dir.with_name("offline"))
        self.app.folder.set(str(target))
        with patch("luvbox.gui.dialogs.askyesno", return_value=True), \
                patch("luvbox.gui.threading.Thread") as thread, \
                patch("luvbox.storage.migrate_records") as migration, \
                patch("luvbox.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
            self.assertFalse(self.app._ensure_storage())
            self.app._relocate_records(*thread.call_args.kwargs["args"])
            self.app._poll()
        migration.assert_not_called()
        self.assertEqual(self.app.record_locations[source.account], str(target))
        self.assertFalse(self.app.relocating)
        migrate_records(destination, source)
        store = HistoryStore.for_storage(source)
        try:
            self.assertEqual({row["tweet_id"] for row in store.records()}, {"100", "101"})
        finally:
            store.close()
        self.assertEqual(file.read_bytes(), b"new-media")

    def test_first_folder_choice_does_not_require_an_unused_default_directory(self):
        self.app.account.set("offline_test")
        with patch("luvbox.gui.filedialog.askdirectory", return_value=self.temp.name), \
                patch("luvbox.gui.threading.Thread") as thread:
            self.app.choose_folder()
        thread.assert_not_called()
        self.assertFalse(self.app.relocating)
        self.assertNotIn("offline_test", self.app.record_locations)

    def test_preferences_failure_after_copy_keeps_previous_active_location(self):
        source, target = self.relocation_source()
        self.app.folder.set(str(target))
        with patch("luvbox.gui.threading.Thread"), patch.object(self.app, "_save_settings", side_effect=PermissionError), \
                patch("luvbox.gui.dialogs.showerror"):
            self.app._ensure_storage()
            self.app._finish_relocation({"ok": True})
        self.assertEqual(self.app.folder.get(), str(source.output_dir))
        self.assertEqual(self.app.record_locations["offline_test"], str(source.output_dir))

    def test_close_waits_for_relocation_to_finish(self):
        self.relocation_source()
        self.app.folder.set(str(Path(self.temp.name) / "new"))
        with patch("luvbox.gui.threading.Thread"), patch.object(self.root, "destroy") as destroy, \
                patch("luvbox.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
            self.app._ensure_storage()
            self.app.close()
            destroy.assert_not_called()
            self.app._finish_relocation({"ok": True})
            destroy.assert_called_once()

    def test_download_passes_save_layout_and_remembers_photos_without_a_baseline(self):
        self.app.account.set("@Offline_Test")
        self.app.folder.set(self.temp.name)
        self.session_mock.return_value = True
        self.app.folder_mode.set("저장일별 폴더")
        with patch("luvbox.gui.threading.Thread") as thread, patch("luvbox.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
            self.app.start()
        options = thread.call_args.kwargs["args"][0]
        self.assertEqual((options.folder_mode, options.scan_mode, options.photo_limit), ("date", "quick", 0))
        persisted = json.loads((Path(self.temp.name) / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["folder_mode"], "date")
        self.assertEqual(persisted["scan_mode"], "quick")
        self.assertNotIn("sync_mode", persisted)
        self.assertNotIn("date_from", persisted)
        self.assertNotIn("cookies_file", persisted)
        self.assertNotIn("photo_limit", persisted)
        self.assertNotIn("token", persisted)
        thread.return_value.start.assert_called_once()

    def test_short_test_is_opt_in(self):
        self.app.account.set("offline_test")
        self.app.folder.set(self.temp.name)
        self.session_mock.return_value = True
        self.app.limited.set(True)
        with patch("luvbox.gui.threading.Thread") as thread, patch("luvbox.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
            self.app.start()
        options = thread.call_args.kwargs["args"][0]
        self.assertEqual((options.scan_mode, options.photo_limit), ("quick", 20))

    def test_full_scan_choice_reaches_worker_and_is_remembered(self):
        self.app.account.set("offline_test")
        self.app.folder.set(self.temp.name)
        self.session_mock.return_value = True
        self.app.scan_mode.set("전체 목록 다시 확인")
        with patch("luvbox.gui.threading.Thread") as thread, patch("luvbox.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
            self.app.start()
        self.assertEqual(thread.call_args.kwargs["args"][0].scan_mode, "full")
        persisted = json.loads((Path(self.temp.name) / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["scan_mode"], "full")
        other_root = tk.Tk()
        other_root.withdraw()
        self.addCleanup(destroy_tk, other_root)
        with patch("luvbox.gui.load_settings", return_value=persisted):
            app = App(other_root)
        self.assertEqual(app.scan_mode.get(), "전체 목록 다시 확인")

    def schedule_patches(self, status):
        class Immediate:
            def __init__(self, target, daemon=None):
                self.target = target

            def start(self):
                self.target()

        def register(when):
            status.clear()
            status.update(registered=True, current=True, time=when, enabled=True, catch_up=True,
                          on_battery=True, next_run="2026-10-07T07:30:00", program="LuvBox.exe")

        def unregister():
            status.clear()
            status.update(registered=False)

        last = {"finished_at": "2026-10-06T09:00:05+09:00", "status": "finished", "saved": 3, "failed": False}
        return (patch("luvbox.gui.threading.Thread", Immediate),
                patch("luvbox.scheduled.task_status", side_effect=lambda: dict(status)),
                patch("luvbox.scheduled.register_task", side_effect=register),
                patch("luvbox.scheduled.unregister_task", side_effect=unregister),
                patch("luvbox.scheduled.last_run", return_value=last))

    def test_schedule_window_turns_the_daily_save_on_and_off(self):
        self.session_mock.return_value = True
        status = {"registered": False}
        target = Path(self.temp.name) / "settings.json"
        patches = self.schedule_patches(status)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.app.account.set("offline_test")
        self.app.folder.set(self.temp.name)
        with patch("luvbox.gui.settings_path", return_value=target):
            self.app.show_schedule()
            self.addCleanup(self.app._close_schedule)
            self.app._poll()
            self.assertIn("꺼져", self.app.schedule_state.get())
            self.assertIn("새로 저장 3개", self.app.schedule_last.get())
            self.assertEqual(self.app.schedule_on_button.cget("text"), "예약 켜기")
            self.app.schedule_time.set("7:30")
            self.app.register_schedule()
            self.app._poll()
        from luvbox import scheduled
        scheduled.register_task.assert_called_once_with("07:30")
        saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual((saved["account"], saved["schedule_time"]), ("offline_test", "07:30"))
        self.assertIn("매일 07:30 · 켜짐", self.app.schedule_text.get())
        self.assertEqual(self.app.schedule_on_button.cget("text"), "예약 갱신")
        self.assertIn("자동 저장을 켰습니다", self.app.status.get())
        with patch("luvbox.gui.dialogs.askyesno", return_value=True):
            self.app.unregister_schedule()
        self.app._poll()
        self.assertIn("꺼짐", self.app.schedule_text.get())
        self.assertEqual(str(self.app.schedule_off_button.cget("state")), "disabled")

    def test_schedule_needs_token_connection_and_valid_time(self):
        patches = self.schedule_patches({"registered": False})
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        from luvbox import scheduled
        self.app.auth.set("cookies.txt 파일")
        with patch("luvbox.gui.dialogs.showinfo") as notice:
            self.app.register_schedule()
        notice.assert_called_once()
        self.app.auth.set("인증 토큰 (권장)")
        self.app.account.set("offline_test")
        self.app.folder.set(self.temp.name)
        self.app.schedule_time.set("25:00")
        with patch("luvbox.gui.dialogs.showerror") as error:
            self.app.register_schedule()
        self.assertIn("24시간", error.call_args.args[1])
        scheduled.register_task.assert_not_called()

    def test_schedule_actions_keep_the_same_height_when_enabled_and_disabled(self):
        with patch.object(self.app, "refresh_schedule"):
            self.app.show_schedule()
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        window = self.app.schedule_window
        window.deiconify()
        self.root.update()
        buttons = self.app.schedule_on_button.master.winfo_children()
        for registered in (False, True):
            self.app.schedule_info = {"registered": registered, "current": True}
            self.app._update_schedule_buttons()
            self.root.update_idletasks()
            heights = [button.winfo_height() for button in buttons]
            self.assertLessEqual(max(heights) - min(heights), 2)
            self.assertGreaterEqual(min(heights), 32)
            self.assertEqual(len({button.winfo_y() for button in buttons}), 1)
        self.app._close_schedule()

    def test_initial_height_is_taller_and_fits_short_screens(self):
        for screen_height, expected_height in ((1440, 1320), (768, 668), (600, 500)):
            with self.subTest(screen_height=screen_height):
                window = tk.Toplevel(self.root)
                window.withdraw()
                try:
                    with patch.object(window, "winfo_screenheight", return_value=screen_height):
                        other = App(window)
                        window.deiconify()
                        self.root.update_idletasks()
                    width, height = map(int, window.geometry().split("+", 1)[0].split("x"))
                    self.assertEqual(height, expected_height)
                    self.assertLessEqual(height, screen_height - 100)
                    self.assertLessEqual(window.minsize()[1], height)
                finally:
                    destroy_tk(window)
                    other = None

    def test_new_version_offers_to_move_the_task_once_and_failures_are_shown(self):
        self.session_mock.return_value = True
        old = {"registered": True, "current": False, "program": r"C:\old\LuvBox-1.7.1.exe", "time": "08:00",
               "enabled": True, "catch_up": False, "on_battery": False, "next_run": ""}
        patches = self.schedule_patches(dict(old))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        from luvbox import scheduled
        self.app.account.set("offline_test")
        self.app.folder.set(self.temp.name)
        with patch("luvbox.gui.dialogs.askyesno", return_value=True) as ask, \
                patch("luvbox.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"), \
                patch("luvbox.gui.load_settings", return_value={}):
            self.app._schedule_result({"type": "schedule", "action": "status", "prompt": True, "result": old})
            self.app._poll()
            self.assertIn("LuvBox-1.7.1.exe", ask.call_args.args[1])
            scheduled.register_task.assert_called_once_with("08:00")
            self.app._schedule_result({"type": "schedule", "action": "status", "prompt": True, "result": old})
        ask.assert_called_once()
        failed = {"finished_at": "2026-10-06T09:00:05+09:00", "status": "error", "saved": 0, "failed": True,
                  "seen": False, "message": "로그인 연결을 다시 해 주세요."}
        with patch("luvbox.scheduled.last_run", return_value=failed), \
                patch("luvbox.scheduled.mark_last_run_seen") as seen, \
                patch.object(self.app, "refresh_schedule"):
            self.app._startup_schedule_check()
        seen.assert_called_once_with(failed)
        self.assertIn("실패", self.app.status.get())
        self.assertIn("로그인 연결을 다시", self.app.status_detail.get())

    def test_default_flat_media_options_and_user_video_cap_reach_worker(self):
        self.assertEqual(self.app.folder_mode.get(), "구분 없이 저장")
        self.assertFalse(self.app.separate_accounts.get())
        self.assertTrue(self.app.include_videos.get())
        self.assertTrue(self.app.include_gifs.get())
        self.assertEqual(self.app.max_video_mb.get(), "200")
        self.app.account.set("offline_test")
        self.app.folder.set(self.temp.name)
        self.app.include_gifs.set(False)
        self.app.max_video_mb.set("37")
        self.session_mock.return_value = True
        with patch("luvbox.gui.threading.Thread") as thread, patch("luvbox.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
            self.app.start()
        options = thread.call_args.kwargs["args"][0]
        self.assertEqual((options.folder_mode, options.include_videos, options.include_gifs, options.max_video_mb), ("flat", True, False, 37))
        self.assertFalse(options.separate_accounts)

    def test_destination_preview_and_folder_open_follow_account_option(self):
        self.app.account.set("@Offline_Test")
        self.app.folder.set(self.temp.name)
        directory = Path(self.temp.name).resolve()
        self.assertEqual(self.app.destination_preview.get(), str(directory))
        account, paths = self.app._history_location()
        self.assertEqual(account, "offline_test")
        self.assertEqual(paths.media_dir, directory)
        self.assertEqual(paths.snapshot_dir, directory / ".luvbox" / "offline_test")
        self.assertTrue(paths.record_dir.is_relative_to(Path(self.temp.name) / "records"))
        with patch("luvbox.gui.os.startfile", create=True) as opened:
            self.app.open_folder()
        opened.assert_called_once_with(str(directory))
        self.app.separate_accounts.set(True)
        self.assertEqual(self.app.destination_preview.get(), str(directory / "offline_test"))
        with patch("luvbox.gui.os.startfile", create=True) as opened:
            self.app.open_folder()
        opened.assert_called_once_with(str(directory / "offline_test"))
        self.app.folder_mode.set("저장일별 폴더")
        self.assertEqual(self.app.destination_preview.get(), str(directory / "offline_test" / "YYYY-MM-DD"))

    def test_extra_save_location_line_appears_only_when_it_adds_a_subfolder(self):
        self.app.account.set("offline_test")
        self.app.folder.set(self.temp.name)
        self.app.folder_mode.set("구분 없이 저장")
        self.app.separate_accounts.set(False)
        self.assertEqual(self.app.destination_text.get(), "")
        self.assertFalse(self.app.preview_label.winfo_manager())
        self.app.folder_mode.set("작성자별 폴더")
        self.assertIn(str(Path(self.temp.name).resolve() / "작성자"), self.app.destination_text.get())
        self.assertTrue(self.app.preview_label.winfo_manager())

    def test_help_tells_what_to_do_without_internal_rules(self):
        text = " ".join(" ".join((title, *items, note or "")) for title, items, note in App.HELP_SECTIONS)
        for internal in ("API", "토큰을", "DB", "archive", "개 이어지면", "다음 한 번은 전체"):
            self.assertNotIn(internal, text)
        self.assertIn("사라진 파일", text)
        self.assertIn("자동 저장", text)

    def test_account_folder_option_reaches_worker_and_is_remembered(self):
        self.app.account.set("offline_test")
        self.app.folder.set(self.temp.name)
        self.app.separate_accounts.set(True)
        self.session_mock.return_value = True
        with patch("luvbox.gui.threading.Thread") as thread, patch("luvbox.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
            self.app.start()
        options = thread.call_args.kwargs["args"][0]
        self.assertTrue(options.separate_accounts)
        persisted = json.loads((Path(self.temp.name) / "settings.json").read_text(encoding="utf-8"))
        self.assertTrue(persisted["separate_accounts"])
        for widget in self.app.inputs:
            self.assertEqual(str(widget.cget("state")), "disabled")

    def test_minimum_window_keeps_actions_details_and_log_reachable(self):
        self.root.attributes("-alpha", 0)
        width, height = self.root.minsize()
        self.root.geometry(f"{width}x{height}")
        self.root.deiconify()
        self.app.progress_details.set("속도 2 MiB/초 · 작성자 @offline_test · 트윗 1000000000000000000\n트윗 게시일 2026-10-04 20:00:00 · 현재 파일의 진행률이며 전체 Like 목록의 완료율은 알 수 없습니다.")
        self.app.status_detail.set("목록은 10초에 한 번씩 천천히 요청합니다. 전체 Like 수와 완료율은 미리 알 수 없습니다.")
        self.app.detailed_progress.set(True)
        self.app._toggle_progress()
        self.root.update()
        self.assertTrue(self.app.start_button.winfo_ismapped())
        self.assertGreater(self.app.progress_detail_label.winfo_height(), 25)
        self.assertGreaterEqual(self.app.log.winfo_height(), 28)
        # A short window scrolls rather than clipping controls or shrinking type.
        for widget in (self.app.start_button, self.app.options_button):
            bottom = widget.winfo_rooty() - self.root.winfo_rooty() + widget.winfo_height()
            self.assertLessEqual(bottom, self.root.winfo_height())
        canvas = self.app.content_view.canvas
        for widget in (self.app.progress_detail_label, self.app.log):
            self.app.content_view._ensure_visible(widget)
            self.root.update()
            self.assertGreaterEqual(widget.winfo_rooty(), canvas.winfo_rooty())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                 canvas.winfo_rooty() + canvas.winfo_height())

    def test_large_dpi_minimum_keeps_work_controls_and_log_reachable_on_short_screens(self):
        # A new Tcl interpreter is important: changing scaling after fonts
        # have already been resolved does not resize existing font instances.
        for scaling in (2.0, 2.6666666667):
            for screen_width, screen_height in ((1920, 1080), (1280, 720)):
                with self.subTest(scaling=scaling, screen=(screen_width, screen_height)):
                    window = tk.Tk()
                    window.withdraw()
                    original_scaling = window.tk.call("tk", "scaling")
                    window.tk.call("tk", "scaling", scaling)
                    other = None
                    try:
                        with patch.object(window, "winfo_screenwidth", return_value=screen_width), \
                                patch.object(window, "winfo_screenheight", return_value=screen_height):
                            other = App(window)
                        window.geometry("780x620")
                        window.deiconify()
                        window.update()
                        self.assertLessEqual(window.winfo_width(), screen_width - 80)
                        self.assertLessEqual(window.winfo_height(), screen_height - 100)
                        for widget in (other.start_button, other.stop_button, other.progress):
                            top = widget.winfo_rooty() - window.winfo_rooty()
                            self.assertGreaterEqual(top, 0)
                            self.assertLessEqual(top + widget.winfo_height(), window.winfo_height())
                            self.assertLessEqual(widget.winfo_rootx() - window.winfo_rootx() + widget.winfo_width(),
                                                 window.winfo_width())
                        actions = [(button.winfo_rootx(), button.winfo_rooty())
                                   for button in (other.start_button, other.stop_button)]
                        other._toggle_setup()
                        window.update()
                        self.assertEqual(actions, [(button.winfo_rootx(), button.winfo_rooty())
                                                   for button in (other.start_button, other.stop_button)])
                        other.content_view.canvas.yview_moveto(1)
                        window.update()
                        canvas = other.content_view.canvas
                        visible_top = max(canvas.winfo_rooty(), other.log.winfo_rooty())
                        visible_bottom = min(canvas.winfo_rooty() + canvas.winfo_height(),
                                             other.log.winfo_rooty() + other.log.winfo_height())
                        linespace = int(window.tk.call("font", "metrics", other.log.cget("font"), "-linespace"))
                        self.assertGreaterEqual(visible_bottom - visible_top, linespace * 3)
                        other._focus_dashboard()
                        window.update()
                        self.assertFalse(other._setup_collapsed)
                        self.assertGreaterEqual(other.account_entry.winfo_rooty(), canvas.winfo_rooty())
                    finally:
                        window.tk.call("tk", "scaling", original_scaling)
                        destroy_tk(window)
                        other = window = widget = canvas = None
                        gc.collect()

    def test_narrow_window_wraps_option_summary_inside_its_column(self):
        self.root.attributes("-alpha", 0)
        self.root.geometry("780x880")
        self.app.folder_mode.set("작성자별 폴더")
        self.app.separate_accounts.set(True)
        self.app.max_video_mb.set("100000")
        self.app.scan_mode.set("전체 목록 다시 확인")
        self.app.option_summary.set(self.app.option_summary.get() + " · 미디어 옵션을 바꿔 전체 목록을 다시 확인합니다.")
        self.root.deiconify()
        self.root.update()
        label = self.app.option_summary_label
        self.assertLessEqual(label.winfo_reqwidth(), label.winfo_width())
        self.assertGreater(label.winfo_height(), 25)

    def test_narrow_form_aligns_fields_and_preserves_folder_input_space(self):
        self.root.attributes("-alpha", 0)
        self.root.geometry("780x880")
        self.root.deiconify()
        self.root.update()
        account, folder = self.app.account_entry, self.app.folder_entry
        self.assertEqual(account.winfo_rootx(), folder.winfo_rootx())
        self.assertGreater(folder.winfo_width(), account.winfo_width())
        self.assertGreater(folder.winfo_width(), 300)
        for widget in (account, folder, self.app.login_button, self.app.options_button):
            self.assertGreaterEqual(widget.winfo_rootx(), self.root.winfo_rootx())
            right = widget.winfo_rootx() - self.root.winfo_rootx() + widget.winfo_width()
            self.assertLessEqual(right, self.root.winfo_width())

    def test_large_font_login_instructions_fit_the_popup(self):
        original_scaling = self.root.tk.call("tk", "scaling")
        self.addCleanup(self.root.tk.call, "tk", "scaling", original_scaling)
        self.root.tk.call("tk", "scaling", 2.0)
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        self.app.show_connect()
        self.app.toggle_manual_token()
        window = self.app.connect_window
        window.attributes("-alpha", 0)
        window.update()
        view = window.content_view
        labels = [widget for widget in self.app.token_controls.winfo_children()
                  if widget.winfo_class() == "TLabel"]
        for label in labels:
            self.assertLessEqual(label.winfo_reqwidth(), label.winfo_width() + 2)
            right = label.winfo_rootx() - window.winfo_rootx() + label.winfo_width()
            self.assertLessEqual(right, window.winfo_width())
        self.assertGreater(self.app.token_entry.winfo_width(), 250)
        self.assertGreaterEqual(view.content.winfo_height(), view.content.winfo_reqheight())

    def test_popups_first_map_uses_the_final_size(self):
        self.root.deiconify()
        self.root.update()
        fit = self.app._fit_popup
        mapped = []

        def observed(window):
            window.bind("<Map>", lambda event: mapped.append(
                (window, window.winfo_width(), window.winfo_height()))
                if event.widget is window else None, add="+")
            fit(window)

        with patch.object(self.app, "_fit_popup", side_effect=observed):
            for show, name, close in ((self.app.show_connect, "connect_window", self.app._close_connect),
                                      (self.app.show_options, "options_window",
                                       lambda: self.app._close_options(save=False))):
                with self.subTest(popup=name):
                    show()
                    self.root.update()
                    window = getattr(self.app, name)
                    first = next(size for owner, *size in mapped if owner is window)
                    self.assertEqual(first, [window.winfo_width(), window.winfo_height()])
                    close()

    def test_short_screen_keeps_full_log_reachable_and_wheel_scrolls(self):
        window = tk.Toplevel(self.root)
        window.withdraw()
        window.attributes("-alpha", 0)
        self.addCleanup(window.destroy)
        with patch.object(window, "winfo_screenwidth", return_value=1366), \
                patch.object(window, "winfo_screenheight", return_value=768):
            app = App(window)
        window.deiconify()
        window.update()
        self.assertLessEqual(window.winfo_height(), 688)
        self.assertGreaterEqual(app.log.winfo_height(), 60)
        canvas = app.content_view.canvas
        self.assertLess(canvas.yview()[1], 1)
        # Account and work controls stay fixed; only the recent-work region
        # scrolls, so a wheel over Start must not move the content underneath.
        position = canvas.yview()
        app.start_button.event_generate("<MouseWheel>", delta=-120)
        window.update()
        self.assertEqual(canvas.yview(), position)
        self.assertLessEqual(app.progress.winfo_rooty() - window.winfo_rooty() + app.progress.winfo_height(),
                             window.winfo_height())
        canvas.event_generate("<MouseWheel>", delta=-120)
        window.update()
        self.assertGreater(canvas.yview()[0], 0)
        canvas.yview_moveto(1)
        window.update()
        top = app.log.winfo_rooty() - window.winfo_rooty()
        self.assertGreaterEqual(top, 0)
        self.assertLessEqual(top + app.log.winfo_height(), window.winfo_height())
        # The log has its own scroll; its wheel must not move the whole screen.
        position = canvas.yview()
        app.log.event_generate("<MouseWheel>", delta=120)
        window.update()
        self.assertEqual(canvas.yview(), position)
        # Keyboard navigation also brings an off-screen field into view.
        canvas.yview_moveto(0)
        window.update()
        app.log.focus_force()
        window.update()
        self.assertGreater(canvas.yview()[0], 0)
        self.assertGreaterEqual(app.log.winfo_rooty() - window.winfo_rooty(), 0)

    def test_long_paths_and_live_progress_keep_log_full_height_after_resize(self):
        self.root.attributes("-alpha", 0)
        self.root.geometry("960x760")
        self.root.deiconify()
        self.root.update()
        self.app.folder.set("C:/GoogleDrive/" + "very-long-path-name/" * 12)
        self.app.status_detail.set("전체 Like 목록을 확인하는 중입니다. " * 12)
        self.app.events.put({"type": "progress", "filename": "1000000000000000000_video_1.mp4",
                             "tweet_id": "1000000000000000000", "author": "offline_test",
                             "downloaded": 50, "total": 100, "speed": 25})
        self.app._poll()
        self.app.detailed_progress.set(True)
        self.app._toggle_progress()
        self.root.update()
        self.root.geometry("780x760")
        self.root.update()
        view = self.app.content_view
        self.assertGreaterEqual(view.content.winfo_height(), view.content.winfo_reqheight())
        self.assertGreaterEqual(self.app.log.winfo_height(), 60)
        view.canvas.yview_moveto(1)
        self.root.update()
        bottom = self.app.log.winfo_rooty() - self.root.winfo_rooty() + self.app.log.winfo_height()
        self.assertLessEqual(bottom, self.root.winfo_height())

    def test_trial_applies_to_one_run_and_validation_does_not_consume_it(self):
        self.app.account.set("offline_test")
        self.app.folder.set(self.temp.name)
        self.app.limited.set(True)
        self.session_mock.return_value = True
        self.app.max_video_mb.set("bad")
        with patch("luvbox.gui.dialogs.showerror"), patch("luvbox.gui.threading.Thread") as thread:
            self.app.start()
            thread.assert_not_called()
        self.assertTrue(self.app.limited.get())
        self.app.max_video_mb.set("200")
        with patch("luvbox.gui.threading.Thread") as thread, \
                patch("luvbox.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
            self.app.start()
            self.assertEqual(thread.call_args.kwargs["args"][0].photo_limit, 20)
            self.app.events.put({"type": "finished", "status": "finished"})
            self.app.events.put({"type": "process_exit", "code": 0})
            self.app._poll()
            self.assertFalse(self.app.limited.get())
            self.app.start()
            self.assertEqual(thread.call_args.kwargs["args"][0].photo_limit, 0)

    def test_stored_login_does_not_claim_server_validation(self):
        self.session_mock.return_value = True
        self.app._auth_changed()
        self.assertIn("연결 정보 저장됨", self.app.login_text.get())

    def test_main_text_uses_readable_sizes(self):
        import tkinter.font as tkfont
        from tkinter import ttk
        style = ttk.Style(self.root)
        sizes = {name: tkfont.Font(root=self.root, font=style.lookup(name, "font")).actual("size")
                 for name in ("TLabel", "Muted.TLabel", "Title.TLabel", "CardBold.TLabel", "TileNumber.TLabel")}
        self.assertGreaterEqual(sizes["TLabel"], 12)
        self.assertGreaterEqual(sizes["Muted.TLabel"], 11)
        self.assertGreater(sizes["Title.TLabel"], sizes["TLabel"])
        # No option-database font may override ttk label styles.
        label = ttk.Label(self.root, text="x", style="Title.TLabel")
        self.assertEqual(str(label.cget("font")), "")

    def test_options_window_edits_the_same_preferences(self):
        self.app.show_options()
        self.assertTrue(self.app.options_window.winfo_exists())
        self.app.folder_mode.set("저장일별 폴더")
        self.app.include_gifs.set(False)
        self.app.scan_mode.set("전체 목록 다시 확인")
        self.assertIn("날짜별 폴더", self.app.option_summary.get())
        self.assertNotIn("GIF", self.app.option_summary.get())
        self.assertIn("전체 확인", self.app.option_summary.get())
        self.app._close_options()
        self.assertIsNone(self.app.options_window)

    def test_saved_options_persist_without_replacing_draft_account_or_folder(self):
        active = {"account": "saved_account", "output_dir": "C:/active-folder",
                  "record_locations": {"saved_account": "C:/active-folder"}, "appearance": "dark"}
        self.app.account.set("draft_account")
        self.app.folder.set(str(Path(self.temp.name) / "uncommitted-folder"))
        self.app.show_options()
        self.app.include_videos.set(False)
        self.app.max_video_mb.set("350")
        with patch("luvbox.gui.load_settings", return_value=active):
            self.app._close_options()
        saved = json.loads((Path(self.temp.name) / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual((saved["account"], saved["output_dir"], saved["record_locations"]),
                         ("saved_account", "C:/active-folder", {"saved_account": "C:/active-folder"}))
        self.assertEqual((saved["include_videos"], saved["max_video_mb"], saved["appearance"]), (False, 350, "dark"))
        self.assertFalse((Path(self.temp.name) / "uncommitted-folder").exists())

    def test_cancel_options_restores_choices_and_does_not_persist(self):
        self.app.show_options()
        self.app.include_gifs.set(False)
        self.app.folder_mode.set("작성자별 폴더")
        self.app.max_video_mb.set("-1")
        self.app._close_options(save=False, restore=True)
        self.assertIsNone(self.app.options_window)
        self.assertTrue(self.app.include_gifs.get())
        self.assertEqual((self.app.folder_mode.get(), self.app.max_video_mb.get()), ("구분 없이 저장", "200"))
        self.assertFalse((Path(self.temp.name) / "settings.json").exists())

    def test_invalid_options_stay_open_and_irrelevant_gif_fields_are_disabled(self):
        self.app.show_options()
        self.app.max_video_mb.set("-1")
        with patch("luvbox.gui.dialogs.showerror") as error:
            self.app._close_options()
        error.assert_called_once()
        self.assertTrue(self.app.options_window.winfo_exists())
        self.assertEqual(self.app.option_tabs.index("current"), 1)
        self.assertFalse((Path(self.temp.name) / "settings.json").exists())
        self.app.include_gifs.set(False)
        self.assertEqual(str(self.app.gif_size_combo.cget("state")), "disabled")
        self.app.include_gifs.set(True)
        self.app.gif_format.set("원본 MP4 그대로 저장")
        self.assertEqual(str(self.app.gif_size_combo.cget("state")), "disabled")
        self.app.gif_format.set("GIF로 바꿔 저장")
        self.assertEqual(str(self.app.gif_size_combo.cget("state")), "readonly")

    def test_options_window_close_cancels_without_saving(self):
        self.app.show_options()
        window = self.app.options_window
        self.app.include_gifs.set(False)
        self.root.tk.call(window.protocol("WM_DELETE_WINDOW"))
        self.assertIsNone(self.app.options_window)
        self.assertTrue(self.app.include_gifs.get())
        self.assertFalse((Path(self.temp.name) / "settings.json").exists())

    def test_draft_options_block_other_actions_until_saved_or_cancelled(self):
        self.app.account.set("offline_test")
        self.app.folder.set(self.temp.name)
        self.session_mock.return_value = True
        self.app.show_options()
        self.app.include_gifs.set(False)
        self.assertIs(self.app.options_window.grab_current(), self.app.options_window)
        with patch("luvbox.gui.filedialog.askdirectory") as folder, \
                patch("luvbox.gui.threading.Thread") as worker, \
                patch.object(self.app, "_save_settings") as save:
            self.app.show_history()
            self.app.choose_folder()
            self.app.start()
            self.app.register_schedule()
            self.assertIsNone(self.app.history_window)
            self.assertFalse(self.app.running)
            self.assertFalse(self.app.relocating)
            folder.assert_not_called()
            worker.assert_not_called()
            save.assert_not_called()
        self.app._close_options(save=False, restore=True)
        self.assertTrue(self.app.include_gifs.get())
        self.assertIsNone(self.root.grab_current())
        self.assertFalse((Path(self.temp.name) / "settings.json").exists())

    def test_history_shortcut_does_not_change_account_text(self):
        self.app.account.set("offline_test")
        self.root.deiconify()
        self.root.update()
        self.app.account_entry.focus_force()
        self.root.update()
        self.app.account_entry.icursor("end")
        with patch.object(self.app, "show_history") as history:
            self.app.account_entry.event_generate("<F6>")
            self.root.update()
        history.assert_called_once()
        self.assertEqual(self.app.account.get(), "offline_test")

    def test_stop_after_final_result_does_not_replace_completion(self):
        self.app.running = True
        self.app._set_running(True)
        self.app.events.put({"type": "finished", "status": "finished", "saved": 3})
        self.app._poll()
        self.app.stop()
        self.assertFalse(self.app.stopping)
        self.app.events.put({"type": "process_exit", "code": 0})
        self.app._poll()
        self.assertFalse(self.app.running)
        self.assertIn("완료", self.app.status.get())
        self.assertEqual(self.app.progress.cget("value"), 100)

    def test_final_worker_result_wins_over_a_late_stop_request(self):
        self.app.running = True
        self.app.stop()
        self.app.events.put({"type": "finished", "status": "finished", "saved": 3})
        self.app.events.put({"type": "process_exit", "code": 0})
        self.app._poll()
        self.assertIn("완료", self.app.status.get())
        self.assertEqual(self.app.progress.cget("value"), 100)

    def test_schedule_requires_saved_connection_and_valid_media_limit(self):
        self.app.account.set("offline_test")
        self.app.folder.set(self.temp.name)
        self.app.scan_mode.set("전체 목록 다시 확인")
        self.app.limited.set(True)
        self.assertIn("전체 확인", self.app.option_summary.get())
        self.assertIn("시험 20개", self.app.option_summary.get())
        self.assertIn("새 Like만", self.app.scheduled_option_summary.get())
        self.assertNotIn("전체 확인", self.app.scheduled_option_summary.get())
        self.assertNotIn("시험", self.app.scheduled_option_summary.get())
        with patch("luvbox.scheduled.register_task") as register, patch.object(self.app, "_schedule_job") as job:
            self.app.register_schedule()
            self.assertTrue(self.app.connect_window.winfo_exists())
            self.assertIn("로그인", self.app.status.get())
            job.assert_not_called()
            register.assert_not_called()
            self.app._close_connect()
            self.session_mock.return_value = True
            self.app.max_video_mb.set("-1")
            with patch("luvbox.gui.dialogs.showerror") as error:
                self.app.register_schedule()
            error.assert_called_once()
            job.assert_not_called()
        self.assertFalse((Path(self.temp.name) / "settings.json").exists())

    def test_failed_worker_exit_never_displays_completed_progress(self):
        self.app.running = True
        self.app.events.put({"type": "finished", "status": "finished", "saved": 2})
        self.app.events.put({"type": "process_exit", "code": 1})
        self.app._poll()
        self.assertIn("오류", self.app.status.get())
        self.assertEqual(self.app.progress.cget("value"), 0)
        self.assertEqual(str(self.app.stop_button.cget("state")), "disabled")

    def test_reading_older_log_entries_does_not_jump_to_the_latest(self):
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        self.root.update()
        for number in range(80):
            self.app._log(f"작업 {number}")
        self.app.content_view.canvas.yview_moveto(1)
        self.root.update()
        self.app.log.yview_moveto(0)
        self.root.update()
        self.app._log("새 작업")
        self.root.update()
        self.assertEqual(self.app.log.yview()[0], 0)
        self.assertIn("새 작업", self.app.log.get("1.0", "end"))
        self.app._clear_log()
        self.assertEqual(self.app.log.get("1.0", "end").strip(), "")
        self.assertEqual(str(self.app.log.cget("state")), "disabled")
        self.assertTrue(self.app.log_placeholder.winfo_manager())

    def test_options_fit_short_screen_and_close_stays_reachable(self):
        self.root.attributes("-alpha", 0)
        with patch.object(self.root, "winfo_screenheight", return_value=768):
            self.app.show_options()
        window = self.app.options_window
        window.attributes("-alpha", 0)
        self.root.deiconify()
        window.update()
        self.assertLessEqual(window.winfo_height(), 648)
        view = window.content_view
        self.assertLess(view.canvas.yview()[1], 1)
        view.canvas.yview_moveto(1)
        window.update()
        close = next(widget for widget in window.footer.winfo_children()
                     if widget.winfo_class() == "TButton" and widget.cget("text") == "설정 저장")
        top = close.winfo_rooty() - window.winfo_rooty()
        self.assertGreaterEqual(top, 0)
        self.assertLessEqual(top + close.winfo_height(), window.winfo_height())

    def test_invalid_video_limit_stops_before_worker(self):
        self.app.account.set("offline_test")
        self.session_mock.return_value = True
        for invalid in ("bad", "1.5", "-1"):
            self.app.max_video_mb.set(invalid)
            with patch("luvbox.gui.dialogs.showerror") as error, patch("luvbox.gui.threading.Thread") as thread:
                self.app.start()
            error.assert_called_once()
            thread.assert_not_called()

    def test_progress_reports_current_file_without_inventing_overall_completion(self):
        self.app.events.put({"type": "progress", "filename": "100_video_1.mp4", "tweet_id": "100", "media_type": "video", "author": "alice", "posted_at": "2020-01-02T00:30:00+09:00", "downloaded": 50, "total": 100, "speed": 25})
        self.app._poll()
        self.assertEqual(str(self.app.progress.cget("mode")), "determinate")
        self.assertEqual(self.app.progress.cget("value"), 50)
        self.assertIn("50%", self.app.progress_summary.get())
        self.assertIn("alice", self.app.progress_details.get())
        self.assertIn("지금 받는 파일의 진행률", self.app.progress_details.get())
        self.app.events.put({"type": "progress", "filename": "101_1.jpg", "downloaded": 123, "total": 0})
        self.app._poll()
        self.assertEqual(str(self.app.progress.cget("mode")), "indeterminate")
        self.assertIn("전체 용량 미확인", self.app.progress_summary.get())
        self.assertNotIn("%", self.app.progress_summary.get())

    def test_size_skips_do_not_increment_saved_or_existing_counts(self):
        self.app.events.put({"type": "size-skipped", "filename": "100_video_1.mp4"})
        self.app._poll()
        self.assertEqual((self.app.saved, self.app.skipped, self.app.size_skipped), (0, 0, 1))
        self.assertIn("용량 초과 1개", self.app.counts.get())

    def test_detail_view_can_toggle_during_a_download(self):
        self.app.running = True
        self.app.detailed_progress.set(True)
        self.app._toggle_progress()
        self.assertEqual(self.app.progress_detail_label.winfo_manager(), "pack")
        self.app.detailed_progress.set(False)
        self.app._toggle_progress()
        self.assertEqual(self.app.progress_detail_label.winfo_manager(), "")

    def test_old_snapshot_and_test_settings_do_not_limit_normal_saving(self):
        other_root = tk.Tk()
        other_root.withdraw()
        self.addCleanup(destroy_tk, other_root)
        with patch("luvbox.gui.load_settings", return_value={"sync_mode": "new", "scan_mode": "bogus", "photo_limit": 20, "date_from": "2026-10-02"}):
            app = App(other_root)
        self.assertFalse(app.limited.get())
        self.assertFalse(hasattr(app, "incremental"))
        app.account.set("offline_test")
        app.folder.set(self.temp.name)
        self.session_mock.return_value = True
        with patch("luvbox.gui.threading.Thread") as thread, patch("luvbox.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
            app.start()
        options = thread.call_args.kwargs["args"][0]
        self.assertEqual((options.scan_mode, options.photo_limit), ("quick", 0))
        self.assertFalse(hasattr(options, "sync_mode") or hasattr(options, "date_from"))

    def test_invalid_account_stops_before_worker(self):
        self.app.account.set("bad account")
        self.session_mock.return_value = True
        with patch("luvbox.gui.dialogs.showerror") as error, patch("luvbox.gui.threading.Thread") as thread:
            self.app.start()
        self.assertTrue(error.called)
        thread.assert_not_called()

    def test_browser_connection_runs_off_ui_thread_and_blocks_duplicate_actions(self):
        self.app.show_connect()
        self.assertFalse(self.app.manual_controls.winfo_manager())
        with patch("luvbox.gui.threading.Thread") as thread:
            self.app.connect_browser()
            self.app.connect_browser()
        thread.assert_called_once()
        self.assertTrue(self.app.login_busy)
        self.assertFalse(self.app.start())
        with patch("luvbox.gui.save_auth_token") as save:
            self.assertFalse(self.app.connect_token())
        save.assert_not_called()
        args = thread.call_args.kwargs["args"]
        self.assertIs(args[0], self.app.events)
        self.assertIsInstance(args[1], threading.Event)
        with patch("luvbox.browser_login.sign_in") as sign_in:
            thread.call_args.kwargs["target"](*args)
        sign_in.assert_called_once_with(self.app.login_cancel)
        self.session_mock.return_value = True
        self.app._poll()
        self.assertFalse(self.app.login_busy)
        self.assertIsNone(self.app.connect_window)
        self.assertIn("로그인을 연결", self.app.status.get())

    def test_closing_login_popup_cancels_and_error_does_not_leak_exception(self):
        self.app.show_connect()
        with patch("luvbox.gui.threading.Thread"):
            self.app.connect_browser()
        cancel = self.app.login_cancel
        self.app._close_connect()
        self.assertTrue(cancel.is_set())
        secret = "offline-secret-in-exception"
        with patch("luvbox.browser_login.sign_in", side_effect=OSError(secret)):
            App._browser_worker(self.app.events, cancel)
        self.app._poll()
        self.assertFalse(self.app.login_busy)
        self.assertNotIn(secret, self.app.status.get())

    def test_app_close_waits_for_login_cleanup(self):
        self.app.login_busy = True
        with patch.object(self.root, "destroy") as destroy, patch.object(self.root, "after") as after:
            self.app.close()
        destroy.assert_not_called()
        self.assertTrue(self.app.login_cancel.is_set())
        self.assertTrue(self.app.closing)
        after.assert_called_once_with(150, self.app.close)

    def test_app_close_stops_deletion_and_waits_for_its_result_before_destroying_tk(self):
        library = SimpleNamespace(winfo_exists=lambda: True, metadata_active=False, delete_active=True,
                                  choice_active=False, repair_active=False, stop_metadata=Mock())
        self.app.history_window = library
        with patch.object(self.root, "destroy") as destroy, patch.object(self.root, "after") as after:
            self.app.close()
            library.stop_metadata.assert_called_once()
            destroy.assert_not_called()
            after.assert_called_once_with(150, self.app.close)
            self.assertTrue(self.app.closing)
            library.delete_active = False
            self.app.close()
            destroy.assert_called_once()

    def test_library_deletion_blocks_a_new_download_before_login_or_worker_creation(self):
        self.app.history_window = SimpleNamespace(winfo_exists=lambda: True, metadata_active=False,
                                                  delete_active=True, choice_active=False, repair_active=False)
        with patch("luvbox.gui.threading.Thread") as worker, \
                patch("luvbox.gui.dialogs.showinfo") as info:
            self.assertFalse(self.app.start())
        worker.assert_not_called()
        info.assert_called_once()
        self.assertEqual(info.call_args.args[0], "기록 화면 작업 중")
        self.assertFalse(self.app.running)
        self.assertIsNone(self.app.connect_window)

    def test_token_connection_masks_and_clears_input_without_launching_worker(self):
        self.app.saved, self.app.skipped = 7, 3
        secret = "offline-secret-not-an-account-cookie"
        self.app.show_connect()
        self.app.token.set(secret)
        self.assertTrue(self.app.token_entry.cget("show"))
        with patch("luvbox.gui.save_auth_token") as save, patch("luvbox.gui.threading.Thread") as thread:
            self.assertTrue(self.app.connect_token())
        save.assert_called_once_with(secret)
        thread.assert_not_called()
        self.assertEqual(self.app.token.get(), "")
        self.assertNotIn(secret, self.app.log.get("1.0", "end"))
        self.assertIn("실제 유효성", self.app.status_detail.get())
        self.assertEqual((self.app.saved, self.app.skipped), (7, 3))
        self.assertFalse(self.app.running)
        self.assertEqual(str(self.app.options_button.cget("state")), "normal")

    def test_failed_token_connection_clears_input_and_does_not_start_download(self):
        self.session_mock.return_value = True  # A previous saved connection exists.
        secret = "offline-invalid-new-token"
        self.app.token.set(secret)
        with patch("luvbox.gui.save_auth_token", side_effect=ValueError("인증 토큰을 확인해 주세요.")), patch("luvbox.gui.dialogs.showerror") as error, patch("luvbox.gui.threading.Thread") as thread:
            self.app.start()
        error.assert_called_once()
        thread.assert_not_called()
        self.assertFalse(self.app.running)
        self.assertEqual(self.app.token.get(), "")
        self.assertNotIn(secret, self.app.log.get("1.0", "end"))

    def test_pending_token_is_saved_before_download_without_entering_worker_options(self):
        self.app.account.set("offline_test")
        self.app.folder.set(self.temp.name)
        self.app.token.set("offline-test-token")
        self.session_mock.return_value = True
        with patch("luvbox.gui.save_auth_token") as save, patch("luvbox.gui.threading.Thread") as thread, patch("luvbox.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
            self.app.start()
        save.assert_called_once_with("offline-test-token")
        options = thread.call_args.kwargs["args"][0]
        self.assertEqual(options.auth_mode, "managed")
        self.assertFalse(hasattr(options, "token"))
        self.assertEqual(self.app.token.get(), "")
        self.assertNotIn("offline-test-token", (Path(self.temp.name) / "settings.json").read_text())

    def test_only_two_modes_and_old_browser_settings_fall_back_to_token(self):
        self.assertEqual(set(AUTH_MODES.values()), {"managed", "file"})
        for old_mode in ("chrome", "edge", "firefox"):
            window = tk.Toplevel(self.root)
            with patch("luvbox.gui.load_settings", return_value={"auth_mode": old_mode}):
                app = App(window)
            self.assertEqual(AUTH_MODES[app.auth.get()], "managed")
            window.destroy()

    def test_switch_to_file_hides_and_clears_token_input(self):
        self.app.show_connect()
        self.app.token.set("offline-test-token")
        self.app.auth.set(next(k for k, v in AUTH_MODES.items() if v == "file"))
        self.assertEqual(self.app.token.get(), "")
        self.assertEqual(self.app.token_controls.winfo_manager(), "")
        self.assertEqual(self.app.cookie_controls.winfo_manager(), "pack")
        self.assertIn("cookies.txt", self.app.login_text.get())
        self.app._close_connect()
        self.assertIsNone(self.app.connect_window)

    def test_disconnect_clears_pending_input_and_stored_session(self):
        self.app.token.set("offline-test-token")
        with patch("luvbox.gui.clear_session") as clear:
            self.app.disconnect()
        clear.assert_called_once()
        self.assertEqual(self.app.token.get(), "")

    def test_abnormal_exit_does_not_look_successful(self):
        self.app.events.put({"type": "finished", "status": "finished", "saved": 7, "skipped": 3, "message": "완료"})
        self.app.events.put({"type": "process_exit", "code": 1})
        self.app._poll()
        self.assertIn("오류로 종료", self.app.status.get())
        self.assertEqual((self.app.saved, self.app.skipped), (7, 3))

    def test_worker_command_has_only_download_branch(self):
        args = worker_command(Path("request"), Path("events"), Path("stop"))
        self.assertEqual(args[-4:], ["--worker", "request", "events", "stop"])

    def test_removed_login_command_is_rejected_without_launching_ui(self):
        import entry
        with patch("sys.argv", ["entry.py", "--login", "request", "events", "stop"]):
            self.assertEqual(entry.main(), 2)

    def test_file_log_lines_include_their_own_author_and_posted_date(self):
        folder = Path(self.temp.name) / "아주 긴 저장 폴더 이름"
        cases = (("saved", "alice", "2020-01-02", "저장 · "),
                 ("skipped", "bob", "2021-03-04", "이미 저장 · "),
                 ("skipped", "dave", "2021-03-05", "이미 저장 · "),
                 ("size-skipped", "carol", "2022-05-06", "용량 초과로 건너뜀 · "))
        for kind, author, day, _ in cases:
            self.app.events.put({"type": kind, "author": author, "posted_at": day + "T10:20:30+09:00",
                                 "tweet_id": "123", "filename": "123_1.jpg", "path": str(folder / "123_1.jpg")})
        self.app.events.put({"type": "log", "message": "전체 목록을 확인합니다."})
        self.app._poll()
        lines = self.app.log.get("1.0", "end").strip().splitlines()
        self.assertEqual(len(lines), 5)
        # Every file, including each already saved one, has its own line.
        for line, (_, author, day, action) in zip(lines, cases):
            self.assertIn(action + "123_1.jpg · @" + author, line)
            self.assertIn("게시 " + day + " 10:20", line)
            self.assertNotIn(str(folder), line)
        self.assertNotIn("@", lines[4])
        self.assertEqual((self.app.saved, self.app.skipped, self.app.size_skipped), (1, 2, 1))

    def test_gif_options_are_saved_summarized_and_sent_to_the_worker(self):
        from luvbox import gui
        self.assertEqual(self.app._gif_options(), ("original", 200))
        self.app.gif_size.set("최대 480px · 초당 12장 (가장 작게)")
        self.assertIn("GIF 480px", self.app.option_summary.get())
        self.assertEqual(self.app._gif_options(), ("480", 200))
        self.app.gif_format.set("원본 MP4 그대로 저장")
        self.assertIn("GIF(MP4 그대로)", self.app.option_summary.get())
        target = Path(self.temp.name) / "settings.json"
        self.app.account.set("offline_test")
        self.app.folder.set(self.temp.name)
        self.session_mock.return_value = True
        with patch("luvbox.gui.threading.Thread") as thread, patch("luvbox.gui.settings_path", return_value=target):
            self.app.start()
        options = thread.call_args.kwargs["args"][0]
        self.assertEqual((options.gif_format, options.gif_size), ("mp4", "480"))
        saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual((saved["gif_format"], saved["gif_size"]), ("mp4", "480"))
        other_root = tk.Tk()
        other_root.withdraw()
        self.addCleanup(destroy_tk, other_root)
        with patch("luvbox.gui.load_settings", return_value=saved):
            restored = gui.App(other_root)
        self.assertEqual((restored.gif_format.get(), restored.gif_size.get()),
                         ("원본 MP4 그대로 저장", "최대 480px · 초당 12장 (가장 작게)"))

    def test_conversion_fallback_tells_the_original_mp4_is_saved(self):
        self.app.events.put({"type": "converting", "stage": "fallback", "author": "alice",
                             "posted_at": "2020-01-02T10:20:30+09:00", "filename": "123_gif_1.mp4"})
        self.app.events.put({"type": "log", "level": "warning", "message": "GIF로 바꾸지 못해 원본 MP4로 저장합니다.",
                             "author": "alice", "posted_at": "2020-01-02T10:20:30+09:00",
                             "tweet_id": "123", "filename": "123_gif_1.mp4"})
        self.app._poll()
        # A cold formatter import can consume one UI slice. Let the tracked
        # poll deliver the following log event without requiring one long tick.
        drained = tk.BooleanVar(self.root, value=False)
        def check_queue():
            if self.app.events.empty() and self.app._deferred_event is None:
                drained.set(True)
            else:
                self.root.after(8, check_queue)
        timeout = self.root.after(1000, lambda: drained.set(True))
        self.root.after(0, check_queue)
        self.root.wait_variable(drained)
        self.root.after_cancel(timeout)
        self.assertIn("원본 MP4", self.app.progress_summary.get())
        self.assertIn("원본 MP4", self.app.status.get())
        self.assertIn("원본 MP4로 저장합니다 · 123_gif_1.mp4 · @alice", self.app.log.get("1.0", "end"))

    def test_conversion_progress_does_not_claim_the_file_is_already_saved(self):
        self.app.events.put({"type": "converting", "stage": "started", "author": "alice",
                             "posted_at": "2020-01-02T10:20:30+09:00", "filename": "123_gif_1.mp4"})
        self.app._poll()
        self.assertEqual(self.app.progress.cget("mode"), "indeterminate")
        self.assertIn("GIF로 변환", self.app.status.get())
        self.assertIn("alice", self.app.progress_details.get())
        self.assertEqual(self.app.saved, 0)

    def test_invalid_log_author_cannot_inject_another_line(self):
        self.app._log("저장", {"tweet_id": "123", "author": "alice\nfake", "posted_at": "invalid"})
        content = self.app.log.get("1.0", "end")
        self.assertIn("작성자 미확인", content)
        self.assertNotIn("fake", content)
        self.assertEqual(len(content.strip().splitlines()), 1)

    def test_theme_can_switch_during_work_and_preserves_unedited_settings(self):
        from luvbox import theme
        original = {"account": "remembered", "output_dir": "old-folder", "future_option": 7}
        target = Path(self.temp.name) / "settings.json"
        self.app.running = True
        with patch("luvbox.gui.load_settings", return_value=original.copy()), \
                patch("luvbox.gui.settings_path", return_value=target):
            self.app.toggle_theme()
            self.root.update_idletasks()
            saved = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(saved, {**original, "appearance": "dark"})
            self.assertEqual(self.app.appearance.get(), "dark")
            self.assertEqual(self.app.log.cget("background"), theme.CARD)
            self.assertEqual(str(self.app.theme_button.cget("state")), "normal")
            self.app.toggle_theme()
            self.assertEqual(self.app.appearance.get(), "light")

    def test_persisted_dark_theme_is_loaded_without_starting_a_worker(self):
        from luvbox import theme
        with patch("luvbox.gui.load_settings", return_value={"appearance": "dark"}), \
                patch("luvbox.gui.threading.Thread") as worker:
            window = tk.Toplevel(self.root)
            app = App(window)
            self.root.update_idletasks()
            self.assertEqual(app.appearance.get(), "dark")
            self.assertEqual(app.log.cget("background"), theme.CARD)
            self.assertEqual(app.theme_button_text.get(), "라이트 테마")
            worker.assert_not_called()
            window.destroy()
        theme.apply(self.root, "light")


class ScheduleWorkerLifetimeTests(unittest.TestCase):
    def test_delayed_schedule_result_does_not_retain_closed_app_or_tk(self):
        root = app = None
        enabled = gc.isenabled()
        gc.disable()
        try:
            with patch("luvbox.gui.load_settings", return_value={}), \
                    patch("luvbox.gui.has_session", return_value=False):
                root = tk.Tk()
                root.withdraw()
                app = App(root)
            events = app.events
            refs = [weakref.ref(value) for value in (app, root, app.status)]
            with patch("luvbox.gui.threading.Thread") as worker:
                app._schedule_job("status", lambda: None, prompt=False)
                target = worker.call_args.kwargs["target"]
            destroy_tk(root)
            root = app = None
            self.assertTrue(all(ref() is None for ref in refs), "Scheduler worker retained closed Tk resources")
            with patch("luvbox.scheduled.task_status", return_value={"registered": False}):
                target()
            self.assertEqual(events.get_nowait(), {"type": "schedule", "action": "status",
                                                   "result": {"registered": False}, "prompt": False})
        finally:
            if root is not None:
                destroy_tk(root)
            root = app = None
            if enabled:
                gc.enable()
            gc.collect()


# A worker that pauses in the middle of a Korean character, as a line can be
# read while the worker is still writing it.
_SPLIT_WRITER = r"""
import sys, time
line = '{"type": "log", "level": "info", "message": "한글 진행 상황"}\n'.encode("utf-8")
cut = line.index("글".encode("utf-8")) + 1
with open(sys.argv[2], "ab", buffering=0) as stream:
    stream.write(line[:cut])
    time.sleep(0.5)
    stream.write(line[cut:])
    stream.write('{"type": "finished", "status": "finished", "saved": 1, "message": "완료"}\n'.encode("utf-8"))
"""


class StartupMigrationTests(unittest.TestCase):
    def test_failed_data_migration_explains_and_never_enters_main_loop(self):
        from luvbox.gui import launch
        from luvbox.settings import AppDataError

        with patch("luvbox.gui.tk.Tk") as create_root, \
                patch("luvbox.gui.App", side_effect=AppDataError("기존 데이터를 보존했습니다")), \
                patch("luvbox.gui.dialogs.showerror") as notice:
            launch()
        create_root.return_value.mainloop.assert_not_called()
        create_root.return_value.destroy.assert_called_once_with()
        notice.assert_called_once_with("기존 데이터 이전 확인", "기존 데이터를 보존했습니다",
                                       parent=create_root.return_value)


    def test_old_build_data_conflict_opens_the_app_only_after_the_person_moves_it_aside(self):
        from luvbox.gui import launch
        from luvbox.settings import AppDataConflict

        for choice in (True, None):
            with self.subTest(choice=choice), patch("luvbox.gui.tk.Tk") as create_root, \
                    patch("luvbox.gui.App", side_effect=[AppDataConflict("두 데이터 폴더"), object()]) as app, \
                    patch("luvbox.gui.dialogs.Dialog") as dialog, \
                    patch("luvbox.settings.set_aside_previous") as set_aside:
                dialog.return_value.run.return_value = choice
                launch()
            self.assertIn("두 데이터 폴더", dialog.call_args.args[2])
            if choice:
                set_aside.assert_called_once_with()
                self.assertEqual(app.call_count, 2)
                create_root.return_value.mainloop.assert_called_once_with()
            else:
                set_aside.assert_not_called()
                self.assertEqual(app.call_count, 1)
                create_root.return_value.mainloop.assert_not_called()
                create_root.return_value.destroy.assert_called_once_with()


    def test_closing_the_unfinished_window_during_a_data_dialog_exits_quietly(self):
        from luvbox.gui import launch
        from luvbox.settings import AppDataConflict, AppDataError

        for error in (AppDataConflict("두 데이터 폴더"), AppDataError("기존 데이터를 보존했습니다")):
            with self.subTest(error=type(error).__name__), patch("luvbox.gui.tk.Tk") as create_root, \
                    patch("luvbox.gui.App", side_effect=error), \
                    patch("luvbox.gui.dialogs.Dialog") as dialog, patch("luvbox.gui.dialogs.showerror"):
                dialog.return_value.run.return_value = None
                # The person closed the empty main window behind the dialog.
                create_root.return_value.destroy.side_effect = tk.TclError("application has been destroyed")
                launch()
                create_root.return_value.mainloop.assert_not_called()


class WorkerEventReaderTests(unittest.TestCase):
    def test_partially_written_character_never_stops_progress_updates(self):
        import queue
        import sys
        from luvbox.core import JobOptions

        app = App.__new__(App)
        app.events, app.stopping, app.stop_path, app.process = queue.Queue(), False, None, None
        from luvbox.review import ReviewControl
        app.review_control = ReviewControl(False)
        command = lambda request, events, stop: [sys.executable, "-c", _SPLIT_WRITER, str(request), str(events), str(stop)]
        with tempfile.TemporaryDirectory() as folder, patch("luvbox.gui.worker_command", command):
            app._run_process(JobOptions(account="example", output_dir=folder))
        events = []
        while not app.events.empty():
            events.append(app.events.get_nowait())
        self.assertEqual([event.get("message") for event in events if event["type"] == "log"], ["한글 진행 상황"])
        self.assertEqual([event["type"] for event in events], ["log", "finished", "process_exit"])
        self.assertEqual(events[-1]["code"], 0)


if __name__ == "__main__":
    unittest.main()
