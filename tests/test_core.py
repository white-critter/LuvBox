"""Offline checks using the real gallery-dl job/path/archive pipeline."""

import json
import re
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from gallery_dl import config, exception
from gallery_dl.extractor.common import Extractor
from gallery_dl.extractor.message import Message

from xlikesaver.core import JobOptions, run_worker
from xlikesaver.storage import resolve_storage


SECRET = "offline-test-cookie-secret-never-emit"


class FakeLikesExtractor(Extractor):
    category = "twitter"
    subcategory = "likes"
    cookies_domain = ".x.com"
    filename_fmt = "{tweet_id}_{num}.{extension}"
    archive_fmt = "{tweet_id}_{num}"

    def __init__(self, url, rows, error=None, before_row=None):
        super().__init__(re.match(r"https://x\.com/([^/]+)/likes", url))
        self.rows = rows
        self.error = error
        self.before_row = before_row

    def items(self):
        if self.error:
            raise self.error
        for row in self.rows:
            if self.before_row:
                self.before_row(self, row)
            metadata = {"tweet_id": row[0], "num": row[1], "type": row[2],
                        "extension": "mp4" if row[2] in {"video", "animated_gif"} else "jpg",
                        "author": {"name": row[3]}, "user": {"name": "myaccount", "id": 999}}
            if len(row) > 4:
                metadata["date"] = row[4]
            if len(row) > 5:
                metadata["author"]["id"] = row[5]
            yield Message.Directory, "", metadata
            url = (f"https://video.twimg.com/{row[0]}_{row[1]}.mp4" if row[2] in {"video", "animated_gif"}
                   else f"https://pbs.twimg.com/media/{row[0]}_{row[1]}?format=jpg&name=orig")
            yield Message.Url, url, metadata


