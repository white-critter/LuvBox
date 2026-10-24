import unittest
from pathlib import Path

from luvbox.qt_browser_setup import plan_extension_launch
from luvbox.qt_library import absence_note


class AbsenceNoteTests(unittest.TestCase):
    def test_missing_drive_is_distinct_from_a_deleted_file(self):
        present = Path("C:/LuvBox")
        self.assertIn("지우거나 옮긴", absence_note(present))
        missing = Path("Q:/LuvBox") if not Path("Q:/").exists() else None
        if missing is not None:
            self.assertIn("드라이브", absence_note(missing))


class ExtensionLaunchTests(unittest.TestCase):
    def test_closed_edge_loads_the_unpacked_extension(self):
        browser = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
        folder = Path("C:/LuvBox/assets/browser-extension/chromium")
        plan = plan_extension_launch(browser, False, folder)
        self.assertEqual(plan["command"], [str(browser), "--load-extension=" + str(folder)])

    def test_chrome_is_not_told_it_loaded_an_extension_it_ignores(self):
        # Chrome's stable builds ignore --load-extension; only the manual load works.
        browser = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        for running in (False, True):
            plan = plan_extension_launch(browser, running, Path("C:/extension"))
            self.assertNotIn("command", plan)
            self.assertIn("직접 불러와", plan["status"])

    def test_running_browser_is_left_open(self):
        browser = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
        plan = plan_extension_launch(browser, True, Path("C:/extension"))
        self.assertNotIn("command", plan)
        self.assertIn("이미 켜져", plan["status"])

    def test_missing_browser_keeps_the_manual_steps(self):
        plan = plan_extension_launch(None, False, Path("C:/extension"))
        self.assertNotIn("command", plan)
        self.assertIn("찾지 못했습니다", plan["status"])
