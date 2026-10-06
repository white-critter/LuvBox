"""Native UI workflow checks without account or browser access."""

import gc
import json
import os
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
import weakref
from unittest.mock import patch

from xlikesaver.gui import App, AUTH_MODES, worker_command


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
        self.guards = patch("xlikesaver.protection.state_directory", return_value=Path(self.temp.name) / "locks")
        self.guards.start()
        self.addCleanup(self.guards.stop)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(release_tk, self, "app", "root")
        self.addCleanup(destroy_tk, self.root)
        self.session = patch("xlikesaver.gui.has_session", return_value=False)
        self.session_mock = self.session.start()
        self.addCleanup(self.session.stop)
        self.settings = patch("xlikesaver.gui.load_settings", return_value={})
        self.settings.start()
        self.addCleanup(self.settings.stop)
        preferences = patch("xlikesaver.gui.settings_path", return_value=Path(self.temp.name) / "settings.json")
        preferences.start()
        self.addCleanup(preferences.stop)
        self.app = App(self.root)

    def test_unconnected_token_mode_requests_connection_before_download(self):
        with patch("xlikesaver.gui.threading.Thread") as thread:
            self.app.start()
        # The login window opens with the reason instead of a separate message box.
        self.assertTrue(self.app.connect_window.winfo_exists())
        self.assertIn("로그인", self.app.status.get())
        thread.assert_not_called()
        self.assertFalse(self.app.running)
        self.assertIn("연결 필요", self.app.login_text.get())

    def test_connection_status_can_grow_after_a_short_disconnected_label(self):
        import tkinter.font as tkfont
        from xlikesaver import theme
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
        from xlikesaver.history import HistoryStore
        from xlikesaver.storage import resolve_storage
        source = resolve_storage(Path(self.temp.name) / "이전 폴더", "offline_test")
        source.output_dir.mkdir()
        media = source.output_dir / "100_1.jpg"
        media.write_bytes(b"original-media")
        store = HistoryStore.for_storage(source)
        try:
            store.record_media("flat", {"tweet_id": "100", "num": 1, "type": "photo",
                                        "_xlikesaver_metadata_status": "embedded"}, media)
        finally:
            store.close()
        with closing(sqlite3.connect(source.record_dir / ".xlikesaver-archive.flat.sqlite3")) as db:
            db.execute("CREATE TABLE archive(entry TEXT PRIMARY KEY)")
            db.execute("INSERT INTO archive VALUES ('twitter100_1')")
            db.commit()
        self.app.account.set("offline_test")
        self.app.folder.set(str(source.output_dir))
        self.app.record_locations["offline_test"] = str(source.output_dir)
        return source, Path(self.temp.name) / "새 폴더"

    def test_folder_choice_carries_records_and_remembers_new_location(self):
        from xlikesaver.history import HistoryStore
        from xlikesaver.storage import resolve_storage
        source, target = self.relocation_source()
        with patch("xlikesaver.gui.filedialog.askdirectory", return_value=str(target)), \
                patch("xlikesaver.gui.threading.Thread") as thread, \
                patch("xlikesaver.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
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
        with patch("xlikesaver.gui.threading.Thread") as thread, \
                patch("xlikesaver.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
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
        with patch("xlikesaver.gui.threading.Thread") as thread, \
                patch("xlikesaver.storage.migrate_records", side_effect=OSError("private-error")), \
                patch("xlikesaver.gui.dialogs.showerror") as error:
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
        from xlikesaver.storage import resolve_storage
        def resolve(directory, account, split):
            if str(directory) == str(source.output_dir):
                raise ValueError("linked-source")
            return resolve_storage(directory, account, split)
        with patch("xlikesaver.gui.resolve_storage", side_effect=resolve), \
                patch("xlikesaver.gui.threading.Thread") as thread:
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
        with patch("xlikesaver.gui.dialogs.askyesno", return_value=False) as notice, \
                patch("xlikesaver.gui.threading.Thread") as thread:
            self.assertFalse(self.app._ensure_storage("start"))
        notice.assert_called_once()
        thread.assert_not_called()
        self.assertEqual(self.app.folder.get(), str(offline))
        self.assertFalse(target.exists())

    def test_explicit_fresh_start_preserves_destination_records_and_can_merge_old_records_later(self):
        from xlikesaver.history import HistoryStore
        from xlikesaver.storage import migrate_records, resolve_storage
        source, target = self.relocation_source()
        destination = resolve_storage(target, source.account)
        store = HistoryStore.for_storage(destination)
        file = target / "101_1.jpg"
        file.write_bytes(b"new-media")
        store.record_media("flat", {"tweet_id": "101", "num": 1, "type": "photo"}, file)
        store.close()
        self.app.record_locations[source.account] = str(source.output_dir.with_name("offline"))
        self.app.folder.set(str(target))
        with patch("xlikesaver.gui.dialogs.askyesno", return_value=True), \
                patch("xlikesaver.gui.threading.Thread") as thread, \
                patch("xlikesaver.storage.migrate_records") as migration, \
                patch("xlikesaver.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
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
        with patch("xlikesaver.gui.filedialog.askdirectory", return_value=self.temp.name), \
                patch("xlikesaver.gui.threading.Thread") as thread:
            self.app.choose_folder()
        thread.assert_not_called()
        self.assertFalse(self.app.relocating)
        self.assertNotIn("offline_test", self.app.record_locations)

    def test_preferences_failure_after_copy_keeps_previous_active_location(self):
        source, target = self.relocation_source()
        self.app.folder.set(str(target))
        with patch("xlikesaver.gui.threading.Thread"), patch.object(self.app, "_save_settings", side_effect=PermissionError), \
                patch("xlikesaver.gui.dialogs.showerror"):
            self.app._ensure_storage()
            self.app._finish_relocation({"ok": True})
        self.assertEqual(self.app.folder.get(), str(source.output_dir))
        self.assertEqual(self.app.record_locations["offline_test"], str(source.output_dir))

    def test_close_waits_for_relocation_to_finish(self):
        self.relocation_source()
        self.app.folder.set(str(Path(self.temp.name) / "new"))
        with patch("xlikesaver.gui.threading.Thread"), patch.object(self.root, "destroy") as destroy, \
                patch("xlikesaver.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
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
        with patch("xlikesaver.gui.threading.Thread") as thread, patch("xlikesaver.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
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
        with patch("xlikesaver.gui.threading.Thread") as thread, patch("xlikesaver.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
            self.app.start()
        options = thread.call_args.kwargs["args"][0]
        self.assertEqual((options.scan_mode, options.photo_limit), ("quick", 20))

    def test_full_scan_choice_reaches_worker_and_is_remembered(self):
        self.app.account.set("offline_test")
        self.app.folder.set(self.temp.name)
        self.session_mock.return_value = True
        self.app.scan_mode.set("전체 목록 다시 확인")
        with patch("xlikesaver.gui.threading.Thread") as thread, patch("xlikesaver.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
            self.app.start()
        self.assertEqual(thread.call_args.kwargs["args"][0].scan_mode, "full")
        persisted = json.loads((Path(self.temp.name) / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["scan_mode"], "full")
        other_root = tk.Tk()
        other_root.withdraw()
        self.addCleanup(destroy_tk, other_root)
        with patch("xlikesaver.gui.load_settings", return_value=persisted):
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
                          on_battery=True, next_run="2026-10-07T07:30:00", program="XlikeSaver.exe")

        def unregister():
            status.clear()
            status.update(registered=False)

        last = {"finished_at": "2026-10-06T09:00:05+09:00", "status": "finished", "saved": 3, "failed": False}
        return (patch("xlikesaver.gui.threading.Thread", Immediate),
                patch("xlikesaver.scheduled.task_status", side_effect=lambda: dict(status)),
                patch("xlikesaver.scheduled.register_task", side_effect=register),
                patch("xlikesaver.scheduled.unregister_task", side_effect=unregister),
                patch("xlikesaver.scheduled.last_run", return_value=last))

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
        with patch("xlikesaver.gui.settings_path", return_value=target):
            self.app.show_schedule()
            self.addCleanup(self.app._close_schedule)
            self.app._poll()
            self.assertIn("꺼져", self.app.schedule_state.get())
            self.assertIn("새로 저장 3개", self.app.schedule_last.get())
            self.assertEqual(self.app.schedule_on_button.cget("text"), "예약 켜기")
            self.app.schedule_time.set("7:30")
            self.app.register_schedule()
            self.app._poll()
        from xlikesaver import scheduled
        scheduled.register_task.assert_called_once_with("07:30")
        saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual((saved["account"], saved["schedule_time"]), ("offline_test", "07:30"))
        self.assertIn("매일 07:30 · 켜짐", self.app.schedule_text.get())
        self.assertEqual(self.app.schedule_on_button.cget("text"), "예약 갱신")
        self.assertIn("자동 저장을 켰습니다", self.app.status.get())
        with patch("xlikesaver.gui.dialogs.askyesno", return_value=True):
            self.app.unregister_schedule()
        self.app._poll()
        self.assertIn("꺼짐", self.app.schedule_text.get())
        self.assertEqual(str(self.app.schedule_off_button.cget("state")), "disabled")

    def test_schedule_needs_token_connection_and_valid_time(self):
        patches = self.schedule_patches({"registered": False})
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        from xlikesaver import scheduled
        self.app.auth.set("cookies.txt 파일")
        with patch("xlikesaver.gui.dialogs.showinfo") as notice:
            self.app.register_schedule()
        notice.assert_called_once()
        self.app.auth.set("인증 토큰 (권장)")
        self.app.account.set("offline_test")
        self.app.folder.set(self.temp.name)
        self.app.schedule_time.set("25:00")
        with patch("xlikesaver.gui.dialogs.showerror") as error:
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
        old = {"registered": True, "current": False, "program": r"C:\old\XlikeSaver-1.7.1.exe", "time": "08:00",
               "enabled": True, "catch_up": False, "on_battery": False, "next_run": ""}
        patches = self.schedule_patches(dict(old))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        from xlikesaver import scheduled
        self.app.account.set("offline_test")
        self.app.folder.set(self.temp.name)
        with patch("xlikesaver.gui.dialogs.askyesno", return_value=True) as ask, \
                patch("xlikesaver.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"), \
                patch("xlikesaver.gui.load_settings", return_value={}):
            self.app._schedule_result({"type": "schedule", "action": "status", "prompt": True, "result": old})
            self.app._poll()
            self.assertIn("XlikeSaver-1.7.1.exe", ask.call_args.args[1])
            scheduled.register_task.assert_called_once_with("08:00")
            self.app._schedule_result({"type": "schedule", "action": "status", "prompt": True, "result": old})
        ask.assert_called_once()
        failed = {"finished_at": "2026-10-06T09:00:05+09:00", "status": "error", "saved": 0, "failed": True,
                  "seen": False, "message": "로그인 연결을 다시 해 주세요."}
        with patch("xlikesaver.scheduled.last_run", return_value=failed), \
                patch("xlikesaver.scheduled.mark_last_run_seen") as seen, \
                patch.object(self.app, "refresh_schedule"):
            self.app._startup_schedule_check()
        seen.assert_called_once()
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
        with patch("xlikesaver.gui.threading.Thread") as thread, patch("xlikesaver.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
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
        self.assertEqual(paths.snapshot_dir, directory / ".xlikesaver" / "offline_test")
        self.assertTrue(paths.record_dir.is_relative_to(Path(self.temp.name) / "records"))
        with patch("xlikesaver.gui.os.startfile") as opened:
            self.app.open_folder()
        opened.assert_called_once_with(str(directory))
        self.app.separate_accounts.set(True)
        self.assertEqual(self.app.destination_preview.get(), str(directory / "offline_test"))
        with patch("xlikesaver.gui.os.startfile") as opened:
            self.app.open_folder()
        opened.assert_called_once_with(str(directory / "offline_test"))
        self.app.folder_mode.set("저장일별 폴더")
        self.assertEqual(self.app.destination_preview.get(), str(directory / "offline_test" / "YYYY-MM-DD"))

    def test_account_folder_option_reaches_worker_and_is_remembered(self):
        self.app.account.set("offline_test")
        self.app.folder.set(self.temp.name)
        self.app.separate_accounts.set(True)
        self.session_mock.return_value = True
        with patch("xlikesaver.gui.threading.Thread") as thread, patch("xlikesaver.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
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
        self.app.content_view.canvas.yview_moveto(1)
        self.root.update()
        for widget in (self.app.progress_detail_label, self.app.log):
            top = widget.winfo_rooty() - self.root.winfo_rooty()
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(top + widget.winfo_height(), self.root.winfo_height())

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
        self.assertLessEqual(label.winfo_reqwidth(), label.winfo_width() + 2)
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
        app.start_button.event_generate("<MouseWheel>", delta=-120)
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
        app.log.event_generate("<FocusIn>")
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
        with patch("xlikesaver.gui.dialogs.showerror"), patch("xlikesaver.gui.threading.Thread") as thread:
            self.app.start()
            thread.assert_not_called()
        self.assertTrue(self.app.limited.get())
        self.app.max_video_mb.set("200")
        with patch("xlikesaver.gui.threading.Thread") as thread, \
                patch("xlikesaver.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
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
        with patch("xlikesaver.gui.load_settings", return_value=active):
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
        with patch("xlikesaver.gui.dialogs.showerror") as error:
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
        with patch("xlikesaver.gui.filedialog.askdirectory") as folder, \
                patch("xlikesaver.gui.threading.Thread") as worker, \
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
        with patch("xlikesaver.scheduled.register_task") as register, patch.object(self.app, "_schedule_job") as job:
            self.app.register_schedule()
            self.assertTrue(self.app.connect_window.winfo_exists())
            self.assertIn("로그인", self.app.status.get())
            job.assert_not_called()
            register.assert_not_called()
            self.app._close_connect()
            self.session_mock.return_value = True
            self.app.max_video_mb.set("-1")
            with patch("xlikesaver.gui.dialogs.showerror") as error:
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
        self.assertFalse(self.app.stop_button.winfo_manager())

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
            with patch("xlikesaver.gui.dialogs.showerror") as error, patch("xlikesaver.gui.threading.Thread") as thread:
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
        self.assertIn("전체 Like 목록의 완료율은 알 수 없습니다", self.app.progress_details.get())
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
        with patch("xlikesaver.gui.load_settings", return_value={"sync_mode": "new", "scan_mode": "bogus", "photo_limit": 20, "date_from": "2026-10-02"}):
            app = App(other_root)
        self.assertFalse(app.limited.get())
        self.assertFalse(hasattr(app, "incremental"))
        app.account.set("offline_test")
        app.folder.set(self.temp.name)
        self.session_mock.return_value = True
        with patch("xlikesaver.gui.threading.Thread") as thread, patch("xlikesaver.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
            app.start()
        options = thread.call_args.kwargs["args"][0]
        self.assertEqual((options.scan_mode, options.photo_limit), ("quick", 0))
        self.assertFalse(hasattr(options, "sync_mode") or hasattr(options, "date_from"))

    def test_invalid_account_stops_before_worker(self):
        self.app.account.set("bad account")
        self.session_mock.return_value = True
        with patch("xlikesaver.gui.dialogs.showerror") as error, patch("xlikesaver.gui.threading.Thread") as thread:
            self.app.start()
        self.assertTrue(error.called)
        thread.assert_not_called()

    def test_token_connection_masks_and_clears_input_without_launching_worker(self):
        self.app.saved, self.app.skipped = 7, 3
        secret = "offline-secret-not-an-account-cookie"
        self.app.show_connect()
        self.app.token.set(secret)
        self.assertTrue(self.app.token_entry.cget("show"))
        with patch("xlikesaver.gui.save_auth_token") as save, patch("xlikesaver.gui.threading.Thread") as thread:
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
        with patch("xlikesaver.gui.save_auth_token", side_effect=ValueError("인증 토큰을 확인해 주세요.")), patch("xlikesaver.gui.dialogs.showerror") as error, patch("xlikesaver.gui.threading.Thread") as thread:
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
        with patch("xlikesaver.gui.save_auth_token") as save, patch("xlikesaver.gui.threading.Thread") as thread, patch("xlikesaver.gui.settings_path", return_value=Path(self.temp.name) / "settings.json"):
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
            with patch("xlikesaver.gui.load_settings", return_value={"auth_mode": old_mode}):
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
        with patch("xlikesaver.gui.clear_session") as clear:
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
        from xlikesaver import gui
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
        with patch("xlikesaver.gui.threading.Thread") as thread, patch("xlikesaver.gui.settings_path", return_value=target):
            self.app.start()
        options = thread.call_args.kwargs["args"][0]
        self.assertEqual((options.gif_format, options.gif_size), ("mp4", "480"))
        saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual((saved["gif_format"], saved["gif_size"]), ("mp4", "480"))
        other_root = tk.Tk()
        other_root.withdraw()
        self.addCleanup(destroy_tk, other_root)
        with patch("xlikesaver.gui.load_settings", return_value=saved):
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
        from xlikesaver import theme
        original = {"account": "remembered", "output_dir": "old-folder", "future_option": 7}
        target = Path(self.temp.name) / "settings.json"
        self.app.running = True
        with patch("xlikesaver.gui.load_settings", return_value=original.copy()), \
                patch("xlikesaver.gui.settings_path", return_value=target):
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
        from xlikesaver import theme
        with patch("xlikesaver.gui.load_settings", return_value={"appearance": "dark"}), \
                patch("xlikesaver.gui.threading.Thread") as worker:
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
            with patch("xlikesaver.gui.load_settings", return_value={}), \
                    patch("xlikesaver.gui.has_session", return_value=False):
                root = tk.Tk()
                root.withdraw()
                app = App(root)
            events = app.events
            refs = [weakref.ref(value) for value in (app, root, app.status)]
            with patch("xlikesaver.gui.threading.Thread") as worker:
                app._schedule_job("status", lambda: None, prompt=False)
                target = worker.call_args.kwargs["target"]
            destroy_tk(root)
            root = app = None
            self.assertTrue(all(ref() is None for ref in refs), "Scheduler worker retained closed Tk resources")
            with patch("xlikesaver.scheduled.task_status", return_value={"registered": False}):
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


if __name__ == "__main__":
    unittest.main()
