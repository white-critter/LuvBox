"""The updater against a local release server: what it reads, sends, refuses and replaces."""

from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from luvbox import launcher, updater
from tests.release_server import ReleaseServer, make_program
from tools import build_launcher


class VersionAndTextTests(unittest.TestCase):
    def test_versions_compare_as_numbers_not_text(self):
        self.assertEqual(updater.parse_version("v1.8.0"), (1, 8, 0))
        self.assertEqual(updater.parse_version("1.10.2"), (1, 10, 2))
        self.assertGreater(updater.parse_version("1.10.0"), updater.parse_version("1.9.9"))
        self.assertGreater(updater.parse_version("2.0.0"), updater.parse_version("1.99.99"))
        for text in ("", "1.8", "1.8.0-beta", "1.8.0.1", "v", "latest", " 1.8.0", "1.8.0\n", None, 180, "１.8.0"):
            with self.subTest(text=text):
                self.assertIsNone(updater.parse_version(text))

    def test_change_summary_takes_the_top_level_items_under_the_changes_heading(self):
        body = ("처음 문단입니다.\n\n### 주요 변경 사항\n\n"
                "- **파일 하나로 배포**: `LuvBox.exe` 하나를 [받아](https://example.invalid) 실행합니다.\n"
                "  - 들여 쓴 세부 항목\n- 둘째 항목\n\n### 검증\n\n- 테스트 1,300개\n\n```\nabc  LuvBox.exe\n```\n")
        self.assertEqual(updater.summarize(body),
                         "· 파일 하나로 배포: LuvBox.exe 하나를 받아 실행합니다.\n· 둘째 항목")
        self.assertEqual(updater.summarize("고친 것 하나\n\n다른 줄\n```\ncode\n```"), "· 고친 것 하나\n· 다른 줄")
        self.assertEqual(updater.summarize("\r\n".join(f"- 항목 {n}" for n in range(20))).count("\n"), 7)
        self.assertEqual(len(updater.summarize("- " + "가" * 900)), 2 + 220)
        for nothing in (None, "", 7, ["- item"], "### 주요 변경 사항\n"):
            self.assertEqual(updater.summarize(nothing), "")

    def test_the_daily_check_is_due_once_a_day_and_never_when_switched_off(self):
        now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
        recent = (now - timedelta(hours=23, minutes=59)).isoformat()
        old = (now - timedelta(hours=24)).isoformat()
        self.assertTrue(updater.check_due(True, "", now))
        self.assertTrue(updater.check_due(True, None, now))
        self.assertTrue(updater.check_due(True, "not a date", now))
        self.assertFalse(updater.check_due(True, recent, now))
        self.assertTrue(updater.check_due(True, old, now))
        # A clock that was set back must not postpone the check indefinitely.
        self.assertTrue(updater.check_due(True, (now + timedelta(days=30)).isoformat(), now))
        self.assertFalse(updater.check_due(True, "2026-11-12T11:00:00", now))  # stored without a zone: UTC
        for disabled in (False, None, "true", 1):
            self.assertFalse(updater.check_due(disabled, "", now))

    def test_release_hosts_are_https_github_only(self):
        allows = updater.GITHUB.allows
        for url in ("https://api.github.com/repos/white-critter/LuvBox/releases/latest",
                    "https://github.com/white-critter/LuvBox/releases/download/v1.8.0/LuvBox.exe",
                    "https://objects.githubusercontent.com/x", "https://release-assets.githubusercontent.com/x",
                    "https://GitHub.com:443/x"):
            self.assertTrue(allows(url), url)
        for url in ("http://github.com/x", "https://github.com.example.org/x", "https://examplegithubusercontent.com/x",
                    "https://user:secret@github.com/x", "https://github.com:8443/x", "https://127.0.0.1/x",
                    "file:///C:/LuvBox.exe", "ftp://github.com/x", "https:///x", ""):
            self.assertFalse(allows(url), url)


