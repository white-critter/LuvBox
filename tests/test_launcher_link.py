"""The application's side of the launcher: its path, the ready signal, the taskbar and schedules."""

import ctypes
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from luvbox import launcher, scheduled
from tests.release_server import make_program


class FooterTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="luvbox-launcher-link-"))
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_footer_reads_back_and_other_files_are_not_launchers(self):
        program = self.folder / "LuvBox.exe"
        data = make_program("1.8.0")
        program.write_bytes(data)
        footer = launcher.read_footer(program)
        self.assertEqual(footer["version"], "1.8.0")
        self.assertEqual(data[footer["offset"]:footer["offset"] + 4], b"PK\x03\x04")
        self.assertEqual(footer["offset"] + footer["length"] + 84, len(data))
        self.assertEqual(launcher.payload_digest(program, footer), footer["sha256"])
        for name, content in (("empty.exe", b""), ("small.exe", b"MZ" + b"\0" * 100),
                              ("other.exe", os.urandom(64 * 1024)), ("cut.exe", data[:-10])):
            path = self.folder / name
            path.write_bytes(content)
            self.assertIsNone(launcher.read_footer(path), name)

    def test_a_footer_that_does_not_add_up_is_damage_not_absence(self):
        data = bytearray(make_program("1.8.0"))
        for position, label in ((-64, "length"), (-72, "offset"), (-76, "format"), (-24, "version")):
            with self.subTest(label=label):
                changed = bytearray(data)
                changed[position] ^= 0x40
                path = self.folder / f"{label}.exe"
                path.write_bytes(changed)
                with self.assertRaises(ValueError):
                    launcher.read_footer(path)
        for arguments in ((100, 1000, b"\0" * 32, "1.8.0"), (8192, 1000, b"\0" * 31, "1.8.0"),
                          (8192, 1000, b"\0" * 32, "v1.8.0"), (8192, 1000, b"\0" * 32, "1.8"), (8192, 2, b"\0" * 32, "1.8.0")):
            with self.assertRaises(ValueError):
                launcher.make_footer(*arguments)

    def test_a_changed_archive_no_longer_matches_its_footer(self):
        data = bytearray(make_program("1.8.0"))
        program = self.folder / "LuvBox.exe"
        program.write_bytes(data)
        footer = launcher.read_footer(program)
        self.assertEqual(launcher.payload_digest(program, footer), footer["sha256"])
        data[footer["offset"] + 20] ^= 1
        program.write_bytes(data)
        self.assertNotEqual(launcher.payload_digest(program, footer), footer["sha256"])


class LauncherPathTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="luvbox-launcher-path-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.launcher = self.folder / "LuvBox.exe"
        self.launcher.write_bytes(make_program("1.8.0"))
        self.inner = self.folder / "app" / "1.8.0" / "LuvBox.exe"
        self.inner.parent.mkdir(parents=True)
        self.inner.write_bytes(b"MZ unpacked program")

    def packaged(self, **environment):
        patches = [patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(self.inner)),
                   patch.dict(os.environ, environment)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_the_launcher_is_known_only_to_a_packaged_program_it_started(self):
        with patch.dict(os.environ, {"LUVBOX_LAUNCHER": str(self.launcher)}):
            self.assertIsNone(launcher.launcher_path())  # running from source
            self.assertEqual(launcher.program_path(), sys.executable)
        self.packaged(LUVBOX_LAUNCHER=str(self.launcher))
        self.assertEqual(launcher.launcher_path(), self.launcher)
        self.assertEqual(launcher.program_path(), str(self.launcher))

    def test_a_missing_relative_or_unrelated_path_falls_back_to_this_program(self):
        other = self.folder / "notepad.exe"
        other.write_bytes(os.urandom(64 * 1024))
        for value in ("", "LuvBox.exe", str(self.folder / "missing.exe"), str(other), str(self.inner),
                      str(self.folder)):
            with self.subTest(value=value):
                with patch.object(sys, "frozen", True, create=True), \
                        patch.object(sys, "executable", str(self.inner)), \
                        patch.dict(os.environ, {"LUVBOX_LAUNCHER": value}):
                    self.assertIsNone(launcher.launcher_path())
                    self.assertEqual(launcher.program_path(), str(self.inner))

    def test_a_newer_release_at_the_same_place_is_still_the_launcher(self):
        # After an update the file is the next version while this one keeps running.
        self.launcher.write_bytes(make_program("1.9.0"))
        self.packaged(LUVBOX_LAUNCHER=str(self.launcher))
        self.assertEqual(launcher.launcher_path(), self.launcher)

    def test_the_scheduled_task_and_the_open_button_use_the_launcher(self):
        self.packaged(LUVBOX_LAUNCHER=str(self.launcher))
        self.assertEqual(scheduled.launch_parts(), (str(self.launcher), "--run-saved"))
        self.assertEqual(scheduled.app_command(), [str(self.launcher)])
        registered = {"registered": True, "name": scheduled.TASK_NAME, "program": str(self.launcher).upper(),
                      "arguments": "--run-saved", "start": "2026-11-12T09:00:00", "enabled": True,
                      "running": False, "catch_up": True, "on_battery": True, "last_run": "", "next_run": ""}
        import json
        with patch.object(scheduled, "_powershell", return_value=json.dumps(registered)):
            self.assertTrue(scheduled.task_status()["current"])
        # A task made by 1.7.x points at that version's own program: offered for one change.
        registered["program"] = r"C:\Apps\LuvBox-1.7.0\LuvBox-1.7.0.exe"
        with patch.object(scheduled, "_powershell", return_value=json.dumps(registered)):
            self.assertFalse(scheduled.task_status()["current"])

    def test_without_a_launcher_the_schedule_uses_this_program_as_before(self):
        self.packaged(LUVBOX_LAUNCHER="")
        self.assertEqual(scheduled.launch_parts(), (str(self.inner), "--run-saved"))
        self.assertEqual(scheduled.app_command(), [str(self.inner)])

    def test_a_started_copy_does_not_inherit_this_instances_launcher_state(self):
        self.packaged(LUVBOX_LAUNCHER=str(self.launcher), LUVBOX_READY_EVENT="Local\\LuvBox-ready-" + "a" * 32)
        with patch.object(launcher.subprocess, "Popen") as start, \
                patch.object(launcher.ctypes, "WinDLL", create=True) as library:
            launcher.spawn_detached([str(self.launcher)])
        command = start.call_args.args[0]
        options = start.call_args.kwargs
        self.assertEqual(command, [str(self.launcher)])
        self.assertNotIn("LUVBOX_LAUNCHER", options["env"])
        self.assertNotIn("LUVBOX_READY_EVENT", options["env"])
        self.assertEqual(options["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertTrue(options["close_fds"])
        if os.name == "nt":
            # The packaged program's DLL folder is not handed to the launcher.
            library.return_value.SetDllDirectoryW.assert_any_call(None)

    def test_a_source_run_starts_copies_without_touching_the_dll_search_path(self):
        with patch.object(launcher.subprocess, "Popen") as start, \
                patch.object(launcher.ctypes, "WinDLL", create=True) as library:
            launcher.spawn_detached(["python", "entry.py"])
        self.assertNotIn("PYINSTALLER_RESET_ENVIRONMENT", start.call_args.kwargs["env"])
        library.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Windows only")
class WindowsSignalTests(unittest.TestCase):
    def test_the_ready_signal_sets_the_launchers_event_once(self):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateEventW.restype = ctypes.c_void_p
        kernel32.CreateEventW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p)
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        name = "Local\\LuvBox-ready-" + hashlib.sha256(os.urandom(16)).hexdigest()[:32]
        handle = kernel32.CreateEventW(None, 1, 0, name)
        self.assertTrue(handle)
        self.addCleanup(kernel32.CloseHandle, handle)
        with patch.dict(os.environ, {"LUVBOX_READY_EVENT": name}):
            self.assertEqual(kernel32.WaitForSingleObject(handle, 0), 258)  # not yet signalled
            self.assertTrue(launcher.signal_ready())
            self.assertEqual(kernel32.WaitForSingleObject(handle, 0), 0)
            # The name is used once and not handed on to worker processes.
            self.assertNotIn("LUVBOX_READY_EVENT", os.environ)
            self.assertFalse(launcher.signal_ready())

    def test_names_that_are_not_the_launchers_are_ignored(self):
        for name in ("", "Global\\Something", "Local\\LuvBox-ready-xyz", "Local\\LuvBox-ready-" + "a" * 32 + "\\x",
                     "Local\\LuvBox-ready-" + "a" * 32):
            with patch.dict(os.environ, {"LUVBOX_READY_EVENT": name}):
                self.assertFalse(launcher.signal_ready(), name)

    def test_a_window_is_marked_so_a_taskbar_pin_starts_the_launcher(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.CreateWindowExW.argtypes = (ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        user32.DestroyWindow.argtypes = (ctypes.c_void_p,)
        window = user32.CreateWindowExW(0, "STATIC", "LuvBox test", 0, 0, 0, 10, 10, None, None, None, None)
        self.assertTrue(window)
        self.addCleanup(user32.DestroyWindow, window)
        path = Path(r"C:\사용자 폴더\앱 모음\LuvBox.exe")
        self.assertEqual(launcher.window_identity(window), dict.fromkeys(("command", "icon", "name", "identifier")))
        self.assertTrue(launcher.mark_window(window, path))
        self.assertEqual(launcher.window_identity(window), {
            "command": f'"{path}"', "icon": f"{path},0", "name": "LuvBox", "identifier": "WhiteCritter.LuvBox"})
        # Removed again before the window closes.
        self.assertTrue(launcher.mark_window(window, None))
        self.assertEqual(launcher.window_identity(window), dict.fromkeys(("command", "icon", "name", "identifier")))
        self.assertFalse(launcher.mark_window(0, path))

    def test_the_process_identity_matches_the_launchers(self):
        code = ("import ctypes; from luvbox import launcher; assert launcher.set_process_identity(); "
                "value = ctypes.c_wchar_p(); "
                "assert ctypes.WinDLL('shell32').GetCurrentProcessExplicitAppUserModelID(ctypes.byref(value)) == 0; "
                "print(value.value)")
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "WhiteCritter.LuvBox")
        source = (Path(__file__).resolve().parents[1] / "tools" / "launcher" / "Launcher.cs").read_text(encoding="utf-8")
        self.assertIn('const string AppIdentifier = "WhiteCritter.LuvBox";', source)


if __name__ == "__main__":
    unittest.main()
