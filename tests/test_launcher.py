"""Run the real launcher end to end with a tiny stand-in application.

The launcher is compiled from tools/launcher/Launcher.cs exactly as a release
build does, a few-kilobyte program plays the application, and every run gets
its own LOCALAPPDATA. Nothing here reads or writes the developer's LuvBox data.
"""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import zipfile

from luvbox import launcher as launcher_format
from tools import build_launcher


VERSION = "9.8.7"
# Reports how it was started, optionally signals "window drawn", and returns a chosen code.
STAND_IN = r"""
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;

static class StandIn
{
    static int Number(string name)
    {
        string value = Environment.GetEnvironmentVariable(name);
        return string.IsNullOrEmpty(value) ? 0 : int.Parse(value);
    }

    static int Main()
    {
        string signal = Environment.GetEnvironmentVariable("LUVBOX_READY_EVENT") ?? "";
        if (Number("STANDIN_READY_MS") > 0)
        {
            Thread.Sleep(Number("STANDIN_READY_MS"));
            using (EventWaitHandle ready = EventWaitHandle.OpenExisting(signal))
                ready.Set();
        }
        Thread.Sleep(Number("STANDIN_STAY_MS"));
        string report = Environment.GetEnvironmentVariable("STANDIN_REPORT");
        if (!string.IsNullOrEmpty(report))
        {
            File.WriteAllLines(report, new string[] {
                Environment.CommandLine,
                Environment.GetEnvironmentVariable("LUVBOX_LAUNCHER") ?? "",
                Environment.CurrentDirectory,
                Process.GetCurrentProcess().MainModule.FileName,
                signal }, new UTF8Encoding(false));
        }
        return Number("STANDIN_EXIT");
    }
}
"""


def _compiler():
    try:
        return build_launcher.compiler()
    except FileNotFoundError:
        return None