class ReleaseServerCase(unittest.TestCase):
    def setUp(self):
        self.server = ReleaseServer()
        self.addCleanup(self.server.close)
        self.folder = Path(tempfile.mkdtemp(prefix="luvbox-updater-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.downloads = self.folder / "updates"

    def fails(self, kind, call, *arguments, **options):
        with self.assertRaises(updater.UpdateError) as caught:
            call(*arguments, **options)
        self.assertEqual(caught.exception.kind, kind)
        self.assertEqual(str(caught.exception), updater.MESSAGES[kind])


class CheckTests(ReleaseServerCase):
    def test_a_newer_release_is_offered_with_its_change_summary(self):
        program = self.server.publish("1.9.0")
        found = updater.check("1.8.0", self.server.source)
        self.assertEqual(found, {"version": "1.9.0", "newer": True, "installable": True,
                                 "notes": "· 첫째: 고친 것\n· 둘째", "size": len(program)})

    def test_the_same_or_an_older_release_is_not_an_update(self):
        for current in ("1.9.0", "1.9.1", "1.10.0", "2.0.0"):
            with self.subTest(current=current):
                self.server.publish("1.9.0")
                found = updater.check(current, self.server.source)
                self.assertEqual((found["version"], found["newer"], found["installable"]), ("1.9.0", False, False))
        self.server.publish("1.10.0")
        self.assertTrue(updater.check("1.9.9", self.server.source)["newer"])

    def test_a_release_without_the_program_or_checksum_is_announced_but_not_fetched(self):
        for missing in ({"with_program": False}, {"with_checksum": False}):
            with self.subTest(missing=missing):
                self.server.publish("1.9.0", **missing)
                found = updater.check("1.8.0", self.server.source)
                self.assertTrue(found["newer"])
                self.assertFalse(found["installable"])
        # The 1.7.0 release: a versioned ZIP and EXE, nothing this updater can fetch.
        self.server.description = {"tag_name": "v1.7.0", "body": "- 이전 형식", "assets": [
            {"name": "LuvBox-1.7.0.zip", "size": 277_000_000, "state": "uploaded"},
            {"name": "SHA256-1.7.0.txt", "size": 164, "state": "uploaded"}]}
        found = updater.check("1.6.10", self.server.source)
        self.assertEqual((found["newer"], found["installable"], found["notes"]), (True, False, "· 이전 형식"))

    def test_a_program_of_an_implausible_size_or_still_uploading_is_not_fetched(self):
        self.server.publish("1.9.0")
        program = next(asset for asset in self.server.description["assets"] if asset["name"] == "LuvBox.exe")
        for change in ({"size": 12}, {"size": 3 * 1024 ** 3}, {"size": "104857600"}, {"size": True},
                       {"state": "starter"}):
            with self.subTest(change=change):
                original = dict(program)
                program.update(change)
                self.assertFalse(updater.check("1.8.0", self.server.source)["installable"])
                program.clear()
                program.update(original)

    def test_unexpected_descriptions_are_refused_with_a_fixed_message(self):
        self.server.publish("1.9.0")
        good = dict(self.server.description)
        for change in ({"tag_name": "nightly"}, {"tag_name": None}, {"draft": True}, {"prerelease": True}):
            with self.subTest(change=change):
                self.server.description = {**good, **change}
                self.fails("unexpected", updater.check, "1.8.0", self.server.source)
        for raw in (b"[]", b"null", b'"1.9.0"'):
            with self.subTest(raw=raw):
                self.server.raw_description = raw
                self.fails("unexpected", updater.check, "1.8.0", self.server.source)
        self.server.raw_description = b"{" + b" " * (1024 * 1024 + 8) + b"}"
        self.fails("unexpected", updater.check, "1.8.0", self.server.source)
        self.server.raw_description = b"not json"
        self.fails("network", updater.check, "1.8.0", self.server.source)
        self.fails("unexpected", updater.check, "dev-build", self.server.source)

    def test_server_answers_map_to_what_the_person_can_do(self):
        self.server.publish("1.9.0")
        self.server.status = 404
        self.assertFalse(updater.check("1.8.0", self.server.source)["newer"])
        for status, kind in ((403, "limited"), (429, "limited"), (500, "network"), (502, "network")):
            with self.subTest(status=status):
                self.server.status = status
                self.fails(kind, updater.check, "1.8.0", self.server.source)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            unused = probe.getsockname()[1]
        nowhere = updater.Source(latest=f"http://127.0.0.1:{unused}/latest",
                                 downloads=f"http://127.0.0.1:{unused}/download", secure=False)
        self.fails("network", updater.check, "1.8.0", nowhere, timeout=3)

    def test_requests_carry_a_generic_client_name_and_nothing_about_this_pc(self):
        program = self.server.publish("1.9.0")
        with patch.dict(os.environ, {"USERNAME": "private-user-name", "COMPUTERNAME": "PRIVATE-PC"}):
            updater.check("1.8.0", self.server.source)
            updater.download("1.9.0", self.downloads, self.server.source, size=len(program))
        self.assertEqual([path for path, _headers in self.server.requests],
                         ["/latest", "/download/v1.9.0/SHA256-1.9.0.txt", "/download/v1.9.0/LuvBox.exe"])
        for path, headers in self.server.requests:
            with self.subTest(path=path):
                self.assertEqual(headers["user-agent"], "LuvBox-Updater")
                self.assertLessEqual(set(headers), {"host", "user-agent", "accept", "accept-encoding", "connection"})
                self.assertNotIn("?", path)
                sent = " ".join([path, *headers.values()])
                for private in ("private-user-name", "PRIVATE-PC", "1.8.0", os.environ.get("USERNAME", "\0")):
                    self.assertNotIn(private, sent)

    def test_the_default_source_refuses_anything_but_github_before_connecting(self):
        elsewhere = updater.Source(latest="https://example.org/latest", downloads="https://example.org/download")
        with patch.object(updater.urllib.request, "build_opener", side_effect=AssertionError("no connection")):
            self.fails("unexpected", updater.check, "1.8.0", elsewhere)
            self.fails("unexpected", updater.download, "1.9.0", self.downloads, elsewhere)


class DownloadTests(ReleaseServerCase):
    def leftovers(self):
        return sorted(path.name for path in self.downloads.iterdir()) if self.downloads.is_dir() else []

    def test_a_verified_program_is_kept_with_its_checksum(self):
        program = self.server.publish("1.9.0")
        seen = []
        path, digest = updater.download("1.9.0", self.downloads, self.server.source, size=len(program),
                                        progress=lambda received, total: seen.append((received, total)))
        self.assertEqual(path, self.downloads / "LuvBox-1.9.0.exe")
        self.assertEqual(path.read_bytes(), program)
        self.assertEqual(digest, hashlib.sha256(program).hexdigest())
        self.assertEqual(seen[-1], (len(program), len(program)))
        self.assertEqual(self.leftovers(), ["LuvBox-1.9.0.exe"])

    def test_a_program_that_differs_from_the_published_checksum_is_deleted(self):
        program = self.server.publish("1.9.0", checksum="a" * 64)
        self.fails("checksum", updater.download, "1.9.0", self.downloads, self.server.source, size=len(program))
        self.assertEqual(self.leftovers(), [])
        # One changed byte in transit, the checksum file intact.
        program = self.server.publish("1.9.0")
        changed = bytearray(program)
        changed[len(changed) // 2] ^= 1
        self.server.files["/download/v1.9.0/LuvBox.exe"] = bytes(changed)
        self.fails("checksum", updater.download, "1.9.0", self.downloads, self.server.source, size=len(program))
        self.assertEqual(self.leftovers(), [])

    def test_a_file_that_is_not_this_release_of_luvbox_is_refused_even_with_a_matching_checksum(self):
        other_version = make_program("1.8.5")
        self.server.publish("1.9.0", other_version)
        self.fails("program", updater.download, "1.9.0", self.downloads, self.server.source)
        not_a_program = os.urandom(2 * 1024 * 1024)
        self.server.publish("1.9.0", not_a_program)
        self.fails("program", updater.download, "1.9.0", self.downloads, self.server.source)
        damaged = bytearray(make_program("1.9.0"))
        damaged[-70] ^= 0xFF  # inside the footer: the archive length no longer adds up
        self.server.publish("1.9.0", bytes(damaged))
        self.fails("program", updater.download, "1.9.0", self.downloads, self.server.source)
        self.assertEqual(self.leftovers(), [])

    def test_a_transfer_that_stops_early_or_is_cancelled_leaves_nothing_behind(self):
        program = self.server.publish("1.9.0", make_program("1.9.0", os.urandom(1024 * 1024)))
        self.server.cut_short.add("/download/v1.9.0/LuvBox.exe")
        self.fails("download", updater.download, "1.9.0", self.downloads, self.server.source, size=len(program))
        self.assertEqual(self.leftovers(), [])
        self.server.cut_short.clear()
        stop = threading.Event()
        self.fails("cancelled", updater.download, "1.9.0", self.downloads, self.server.source, size=len(program),
                   progress=lambda received, total: stop.set(), cancelled=stop.is_set)
        self.assertEqual(self.leftovers(), [])

    def test_a_release_without_a_usable_checksum_row_costs_no_large_download(self):
        program = self.server.publish("1.9.0")
        for text in (b"", b"not a checksum file", hashlib.sha256(program).hexdigest().encode() + b" LuvBox.exe\n",
                     hashlib.sha256(program).hexdigest().upper().encode() + b"  LuvBox.exe\n",
                     hashlib.sha256(program).hexdigest().encode() + b"  LuvBox-1.9.0.exe\n"):
            with self.subTest(text=text):
                self.server.requests.clear()
                self.server.files["/download/v1.9.0/SHA256-1.9.0.txt"] = text
                self.fails("unexpected", updater.download, "1.9.0", self.downloads, self.server.source)
                self.assertEqual([path for path, _headers in self.server.requests], ["/download/v1.9.0/SHA256-1.9.0.txt"])
        del self.server.files["/download/v1.9.0/SHA256-1.9.0.txt"]
        self.fails("download", updater.download, "1.9.0", self.downloads, self.server.source)
        self.server.files["/download/v1.9.0/SHA256-1.9.0.txt"] = b"x" * (64 * 1024 + 1)
        self.fails("unexpected", updater.download, "1.9.0", self.downloads, self.server.source)

    def test_more_bytes_than_announced_and_redirects_elsewhere_are_refused(self):
        program = self.server.publish("1.9.0")
        self.fails("unexpected", updater.download, "v1.9.0", self.downloads, self.server.source)
        self.fails("unexpected", updater.download, "../1.9.0", self.downloads, self.server.source)
        redirecting = updater.Source(latest=self.server.source.latest,
                                     downloads=f"http://127.0.0.1:{self.server.port}/download", secure=False)
        with patch.object(updater.Source, "asset", lambda _self, _version, _name: redirecting.downloads + "/elsewhere"):
            self.fails("download", updater.download, "1.9.0", self.downloads, redirecting)
        self.assertEqual(self.leftovers(), [])
        self.assertGreater(len(program), 0)

    def test_fetched_programs_are_cleared_without_touching_other_files(self):
        self.downloads.mkdir()
        for name in ("LuvBox-1.9.0.exe", "LuvBox-1.9.0.exe.part", "LuvBox-1.10.2.exe", "notes.txt",
                     "LuvBox-1.9.0.exe.bak", "LuvBox.exe"):
            (self.downloads / name).write_bytes(b"x")
        (self.downloads / "LuvBox-2.0.0.exe").mkdir()
        updater.clear_downloads(self.downloads)
        self.assertEqual(self.leftovers(), ["LuvBox-1.9.0.exe.bak", "LuvBox-2.0.0.exe", "LuvBox.exe", "notes.txt"])
        updater.clear_downloads(self.folder / "missing")

    def test_the_sweep_at_start_leaves_what_was_touched_recently(self):
        self.downloads.mkdir()
        an_hour_ago = time.time() - 3600
        for name, stamp in (("LuvBox-1.9.0.exe.part", an_hour_ago), ("LuvBox-1.9.0.exe", an_hour_ago),
                            ("LuvBox-1.9.1.exe.part", None), ("LuvBox-1.9.1.exe", None), ("notes.txt", an_hour_ago)):
            (self.downloads / name).write_bytes(b"x")
            if stamp:
                os.utime(self.downloads / name, (stamp, stamp))
        updater.clear_downloads(self.downloads, older_than=600)
        # Another LuvBox may still be fetching or about to install the recent ones.
        self.assertEqual(self.leftovers(), ["LuvBox-1.9.1.exe", "LuvBox-1.9.1.exe.part", "notes.txt"])
        updater.clear_downloads(self.folder / "missing", older_than=600)


class InstallTests(ReleaseServerCase):
    def setUp(self):
        super().setUp()
        self.place = self.folder / "한글 폴더 with space"
        self.place.mkdir()
        self.launcher = self.place / "LuvBox.exe"
        self.old = make_program("1.8.0")
        self.launcher.write_bytes(self.old)
        self.new = make_program("1.9.0")
        self.downloaded = self.folder / "LuvBox-1.9.0.exe"
        self.downloaded.write_bytes(self.new)
        self.digest = hashlib.sha256(self.new).hexdigest()

    def names(self):
        return sorted(path.name for path in self.place.iterdir())

    def test_the_new_program_takes_the_launchers_place_and_name(self):
        updater.install(self.downloaded, self.launcher, self.digest)
        self.assertEqual(self.launcher.read_bytes(), self.new)
        self.assertEqual(launcher.read_footer(self.launcher)["version"], "1.9.0")
        # Nothing else is left beside it when the old program was not running.
        self.assertEqual(self.names(), ["LuvBox.exe"])

    def test_the_whole_flow_from_a_release_to_the_replaced_launcher(self):
        program = self.server.publish("1.9.0", self.new)
        found = updater.check("1.8.0", self.server.source)
        path, digest = updater.download(found["version"], self.downloads, self.server.source, size=found["size"])
        updater.install(path, self.launcher, digest)
        updater.clear_downloads(self.downloads)
        self.assertEqual(self.launcher.read_bytes(), program)
        self.assertEqual(list(self.downloads.iterdir()), [])

    def test_a_copy_that_no_longer_matches_is_not_installed(self):
        self.fails("checksum", updater.install, self.downloaded, self.launcher, "0" * 64)
        self.assertEqual(self.launcher.read_bytes(), self.old)
        self.assertEqual(self.names(), ["LuvBox.exe"])

    def test_a_failed_second_rename_puts_the_old_program_back(self):
        replace = os.replace
        calls = []

        def failing(source, target):
            calls.append((Path(source).name, Path(target).name))
            if len(calls) == 2:
                raise PermissionError("the folder became read-only")
            return replace(source, target)

        with patch.object(updater.os, "replace", failing):
            self.fails("replace", updater.install, self.downloaded, self.launcher, self.digest)
        self.assertEqual(calls, [("LuvBox.exe", "LuvBox.exe.old"), ("LuvBox.exe.new", "LuvBox.exe"),
                                 ("LuvBox.exe.old", "LuvBox.exe")])
        self.assertEqual(self.launcher.read_bytes(), self.old)
        self.assertEqual(self.names(), ["LuvBox.exe"])

    def test_a_launcher_that_cannot_be_set_aside_or_is_gone_is_left_alone(self):
        with patch.object(updater.os, "replace", side_effect=PermissionError("locked")):
            self.fails("replace", updater.install, self.downloaded, self.launcher, self.digest)
        self.assertEqual(self.launcher.read_bytes(), self.old)
        self.assertEqual(self.names(), ["LuvBox.exe"])
        self.launcher.unlink()
        self.fails("replace", updater.install, self.downloaded, self.launcher, self.digest)
        self.assertEqual(self.names(), [])

    def test_a_folder_that_cannot_be_written_reports_what_to_do(self):
        with patch.object(Path, "open", side_effect=PermissionError("read-only folder")):
            self.fails("replace", updater.install, self.downloaded, self.launcher, self.digest)
        self.assertEqual(self.launcher.read_bytes(), self.old)
        self.assertIn("릴리스 페이지에서 직접", updater.MESSAGES["replace"])

    def test_an_earlier_replaced_program_still_in_use_gets_the_next_name(self):
        earlier = updater.replaced_name(self.launcher)
        earlier.write_bytes(b"an earlier program a scheduled run still holds")
        unlink = Path.unlink
        replace = os.replace
        renamed = []

        def held(path, *arguments, **options):
            if path == earlier:
                raise PermissionError("in use")
            return unlink(path, *arguments, **options)

        def recorded(source, target):
            renamed.append((Path(source).name, Path(target).name))
            return replace(source, target)

        with patch.object(Path, "unlink", held), patch.object(updater.os, "replace", recorded):
            updater.install(self.downloaded, self.launcher, self.digest)
        self.assertEqual(self.launcher.read_bytes(), self.new)
        self.assertEqual(renamed, [("LuvBox.exe", "LuvBox.exe.old.1"), ("LuvBox.exe.new", "LuvBox.exe")])
        # The one just replaced was not running and is already gone; the held one waits.
        self.assertEqual(self.names(), ["LuvBox.exe", "LuvBox.exe.old"])
        self.assertEqual(earlier.read_bytes(), b"an earlier program a scheduled run still holds")
        self.assertEqual([updater.replaced_name(self.launcher, index).name for index in (0, 1, 12)],
                         ["LuvBox.exe.old", "LuvBox.exe.old.1", "LuvBox.exe.old.12"])


def _compiler():
    try:
        return build_launcher.compiler()
    except FileNotFoundError:
        return None


@unittest.skipUnless(os.name == "nt" and _compiler() is not None, "needs the Windows C# compiler")
class RunningProgramTests(ReleaseServerCase):
    def test_a_launcher_that_is_running_is_renamed_aside_and_removed_once_it_ends(self):
        # A real program that is running: Windows lets it be renamed, not overwritten or deleted.
        source = self.folder / "wait.cs"
        source.write_text("static class Wait { static void Main() { System.Threading.Thread.Sleep(60000); } }",
                          encoding="utf-8")
        program = self.folder / "LuvBox.exe"
        built = subprocess.run([str(_compiler()), "/nologo", "/target:winexe", f"/out:{program}", str(source)],
                               capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        running_bytes = program.read_bytes()
        process = subprocess.Popen([str(program)], creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(process.kill)
        time.sleep(0.5)
        with self.assertRaises(PermissionError):
            program.write_bytes(b"cannot overwrite a running program")
        new = make_program("1.9.0")
        downloaded = self.folder / "LuvBox-1.9.0.exe"
        downloaded.write_bytes(new)
        updater.install(downloaded, program, hashlib.sha256(new).hexdigest())
        self.assertEqual(program.read_bytes(), new)
        aside = updater.replaced_name(program)
        self.assertEqual(aside.read_bytes(), running_bytes)
        self.assertIsNone(process.poll())
        process.kill()
        process.wait(10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                aside.unlink()
                break
            except PermissionError:
                time.sleep(0.1)
        self.assertFalse(aside.exists())


if __name__ == "__main__":
    unittest.main()
