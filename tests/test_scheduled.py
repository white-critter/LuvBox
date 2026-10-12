"""Unattended scheduled runs from saved preferences, without X requests."""

import base64
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import entry
from xlikesaver import scheduled


class ScheduledRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "사진 저장"
        self.settings = {"account": "Offline_Test", "output_dir": str(self.output), "auth_mode": "managed",
                         "folder_mode": "date", "include_videos": False, "include_gifs": True,
                         "max_video_mb": 50, "separate_accounts": True, "scan_mode": "full"}
        for target, value in (("xlikesaver.scheduled.log_directory", self.root / "logs"),
                              ("xlikesaver.scheduled.last_run_path", self.root / "last-scheduled.json")):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        notify = patch("xlikesaver.scheduled._notify_failure")
        self.notify = notify.start()
        self.addCleanup(notify.stop)

    def run_saved(self, settings, **kwargs):
        calls = []

        def worker(options, emit, stop_file=None):
            calls.append(options)
            emit({"type": "log", "level": "info", "message": "목록 확인"})
            emit({"type": "progress", "downloaded": 1})
            emit({"type": "skipped", "path": "old.jpg"})
            emit({"type": "saved", "path": str(self.output / "100_1.jpg")})
            emit({"type": "finished", "status": "finished", "saved": 1, "skipped": 1, "size_skipped": 0,
                  "message": "새 Like 확인을 마쳤습니다.", "retry_at": ""})
            return 0

        with patch("xlikesaver.scheduled.load_settings", return_value=settings), \
                patch("xlikesaver.core.run_worker", side_effect=worker):
            code = scheduled.run_saved(**kwargs)
        logs = sorted((self.root / "logs").glob("scheduled-*.log"))
        return code, calls, logs[-1].read_text(encoding="utf-8") if logs else ""

    def test_saved_preferences_run_quick_scan_with_token_connection(self):
        code, calls, log = self.run_saved(self.settings)
        self.assertEqual(code, 0)
        options = calls[0]
        self.assertEqual((options.account, options.auth_mode, options.scan_mode), ("offline_test", "managed", "quick"))
        self.assertEqual((options.folder_mode, options.include_videos, options.include_gifs, options.max_video_mb,
                          options.separate_accounts), ("date", False, True, 50, True))
        self.assertEqual(options.output_dir, str(self.output.resolve()))
        self.assertIn("새 Like만 빠르게 확인", log)
        self.assertIn("저장 · ", log)
        self.assertIn("종료 · finished · 저장 1개", log)
        self.assertNotIn("old.jpg", log)
        self.assertEqual(self.run_saved(self.settings, full=True)[1][0].scan_mode, "full")
        # Earlier settings have no GIF choices; they keep converting at full size.
        self.assertEqual((options.gif_format, options.gif_size), ("gif", "original"))
        options = self.run_saved(dict(self.settings, gif_format="mp4", gif_size="720"))[1][0]
        self.assertEqual((options.gif_format, options.gif_size), ("mp4", "720"))

    def test_runs_that_cannot_start_safely_are_refused_and_logged(self):
        cases = (
            ({}, "저장된 설정이 없습니다"),
            (dict(self.settings, auth_mode="file"), "브라우저 로그인 연결"),
            (dict(self.settings, record_locations={"offline_test": str(self.root / "다른 폴더")}), "저장 폴더 변경이 끝나지"),
            (dict(self.settings, max_video_mb="200"), "0 이상의 정수"),
            (dict(self.settings, gif_format=["gif"]), "GIF 저장 형식"),
            (dict(self.settings, gif_size="1080"), "GIF 저장 형식"),
        )
        for settings, expected in cases:
            with self.subTest(expected=expected):
                code, calls, log = self.run_saved(settings)
                self.assertEqual(code, 1)
                self.assertEqual(calls, [])
                self.assertIn(expected, log)

    def test_matching_record_location_is_accepted(self):
        settings = dict(self.settings, record_locations={"offline_test": str(self.output)})
        self.assertEqual(self.run_saved(settings)[0], 0)

    def test_saved_preferences_never_restore_missing_folder_confirmation(self):
        with patch("xlikesaver.protection.state_directory", return_value=self.root / "state"), \
                patch("xlikesaver.storage.saved_media_missing", return_value=False):
            for full in (False, True):
                with self.subTest(full=full):
                    options = scheduled._options_from_settings(dict(self.settings, allow_recreate_media=True), full)
                    self.assertFalse(options.allow_recreate_media)

    def test_only_recent_logs_are_kept(self):
        logs = self.root / "logs"
        logs.mkdir()
        for index in range(40):
            (logs / f"scheduled-20260101-{index:06d}.log").write_text("old", encoding="utf-8")
        self.run_saved({})
        self.assertEqual(len(list(logs.glob("scheduled-*.log"))), 30)

    def test_scheduled_outcome_is_kept_and_only_real_failures_alert(self):
        self.assertEqual(self.run_saved(self.settings)[0], 0)
        last = scheduled.last_run()
        self.assertEqual((last["status"], last["saved"], last["failed"], last["seen"]), ("finished", 1, False, False))
        self.notify.assert_not_called()
        self.run_saved({})
        last = scheduled.last_run()
        self.assertTrue(last["failed"])
        self.assertIn("저장된 설정이 없습니다", last["message"])
        self.notify.assert_called_once()
        scheduled.mark_last_run_seen()
        self.assertTrue(scheduled.last_run()["seen"])
        # The app itself was saving: not a failure worth an alert.
        from xlikesaver.protection import _BUSY_HELP

        def busy(options, emit, stop_file=None):
            emit({"type": "finished", "status": "error", "saved": 0, "message": _BUSY_HELP})
            return 1

        self.notify.reset_mock()
        with patch("xlikesaver.scheduled.load_settings", return_value=self.settings), \
                patch("xlikesaver.core.run_worker", side_effect=busy):
            scheduled.run_saved()
        last = scheduled.last_run()
        self.assertEqual((last["busy"], last["failed"]), (True, False))
        self.notify.assert_not_called()

    def test_acknowledgment_matches_only_the_exact_displayed_outcome(self):
        old = {"finished_at": "2026-10-08T09:00:00+09:00", "status": "error", "message": "old failure",
               "failed": True, "log": "old.log", "seen": False}
        new = dict(old, finished_at="2026-10-09T09:00:00+09:00", message="new failure", log="new.log")
        scheduled._write_last_run(old)
        before = scheduled.last_run_path().read_bytes()
        scheduled.mark_last_run_seen(scheduled.last_run())
        self.assertTrue(scheduled.last_run()["seen"])
        self.assertEqual(scheduled.last_run_path().read_bytes(), before)
        scheduled._write_last_run(new)
        scheduled.mark_last_run_seen(old)
        self.assertFalse(scheduled.last_run()["seen"])
        scheduled.mark_last_run_seen(scheduled.last_run())
        self.assertTrue(scheduled.last_run()["seen"])

    def test_new_result_written_during_acknowledgment_is_preserved_and_unseen(self):
        old = {"finished_at": "2026-10-08T09:00:00+09:00", "status": "error", "failed": True, "seen": False}
        new = dict(old, finished_at="2026-10-09T09:00:00+09:00", status="finished", failed=False)
        scheduled._write_last_run(old)
        displayed = scheduled.last_run()
        entered, resume = threading.Event(), threading.Event()
        errors = []
        replace = scheduled.os.replace

        def delayed_acknowledgment(source, target):
            if Path(target) == scheduled._last_run_seen_path(displayed):
                entered.set()
                if not resume.wait(3):
                    raise TimeoutError("acknowledgment fixture did not resume")
            return replace(source, target)

        def acknowledge():
            try:
                scheduled.mark_last_run_seen(displayed)
            except Exception as error:
                errors.append(error)

        with patch("xlikesaver.scheduled.os.replace", side_effect=delayed_acknowledgment):
            worker = threading.Thread(target=acknowledge)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                scheduled._write_last_run(new)
                before = scheduled.last_run_path().read_bytes()
            finally:
                resume.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(scheduled.last_run(), new)
        self.assertEqual(scheduled.last_run_path().read_bytes(), before)
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_delayed_old_acknowledgment_cannot_erase_the_new_acknowledgment(self):
        old = {"finished_at": "2026-10-08T09:00:00+09:00", "status": "error", "failed": True, "seen": False}
        new = dict(old, finished_at="2026-10-09T09:00:00+09:00")
        scheduled._write_last_run(old)
        displayed = scheduled.last_run()
        entered, resume = threading.Event(), threading.Event()
        errors = []
        replace = scheduled.os.replace

        def delayed_acknowledgment(source, target):
            if Path(target) == scheduled._last_run_seen_path(displayed):
                entered.set()
                if not resume.wait(3):
                    raise TimeoutError("acknowledgment fixture did not resume")
            return replace(source, target)

        def acknowledge():
            try:
                scheduled.mark_last_run_seen(displayed)
            except Exception as error:
                errors.append(error)

        with patch("xlikesaver.scheduled.os.replace", side_effect=delayed_acknowledgment):
            worker = threading.Thread(target=acknowledge)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                scheduled._write_last_run(new)
                before = scheduled.last_run_path().read_bytes()
                scheduled.mark_last_run_seen(scheduled.last_run())
                self.assertTrue(scheduled.last_run()["seen"])
            finally:
                resume.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(scheduled.last_run()["seen"])
        self.assertEqual(scheduled.last_run()["finished_at"], new["finished_at"])
        self.assertEqual(scheduled.last_run_path().read_bytes(), before)
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_legacy_seen_field_and_invalid_acknowledgments_remain_compatible(self):
        outcome = {"finished_at": "2026-10-09T09:00:00+09:00", "status": "error", "seen": True}
        scheduled._write_last_run(outcome)
        self.assertTrue(scheduled.last_run()["seen"])
        scheduled.mark_last_run_seen()
        self.assertFalse(scheduled._last_run_seen_path(outcome).exists())
        scheduled._write_last_run(dict(outcome, seen=False))
        marker = scheduled._last_run_seen_path(outcome)
        marker.parent.mkdir(parents=True, exist_ok=True)
        for data in ("not json", "[]", '{"outcome": "old"}'):
            with self.subTest(data=data):
                marker.write_text(data, encoding="utf-8")
                self.assertFalse(scheduled.last_run()["seen"])

    def test_identical_fast_runs_with_the_same_timestamp_need_separate_acknowledgments(self):
        fixed = datetime(2026, 10, 9, 0, 0, 0, tzinfo=timezone.utc)
        with patch("xlikesaver.scheduled.datetime") as clock:
            clock.now.return_value = fixed
            self.run_saved({})
            first = scheduled.last_run()
            scheduled.mark_last_run_seen(first)
            self.assertTrue(scheduled.last_run()["seen"])
            self.run_saved({})
            second = scheduled.last_run()
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual({key: value for key, value in first.items() if key != "run_id"},
                         {key: value for key, value in second.items() if key != "run_id"})
        self.assertFalse(second["seen"])
        scheduled.mark_last_run_seen(first)
        self.assertFalse(scheduled.last_run()["seen"])
        scheduled.mark_last_run_seen(second)
        self.assertTrue(scheduled.last_run()["seen"])