class FakeHttpDownloader:
    scheme = "http"
    downloaded = []
    failure = False

    def __init__(self, job):
        self.job = job

    def download(self, url, pathfmt):
        if self.failure:
            self.job.log.warning("PermissionError: cookie auth_token=" + SECRET)
            return False
        self.downloaded.append(url)
        with pathfmt.open() as fp:
            fp.write(b"\xff\xd8\xffoffline-photo")
        return True


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.protection_root = patch("xlikesaver.protection.state_directory", return_value=self.base / "protection")
        self.protection_root.start()
        self.addCleanup(self.protection_root.stop)
        self.output = self.base / "photos"
        # Live records are on this PC (patched app data), not in the output.
        self.record_dir = resolve_storage(self.output, "myaccount").record_dir
        self.cookie_file = self.base / "cookies.txt"
        self.write_cookies()
        self.options = JobOptions("@MyAccount", str(self.output), "file", str(self.cookie_file),
                                  folder_mode="author", include_videos=False, include_gifs=False, separate_accounts=True)
        self.metadata_available = patch("xlikesaver.metadata.available", return_value=True)
        self.metadata_available.start()
        self.addCleanup(self.metadata_available.stop)
        self.metadata_embed = patch("xlikesaver.metadata.embed_source",
                                    side_effect=lambda path, tweet_id, **values: {"status": "embedded", "url": f"https://x.com/i/web/status/{tweet_id}", "message": "", "author_status": "embedded" if values.get("author") or values.get("author_id") else "unknown"})
        self.embed_source = self.metadata_embed.start()
        self.addCleanup(self.metadata_embed.stop)
        FakeHttpDownloader.downloaded = []
        FakeHttpDownloader.failure = False

    def tearDown(self):
        self.temp.cleanup()

    def write_cookies(self, domain=".x.com", expiry="0", name="auth_token", value=SECRET):
        self.cookie_file.write_text(
            f"# Netscape HTTP Cookie File\n#HttpOnly_{domain}\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}\n",
            encoding="utf-8")

    def worker(self, rows=(), *, options=None, error=None, before_row=None, stop_file=None):
        events = []
        with patch("gallery_dl.extractor.find", side_effect=lambda url: FakeLikesExtractor(url, rows, error, before_row)), \
                patch("gallery_dl.downloader.find", return_value=FakeHttpDownloader):
            result = run_worker(options or self.options, events.append, stop_file)
        self.assertNotIn(SECRET, json.dumps(events))
        self.assertEqual(events[-1]["type"], "finished")
        return result, events

    def runs(self):
        with closing(sqlite3.connect(self.record_dir / ".xlikesaver-history.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM runs ORDER BY id")]

    def copied_records_for_new_output(self, output):
        """Simulate the verified ledger transfer without copying media."""
        target = resolve_storage(output, "myaccount").record_dir
        target.mkdir(parents=True)
        for source in self.record_dir.glob("*.sqlite3"):
            with closing(sqlite3.connect(source)) as old, closing(sqlite3.connect(target / source.name)) as copied:
                old.backup(copied)
        with closing(sqlite3.connect(target / ".xlikesaver-history.sqlite3")) as catalog:
            catalog.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('allowed_media_roots',?)",
                            (json.dumps([str(self.output), str(output)]),))
            catalog.commit()
        return target

    def test_output_change_skips_existing_old_media_and_saves_new_media_at_new_root(self):
        from xlikesaver.history import HistoryStore
        from xlikesaver.storage import migrate_records, resolve_storage

        options = replace(self.options, folder_mode="flat", separate_accounts=False)
        first, events = self.worker([("100", 1, "photo", "alice")], options=options)
        self.assertEqual(first, 0)
        old_path = self.output / "100_1.jpg"
        original = HistoryStore.for_storage(resolve_storage(self.output, "myaccount"))
        try:
            before = original.records()[0]
        finally:
            original.close()
        new_output = self.base / "new-output"
        destination = resolve_storage(new_output, "myaccount")
        migrated = migrate_records(resolve_storage(self.output, "myaccount"), destination)
        self.assertTrue(migrated.changed)
        target = destination.record_dir
        result, events = self.worker([("101", 1, "photo", "alice"), ("100", 1, "photo", "alice")],
                                     options=replace(options, output_dir=str(new_output)))
        self.assertEqual(result, 0)
        self.assertEqual((events[-1]["saved"], events[-1]["skipped"]), (1, 1))
        self.assertEqual(len(FakeHttpDownloader.downloaded), 2)
        self.assertTrue(old_path.is_file())
        self.assertFalse((new_output / old_path.name).exists())
        self.assertEqual(next(event for event in events if event["type"] == "skipped")["path"], str(old_path))
        self.assertEqual(next(event for event in events if event["type"] == "saved")["path"], str(new_output / "101_1.jpg"))
        with closing(sqlite3.connect(target / ".xlikesaver-history.sqlite3")) as catalog:
            catalog.row_factory = sqlite3.Row
            after = dict(catalog.execute("SELECT * FROM media WHERE tweet_id='100'").fetchone())
        for key in ("path", "saved_at", "source_url", "metadata_status"):
            self.assertEqual(after[key], before[key])

    def test_former_output_concurrent_writer_blocks_worker_before_network(self):
        from xlikesaver.storage import OutputGuard

        first, _ = self.worker([("100", 1, "photo", "alice")])
        self.assertEqual(first, 0)
        new_output = self.base / "new-output"
        self.copied_records_for_new_output(new_output)
        guard = OutputGuard(self.output)
        guard.acquire()
        try:
            with patch("gallery_dl.extractor.find") as find:
                events = []
                result = run_worker(replace(self.options, output_dir=str(new_output)), events.append)
            find.assert_not_called()
            self.assertNotEqual(result, 0)
            self.assertEqual((events[-1]["saved"], events[-1]["skipped"]), (0, 0))
        finally:
            guard.close()
        result, _ = self.worker([("100", 1, "photo", "alice")],
                                options=replace(self.options, output_dir=str(new_output)))
        self.assertEqual(result, 0)

    def test_accepts_handles_and_profile_urls(self):
        for account in ("@MyAccount", "MyAccount", "https://x.com/MyAccount", "https://twitter.com/MyAccount/?s=20", "x.com/MyAccount/likes"):
            with self.subTest(account=account):
                options = JobOptions(account, str(self.output)).validate()
                self.assertEqual(options.account, "myaccount")
                self.assertEqual(options.output_dir, str(self.output.resolve()))

    def test_rejects_non_profile_urls_and_invalid_accounts(self):
        for account in ("", "@two words", "too_long_account_name", "https://example.com/person", "https://x.com/a/status/123", "https://x.com/settings", "https://x.com@evil.example/a", "https://x.com:443/person"):
            with self.subTest(account=account), self.assertRaises(ValueError):
                JobOptions(account, str(self.output)).validate()

    def test_requires_netscape_authenticated_x_cookie_file(self):
        for data in ("{}", "# Netscape HTTP Cookie File\n", "# Netscape HTTP Cookie File\n" + SECRET,
                     f"# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tauth_token\t{SECRET}\n"):
            self.cookie_file.write_text(data, encoding="utf-8")
            with self.subTest(data=data[:20]), self.assertRaises(ValueError) as raised:
                self.options.validate()
            self.assertNotIn(SECRET, str(raised.exception))

    def test_rejects_expired_auth(self):
        self.write_cookies(expiry=str(int(time.time()) - 20))
        with self.assertRaisesRegex(ValueError, "만료"):
            self.options.validate()

    def test_rejects_invalid_limit_and_output_file(self):
        for limit in (-1, 2.5, True, "20"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                JobOptions("me", str(self.output), photo_limit=limit).validate()
        with self.assertRaises(ValueError):
            JobOptions("me", str(self.cookie_file)).validate()

    def test_validates_layout_and_scan_mode(self):
        for value in ("other", "", None, "new", "all"):
            with self.subTest(folder_mode=value), self.assertRaises(ValueError):
                replace(self.options, folder_mode=value).validate()
            with self.subTest(scan_mode=value), self.assertRaises(ValueError):
                replace(self.options, scan_mode=value).validate()
        normalized = replace(self.options, folder_mode=" DATE ", scan_mode=" QUICK ").validate()
        self.assertEqual((normalized.folder_mode, normalized.scan_mode), ("date", "quick"))
        self.assertEqual(JobOptions("me", str(self.output)).scan_mode, "full")
        for removed in ("sync_mode", "date_from", "date_to"):
            with self.subTest(removed=removed), self.assertRaises(TypeError):
                JobOptions("me", str(self.output), **{removed: ""})

    def test_downloads_each_photo_and_skips_non_photos(self):
        rows = [("100", 1, "photo", "alice"), ("100", 2, "photo", "alice"),
                ("101", 1, "video", "bob"), ("102", 1, "preview", "bob"),
                ("103", 1, "card", "bob"), ("104", 1, "photo", "bob")]
        code, events = self.worker(rows)
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["status"], "finished")
        self.assertEqual(events[-1]["saved"], 3)
        self.assertEqual(events[-1]["skipped"], 0)
        self.assertEqual(len(FakeHttpDownloader.downloaded), 3)
        self.assertTrue((self.output / "myaccount" / "alice" / "100_2.jpg").is_file())
        self.assertTrue((self.output / "myaccount" / "bob" / "104_1.jpg").is_file())

    def test_preserves_likes_list_order_and_partial_run_archive_without_sorting_posts(self):
        # A very old post can be the most recently liked item. Only the server
        # list's supplied order controls saving, not tweet IDs/publication dates.
        rows = [("10", 1, "photo", "alice", datetime(2010, 1, 1)),
                ("99999", 1, "photo", "bob", datetime(2026, 10, 4)),
                ("500", 1, "photo", "alice", datetime(2020, 4, 1))]
        result, events = self.worker(rows, options=replace(self.options, photo_limit=1))
        self.assertEqual(result, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertIn("/10_1?", FakeHttpDownloader.downloaded[0])
        with closing(sqlite3.connect(self.record_dir / ".xlikesaver-archive.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM archive").fetchone()[0], 1)
        result, events = self.worker(rows)
        self.assertEqual(result, 0)
        self.assertEqual(events[-1]["skipped"], 1)
        self.assertEqual(events[-1]["saved"], 2)
        self.assertEqual([url.split("/media/")[1].split("?")[0] for url in FakeHttpDownloader.downloaded],
                         ["10_1", "99999_1", "500_1"])

    def test_date_layout_groups_by_korean_saving_day_independent_of_posted_dates(self):
        kst = timezone(timedelta(hours=9))
        saving = datetime(2026, 10, 4, 0, 1, tzinfo=kst)
        rows = [("100", 1, "photo", "alice", datetime(2020, 1, 1)),
                ("101", 1, "photo", "bob", datetime(2027, 12, 31)),
                ("102", 1, "photo", "alice")]
        with patch("xlikesaver.core.datetime") as clock:
            clock.now.return_value = saving
            code, events = self.worker(rows, options=replace(self.options, folder_mode="date"))
        self.assertEqual(clock.now.call_count, 6)  # Start and completion for each new file.
        for call in clock.now.call_args_list:
            self.assertEqual(call.args[0].utcoffset(None), timedelta(hours=9))
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 3)
        self.assertEqual(events[-1]["scan_mode"], "full")
        for tweet in ("100", "101", "102"):
            self.assertTrue((self.output / "myaccount" / "2026-10-04" / f"{tweet}_1.jpg").is_file())
        self.assertFalse((self.output / "myaccount" / "alice").exists())
        self.assertFalse((self.output / "myaccount" / "bob").exists())

    def test_flat_layout_has_no_author_or_date_subfolders(self):
        code, events = self.worker([("100", 1, "photo", "alice"), ("101", 1, "photo", "bob")],
                                   options=replace(self.options, folder_mode="flat"))
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 2)
        account = self.output / "myaccount"
        self.assertTrue((account / "100_1.jpg").is_file())
        self.assertTrue((account / "101_1.jpg").is_file())
        self.assertFalse(any(path.is_dir() for path in account.iterdir()))

    def test_date_layout_checks_each_photo_saving_day_and_deduplicates_across_days(self):
        from datetime import date

        options = replace(self.options, folder_mode="date")
        rows = [("100", 1, "photo", "alice"), ("101", 1, "photo", "alice")]
        with patch("xlikesaver.core._save_day", side_effect=[date(2026, 10, 4), date(2026, 10, 4),
                                                             date(2026, 10, 5), date(2026, 10, 5)]):
            code, events = self.worker(rows, options=options)
        self.assertEqual(code, 0)
        self.assertTrue((self.output / "myaccount" / "2026-10-04" / "100_1.jpg").is_file())
        self.assertTrue((self.output / "myaccount" / "2026-10-05" / "101_1.jpg").is_file())
        with patch("xlikesaver.core._save_day", return_value=date(2026, 10, 6)):
            code, events = self.worker(rows, options=options)
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 0)
        self.assertEqual(events[-1]["skipped"], 2)
        self.assertFalse(list((self.output / "myaccount" / "2026-10-06").glob("*.jpg")))

    def test_date_layout_midnight_completion_moves_after_archive_and_preserves_later_skip(self):
        from datetime import date
        from xlikesaver.core import _move_without_overwrite

        options = replace(self.options, folder_mode="date", include_videos=True)
        account = self.output / "myaccount"
        old_path = account / "2026-10-04" / "100_video_1.mp4"
        completed_path = account / "2026-10-05" / old_path.name
        observed = []
        def move(source, destination):
            with closing(sqlite3.connect(self.record_dir / ".xlikesaver-archive.date.sqlite3")) as db:
                observed.extend(row[0] for row in db.execute("SELECT entry FROM archive"))
            self.assertTrue(source.is_file())
            _move_without_overwrite(source, destination)

        rows = [("100", 1, "video", "alice")]
        with patch("xlikesaver.core._save_day", side_effect=[date(2026, 10, 4), date(2026, 10, 5)]), \
                patch("xlikesaver.core._move_without_overwrite", side_effect=move):
            code, events = self.worker(rows, options=options)
        self.assertEqual(code, 0)
        self.assertEqual(observed, ["twitter100_video_1"])
        self.assertFalse(old_path.exists())
        self.assertTrue(completed_path.is_file())
        self.assertEqual(next(event for event in events if event["type"] == "saved")["path"], str(completed_path))
        self.assertEqual(next(event for event in events if event["type"] == "progress" and event["final"])["path"], str(completed_path))
        self.assertEqual(str(self.embed_source.call_args.args[0]), str(completed_path))
        with closing(sqlite3.connect(self.record_dir / ".xlikesaver-history.sqlite3")) as db:
            before = db.execute("SELECT path,saved_at FROM media").fetchone()
        self.assertEqual(before[0], str(completed_path))
        self.assertTrue(before[1])
        with patch("xlikesaver.core._save_day", return_value=date(2026, 10, 6)):
            code, events = self.worker(rows, options=options)
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 0)
        self.assertEqual(events[-1]["skipped"], 1)
        self.assertEqual(next(event for event in events if event["type"] == "skipped")["path"], str(completed_path))
        self.assertEqual(len(FakeHttpDownloader.downloaded), 1)
        self.embed_source.assert_called_once()
        with closing(sqlite3.connect(self.record_dir / ".xlikesaver-history.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT path,saved_at FROM media").fetchone(), before)
        completed_path.unlink()
        with patch("xlikesaver.core._save_day", return_value=date(2026, 10, 7)):
            code, events = self.worker(rows, options=options)
        self.assertEqual(code, 0)
        self.assertEqual(next(event for event in events if event["type"] == "skipped")["path"], "")
        self.assertEqual(len(FakeHttpDownloader.downloaded), 1)

    def test_date_layout_midnight_collision_never_overwrites_either_file(self):
        from datetime import date

        account = self.output / "myaccount"
        old_path = account / "2026-10-04" / "100_1.jpg"
        collision = account / "2026-10-05" / old_path.name
        collision.parent.mkdir(parents=True)
        collision.write_bytes(b"existing-user-file")
        with patch("xlikesaver.core._save_day", side_effect=[date(2026, 10, 4), date(2026, 10, 5)]):
            code, events = self.worker([("100", 1, "photo", "alice")],
                                       options=replace(self.options, folder_mode="date"))
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertEqual(old_path.read_bytes(), b"\xff\xd8\xffoffline-photo")
        self.assertEqual(collision.read_bytes(), b"existing-user-file")
        self.assertEqual(next(event for event in events if event["type"] == "saved")["path"], str(old_path))
        self.assertEqual(next(event for event in events if event["type"] == "progress" and event["final"])["path"], str(old_path))
        self.assertTrue(any("완료일 폴더" in event.get("message", "") for event in events))
        with closing(sqlite3.connect(self.record_dir / ".xlikesaver-archive.date.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT entry FROM archive").fetchall(), [("twitter100_1",)])
        with closing(sqlite3.connect(self.record_dir / ".xlikesaver-history.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT path FROM media").fetchone()[0], str(old_path))

    def test_date_layout_midnight_move_failure_preserves_file_archive_and_retry_location(self):
        from datetime import date

        options = replace(self.options, folder_mode="date")
        rows = [("100", 1, "photo", "alice")]
        account = self.output / "myaccount"
        old_path = account / "2026-10-04" / "100_1.jpg"
        with patch("xlikesaver.core._save_day", side_effect=[date(2026, 10, 4), date(2026, 10, 5)]), \
                patch("xlikesaver.core._move_without_overwrite", side_effect=PermissionError(SECRET)):
            code, events = self.worker(rows, options=options)
        self.assertEqual(code, 0)
        self.assertEqual(old_path.read_bytes(), b"\xff\xd8\xffoffline-photo")
        self.assertFalse((account / "2026-10-05" / old_path.name).exists())
        self.assertEqual(next(event for event in events if event["type"] == "saved")["path"], str(old_path))
        self.assertTrue(any("완료일 폴더" in event.get("message", "") for event in events))
        with closing(sqlite3.connect(self.record_dir / ".xlikesaver-archive.date.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT entry FROM archive").fetchall(), [("twitter100_1",)])
        with patch("xlikesaver.core._save_day", return_value=date(2026, 10, 6)):
            code, events = self.worker(rows, options=options)
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 0)
        self.assertEqual(next(event for event in events if event["type"] == "skipped")["path"], str(old_path))
        self.assertEqual(len(FakeHttpDownloader.downloaded), 1)

    def test_date_layout_midnight_folder_redirect_after_creation_cannot_move_outside_account(self):
        from datetime import date

        account = self.output / "myaccount"
        old_path = account / "2026-10-04" / "100_1.jpg"
        completed_path = account / "2026-10-05" / old_path.name
        outside = self.base / "outside" / old_path.name
        resolve = Path.resolve
        checks = []
        def redirected(path, *args, **kwargs):
            if path == completed_path:
                checks.append(path)
                if len(checks) > 1:
                    return outside
            return resolve(path, *args, **kwargs)

        with patch("xlikesaver.core._save_day", side_effect=[date(2026, 10, 4), date(2026, 10, 5)]), \
                patch.object(Path, "resolve", autospec=True, side_effect=redirected), \
                patch("xlikesaver.core._move_without_overwrite") as move:
            code, events = self.worker([("100", 1, "photo", "alice")],
                                       options=replace(self.options, folder_mode="date"))
        self.assertEqual(code, 0)
        self.assertEqual(len(checks), 2)
        move.assert_not_called()
        self.assertEqual(old_path.read_bytes(), b"\xff\xd8\xffoffline-photo")
        self.assertFalse(outside.exists())
        self.assertFalse(completed_path.exists())
        self.assertEqual(next(event for event in events if event["type"] == "saved")["path"], str(old_path))
        self.assertTrue(any("완료일 폴더" in event.get("message", "") for event in events))
        with closing(sqlite3.connect(self.record_dir / ".xlikesaver-archive.date.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT entry FROM archive").fetchall(), [("twitter100_1",)])

    def test_changing_layout_downloads_into_new_destination_then_skips_within_layout(self):
        rows = [("100", 1, "photo", "alice", datetime(2026, 10, 4))]
        for mode, relative, archive_name in (
            ("author", "alice/100_1.jpg", ".xlikesaver-archive.sqlite3"),
            ("date", "2026-10-04/100_1.jpg", ".xlikesaver-archive.date.sqlite3"),
            ("flat", "100_1.jpg", ".xlikesaver-archive.flat.sqlite3"),
        ):
            options = replace(self.options, folder_mode=mode)
            with self.subTest(mode=mode):
                with patch("xlikesaver.core._save_day", return_value=datetime(2026, 10, 4).date()):
                    code, events = self.worker(rows, options=options)
                self.assertEqual(code, 0)
                self.assertEqual(events[-1]["saved"], 1)
                self.assertTrue((self.output / "myaccount" / relative).is_file())
                self.assertTrue((self.record_dir / archive_name).is_file())
                code, events = self.worker(rows, options=options)
                self.assertEqual(code, 0)
                self.assertEqual(events[-1]["saved"], 0)
                self.assertEqual(events[-1]["skipped"], 1)
        self.assertEqual(len(FakeHttpDownloader.downloaded), 3)

    def test_archive_prevents_repeat_downloads_even_when_file_moved(self):
        rows = [("100", 1, "photo", "alice"), ("100", 2, "photo", "alice")]
        self.worker(rows)
        (self.output / "myaccount" / "alice" / "100_1.jpg").unlink()
        code, events = self.worker(rows)
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 0)
        self.assertEqual(events[-1]["skipped"], 2)
        self.assertEqual(len(FakeHttpDownloader.downloaded), 2)
        with closing(sqlite3.connect(self.record_dir / ".xlikesaver-archive.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM archive").fetchone()[0], 2)

    def test_quick_scan_runs_complete_scan_until_a_run_finishes(self):
        rows = [("100", 1, "photo", "alice"), ("101", 1, "photo", "alice")]
        quick = replace(self.options, scan_mode="quick")
        code, events = self.worker(rows, options=quick)
        self.assertEqual(code, 0)
        self.assertEqual((events[-1]["scan_mode"], events[-1]["saved"]), ("full", 2))
        self.assertTrue(any("전체 목록을 확인" in event.get("message", "") for event in events if event["type"] == "log"))
        code, events = self.worker(rows, options=quick)
        self.assertEqual(code, 0)
        self.assertEqual((events[-1]["scan_mode"], events[-1]["status"], events[-1]["skipped"]), ("quick", "finished", 2))
        self.assertEqual([(run["scan_mode"], run["status"]) for run in self.runs()],
                         [("full", "finished"), ("quick", "finished")])

    def test_quick_scan_stops_after_streak_of_saved_files_without_listing_the_rest(self):
        old = [(str(tweet), 1, "photo", "alice") for tweet in range(100, 106)]
        self.worker(old)
        listed = []
        new_rows = [("200", 1, "photo", "bob"), ("201", 1, "photo", "bob")]
        with patch("xlikesaver.core.QUICK_STOP_AFTER", 3):
            code, events = self.worker(new_rows + old, options=replace(self.options, scan_mode="quick"),
                                       before_row=lambda extractor, row: listed.append(row[0]))
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["status"], "finished")
        self.assertEqual((events[-1]["scan_mode"], events[-1]["saved"], events[-1]["skipped"]), ("quick", 2, 3))
        self.assertIn("이어져 멈췄습니다", events[-1]["message"])
        self.assertEqual(listed, ["200", "201", "100", "101", "102"])
        self.assertEqual(FakeHttpDownloader.downloaded[-2:], [
            "https://pbs.twimg.com/media/200_1?format=jpg&name=orig",
            "https://pbs.twimg.com/media/201_1?format=jpg&name=orig"])
        self.assertEqual(self.runs()[-1]["status"], "finished")

    def test_saved_file_resets_the_streak_so_interleaved_new_likes_are_kept(self):
        old = [(str(tweet), 1, "photo", "alice") for tweet in range(100, 104)]
        self.worker(old)
        rows = [old[0], old[1], ("200", 1, "photo", "bob"), old[2], old[3], ("201", 1, "photo", "bob")]
        with patch("xlikesaver.core.QUICK_STOP_AFTER", 3):
            code, events = self.worker(rows, options=replace(self.options, scan_mode="quick"))
        self.assertEqual(code, 0)
        self.assertEqual((events[-1]["saved"], events[-1]["skipped"]), (2, 4))
        self.assertNotIn("이전 확인 지점", events[-1]["message"])

    def test_unfinished_run_makes_the_next_quick_scan_complete(self):
        old = [(str(tweet), 1, "photo", "alice") for tweet in range(100, 105)]
        self.worker(old)
        quick = replace(self.options, scan_mode="quick")
        rows = [("200", 1, "photo", "bob"), ("201", 1, "photo", "bob"), ("202", 1, "photo", "bob")] + old
        with patch("xlikesaver.core.QUICK_STOP_AFTER", 2):
            # A short test run stops after one new file: newer likes below it
            # would otherwise sit behind the saved streak forever.
            code, events = self.worker(rows, options=replace(quick, photo_limit=1))
            self.assertEqual((code, events[-1]["saved"]), (0, 1))
            self.assertEqual(self.runs()[-1]["status"], "limited")
            code, events = self.worker(rows, options=quick)
            self.assertEqual((events[-1]["scan_mode"], events[-1]["saved"], events[-1]["skipped"]), ("full", 2, 6))
            FakeHttpDownloader.failure = True
            code, events = self.worker([("300", 1, "photo", "bob")] + rows, options=quick)
            self.assertEqual((events[-1]["scan_mode"], events[-1]["status"]), ("quick", "error"))
            FakeHttpDownloader.failure = False
            code, events = self.worker([("300", 1, "photo", "bob")] + rows, options=quick)
            self.assertEqual((events[-1]["scan_mode"], events[-1]["saved"]), ("full", 1))
            flag = self.base / "stop.flag"
            code, events = self.worker(rows, options=quick, stop_file=flag,
                                       before_row=lambda extractor, row: row[0] == "200" or flag.touch())
            self.assertEqual((events[-1]["status"], self.runs()[-1]["status"]), ("stopped", "stopped"))
            flag.unlink()
            code, events = self.worker(rows, options=quick)
            self.assertEqual(events[-1]["scan_mode"], "full")
            code, events = self.worker(rows, options=quick)
            self.assertEqual(events[-1]["scan_mode"], "quick")

    def test_changed_media_settings_make_one_complete_scan(self):
        rows = [("100", 1, "photo", "alice"), ("101", 2, "video", "alice")]
        quick = replace(self.options, scan_mode="quick")
        self.worker(rows, options=quick)
        self.assertEqual(self.worker(rows, options=quick)[1][-1]["scan_mode"], "quick")
        for changed in (replace(quick, include_videos=True), replace(quick, include_videos=True, max_video_mb=500)):
            code, events = self.worker(rows, options=changed)
            self.assertEqual(events[-1]["scan_mode"], "full")
            self.assertTrue(any("설정이 바뀌어" in event.get("message", "") for event in events))
            self.assertEqual(self.worker(rows, options=changed)[1][-1]["scan_mode"], "quick")

    def test_quick_readiness_is_separate_for_each_folder_layout(self):
        rows = [("100", 1, "photo", "alice")]
        self.worker(rows)
        code, events = self.worker(rows, options=replace(self.options, scan_mode="quick", folder_mode="flat"))
        self.assertEqual(events[-1]["scan_mode"], "full")

    def test_all_mode_ignores_legacy_corrupt_sync_database_and_uses_photo_archive(self):
        account = self.output / "myaccount"
        account.mkdir(parents=True)
        sync_path = account / ".xlikesaver-sync.sqlite3"
        sync_path.write_bytes(b"old invalid sync database")
        rows = [("100", 1, "photo", "alice")]
        code, events = self.worker(rows)
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        code, events = self.worker(rows)
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 0)
        self.assertEqual(events[-1]["skipped"], 1)
        self.assertEqual(sync_path.read_bytes(), b"old invalid sync database")

    def test_limit_counts_new_downloads_and_archive_contains_last_saved_photo(self):
        rows = [("100", 1, "photo", "alice"), ("101", 1, "photo", "alice"), ("102", 1, "photo", "alice")]
        self.worker(rows[:1])
        limited = replace(self.options, photo_limit=1)
        code, events = self.worker(rows, options=limited)
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertEqual(events[-1]["skipped"], 1)
        code, events = self.worker(rows)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertEqual(events[-1]["skipped"], 2)

    def test_existing_files_reuse_one_catalog_read_and_keep_metadata_retry_author(self):
        from xlikesaver.history import HistoryStore

        rows = [("100", 1, "photo", "alice"), ("101", 1, "photo", "bob")]
        self.assertEqual(self.worker(rows)[0], 0)
        with closing(sqlite3.connect(self.record_dir / ".xlikesaver-history.sqlite3")) as database:
            database.execute("UPDATE media SET metadata_status='failed' WHERE tweet_id='100'")
            database.commit()
        self.embed_source.reset_mock()
        # The current result omits the author. Retrying its missing source must
        # keep the author's identity from the same row used to find the file.
        changed = [("100", 1, "photo", ""), rows[1]]
        with patch.object(HistoryStore, "media_record", autospec=True,
                          side_effect=HistoryStore.media_record) as lookup:
            code, events = self.worker(changed)
        self.assertEqual((code, events[-1]["saved"], events[-1]["skipped"]), (0, 0, 2))
        self.assertEqual(lookup.call_count, len(rows))
        self.embed_source.assert_called_once()
        self.assertEqual(self.embed_source.call_args.kwargs["author"], "alice")
        self.assertEqual(len(FakeHttpDownloader.downloaded), len(rows))

    def test_ordinary_post_warnings_never_claim_an_expired_login(self):
        def warn(extractor, row):
            extractor.log.warning("%s: Missing 'author' data", row[0])
            extractor.log.warning("'%s' (%s)", "Withheld in: KR", row[0])
            extractor.log.warning("Media unavailable (%s - '%s')", row[0], "Copyright")

        with patch.object(FakeHttpDownloader, "download", lambda self, url, pathfmt: False):
            code, events = self.worker([("1840412345", 1, "photo", "alice")], before_row=warn)
        messages = [event["message"] for event in events if event["type"] == "log"]
        self.assertNotEqual(code, 0)
        self.assertFalse(any("인증 토큰" in message or "로그인 상태" in message for message in messages), messages)
        self.assertIn("작성자 정보가 없는 게시물은 건너뛸 수 있습니다.", messages)
        self.assertIn("국가 제한(withheld) 등으로 볼 수 없는 게시물은 건너뜁니다.", messages)
        self.assertIn("볼 수 없는 미디어(저작권·지역 제한 등)는 건너뜁니다.", messages)
        # The failed download stays the run's reason, not a post notice and not
        # "not found" because the tweet ID happens to contain 404.
        self.assertIn("일부 파일을 저장하지 못했습니다", events[-1]["message"])

    def test_diagnostics_match_whole_terms(self):
        from xlikesaver.core import _safe_diagnostic

        for raw in ("AuthRequired: authenticated cookies needed", "AuthorizationError: Account temporarily locked",
                    "Invalid login credentials", "'401 Unauthorized'", "403 Forbidden", "cookie expired"):
            with self.subTest(raw=raw):
                self.assertIn("Edge에서 X 로그인", _safe_diagnostic(raw, "managed"))
        for raw in ("1849: Missing 'author' data", "Failed to download 1429_1.jpg",
                    "Failed to download 1840412345_1.jpg"):
            with self.subTest(raw=raw):
                message = _safe_diagnostic(raw, "managed")
                self.assertNotIn("인증 토큰", message)
                self.assertNotIn("요청 한도", message)
                self.assertNotIn("찾지 못했습니다", message)
        self.assertIn("요청 한도", _safe_diagnostic("HTTP 429 Too Many Requests", "managed"))

    def test_gallery_hook_change_stops_before_any_request(self):
        from gallery_dl.extractor.twitter import TwitterExtractor

        with patch.object(TwitterExtractor, "_extract_media_source", lambda self, media: None), \
                patch("gallery_dl.extractor.find") as find:
            events = []
            code = run_worker(self.options, events.append)
        find.assert_not_called()
        self.assertEqual(code, 1)
        self.assertIn("gallery-dl이 예상과 달라", events[-1]["message"])
        self.assertFalse(self.record_dir.exists())

    def test_no_results_is_actionable_error(self):
        code, events = self.worker()
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]["status"], "error")
        self.assertIn("미디어를 찾지 못", events[-1]["message"])

    def test_rate_limit_errors_are_actionable_and_preserve_previous_downloads(self):
        code, events = self.worker(error=exception.AbortExtraction("Rate limit exceeded " + SECRET))
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]["status"], "error")
        self.assertIn("요청 한도", events[-1]["message"])

    def test_cookie_file_is_not_modified(self):
        original = self.cookie_file.read_bytes()
        with patch("gallery_dl.cookies.load_cookies") as library_load:
            code, events = self.worker([("100", 1, "photo", "alice")])
        library_load.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertEqual(self.cookie_file.read_bytes(), original)

    def test_host_only_x_cookie_is_supported(self):
        self.write_cookies(domain="x.com")
        code, events = self.worker([("100", 1, "photo", "alice")])
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)

    def test_utf8_bom_cookie_file_is_supported(self):
        text = self.cookie_file.read_text(encoding="utf-8")
        self.cookie_file.write_text(text, encoding="utf-8-sig")
        code, events = self.worker([("100", 1, "photo", "alice")])
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)

    def test_managed_session_validation_does_not_read_secrets(self):
        with patch("xlikesaver.login.load_session_cookies") as load:
            normalized = replace(self.options, auth_mode="managed").validate()
        self.assertEqual(normalized.auth_mode, "managed")
        self.assertEqual(normalized.cookies_file, "")
        load.assert_not_called()

    def test_managed_session_loads_only_x_cookies_in_memory(self):
        cookies = [{"name": "auth_token", "value": SECRET, "domain": "x.com",
                    "path": "/", "expires": -1, "secure": True, "httpOnly": True},
                   {"name": "ct0", "value": "offline-csrf", "domain": ".x.com",
                    "path": "/", "expires": time.time() + 3600, "secure": True, "httpOnly": False},
                   {"name": "other", "value": SECRET, "domain": ".example.com",
                    "path": "/", "expires": -1, "secure": True, "httpOnly": True}]
        observed = []

        def before_row(extractor, row):
            observed.extend(list(extractor.cookies))
            self.assertEqual(extractor.cookies.get("auth_token", domain=".x.com"), SECRET)
            self.assertEqual(extractor.cookies.get("ct0", domain=".x.com"), "offline-csrf")

        with patch("xlikesaver.login.load_session_cookies", return_value=cookies) as load, \
                patch("gallery_dl.cookies.load_cookies") as library_load:
            code, events = self.worker([("100", 1, "photo", "alice")],
                                       options=replace(self.options, auth_mode="managed"),
                                       before_row=before_row)
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        load.assert_called_once_with()
        library_load.assert_not_called()
        self.assertEqual({cookie.domain for cookie in observed}, {".x.com"})
        auth = next(cookie for cookie in observed if cookie.name == "auth_token")
        self.assertIsNone(auth.expires)
        self.assertTrue(auth.secure)
        self.assertTrue(auth.has_nonstandard_attr("HttpOnly"))

    def test_missing_managed_session_gives_reconnect_help_without_secret_diagnostics(self):
        with patch("xlikesaver.login.load_session_cookies", side_effect=ValueError(SECRET)):
            code, events = self.worker(options=replace(self.options, auth_mode="managed"))
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]["status"], "error")
        self.assertIn("Edge에서 X 로그인", events[-1]["message"])

    def test_stop_before_managed_start_does_not_load_session(self):
        flag = self.base / "stop.flag"
        flag.touch()
        with patch("xlikesaver.login.load_session_cookies") as load:
            code, events = self.worker(options=replace(self.options, auth_mode="managed"), stop_file=flag)
        load.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["status"], "stopped")

    def test_stop_before_start_does_not_touch_authentication(self):
        flag = self.base / "stop.flag"
        flag.touch()
        with patch("xlikesaver.login.load_session_cookies") as load:
            code, events = self.worker(options=JobOptions("me", str(self.output)), stop_file=flag)
        load.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["status"], "stopped")

    def test_stop_interrupts_request_sleep_and_keeps_archive(self):
        flag = self.base / "stop.flag"

        def before_row(extractor, row):
            if row[0] == "101":
                flag.touch()
                extractor.sleep(2, "request")

        code, events = self.worker([("100", 1, "photo", "alice"), ("101", 1, "photo", "alice")],
                                   before_row=before_row, stop_file=flag)
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["status"], "stopped")
        self.assertEqual(events[-1]["saved"], 1)
        flag.unlink()
        code, events = self.worker([("100", 1, "photo", "alice")])
        self.assertEqual(events[-1]["skipped"], 1)

    def test_current_config_is_isolated_and_cookie_updates_disabled(self):
        config.set(("extractor", "twitter"), "videos", True)
        config.set(("extractor", "twitter"), "cookies-update", True)
        seen = {}

        def before_row(extractor, row):
            for key in ("videos", "previews", "quoted", "cards", "expand", "cookies-update", "sleep-request", "size", "ratelimit", "retries", "retries-api", "reverse"):
                seen[key] = extractor.config(key)
            seen["cache-file"] = config.get(("cache",), "file")
            seen["media-retries"] = config.get(("downloader", "http"), "retries")

        self.worker([("100", 1, "photo", "alice")], before_row=before_row)
        for key in ("videos", "previews", "quoted", "cards", "expand", "cookies-update"):
            self.assertIs(seen[key], False)
        self.assertEqual(seen["cache-file"], ":memory:")
        self.assertEqual(seen["size"], ["orig", "4096x4096", "large", "medium", "small"])
        self.assertGreaterEqual(seen["sleep-request"], 10)
        self.assertEqual(seen["ratelimit"], "abort")
        self.assertEqual(seen["retries"], 0)
        self.assertEqual(seen["retries-api"], 0)
        self.assertEqual(seen["media-retries"], 0)
        self.assertIs(seen["reverse"], False)

    def test_only_token_connection_and_cookie_file_authentication_are_supported(self):
        self.assertEqual(JobOptions("me", str(self.output)).auth_mode, "managed")
        for mode in ("managed", " MANAGED ", "file", " FILE "):
            with self.subTest(mode=mode):
                normalized = replace(self.options, auth_mode=mode).validate()
                self.assertIn(normalized.auth_mode, {"file", "managed"})
        for mode in ("chrome", "edge", "firefox", "", "unknown", None, True, SECRET):
            with self.subTest(mode=mode), \
                    patch("xlikesaver.login.load_session_cookies") as session_load, \
                    patch("gallery_dl.cookies.load_cookies") as library_load, \
                    patch("gallery_dl.extractor.find") as find:
                events = []
                code = run_worker(replace(self.options, auth_mode=mode), events.append)
                self.assertNotEqual(code, 0)
                self.assertEqual(events[-1]["status"], "error")
                self.assertNotIn(SECRET, json.dumps(events))
                self.assertIn("브라우저 로그인", events[-1]["message"])
                self.assertIn("cookies.txt", events[-1]["message"])
                session_load.assert_not_called()
                library_load.assert_not_called()
                find.assert_not_called()

    def test_cookie_file_auth_failure_gives_new_cookie_file_guidance(self):
        code, events = self.worker(error=exception.AuthRequired("authenticated cookies " + SECRET))
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]["status"], "error")
        self.assertIn("cookies.txt", events[-1]["message"])
        self.assertIn("다시 로그인", events[-1]["message"])

    def test_failed_download_reports_error_and_no_archive_success(self):
        FakeHttpDownloader.failure = True
        code, events = self.worker([("100", 1, "photo", "alice")])
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 0)
        self.assertEqual(events[-1]["status"], "error")
        self.assertIn("파일에 접근", events[-1]["message"])
        FakeHttpDownloader.failure = False
        code, events = self.worker([("100", 1, "photo", "alice")])
        self.assertEqual(events[-1]["saved"], 1)

    def test_duplicates_within_one_list_are_counted_as_skips(self):
        code, events = self.worker([("100", 1, "photo", "alice"), ("100", 1, "photo", "alice")])
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertEqual(events[-1]["skipped"], 1)

    def test_unwritable_output_has_actionable_error_before_auth(self):
        with patch("xlikesaver.core.tempfile.TemporaryFile", side_effect=PermissionError), \
                patch("xlikesaver.login.load_session_cookies") as load:
            code, events = self.worker(options=JobOptions("me", str(self.output), "managed"))
        load.assert_not_called()
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]["status"], "error")
        self.assertIn("폴더에 쓸 수", events[-1]["message"])

    def test_media_429_stops_before_size_fallback_and_next_photo(self):
        import requests
        from gallery_dl.downloader.http import HttpDownloader

        def items(extractor):
            for tweet in ("100", "101"):
                metadata = {"tweet_id": tweet, "num": 1, "type": "photo", "extension": "jpg",
                            "author": {"name": "alice"}, "user": {"name": "alice"},
                            "_fallback": ["https://pbs.twimg.com/media/fallback?name=large"]}
                yield Message.Directory, "", metadata
                yield Message.Url, "https://pbs.twimg.com/media/original?name=orig", metadata

        response = requests.Response()
        response.status_code = 429
        response.reason = "Too Many Requests"
        response.headers["Retry-After"] = "3600"
        response._content = b""
        response._content_consumed = True
        events = []
        with patch.object(FakeLikesExtractor, "items", items), \
                patch("gallery_dl.extractor.find", side_effect=lambda url: FakeLikesExtractor(url, [])), \
                patch("gallery_dl.downloader.find", return_value=HttpDownloader), \
                patch("requests.Session.send", return_value=response) as send:
            result = run_worker(self.options, events.append)
        self.assertEqual(send.call_count, 1)
        self.assertNotEqual(result, 0)
        self.assertEqual(events[-1]["saved"], 0)
        self.assertIn("요청 한도", events[-1]["message"])
        self.assertTrue(events[-1]["retry_at"])
        # Changing the output folder cannot bypass the same-account cooldown.
        with patch("gallery_dl.extractor.find") as find:
            result = run_worker(replace(self.options, output_dir=str(self.base / "other-photos")), events.append)
        find.assert_not_called()
        self.assertNotEqual(result, 0)
        self.assertIn("한국 시간", events[-1]["message"])

    def test_api_429_stops_before_library_retry_and_preserves_previous_downloads(self):
        import requests

        self.worker([("100", 1, "photo", "alice")])
        response = requests.Response()
        response.status_code = 429
        response.reason = "Too Many Requests"
        response._content = b""
        response._content_consumed = True

        def request_before_row(extractor, row):
            extractor.request("https://x.com/i/api/graphql/offline/Likes", fatal=None)

        with patch("requests.Session.send", return_value=response) as send:
            result, events = self.worker([("101", 1, "photo", "alice")], before_row=request_before_row)
        self.assertEqual(send.call_count, 1)
        self.assertNotEqual(result, 0)
        self.assertIn("요청 한도", events[-1]["message"])
        self.assertEqual(self.runs()[-1]["status"], "error")
        self.assertTrue((self.output / "myaccount" / "alice" / "100_1.jpg").is_file())

    def test_media_server_error_stops_before_fallback_and_preserves_saved_photo_history(self):
        import requests
        from gallery_dl.downloader.http import HttpDownloader

        self.worker([("100", 1, "photo", "alice")])

        def items(extractor):
            for tweet in ("100", "101", "102"):
                metadata = {"tweet_id": tweet, "num": 1, "type": "photo", "extension": "jpg",
                            "author": {"name": "alice"}, "user": {"name": "alice"},
                            "_fallback": ["https://pbs.twimg.com/media/fallback?name=large"]}
                yield Message.Directory, "", metadata
                yield Message.Url, "https://pbs.twimg.com/media/original?name=orig", metadata

        response = requests.Response()
        response.status_code = 503
        response._content = b""
        response._content_consumed = True
        events = []
        with patch.object(FakeLikesExtractor, "items", items), \
                patch("gallery_dl.extractor.find", side_effect=lambda url: FakeLikesExtractor(url, [])), \
                patch("gallery_dl.downloader.find", return_value=HttpDownloader), \
                patch("requests.Session.send", return_value=response) as send:
            result = run_worker(self.options, events.append)
        self.assertEqual(send.call_count, 1)
        self.assertNotEqual(result, 0)
        self.assertEqual(events[-1]["skipped"], 1)
        self.assertIn("사진 서버", events[-1]["message"])
        code, events = self.worker([("100", 1, "photo", "alice"), ("101", 1, "photo", "alice")])
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertEqual(events[-1]["skipped"], 1)

    def test_streamed_media_failure_stops_before_fallback_or_next_photo(self):
        import requests
        from gallery_dl.downloader.http import HttpDownloader
        from types import SimpleNamespace
        from unittest.mock import Mock

        self.worker([("100", 1, "photo", "alice")])

        def items(extractor):
            for tweet in ("100", "101", "102"):
                metadata = {"tweet_id": tweet, "num": 1, "type": "photo", "extension": "jpg",
                            "author": {"name": "alice"}, "user": {"name": "alice"},
                            "_fallback": ["https://pbs.twimg.com/media/fallback?name=large"]}
                yield Message.Directory, "", metadata
                yield Message.Url, "https://pbs.twimg.com/media/original?name=orig", metadata

        for fail_on_header in (False, True):
            response = requests.Response()
            response.status_code = 200
            response.headers["Content-Type"] = "image/jpeg"
            response.headers["Content-Length"] = "1000"
            response.raw = SimpleNamespace(chunked=True)
            response.close = Mock()

            def broken_content(*args, **kwargs):
                if not fail_on_header:
                    yield b"\xff\xd8\xffoffline-header"
                    yield b"some-image-body"
                raise requests.exceptions.ChunkedEncodingError(SECRET)

            response.iter_content = broken_content
            events = []
            with self.subTest(fail_on_header=fail_on_header), \
                    patch.object(FakeLikesExtractor, "items", items), \
                    patch("gallery_dl.extractor.find", side_effect=lambda url: FakeLikesExtractor(url, [])), \
                    patch("gallery_dl.downloader.find", return_value=HttpDownloader), \
                    patch("requests.Session.send", return_value=response) as send:
                result = run_worker(self.options, events.append)
            self.assertEqual(send.call_count, 1)
            self.assertNotEqual(result, 0)
            self.assertEqual(events[-1]["saved"], 0)
            self.assertEqual(events[-1]["skipped"], 1)
            self.assertIn("사진 서버", events[-1]["message"])
            self.assertNotIn(SECRET, json.dumps(events))
            with closing(sqlite3.connect(self.record_dir / ".xlikesaver-archive.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM archive").fetchone()[0], 1)
        code, events = self.worker([("100", 1, "photo", "alice"), ("101", 1, "photo", "alice")])
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["saved"], 1)
        self.assertEqual(events[-1]["skipped"], 1)

    def test_same_account_parallel_worker_is_blocked_before_cookie_read(self):
        from xlikesaver.protection import AccountGuard

        guard = AccountGuard("myaccount", sleep=lambda *args: None)
        guard.acquire()
        try:
            with patch("xlikesaver.login.load_session_cookies") as load:
                result, events = self.worker(options=replace(self.options, auth_mode="managed"))
            load.assert_not_called()
            self.assertNotEqual(result, 0)
            self.assertIn("다른 저장 작업", events[-1]["message"])
        finally:
            guard.close()
        result, events = self.worker([("100", 1, "photo", "alice")])
        self.assertEqual(result, 0)

    def test_abandoned_run_recovery_waits_for_exclusive_account_lock(self):
        from xlikesaver.history import HistoryStore
        from xlikesaver.protection import AccountGuard

        from xlikesaver.storage import resolve_storage
        catalog = HistoryStore.for_storage(resolve_storage(self.output, "myaccount", True))
        try:
            abandoned = catalog.start_run("author")
            catalog.update_run(abandoned, saved=2, visited=3, last_tweet_id="99")
            guard = AccountGuard("myaccount", sleep=lambda *args: None)
            guard.acquire()
            try:
                result, events = self.worker([("100", 1, "photo", "alice")])
                self.assertNotEqual(result, 0)
                self.assertIn("다른 저장 작업", events[-1]["message"])
                self.assertEqual(catalog.runs()[0]["status"], "running")
            finally:
                guard.close()
            result, events = self.worker([("100", 1, "photo", "alice")])
            self.assertEqual(result, 0)
            recovered = next(row for row in catalog.runs() if row["id"] == abandoned)
            self.assertEqual((recovered["status"], recovered["saved"], recovered["visited"]),
                             ("interrupted", 2, 3))
            self.assertEqual(recovered["last_tweet_id"], "99")
            self.assertIsNone(recovered["finished_at"])
            self.assertEqual(catalog.runs()[0]["status"], "finished")
        finally:
            catalog.close()


if __name__ == "__main__":
    unittest.main()
