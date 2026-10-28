"""Offline rename migration and unattended transfer regressions."""

import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import requests
from gallery_dl.extractor.common import Extractor
from gallery_dl.extractor.message import Message

import test_core
from luvbox import core, login, protection, settings


class AppDataMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        env = patch.dict(os.environ, {"LOCALAPPDATA": str(self.base)})
        env.start()
        self.addCleanup(env.stop)
        self.old, self.new = self.base / "XlikeSaver", self.base / "LuvBox"

    def old_data(self):
        self.old.mkdir()
        (self.old / "settings.json").write_text('{"account":"fixture"}', encoding="utf-8")
        (self.old / "session.bin").write_bytes(b"encrypted-session-fixture")
        record = self.old / "records" / "fixture"
        record.mkdir(parents=True)
        for suffix in ("", "-wal", "-shm"):
            (record / ("old.sqlite3" + suffix)).write_bytes(suffix.encode() or b"database-fixture")

    def test_atomic_root_move_keeps_preferences_session_and_sqlite_sidecars_together(self):
        self.old_data()
        self.assertEqual(settings.load_settings(), {"account": "fixture"})
        self.assertFalse(self.old.exists())
        self.assertEqual(login.session_path(), self.new / "session.bin")
        self.assertEqual(protection.state_directory(), self.new / "request-protection")
        self.assertEqual((self.new / "records" / "fixture" / "old.sqlite3-wal").read_bytes(), b"-wal")
        self.assertEqual((self.new / "session.bin").read_bytes(), b"encrypted-session-fixture")

    def test_empty_destination_does_not_block_an_atomic_root_move(self):
        self.old_data()
        self.new.mkdir()
        self.assertEqual(settings.app_directory(), self.new)
        self.assertTrue((self.new / "session.bin").is_file())

    def test_failed_move_never_starts_with_empty_settings_or_new_protection_state(self):
        self.old_data()
        with patch.object(Path, "rename", side_effect=PermissionError("offline locked fixture")):
            for read in (settings.load_settings, login.session_path, protection.state_directory):
                with self.subTest(read=read.__name__), self.assertRaises(settings.AppDataError):
                    read()
        self.assertFalse(self.new.exists())
        self.assertTrue((self.old / "session.bin").is_file())

    def test_conflicting_libraries_are_preserved_and_require_resolution(self):
        self.old_data()
        self.new.mkdir()
        fixture = self.new / "settings.json"
        fixture.write_text('{"account":"other"}')
        with self.assertRaises(settings.AppDataError):
            settings.load_settings()
        self.assertEqual(fixture.read_text(), '{"account":"other"}')
        self.assertTrue((self.old / "session.bin").is_file())

    def old_build_leftovers(self):
        # What the previous build writes by itself after the move, e.g. from
        # a daily task that still starts the old executable.
        (self.old / "logs").mkdir(parents=True)
        (self.old / "logs" / "scheduled-20261016-090000.log").write_text("old build log", encoding="utf-8")
        (self.old / "last-scheduled.json").write_text('{"failed": true}', encoding="utf-8")
        (self.old / "cache").mkdir()
        (self.old / "cache" / "theme-1.0.0-light-fixture.zip").write_bytes(b"theme-cache")
        # Its window marks that failed run seen when the notice opens it.
        (self.old / "last-scheduled-seen").mkdir()
        (self.old / "last-scheduled-seen" / "0123abcd.json").write_text('{"outcome": {}}', encoding="utf-8")
        (self.old / "last-scheduled-k3j2.tmp").write_bytes(b"interrupted write")

    def test_leftovers_of_an_older_build_after_the_move_are_kept_aside_without_a_lockout(self):
        self.old_data()
        self.assertEqual(settings.load_settings(), {"account": "fixture"})
        for run in (1, 2):
            with self.subTest(run=run):
                self.old_build_leftovers()
                self.assertEqual(settings.load_settings(), {"account": "fixture"})
                self.assertFalse(self.old.exists())
                kept = sorted((self.new / "legacy").iterdir())
                self.assertEqual(len(kept), run)
                self.assertTrue(all(path.name.startswith("XlikeSaver-") for path in kept))
                self.assertEqual((kept[-1] / "logs" / "scheduled-20261016-090000.log").read_text(encoding="utf-8"),
                                 "old build log")
        self.assertEqual((self.new / "session.bin").read_bytes(), b"encrypted-session-fixture")

    def test_old_build_settings_after_the_move_need_a_choice_and_are_only_moved_on_request(self):
        self.old_data()
        settings.load_settings()
        self.old_build_leftovers()
        (self.old / "settings.json").write_text('{"appearance":"dark"}', encoding="utf-8")
        with self.assertRaises(settings.AppDataConflict) as raised:
            settings.load_settings()
        self.assertEqual(str(raised.exception), settings.CONFLICT_HELP)
        self.assertTrue((self.old / "settings.json").is_file())
        self.assertFalse((self.new / "legacy").exists())
        kept = settings.set_aside_previous()
        self.assertEqual(kept.parent, self.new / "legacy")
        self.assertEqual((kept / "settings.json").read_text(encoding="utf-8"), '{"appearance":"dark"}')
        self.assertEqual(settings.load_settings(), {"account": "fixture"})
        self.assertIsNone(settings.set_aside_previous())

    def test_scheduled_run_reports_a_data_folder_conflict_instead_of_crashing(self):
        from luvbox import scheduled

        self.old_data()
        self.new.mkdir()
        (self.new / "settings.json").write_text('{"account":"other"}', encoding="utf-8")
        with patch("luvbox.scheduled._notify_failure") as notify, patch("luvbox.core.run_worker") as worker:
            self.assertEqual(scheduled.run_saved(), 1)
        worker.assert_not_called()
        outcome = notify.call_args.args[0]
        self.assertIn(settings.CONFLICT_HELP, outcome["message"])
        self.assertFalse(outcome["log"])
        self.assertTrue((self.old / "session.bin").is_file())
        self.assertEqual((self.new / "settings.json").read_text(encoding="utf-8"), '{"account":"other"}')

    def test_old_dpapi_pair_is_read_and_new_writes_use_current_header(self):
        payload = {"version": 1, "cookies": [{"name": "auth_token", "value": "offline-fixture",
                    "domain": ".x.com", "path": "/", "expires": -1, "secure": True, "httpOnly": True}]}
        self.new.mkdir()
        session = self.new / "session.bin"
        session.write_bytes(login._LEGACY_HEADER + b"legacy-ciphertext")
        with patch("luvbox.login._dpapi", return_value=json.dumps(payload).encode()) as decrypt:
            self.assertEqual(login.load_session_cookies()[0]["name"], "auth_token")
        decrypt.assert_called_once_with(b"legacy-ciphertext", protect=False, entropy=login._LEGACY_ENTROPY)
        with patch("luvbox.login._dpapi", side_effect=[json.dumps(payload).encode(), b"current-ciphertext"]) as encrypt:
            login.save_auth_token("offline-fixture")
        self.assertEqual(session.read_bytes(), login._HEADER + b"current-ciphertext")
        self.assertEqual(encrypt.call_count, 2)
        self.assertEqual(encrypt.call_args_list[0].args, (b"legacy-ciphertext",))
        self.assertEqual(encrypt.call_args_list[0].kwargs,
                         {"protect": False, "entropy": login._LEGACY_ENTROPY})
        self.assertEqual(encrypt.call_args.kwargs, {"protect": True})

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_native_legacy_dpapi_ciphertext_remains_readable_after_root_migration(self):
        self.old.mkdir()
        payload = {"version": 1, "cookies": [{"name": "auth_token", "value": "offline-native-fixture",
                    "domain": ".x.com", "path": "/", "expires": -1, "secure": True, "httpOnly": True}]}
        encrypted = login._dpapi(json.dumps(payload).encode(), protect=True, entropy=login._LEGACY_ENTROPY)
        (self.old / "session.bin").write_bytes(login._LEGACY_HEADER + encrypted)
        self.assertEqual(login.load_session_cookies()[0]["value"], "offline-native-fixture")
        self.assertFalse(self.old.exists())
        self.assertEqual((self.new / "session.bin").read_bytes(), login._LEGACY_HEADER + encrypted)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_native_relogin_preserves_old_dpapi_account_in_the_current_format(self):
        self.old.mkdir()
        cookies = [{"name": "auth_token", "value": "offline-native-fixture", "domain": ".x.com",
                    "path": "/", "expires": -1, "secure": True, "httpOnly": True}]
        identity = {"user_id": "123", "handle": "legacy_fixture", "display_name": "Legacy fixture"}
        payload = {"version": 1, "cookies": cookies, "identity": identity}
        encrypted = login._dpapi(json.dumps(payload).encode(), protect=True, entropy=login._LEGACY_ENTROPY)
        (self.old / "session.bin").write_bytes(login._LEGACY_HEADER + encrypted)

        login.save_auth_token("new-native-fixture")

        self.assertFalse(self.old.exists())
        self.assertTrue((self.new / "session.bin").read_bytes().startswith(login._HEADER))
        self.assertEqual(login.load_session_cookies()[0]["value"], "new-native-fixture")
        self.assertEqual([row["user_id"] for row in login.list_accounts()], ["123"])
        login.select_account("123")
        self.assertEqual(login.active_account(), identity)
        self.assertEqual(login.load_session_cookies()[0]["value"], "offline-native-fixture")


class UnattendedTransferTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    set_session = test_core.CoreTests.set_session
    worker = test_core.CoreTests.worker

    def test_unattended_final_collision_preserves_both_files_and_can_resume_locally(self):
        original = test_core.FakeHttpDownloader.download
        collision = []

        def download(instance, url, pathfmt):
            result = original(instance, url, pathfmt)
            destination = Path(pathfmt.realpath)
            destination.write_bytes(b"user-file-restored-during-transfer")
            collision.append(destination)
            return result

        with patch.object(test_core.FakeHttpDownloader, "download", download):
            result, events = self.worker([("100", 1, "photo", "alice")])
        self.assertEqual((result, events[-1]["saved"]), (1, 0))
        destination = collision[0]
        self.assertEqual(destination.read_bytes(), b"user-file-restored-during-transfer")
        source = Path(str(destination) + ".part")
        self.assertTrue(core._transfer_complete(source))
        destination.unlink()
        test_core.FakeHttpDownloader.downloaded = []
        result, events = self.worker([("100", 1, "photo", "alice")])
        self.assertEqual((result, events[-1]["saved"]), (0, 1))
        self.assertFalse(test_core.FakeHttpDownloader.downloaded)
        self.assertTrue(destination.read_bytes().startswith(b"\xff\xd8\xff"))

    def test_legacy_completion_is_reused_and_only_validated_markers_are_cleaned(self):
        from dataclasses import replace
        options = replace(self.options, folder_mode="flat", separate_accounts=False)
        self.output.mkdir()
        source = self.output / "100_1.jpg.part"
        source.write_bytes(b"\xff\xd8\xffcompleted-before-the-rename")
        core._mark_transfer_complete(source)
        current = Path(str(source) + ".luvbox-complete")
        legacy = Path(str(source) + ".xlikesaver-complete")
        current.rename(legacy)
        current.write_text("user-changed-marker", encoding="utf-8")
        result, events = self.worker([("100", 1, "photo", "alice")], options=options)
        self.assertEqual((result, events[-1]["saved"]), (0, 1))
        self.assertFalse(test_core.FakeHttpDownloader.downloaded)
        self.assertFalse(legacy.exists())
        self.assertEqual(current.read_text(), "user-changed-marker")

    def test_plain_server_error_stops_selected_retries_before_the_next_tweet(self):
        from dataclasses import replace
        requested = []

        class Tweet(Extractor):
            category = "twitter"
            subcategory = "tweet"
            cookies_domain = ".x.com"
            filename_fmt = "{tweet_id}_{num}.{extension}"
            archive_fmt = "{tweet_id}_{num}"

            def __init__(self, url):
                match = re.fullmatch(r"https://x\.com/i/web/status/(\d+)", url)
                super().__init__(match)
                self.ident = match[1]

            def items(self):
                self.request("https://x.com/offline-api/" + self.ident)
                yield Message.Directory, "", {}

        def send(session, request, **kwargs):
            requested.append(request.url)
            response = requests.Response()
            response.status_code, response.url = 503, request.url
            response.reason = "Service Unavailable"
            response._content, response._content_consumed = b"offline service error", True
            response.headers["Content-Type"] = "text/html"
            return response

        events = []
        options = replace(self.options, targets=("100_1", "200_1"))
        with patch("gallery_dl.extractor.find", side_effect=Tweet), patch("requests.Session.send", new=send):
            result = core.run_worker(options, events.append)
        self.assertEqual((result, events[-1]["status"]), (1, "error"))
        self.assertEqual(requested, ["https://x.com/offline-api/100"])
        self.assertIn("서버 오류", events[-1]["message"])


if __name__ == "__main__":
    unittest.main()
