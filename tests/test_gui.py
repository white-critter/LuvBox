"""Native UI workflow checks without account or browser access."""

import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from xlikesaver.gui import App, AUTH_MODES, worker_command


def destroy_tk(root):
    """Cancel scheduled callbacks before a test destroys its Tcl interpreter."""
    root.update_idletasks()
    for timer in root.tk.call("after", "info"):
        root.tk.call("after", "cancel", timer)
    root.destroy()


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(destroy_tk, self.root)
        self.session = patch("xlikesaver.gui.has_session", return_value=False)
        self.session_mock = self.session.start()
        self.addCleanup(self.session.stop)
        self.settings = patch("xlikesaver.gui.load_settings", return_value={})
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.app = App(self.root)

    def test_unconnected_token_mode_requests_connection_before_download(self):
        with patch("xlikesaver.gui.messagebox.showinfo") as notice, patch("xlikesaver.gui.threading.Thread") as thread:
            self.app.start()
        self.assertTrue(notice.called)
        thread.assert_not_called()
        self.assertFalse(self.app.running)

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
                patch("xlikesaver.gui.messagebox.showerror") as error:
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
                patch("xlikesaver.gui.messagebox.showerror"):
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

    def test_schedule_command_is_copied_for_the_running_application(self):
        self.app.copy_schedule_command()
        command = self.root.clipboard_get()
        self.assertIn('schtasks /Create /TN "XlikeSaver 자동 저장" /SC DAILY', command)
        self.assertIn("--run-saved", command)
        self.assertIn("명령 프롬프트", self.app.status.get())

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
        self.assertEqual(paths.record_dir, directory / ".xlikesaver" / "offline_test")
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

    def test_minimum_window_keeps_actions_details_and_log_visible(self):
        self.root.attributes("-alpha", 0)
        self.root.geometry("740x650")
        self.root.deiconify()
        self.app.progress_details.set("속도 2 MiB/초 · 작성자 @offline_test · 트윗 1000000000000000000\n트윗 게시일 2026-10-04 20:00:00 · 현재 파일의 진행률이며 전체 Like 목록의 완료율은 알 수 없습니다.")
        self.app.detailed_progress.set(True)
        self.app._toggle_progress()
        for index in (0, 1):
            self.app.tabs.select(index)
            self.app._resize_tab()
            self.root.update()
            self.assertTrue(self.app.start_button.winfo_ismapped())
            self.assertGreater(self.app.progress_detail_label.winfo_height(), 25)
            self.assertGreaterEqual(self.app.log.winfo_height(), 28)
            for widget in (self.app.start_button, self.app.progress_detail_label, self.app.log):
                bottom = widget.winfo_rooty() - self.root.winfo_rooty() + widget.winfo_height()
                self.assertLessEqual(bottom, self.root.winfo_height())

    def test_invalid_video_limit_stops_before_worker(self):
        self.app.account.set("offline_test")
        self.session_mock.return_value = True
        for invalid in ("bad", "1.5", "-1"):
            self.app.max_video_mb.set(invalid)
            with patch("xlikesaver.gui.messagebox.showerror") as error, patch("xlikesaver.gui.threading.Thread") as thread:
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
        with patch("xlikesaver.gui.messagebox.showerror") as error, patch("xlikesaver.gui.threading.Thread") as thread:
            self.app.start()
        self.assertTrue(error.called)
        thread.assert_not_called()

    def test_token_connection_masks_and_clears_input_without_launching_worker(self):
        self.app.saved, self.app.skipped = 7, 3
        secret = "offline-secret-not-an-account-cookie"
        self.app.token.set(secret)
        self.assertTrue(self.app.token_entry.cget("show"))
        with patch("xlikesaver.gui.save_auth_token") as save, patch("xlikesaver.gui.threading.Thread") as thread:
            self.assertTrue(self.app.connect_token())
        save.assert_called_once_with(secret)
        thread.assert_not_called()
        self.assertEqual(self.app.token.get(), "")
        self.assertNotIn(secret, self.app.log.get("1.0", "end"))
        self.assertIn("실제 유효성", self.app.status.get())
        self.assertEqual((self.app.saved, self.app.skipped), (7, 3))
        self.assertFalse(self.app.running)
        self.assertEqual(str(self.app.layout_combo.cget("state")), "readonly")

    def test_failed_token_connection_clears_input_and_does_not_start_download(self):
        self.session_mock.return_value = True  # A previous saved connection exists.
        secret = "offline-invalid-new-token"
        self.app.token.set(secret)
        with patch("xlikesaver.gui.save_auth_token", side_effect=ValueError("인증 토큰을 확인해 주세요.")), patch("xlikesaver.gui.messagebox.showerror") as error, patch("xlikesaver.gui.threading.Thread") as thread:
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
        self.app.token.set("offline-test-token")
        self.app.auth.set(next(k for k, v in AUTH_MODES.items() if v == "file"))
        self.assertEqual(self.app.token.get(), "")
        self.assertEqual(self.app.token_controls.winfo_manager(), "")
        self.assertEqual(self.app.cookie_entry.winfo_manager(), "grid")

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


if __name__ == "__main__":
    unittest.main()
