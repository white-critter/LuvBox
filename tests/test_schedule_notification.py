"""Scheduled failures use the current Qt toolkit and plain-text messages."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ScheduleNotificationTests(unittest.TestCase):
    def test_qt_notice_keeps_close_default_and_opens_app_only_when_chosen(self):
        script = r'''
import sys
from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from luvbox import scheduled

app = QApplication([])
seen = []
choose_open = sys.argv[1] == "open"

def execute(dialog):
    assert dialog.textFormat() == Qt.TextFormat.PlainText
    assert "<b>literal failure</b>" in dialog.text()
    assert dialog.defaultButton().text() == "닫기"
    assert dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    seen.append(True)
    return 0

def clicked(dialog):
    wanted = "앱 열기" if choose_open else "닫기"
    return next(button for button in dialog.buttons() if button.text() == wanted)

with patch.object(QMessageBox, "exec", execute), patch.object(QMessageBox, "clickedButton", clicked):
    with patch.object(scheduled.subprocess, "Popen") as launch:
        scheduled._notify_failure({"finished_at": "2026-10-24T18:00:00+09:00",
                                   "message": "<b>literal failure</b>", "log": ""})
        assert seen == [True]
        assert launch.call_count == int(choose_open)
        if choose_open:
            assert launch.call_args.args[0] == scheduled.app_command()
'''
        with tempfile.TemporaryDirectory(prefix="luvbox-qt-notice-") as folder:
            environment = {**os.environ, "LOCALAPPDATA": folder,
                           "APPDATA": str(Path(folder) / "roaming"), "QT_QPA_PLATFORM": "offscreen"}
            for choice in ("close", "open"):
                with self.subTest(choice=choice):
                    result = subprocess.run([sys.executable, "-c", script, choice], cwd=ROOT,
                                            env=environment, capture_output=True, timeout=20,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()