class TaskSchedulerTests(unittest.TestCase):
    PROGRAM = r"C:\Program Files\한글 폴더\XlikeSaver-1.8.0.exe"

    def setUp(self):
        frozen = patch.object(sys, "frozen", True, create=True)
        frozen.start()
        self.addCleanup(frozen.stop)
        executable = patch.object(sys, "executable", self.PROGRAM)
        executable.start()
        self.addCleanup(executable.stop)

    def powershell(self, stdout=b"", returncode=0):
        return patch("xlikesaver.scheduled.subprocess.run",
                     return_value=subprocess.CompletedProcess([], returncode, stdout, b""))

    @staticmethod
    def script(call):
        arguments = call.args[0]
        return base64.b64decode(arguments[arguments.index("-EncodedCommand") + 1]).decode("utf-16-le")

    def test_launch_command_and_time_validation(self):
        self.assertEqual(scheduled.launch_parts(), (self.PROGRAM, "--run-saved"))
        self.assertEqual(scheduled.app_command(), [self.PROGRAM])
        self.assertEqual(scheduled.normal_time(" 7:05 "), "07:05")
        for value in ("24:00", "7", "07:60", "오전 7시"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                scheduled.normal_time(value)

    def test_registration_passes_paths_only_as_environment_values(self):
        with self.powershell() as run:
            scheduled.register_task("7:30")
        call = run.call_args
        environment = call.kwargs["env"]
        self.assertEqual((environment["XLS_PROGRAM"], environment["XLS_ARGUMENTS"], environment["XLS_TIME"]),
                         (self.PROGRAM, "--run-saved", "07:30"))
        self.assertEqual(environment["XLS_TASK"], scheduled.TASK_NAME)
        script = self.script(call)
        for setting in ("-StartWhenAvailable", "-AllowStartIfOnBatteries", "-DontStopIfGoingOnBatteries",
                        "-MultipleInstances IgnoreNew", "-Daily"):
            self.assertIn(setting, script)
        self.assertNotIn(self.PROGRAM, script)
        self.assertNotIn("XlikeSaver-1.8.0", " ".join(call.args[0]))
        with self.powershell() as run:
            scheduled.unregister_task()
        self.assertIn("Unregister-ScheduledTask", self.script(run.call_args))
        with self.powershell(returncode=1), self.assertRaises(scheduled.ScheduleError) as failure:
            scheduled.register_task("07:30")
        self.assertIn("작업 스케줄러", str(failure.exception))

    def test_status_reports_whether_this_version_is_registered(self):
        with self.powershell(b'{"registered": false}'):
            self.assertEqual(scheduled.task_status(), {"registered": False})
        data = {"registered": True, "program": self.PROGRAM, "arguments": "--run-saved",
                "start": "2026-10-06T07:30:00+09:00", "enabled": True, "catch_up": True, "on_battery": True,
                "last_run": "", "next_run": "2026-10-07T07:30:00"}
        with self.powershell(json.dumps(data, ensure_ascii=False).encode("utf-8")):
            status = scheduled.task_status()
        self.assertEqual((status["current"], status["time"], status["next_run"]), (True, "07:30", "2026-10-07T07:30:00"))
        old = dict(data, program=r"C:\old\XlikeSaver-1.7.1.exe", catch_up=False)
        with self.powershell(json.dumps(old).encode("utf-8")):
            status = scheduled.task_status()
        self.assertEqual((status["current"], status["catch_up"]), (False, False))
        with self.powershell(b"not json"), self.assertRaises(scheduled.ScheduleError):
            scheduled.task_status()

    def test_task_under_the_former_app_name_is_found_and_replaced(self):
        data = {"registered": True, "name": "XlikeSaver 자동 저장", "program": self.PROGRAM,
                "arguments": "--run-saved", "start": "2026-10-06T07:30:00+09:00", "enabled": True,
                "catch_up": True, "on_battery": True, "last_run": "", "next_run": ""}
        with self.powershell(json.dumps(data, ensure_ascii=False).encode("utf-8")) as run:
            status = scheduled.task_status()
        self.assertTrue(status["registered"])
        self.assertFalse(status["current"])   # offers to renew it under the new name
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["XLS_TASK"], "LuvBox 자동 저장")
        self.assertEqual(environment["XLS_LEGACY"], "XlikeSaver 자동 저장")
        with self.powershell() as run:
            scheduled.register_task("07:30")
        script = self.script(run.call_args)
        self.assertIn("Unregister-ScheduledTask -TaskName $old", script)
        self.assertEqual(run.call_args.kwargs["env"]["XLS_LEGACY"], "XlikeSaver 자동 저장")
        with self.powershell() as run:
            scheduled.unregister_task()
        self.assertIn("$env:XLS_LEGACY", self.script(run.call_args))

    def test_entry_point_accepts_only_known_run_saved_arguments(self):
        with patch("xlikesaver.scheduled.run_saved", return_value=0) as run:
            for argv, code, full in ((["--run-saved"], 0, False), (["--run-saved", "--full"], 0, True)):
                with self.subTest(argv=argv), patch.object(sys, "argv", ["entry.py", *argv]):
                    self.assertEqual(entry.main(), code)
                    run.assert_called_with(full=full)
            for argv in (["--run-saved", "--bogus"], ["--run-saved", "--full", "x"]):
                with self.subTest(argv=argv), patch.object(sys, "argv", ["entry.py", *argv]):
                    self.assertEqual(entry.main(), 2)


if __name__ == "__main__":
    unittest.main()