@unittest.skipUnless(os.name == "nt" and _compiler() is not None, "needs the Windows C# compiler")
class LauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="luvbox-launcher-test-"))
        cls.addClassCleanup(shutil.rmtree, cls.work, True)
        cls.bare = build_launcher.compile_launcher(cls.work / "bare" / "LuvBox.exe", VERSION)
        cls.application = cls.work / "application"
        (cls.application / "_internal" / "한글 폴더").mkdir(parents=True)
        source = cls.work / "standin.cs"
        source.write_text(STAND_IN, encoding="utf-8")
        result = subprocess.run([str(_compiler()), "/nologo", "/target:winexe", "/platform:x64",
                                 f"/out:{cls.application / 'LuvBox.exe'}", str(source)],
                                capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, result.stdout + result.stderr
        (cls.application / "_internal" / "plain.txt").write_text("plain application data", encoding="utf-8")
        (cls.application / "_internal" / "한글 폴더" / "사진 목록.txt").write_text("한글 경로", encoding="utf-8")
        (cls.application / "_internal" / "large.bin").write_bytes(os.urandom(3 * 1024 * 1024))
        cls.program = cls.work / "program" / "LuvBox.exe"
        cls.footer = build_launcher.pack(cls.application, cls.bare, cls.program, VERSION)

    def setUp(self):
        self.local = Path(tempfile.mkdtemp(prefix="luvbox-launcher-local-"))
        self.addCleanup(shutil.rmtree, self.local, True)
        self.root = self.local / "LuvBox" / "app"
        self.installed = self.root / VERSION
        self.reports = 0

    def environment(self, **extra):
        self.reports += 1
        report = self.local / f"report-{self.reports}.txt"
        environment = {**os.environ, "LOCALAPPDATA": str(self.local), "LUVBOX_LAUNCHER_SILENT": "1",
                       "STANDIN_REPORT": str(report)}
        for name in ("STANDIN_READY_MS", "STANDIN_STAY_MS", "STANDIN_EXIT", "LUVBOX_READY_EVENT", "LUVBOX_LAUNCHER"):
            environment.pop(name, None)
        environment.update({name: str(value) for name, value in extra.items()})
        return environment, report

    def run_program(self, *arguments, program=None, cwd=None, **extra):
        environment, report = self.environment(**extra)
        result = subprocess.run([str(program or self.program), *arguments], env=environment, cwd=cwd,
                                capture_output=True, timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
        lines = report.read_text(encoding="utf-8").splitlines() if report.is_file() else None
        return result.returncode, lines

    def leftovers(self):
        return sorted(path.name for path in self.root.iterdir() if path.name.startswith((".new-", ".old-")))

    def test_first_run_unpacks_once_and_later_runs_start_the_same_copy(self):
        code, report = self.run_program("--check")
        self.assertEqual(code, 0)
        self.assertEqual(Path(report[3]), self.installed / "LuvBox.exe")
        for name, path in build_launcher.application_files(self.application):
            self.assertEqual((self.installed / name).read_bytes(), path.read_bytes(), name)
        marker = (self.installed / launcher_format.MARKER_NAME).read_text(encoding="utf-8")
        self.assertIn(f"version={VERSION}\n", marker)
        self.assertIn(f"payload={self.footer['sha256']}\n", marker)
        self.assertEqual(self.leftovers(), [])
        # A second start must not unpack again: a file added to the unpacked
        # copy survives, and the marker is the same file as before.
        kept = self.installed / "added-after-unpack.txt"
        kept.write_text("still here", encoding="utf-8")
        before = (self.installed / launcher_format.MARKER_NAME).stat()
        started = time.perf_counter()
        code, report = self.run_program("--check")
        elapsed = time.perf_counter() - started
        after = (self.installed / launcher_format.MARKER_NAME).stat()
        self.assertEqual(code, 0)
        self.assertEqual(kept.read_text(encoding="utf-8"), "still here")
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertLess(elapsed, 5)

    def test_arguments_working_folder_and_exit_code_pass_through_unchanged(self):
        arguments = ["--run-saved", "C:\\폴더 with space\\report.json", 'quote"inside', "trailing\\", "", "a&b|c"]
        folder = self.local / "작업 폴더"
        folder.mkdir()
        code, report = self.run_program(*arguments, cwd=folder, STANDIN_EXIT=37)
        self.assertEqual(code, 37)
        command, program, working, inner, signal = report
        self.assertEqual(command, f'"{self.installed / "LuvBox.exe"}" ' + subprocess.list2cmdline(arguments))
        self.assertEqual(Path(program), self.program)
        self.assertEqual(Path(working), folder)
        # Runs with arguments never wait on a window, so there is nothing to signal.
        self.assertEqual(signal, "")

    def test_a_plain_start_returns_when_the_application_says_its_window_is_drawn(self):
        environment, report = self.environment(STANDIN_READY_MS=300, STANDIN_STAY_MS=4000)
        started = time.perf_counter()
        # No pipes here: the application would inherit them, and waiting for
        # their end would wait for the application instead of the launcher.
        result = subprocess.run([str(self.program)], env=environment, timeout=60)
        elapsed = time.perf_counter() - started
        self.assertEqual(result.returncode, 0)
        # The stand-in is still running: the launcher did not wait for it to end.
        self.assertFalse(report.exists())
        self.assertLess(elapsed, 3.5)
        deadline = time.monotonic() + 15
        while not report.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        lines = report.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0].strip(), f'"{self.installed / "LuvBox.exe"}"')
        self.assertRegex(lines[4], r"^Local\\LuvBox-ready-[0-9a-f]{32}$")

    def test_a_plain_start_whose_application_ends_early_returns_its_code(self):
        code, report = self.run_program(STANDIN_EXIT=5)
        self.assertEqual(code, 5)
        self.assertIsNotNone(report)

    def damaged_copy(self, change):
        data = bytearray(self.program.read_bytes())
        change(data)
        target = self.work / f"damaged-{self.id().rsplit('.', 1)[-1]}" / "LuvBox.exe"
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(data)
        return target

    def test_a_changed_archive_is_refused_before_anything_is_unpacked(self):
        def flip(data):
            position = self.footer["offset"] + self.footer["length"] // 2
            data[position] ^= 0xFF

        code, report = self.run_program("--check", program=self.damaged_copy(flip))
        self.assertEqual(code, 71)
        self.assertIsNone(report)
        self.assertFalse(self.installed.exists())
        self.assertEqual(self.leftovers(), [])

    def test_a_download_cut_short_is_reported_as_damaged_and_a_bare_launcher_as_empty(self):
        def cut(data):
            del data[len(data) - 100_000:]

        code, report = self.run_program("--check", program=self.damaged_copy(cut))
        self.assertEqual((code, report), (71, None))
        code, report = self.run_program("--check", program=self.bare)
        self.assertEqual((code, report), (70, None))
        self.assertFalse((self.local / "LuvBox").exists())

    def program_with_archive(self, entries, name, rename=None):
        """A correctly sealed program whose archive holds the given raw entries."""
        target = self.work / name / "LuvBox.exe"
        target.parent.mkdir(exist_ok=True)
        archive = self.work / name / "archive.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            for entry, data in entries:
                output.writestr(zipfile.ZipInfo(entry), data)
        payload = archive.read_bytes()
        if rename and rename[0] != rename[1]:
            # The same length, in the file's own header and in the directory.
            self.assertEqual(payload.count(rename[0].encode()), 2)
            payload = payload.replace(rename[0].encode(), rename[1].encode())
        program = self.bare.read_bytes()
        target.write_bytes(program + payload + launcher_format.make_footer(
            len(program), len(payload), hashlib.sha256(payload).digest(), VERSION))
        return target

    def test_archive_names_that_leave_the_folder_are_refused_even_when_the_hash_is_right(self):
        inner = (self.application / "LuvBox.exe").read_bytes()
        names = ("../escaped.txt", "_internal/../../escaped.txt", "C:/escaped.txt", "/escaped.txt",
                 "_internal\\escaped.txt", "name:stream", "NUL.txt", "ends-with-dot.", "_internal//escaped.txt")
        for index, name in enumerate(names):
            with self.subTest(name=name):
                # zipfile turns a backslash into "/" on Windows, so that name is
                # written with a stand-in letter and patched into the raw archive.
                written = name.replace("\\", "Q")
                program = self.program_with_archive([("LuvBox.exe", inner), (written, b"must not be written")],
                                                    f"unsafe-{index}", rename=(written, name))
                shutil.rmtree(self.local / "LuvBox", True)
                code, report = self.run_program("--check", program=program)
                self.assertEqual((code, report), (71, None))
                self.assertFalse(self.installed.exists())
                self.assertEqual(self.leftovers(), [])
                self.assertEqual([path for path in self.local.rglob("escaped.txt")], [])
                self.assertFalse((self.local.parent / "escaped.txt").exists())

    def test_several_first_starts_at_once_unpack_one_copy_and_all_run(self):
        runs = []
        for _ in range(4):
            environment, report = self.environment(STANDIN_EXIT=3)
            runs.append((subprocess.Popen([str(self.program), "--check"], env=environment,
                                          creationflags=subprocess.CREATE_NO_WINDOW), report))
        for process, report in runs:
            self.assertEqual(process.wait(timeout=120), 3)
            self.assertTrue(report.is_file())
        self.assertEqual(self.leftovers(), [])
        for name, path in build_launcher.application_files(self.application):
            self.assertEqual((self.installed / name).read_bytes(), path.read_bytes(), name)

    def version_folder(self, name, *, marker=True):
        folder = self.root / name
        folder.mkdir(parents=True)
        (folder / "LuvBox.exe").write_bytes(b"an earlier version")
        if marker:
            (folder / launcher_format.MARKER_NAME).write_text(f"version={name}\npayload=0\n", encoding="utf-8")
        return folder

    def test_only_this_version_and_the_one_before_it_are_kept(self):
        for name in ("9.7.0", "9.8.5", "9.8.6", "10.0.0"):
            self.version_folder(name)
        self.version_folder("9.8.4", marker=False)       # not unpacked by a launcher: not ours to remove
        (self.root / "notes").mkdir()
        (self.root / ".new-stale").mkdir()
        code, _report = self.run_program("--check")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(path.name for path in self.root.iterdir() if path.is_dir()),
                         ["10.0.0", "9.8.4", "9.8.6", VERSION, "notes"])

    def test_a_redirected_application_folder_is_refused_and_its_target_left_alone(self):
        outside = self.local / "elsewhere"
        outside.mkdir()
        (self.local / "LuvBox").mkdir()
        made = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(self.root), str(outside)],
                              capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if made.returncode:
            self.skipTest("Cannot create a junction here")
        self.addCleanup(lambda: os.path.lexists(self.root) and os.rmdir(self.root))
        code, report = self.run_program("--check")
        self.assertEqual((code, report), (73, None))
        self.assertEqual(list(outside.iterdir()), [])

    def test_the_program_an_update_set_aside_is_removed_at_the_next_start(self):
        folder = self.work / f"updated-{self.reports}"
        folder.mkdir(exist_ok=True)
        program = folder / "LuvBox.exe"
        shutil.copyfile(self.program, program)
        for name in ("LuvBox.exe.old", "LuvBox.exe.old.2", "LuvBox.exe.older", "LuvBox.exe.old.backup"):
            (folder / name).write_bytes(b"previous program")
        code, _report = self.run_program("--check", program=program)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(path.name for path in folder.iterdir()),
                         ["LuvBox.exe", "LuvBox.exe.old.backup", "LuvBox.exe.older"])

    def test_another_archive_with_the_same_version_replaces_the_unpacked_copy(self):
        self.assertEqual(self.run_program("--check")[0], 0)
        inner = (self.application / "LuvBox.exe").read_bytes()
        rebuilt = self.program_with_archive([("LuvBox.exe", inner), ("_internal/rebuilt.txt", b"second build")],
                                            "rebuilt")
        code, _report = self.run_program("--check", program=rebuilt)
        self.assertEqual(code, 0)
        self.assertEqual((self.installed / "_internal" / "rebuilt.txt").read_bytes(), b"second build")
        self.assertFalse((self.installed / "_internal" / "plain.txt").exists())
        self.assertEqual(self.leftovers(), [])

    def test_a_same_version_copy_that_is_in_use_is_left_alone(self):
        self.assertEqual(self.run_program("--check")[0], 0)
        inner = (self.application / "LuvBox.exe").read_bytes()
        rebuilt = self.program_with_archive([("LuvBox.exe", inner), ("_internal/rebuilt.txt", b"second build")],
                                            "rebuilt-in-use")
        # An open file, as a running copy of that version has: its folder cannot be renamed.
        with (self.installed / "_internal" / "plain.txt").open("rb"):
            code, report = self.run_program("--check", program=rebuilt)
        self.assertEqual((code, report), (76, None))
        self.assertEqual((self.installed / "_internal" / "plain.txt").read_text(encoding="utf-8"),
                         "plain application data")
        self.assertFalse((self.installed / "_internal" / "rebuilt.txt").exists())
        self.assertEqual(self.leftovers(), [])
        # Once it is closed, the same start succeeds.
        self.assertEqual(self.run_program("--check", program=rebuilt)[0], 0)
        self.assertTrue((self.installed / "_internal" / "rebuilt.txt").is_file())

    def replace_as_an_update_does(self, program, newer):
        """What luvbox.updater.install does: the running file is renamed, the new one takes its name."""
        os.replace(program, program.with_name("LuvBox.exe.old"))
        shutil.copyfile(newer, program)

    def test_a_launcher_replaced_before_it_unpacks_says_so_and_the_next_start_runs_the_new_one(self):
        folder = self.local / "place"
        folder.mkdir()
        program = folder / "LuvBox.exe"
        shutil.copyfile(self.program, program)
        seen = folder / "LuvBox.exe.old"
        seen.write_bytes(b"removed by the launcher just before it reads its own footer")
        inner = (self.application / "LuvBox.exe").read_bytes()
        newer = self.program_with_archive([("LuvBox.exe", inner), ("_internal/newer.txt", b"the update")], "newer")
        self.root.mkdir(parents=True)
        environment, report = self.environment()
        # Hold the unpacking gate, as another launcher that is still unpacking does.
        with (self.root / ".lock").open("wb"):
            process = subprocess.Popen([str(program), "--check"], env=environment,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + 60
            while seen.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(seen.exists())
            time.sleep(0.5)                     # it has read its footer and now waits at the gate
            self.assertIsNone(process.poll())
            self.replace_as_an_update_does(program, newer)
        self.assertEqual(process.wait(timeout=120), 77)
        self.assertFalse(report.is_file())
        self.assertFalse(self.installed.exists())
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(program.read_bytes(), newer.read_bytes())
        # Starting again, as the message asks, runs the update.
        code, report = self.run_program("--check", program=program)
        self.assertEqual(code, 0)
        self.assertEqual((self.installed / "_internal" / "newer.txt").read_bytes(), b"the update")
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["LuvBox.exe"])

    def test_a_launcher_replaced_while_it_unpacks_still_unpacks_the_file_it_checked(self):
        folder = self.local / "place"
        folder.mkdir()
        program = folder / "LuvBox.exe"
        inner = (self.application / "LuvBox.exe").read_bytes()
        parts = [(f"_internal/part-{index:02}.bin", os.urandom(1024 * 1024)) for index in range(24)]
        shutil.copyfile(self.program_with_archive([("LuvBox.exe", inner)] + parts, "large"), program)
        newer = self.program_with_archive([("LuvBox.exe", inner), ("_internal/newer.txt", b"the update")],
                                          "newer-during")
        environment, report = self.environment(STANDIN_EXIT=5)
        process = subprocess.Popen([str(program), "--check"], env=environment,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and process.poll() is None:
            if self.root.is_dir() and any(path.name.startswith(".new-") for path in self.root.iterdir()):
                break
            time.sleep(0.002)
        self.replace_as_an_update_does(program, newer)
        self.assertEqual(process.wait(timeout=120), 5)
        self.assertTrue(report.is_file())
        for name, data in parts:
            self.assertEqual((self.installed / name).read_bytes(), data, name)
        self.assertFalse((self.installed / "_internal" / "newer.txt").exists())
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(program.read_bytes(), newer.read_bytes())

    def test_an_unpacked_copy_that_lost_its_program_is_unpacked_again(self):
        self.assertEqual(self.run_program("--check")[0], 0)
        (self.installed / "LuvBox.exe").unlink()
        code, report = self.run_program("--check")
        self.assertEqual(code, 0)
        self.assertTrue((self.installed / "LuvBox.exe").is_file())
        self.assertIsNotNone(report)

    def test_data_kept_under_the_former_name_is_adopted_before_the_application_folder_is_made(self):
        previous = self.local / "XlikeSaver"
        previous.mkdir()
        (previous / "settings.json").write_text('{"account": "kept"}', encoding="utf-8")
        code, _report = self.run_program("--check")
        self.assertEqual(code, 0)
        self.assertFalse(previous.exists())
        self.assertEqual((self.local / "LuvBox" / "settings.json").read_text(encoding="utf-8"), '{"account": "kept"}')
        self.assertTrue((self.installed / "LuvBox.exe").is_file())


if __name__ == "__main__":
    unittest.main()
