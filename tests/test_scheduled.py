"""Unattended scheduled runs from saved preferences, without X requests."""

import sys
import tempfile
import unittest
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
        for target, value in (("xlikesaver.scheduled.log_directory", self.root / "logs"),):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

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
            (dict(self.settings, auth_mode="file"), "인증 토큰 연결"),
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

    def test_only_recent_logs_are_kept(self):
        logs = self.root / "logs"
        logs.mkdir()
        for index in range(40):
            (logs / f"scheduled-20260101-{index:06d}.log").write_text("old", encoding="utf-8")
        self.run_saved({})
        self.assertEqual(len(list(logs.glob("scheduled-*.log"))), 30)

    def test_task_command_quotes_paths_with_spaces(self):
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", r"C:\Program Files\XlikeSaver\XlikeSaver-1.1.0.exe"):
            self.assertEqual(scheduled.launch_command(), r'"C:\Program Files\XlikeSaver\XlikeSaver-1.1.0.exe" --run-saved')
            self.assertEqual(scheduled.schtasks_command("07:30"),
                             r'schtasks /Create /TN "XlikeSaver 자동 저장" /SC DAILY /ST 07:30 '
                             r'/TR "\"C:\Program Files\XlikeSaver\XlikeSaver-1.1.0.exe\" --run-saved" /F')

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
