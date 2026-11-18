"""What one media X does not serve, or one optional picture request, may do to a save.

A file that fails on its own (X answers 404 after every size) is listed in
the unsaved ledger for another try. It used to make the run an error, so every
following quick save checked the whole Like list again, met the same file, and
failed again; the daily task showed its failure dialog every day. Run-ending
problems (a refused or limited request, a storage or login problem, every
download failing) stay errors; tests in test_core pin those.

A media X's image host refuses (403 or 5xx) still stops the save, as the
request protection means it to. It used to stop every save at the same place,
unlisted, so older likes were never reached; now it is listed. A refusal that
means the file is not served to this account (403, 410, ...) counts: after it
has stopped a few saves, saves pass it by and only '다시 저장' asks for it. A
server error (5xx) is an outage that usually passes and never counts.

A small picture fetched for an unsaved or excluded media is optional. Its
request goes through the save's protected session, and a refused or failed
reply there used to end the whole save.
"""
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import app_data_isolation  # noqa: F401  Never open the user's app data.
from tests import test_core, test_missing_files
import requests
from luvbox import scheduled, unsaved
from luvbox.core import run_worker


def _tweet(url):
    """The tweet of a fake media URL (``.../media/<tweet>_<num>?...``)."""
    return url.rsplit("/", 1)[1].split("_", 1)[0]


class QuietDownloader(test_core.FakeHttpDownloader):
    """Saves every photo except those of ``missing`` tweets, which X no longer serves."""
    missing = set()
    attempted = []

    def download(self, url, pathfmt):
        QuietDownloader.attempted.append(_tweet(url))
        if _tweet(url) in QuietDownloader.missing:
            return False          # gallery-dl then logs 'Failed to download' and adds status 4
        return super().download(url, pathfmt)


def _response(url, status, body=b"", kind="image/jpeg", headers=None):
    response = requests.Response()
    response.status_code = status
    response.reason = {200: "OK", 403: "Forbidden", 404: "Not Found", 429: "Too Many Requests",
                       503: "Service Unavailable"}.get(status, "")
    response.url = url
    response.headers["Content-Type"] = kind
    response.headers["Content-Length"] = str(len(body))
    response.headers.update(headers or {})
    response._content = body
    response._content_consumed = True
    response.raw = SimpleNamespace(chunked=True)
    return response


class SaveFailureTests(unittest.TestCase):
    setUp_core = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    set_session = test_core.CoreTests.set_session
    runs = test_core.CoreTests.runs

    def setUp(self):
        self.setUp_core()
        QuietDownloader.missing = set()
        QuietDownloader.attempted = []
        self.sent = []
        # Preview requests reach the transport; anything there answers 404 unless a test says otherwise.
        self.answer = lambda request: _response(request.url, 404)
        for target, value in (("luvbox.unsaved.unsaved_path", self.base / "unsaved.sqlite3"),
                              ("luvbox.thumbcache.directory", self.base / "thumbnails")):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        pacing = patch("luvbox.protection.MEDIA_INTERVAL", 0)
        pacing.start()
        self.addCleanup(pacing.stop)
        self.quick = replace(self.options, scan_mode="quick")

    def send(self, request, **kwargs):
        self.sent.append(request.url)
        return self.answer(request)

    def save(self, rows, options=None, downloader=QuietDownloader):
        events = []
        with patch("gallery_dl.extractor.find", side_effect=lambda url: test_core.FakeLikesExtractor(url, rows)), \
                patch("gallery_dl.downloader.find", return_value=downloader), \
                patch("requests.Session.send", side_effect=self.send):
            code = run_worker(options or self.quick, events.append)
        self.assertNotIn(test_core.SECRET, json.dumps(events))
        self.assertEqual(events[-1]["type"], "finished")
        return code, events[-1], events

    def listed(self):
        return unsaved.identities("myaccount", self.options.output_dir)

    def detail(self, tweet):
        rows = unsaved.page("myaccount", self.options.output_dir, kinds=(unsaved.FAILED,))["rows"]
        return next(row["detail"] for row in rows if row["tweet_id"] == tweet)

    @contextmanager
    def daily(self, rows):
        """The scheduled task's own run_saved, over these likes; yields the failure dialog mock."""
        settings = {"account": "MyAccount", "output_dir": str(self.output), "folder_mode": "author",
                    "include_videos": False, "include_gifs": False, "separate_accounts": True,
                    "auto_classify": False}
        with patch("luvbox.scheduled.load_settings", return_value=settings), \
                patch("luvbox.scheduled.log_directory", return_value=self.base / "logs"), \
                patch("luvbox.scheduled.last_run_path", return_value=self.base / "last-scheduled.json"), \
                patch("luvbox.activity.activity_path", return_value=self.base / "activity.sqlite3"), \
                patch("luvbox.scheduled._notify_failure") as notify, \
                patch("gallery_dl.extractor.find", side_effect=lambda url: test_core.FakeLikesExtractor(url, rows)), \
                patch("gallery_dl.downloader.find", return_value=QuietDownloader), \
                patch("requests.Session.send", side_effect=self.send), \
                patch("luvbox.core.QUICK_STOP_AFTER", 3):
            yield notify

    def outcome(self):
        return json.loads((self.base / "last-scheduled.json").read_text(encoding="utf-8"))

    def exclude(self, *identities):
        from luvbox.history import HistoryStore
        from luvbox.storage import resolve_storage
        paths = resolve_storage(self.options.output_dir, self.options.account, self.options.separate_accounts)
        with closing(HistoryStore.for_storage(paths)) as store:
            store.set_missing_ignored(list(identities), True)

    @staticmethod
    def logs(events):
        return " ".join(event.get("message", "") for event in events if event["type"] == "log")

    # -- one media that never downloads -------------------------------------------------

    def test_one_media_that_never_downloads_lets_quick_saves_stop_again(self):
        old = [(str(tweet), 1, "photo", "alice") for tweet in range(100, 106)]
        gone = ("5000", 1, "photo", "carol")
        QuietDownloader.missing = {"5000"}
        with patch("luvbox.core.QUICK_STOP_AFTER", 3):
            # The first save checks the whole list. Every other like is saved and the one is listed.
            code, finished, events = self.save(old + [gone])
            self.assertEqual((code, finished["status"], finished["scan_mode"]), (0, "finished", "full"))
            self.assertEqual((finished["saved"], finished["failed_media"]), (6, 1))
            self.assertIn("받지 못한 미디어 1개는 건너뛰고 다른 Like를 이어서 확인했습니다", finished["message"])
            self.assertIn("‘설정 › 고급 › 저장 못 한 미디어’", finished["message"])
            self.assertNotIn("연결과 저장 폴더를 확인", finished["message"])
            self.assertIn(("5000", 1, "photo"), self.listed())
            # The next quick save stops at the saved streak instead of meeting that media again.
            QuietDownloader.attempted = []
            code, finished, events = self.save([("200", 1, "photo", "bob")] + old + [gone])
            self.assertEqual((code, finished["status"], finished["scan_mode"]), (0, "finished", "quick"))
            self.assertEqual((finished["saved"], finished["skipped"], finished["failed_media"]), (1, 3, 0))
            self.assertEqual(QuietDownloader.attempted, ["200"])
            self.assertNotIn("전체 목록을 확인", self.logs(events))
            # And so does the one after it: no full check comes back.
            code, finished, events = self.save([("201", 1, "photo", "bob")] + old + [gone])
            self.assertEqual((code, finished["scan_mode"], finished["saved"]), (0, "quick", 1))
        self.assertEqual([(run["scan_mode"], run["status"]) for run in self.runs()],
                         [("full", "finished"), ("quick", "finished"), ("quick", "finished")])
        # It stays in the list for '다시 저장'.
        self.assertIn(("5000", 1, "photo"), self.listed())

    def test_a_new_like_that_fails_in_a_quick_save_keeps_the_next_save_quick(self):
        old = [(str(tweet), 1, "photo", "alice") for tweet in range(100, 104)]
        self.save(old)
        QuietDownloader.missing = {"300"}
        with patch("luvbox.core.QUICK_STOP_AFTER", 3):
            code, finished, events = self.save([("300", 1, "photo", "bob"), ("301", 1, "photo", "bob")] + old)
            self.assertEqual((code, finished["status"], finished["scan_mode"]), (0, "finished", "quick"))
            self.assertEqual((finished["saved"], finished["failed_media"]), (1, 1))
            self.assertIn("새 Like 확인을 마쳤습니다. 1개를 저장했고", finished["message"])
            self.assertIn("받지 못한 미디어 1개는 건너뛰고", finished["message"])
            # Liked recently, it is still above the saved streak: the next quick save tries it once more.
            QuietDownloader.missing = set()
            QuietDownloader.attempted = []
            code, finished, events = self.save([("300", 1, "photo", "bob"), ("301", 1, "photo", "bob")] + old)
            self.assertEqual((code, finished["scan_mode"], finished["saved"]), (0, "quick", 1))
            self.assertEqual(QuietDownloader.attempted, ["300"])
        self.assertNotIn(("300", 1, "photo"), self.listed())

    def test_every_download_failing_is_still_an_error(self):
        QuietDownloader.missing = {"100", "101"}
        code, finished, events = self.save([("100", 1, "photo", "alice"), ("101", 1, "photo", "alice")])
        self.assertEqual((code, finished["status"]), (4, "error"))
        self.assertIn("미디어를 찾았지만 2개를 저장하지 못했습니다", finished["message"])
        self.assertEqual(self.runs()[-1]["status"], "error")

    def test_a_storage_problem_with_the_failed_file_is_still_an_error(self):
        # The fake logs a PermissionError for its failure: that ends the run, as test_core pins.
        old = [(str(tweet), 1, "photo", "alice") for tweet in range(100, 103)]
        self.save(old)
        test_core.FakeHttpDownloader.failure = True
        code, finished, events = self.save([("300", 1, "photo", "bob")] + old,
                                           downloader=test_core.FakeHttpDownloader)
        self.assertEqual((code, finished["status"], finished["failed_media"]), (4, "error", 1))
        self.assertIn("파일에 접근할 수 없습니다", finished["message"])
        test_core.FakeHttpDownloader.failure = False
        code, finished, events = self.save([("300", 1, "photo", "bob")] + old,
                                           downloader=test_core.FakeHttpDownloader)
        self.assertEqual((finished["scan_mode"], finished["saved"]), ("full", 1))

    def test_a_failed_file_the_list_could_not_keep_still_makes_the_next_save_check_everything(self):
        old = [(str(tweet), 1, "photo", "alice") for tweet in range(100, 103)]
        self.save(old)
        QuietDownloader.missing = {"5000"}
        with patch("luvbox.unsaved.record", side_effect=unsaved.UnsavedError("offline")):
            code, finished, events = self.save(old + [("5000", 1, "photo", "carol")])
        self.assertEqual((code, finished["status"], finished["failed_media"]), (4, "error", 1))
        self.assertIn("목록에도 남기지 못했습니다", finished["message"])
        self.assertIn("저장하지 못한 미디어 목록을 갱신하지 못했습니다", self.logs(events))
        self.assertEqual(self.runs()[-1]["status"], "error")
        QuietDownloader.attempted = []
        code, finished, events = self.save(old + [("5000", 1, "photo", "carol")])
        self.assertEqual((code, finished["status"], finished["scan_mode"]), (0, "finished", "full"))
        self.assertEqual(QuietDownloader.attempted, ["5000"])
        self.assertIn(("5000", 1, "photo"), self.listed())

    def test_a_404_from_the_photo_server_after_every_size_is_listed_and_the_save_finishes(self):
        """The real downloader and request protection, with X's 404 for one media."""
        from gallery_dl.downloader.http import HttpDownloader

        body = b"\xff\xd8\xff" + b"offline-photo" * 8
        self.answer = lambda request: (_response(request.url, 404) if _tweet(request.url) == "5000"
                                       else _response(request.url, 200, body))
        rows = [("100", 1, "photo", "alice"), ("5000", 1, "photo", "carol"), ("101", 1, "photo", "alice")]
        code, finished, events = self.save(rows, options=replace(self.quick, unsaved_previews=True),
                                           downloader=HttpDownloader)
        self.assertEqual((code, finished["status"], finished["saved"], finished["failed_media"]),
                         (0, "finished", 2, 1))
        # The original, then the small picture for the list; both 404.
        self.assertEqual([url for url in self.sent if _tweet(url) == "5000"],
                         ["https://pbs.twimg.com/media/5000_1?format=jpg&name=orig",
                          "https://pbs.twimg.com/media/5000_1?format=jpg&name=small"])
        self.assertTrue((self.output / "myaccount" / "alice" / "alice_101_1.jpg").is_file())
        self.assertIn(("5000", 1, "photo"), self.listed())
        self.assertEqual(self.runs()[-1]["status"], "finished")

    def test_the_daily_task_shows_no_failure_dialog_for_a_listed_media(self):
        old = [(str(tweet), 1, "photo", "alice") for tweet in range(100, 104)]
        QuietDownloader.missing = {"5000"}
        with self.daily(old + [("5000", 1, "photo", "carol")]) as notify:
            for day in range(2):
                self.assertEqual(scheduled.run_saved(), 0)
                outcome = self.outcome()
                self.assertEqual((outcome["status"], outcome["failed"], outcome["failed_media"]),
                                 ("finished", False, 1 if day == 0 else 0))
        notify.assert_not_called()
        self.assertEqual([run["scan_mode"] for run in self.runs()], ["full", "quick"])

    def test_the_daily_task_warns_once_when_no_new_like_could_be_saved(self):
        old = [(str(tweet), 1, "photo", "alice") for tweet in range(100, 104)]
        self.save(old)
        new = [(str(tweet), 1, "photo", "bob") for tweet in range(300, 305)]
        QuietDownloader.missing = {row[0] for row in new}
        with self.daily(new + old) as notify:
            for day in range(2):
                self.assertEqual(scheduled.run_saved(), 0)
                outcome = self.outcome()
                self.assertEqual((outcome["status"], outcome["saved"], outcome["failed_media"]), ("finished", 0, 5))
                # Every new like failing is worth the dialog once; the same listed failures are not, every day.
                self.assertEqual(outcome["failed"], day == 0)
                self.assertEqual(notify.call_count, 1)
        self.assertIn("새 미디어를 하나도 받지 못했습니다", scheduled._failure_text(notify.call_args.args[0]))
        # The runs still finished, so each day stays a quick save.
        self.assertEqual([(run["scan_mode"], run["status"]) for run in self.runs()],
                         [("full", "finished"), ("quick", "finished"), ("quick", "finished")])
        # Once one of them saves, nothing is shown.
        QuietDownloader.missing.discard("300")
        with self.daily(new + old) as notify:
            self.assertEqual(scheduled.run_saved(), 0)
        notify.assert_not_called()
        self.assertEqual((self.outcome()["saved"], self.outcome()["failed"]), (1, False))
        # A run that did not finish keeps its own heading.
        self.assertIn("자동 저장을 마치지 못했습니다",
                      scheduled._failure_text({"finished_at": "2026-11-15T09:00:00+09:00", "status": "error",
                                               "message": "사진 서버 접근·연결 오류로 멈췄습니다."}))

    # -- a media X's image host refuses ---------------------------------------------------

    def test_a_media_the_photo_server_always_refuses_is_passed_by_after_it_stopped_three_saves(self):
        """The real downloader and request protection, with a 403 for one media every time."""
        from gallery_dl.downloader.http import HttpDownloader
        from luvbox.core import REFUSED_LIMIT

        self.assertEqual(REFUSED_LIMIT, 3)
        body = b"\xff\xd8\xff" + b"offline-photo" * 8
        refused = "https://pbs.twimg.com/media/5000_1?format=jpg&name=orig"
        self.answer = lambda request: (_response(request.url, 403) if _tweet(request.url) == "5000"
                                       else _response(request.url, 200, body))
        old = [(str(tweet), 1, "photo", "alice") for tweet in range(100, 104)]
        rows = old[:2] + [("5000", 1, "photo", "carol")] + old[2:]
        older = self.output / "myaccount" / "alice" / "alice_102_1.jpg"
        options = replace(self.quick, unsaved_previews=True)
        with patch("luvbox.core.QUICK_STOP_AFTER", 3):
            for index in range(REFUSED_LIMIT):
                if index:
                    rows = [(str(200 + index), 1, "photo", "bob")] + rows
                self.sent = []
                code, finished, events = self.save(rows, options=options, downloader=HttpDownloader)
                # The refusal still stops the save, and the next one checks the whole list again.
                self.assertEqual((code, finished["status"], finished["scan_mode"]), (1, "error", "full"))
                self.assertEqual((finished["saved"], finished["failed_media"]), (1 if index else 2, 1))
                self.assertEqual([url for url in self.sent if _tweet(url) == "5000"], [refused])
                self.assertIn("사진 서버 접근·연결 오류로 멈췄습니다", finished["message"])
                self.assertIn("저장하지 못한 미디어 1개는 ‘설정 › 고급 › 저장 못 한 미디어’", finished["message"])
                self.assertEqual("다음 저장부터는 이 미디어를 건너뜁니다" in finished["message"],
                                 index == REFUSED_LIMIT - 1)
                # It is listed, so the person can see what stops the save.
                self.assertIn(("5000", 1, "photo"), self.listed())
                self.assertIn("사진 서버가 이 파일을 거부해", self.detail("5000"))
                self.assertFalse(older.exists())
            # From now on a save passes it by without asking and reaches the older likes.
            rows = [("203", 1, "photo", "bob")] + rows
            self.sent = []
            code, finished, events = self.save(rows, options=options, downloader=HttpDownloader)
            self.assertEqual((code, finished["status"], finished["scan_mode"]), (0, "finished", "full"))
            self.assertEqual((finished["saved"], finished["failed_media"]), (3, 1))
            self.assertEqual([url for url in self.sent if _tweet(url) == "5000"], [])
            self.assertTrue(older.is_file())
            self.assertIn("받지 못한 미디어 1개는 건너뛰고", finished["message"])
            self.assertIn("‘다시 저장’", self.detail("5000"))
            # And the next quick save stops at the saved streak again.
            rows = [("204", 1, "photo", "bob")] + rows
            self.sent = []
            code, finished, events = self.save(rows, options=options, downloader=HttpDownloader)
            self.assertEqual((code, finished["status"], finished["scan_mode"], finished["saved"]),
                             (0, "finished", "quick", 1))
            self.assertEqual([url for url in self.sent if _tweet(url) == "5000"], [])
        # '다시 저장' still asks for it; saved, it is no longer passed by.
        self.answer = lambda request: _response(request.url, 200, body)
        self.sent = []
        events = []
        with patch("gallery_dl.extractor.find", side_effect=lambda url: test_missing_files.FakeTweetExtractor(
                    url, {"5000": [(1, "photo")]})), \
                patch("gallery_dl.downloader.find", return_value=HttpDownloader), \
                patch("requests.Session.send", side_effect=self.send):
            code = run_worker(replace(self.options, targets=("5000_1",)), events.append)
        self.assertEqual((code, events[-1]["status"], events[-1]["saved"]), (0, "finished", 1))
        self.assertIn(refused, self.sent)
        self.assertNotIn(("5000", 1, "photo"), self.listed())
        self.assertEqual(unsaved.refusals("myaccount", self.options.output_dir), {})

    def test_a_server_error_stops_the_save_but_never_makes_saves_pass_the_media_by(self):
        """Three quick saves during one outage of X's image host must not skip a media for good."""
        from gallery_dl.downloader.http import HttpDownloader
        from luvbox.core import REFUSED_LIMIT

        body = b"\xff\xd8\xff" + b"offline-photo" * 8
        failing = "https://pbs.twimg.com/media/5000_1?format=jpg&name=orig"
        self.answer = lambda request: (_response(request.url, 503) if _tweet(request.url) == "5000"
                                       else _response(request.url, 200, body))
        rows = [("100", 1, "photo", "alice"), ("5000", 1, "photo", "carol"), ("101", 1, "photo", "alice")]
        older = self.output / "myaccount" / "alice" / "alice_101_1.jpg"
        options = replace(self.quick, unsaved_previews=True)
        with patch("luvbox.core.QUICK_STOP_AFTER", 3):
            for index in range(REFUSED_LIMIT + 1):
                rows = [(str(200 + index), 1, "photo", "bob")] + rows
                self.sent = []
                code, finished, events = self.save(rows, options=options, downloader=HttpDownloader)
                # Every save still asks for it and stops there, as the protection means it to,
                self.assertEqual((code, finished["status"], finished["scan_mode"]), (1, "error", "full"))
                self.assertEqual([url for url in self.sent if _tweet(url) == "5000"], [failing])
                self.assertIn("사진 서버 접근·연결 오류로 멈췄습니다", finished["message"])
                self.assertIn("저장하지 못한 미디어 1개는 ‘설정 › 고급 › 저장 못 한 미디어’", finished["message"])
                self.assertNotIn("건너뜁니다", finished["message"])
                # and lists it with why, without promising that saves will pass it by.
                self.assertIn(("5000", 1, "photo"), self.listed())
                self.assertIn("사진 서버 오류로 저장을 멈췄습니다", self.detail("5000"))
                self.assertNotIn("건너뜁니다", self.detail("5000"))
                self.assertEqual(unsaved.refusals("myaccount", self.options.output_dir), {})
                self.assertFalse(older.exists())
        # Once the host serves it again, the next save gets it and reaches the older likes.
        self.answer = lambda request: _response(request.url, 200, body)
        code, finished, events = self.save(rows, options=options, downloader=HttpDownloader)
        self.assertEqual((code, finished["status"], finished["scan_mode"]), (0, "finished", "full"))
        self.assertTrue(older.is_file())
        self.assertNotIn(("5000", 1, "photo"), self.listed())

    def test_only_a_reply_that_the_file_is_not_served_counts_toward_passing_it_by(self):
        from gallery_dl.downloader.http import HttpDownloader

        for status, counted in ((401, 1), (403, 1), (410, 1), (451, 1), (500, 0), (502, 0), (503, 0), (504, 0)):
            with self.subTest(status=status):
                tweet = str(status * 10)                 # a media of its own for each answer
                self.answer = lambda request, _status=status: _response(request.url, _status)
                code, finished, events = self.save([(tweet, 1, "photo", "carol")], downloader=HttpDownloader)
                self.assertEqual((code, finished["status"], finished["failed_media"]), (1, "error", 1))
                self.assertIn((tweet, 1, "photo"), self.listed())
                self.assertEqual(unsaved.refusals("myaccount", self.options.output_dir).get((tweet, 1, "photo"), 0),
                                 counted)
                self.assertEqual("같은 파일에서" in self.detail(tweet), bool(counted))

    def test_the_media_that_stops_a_full_unsaved_list_is_still_remembered(self):
        """The newest failure has to stay: it is the one whose refusal must count."""
        from gallery_dl.downloader.http import HttpDownloader

        body = b"\xff\xd8\xff" + b"offline-photo" * 8
        self.answer = lambda request: (_response(request.url, 403) if _tweet(request.url) == "9000"
                                       else _response(request.url, 404))
        rows = [("100", 1, "photo", "alice"), ("9000", 1, "photo", "carol")]
        with patch("luvbox.unsaved.MAX_ITEMS", 1):
            code, finished, events = self.save(rows, downloader=HttpDownloader)
        self.assertEqual((code, finished["status"]), (1, "error"))
        self.assertIn(("9000", 1, "photo"), self.listed())
        self.assertEqual(unsaved.refusals("myaccount", self.options.output_dir), {("9000", 1, "photo"): 1})

    def test_a_retry_that_is_refused_again_counts_nothing(self):
        from gallery_dl.downloader.http import HttpDownloader

        self.answer = lambda request: _response(request.url, 403)
        events = []
        with patch("gallery_dl.extractor.find", side_effect=lambda url: test_missing_files.FakeTweetExtractor(
                    url, {"5000": [(1, "photo")]})), \
                patch("gallery_dl.downloader.find", return_value=HttpDownloader), \
                patch("requests.Session.send", side_effect=self.send):
            code = run_worker(replace(self.options, targets=("5000_1",)), events.append)
        # Since 1.10.8 a refused retry gives up that file and ends normally (the files chosen with
        # it are still tried; tests/test_retry_and_quick_rules.py). It stays listed and counts nothing.
        self.assertEqual((code, events[-1]["status"], events[-1]["saved"]), (0, "finished", 0))
        self.assertEqual([identity[0] for identity in self.listed()], ["5000"])
        self.assertEqual(unsaved.refusals("myaccount", self.options.output_dir), {})

    def test_a_cut_off_or_timed_out_media_is_not_counted_as_refused(self):
        """Connection trouble belongs to this PC's network as often as to the media; only replies count."""
        from gallery_dl.downloader.http import HttpDownloader

        def timeout(request):
            raise requests.exceptions.ReadTimeout("timed out")

        self.answer = timeout
        for _ in range(4):
            code, finished, events = self.save([("5000", 1, "photo", "carol")], downloader=HttpDownloader)
            self.assertEqual((code, finished["status"]), (1, "error"))
        self.assertEqual(unsaved.refusals("myaccount", self.options.output_dir), {})
        self.assertEqual(self.sent.count("https://pbs.twimg.com/media/5000_1?format=jpg&name=orig"), 4)

    def test_forgetting_a_listed_media_forgets_its_refusals(self):
        item = dict(reason=unsaved.FAILED, tweet_id="5000", num=1, media_type="photo", detail="거부")
        scope = dict(account="myaccount", output_dir=self.options.output_dir)
        for _ in range(3):
            unsaved.record([item], refused=("5000", 1, "photo"), **scope)
        self.assertEqual(unsaved.refusals(**scope), {("5000", 1, "photo"): 3})
        unsaved.remove("myaccount", self.options.output_dir, items=[("5000", 1, "photo")])
        self.assertEqual(unsaved.refusals(**scope), {})
        # Listed again later, it starts from nothing.
        unsaved.record([item], **scope)
        self.assertEqual(unsaved.refusals(**scope), {})
        # A row the ledger trims away takes its count with it.
        unsaved.record([item], refused=("5000", 1, "photo"), **scope)
        with patch("luvbox.unsaved.MAX_ITEMS", 1):
            unsaved.record([dict(item, tweet_id="5001")], **scope)
        self.assertEqual(self.listed(), {("5001", 1, "photo")})
        unsaved.record([item], **scope)
        self.assertEqual(unsaved.refusals(**scope), {})

    # -- the optional picture of an unsaved or excluded media --------------------------------

    def test_a_refused_or_failed_picture_request_never_ends_the_save(self):
        """The real downloader and request protection; only the picture request goes wrong."""
        from gallery_dl.downloader.http import HttpDownloader

        def cut_off(request):
            response = _response(request.url, 200, b"\xff\xd8\xff")

            def broken(*args, **kwargs):
                raise requests.exceptions.ChunkedEncodingError("cut off")
                yield b""

            response.iter_content = broken
            return response

        def timeout(request):
            raise requests.exceptions.ReadTimeout("timed out")

        body = b"\xff\xd8\xff" + b"offline-photo" * 8
        # A 429 leaves a cooldown for the next save of this account, so it comes last.
        cases = (("503", lambda request: _response(request.url, 503)),
                 ("403", lambda request: _response(request.url, 403)),
                 ("timeout", timeout), ("cut off", cut_off),
                 ("429", lambda request: _response(request.url, 429, headers={"Retry-After": "3600"})))
        for index, (name, preview) in enumerate(cases):
            with self.subTest(answer=name):
                first = 1000 * (index + 1)
                tweets = [str(first + offset) for offset in range(3)]
                self.answer = lambda request, _preview=preview: (
                    _preview(request) if "name=small" in request.url else _response(request.url, 200, body))
                self.sent = []
                self.exclude((tweets[0], 1, "photo"))
                code, finished, events = self.save([(tweet, 1, "photo", "alice") for tweet in tweets],
                                                   options=replace(self.options, unsaved_previews=True),
                                                   downloader=HttpDownloader)
                self.assertEqual([url for url in self.sent if "name=small" in url],
                                 [f"https://pbs.twimg.com/media/{tweets[0]}_1?format=jpg&name=small"])
                self.assertEqual((code, finished["status"], finished["saved"]), (0, "finished", 2))
                self.assertNotIn("사진 서버", finished["message"])
                if name == "429":
                    # The save finished, but the next one waits for the limit; say until when.
                    self.assertGreater(datetime.fromisoformat(finished["retry_at"]).timestamp(), time.time() + 3000)
                    self.assertIn("미리보기 요청이 X 요청 한도에 걸려", finished["message"])
                    self.assertIn("이후에 다시 저장할 수 있습니다", finished["message"])
                else:
                    self.assertEqual(finished["retry_at"], "")
                    self.assertNotIn("요청 한도", finished["message"])
                for tweet in tweets[1:]:
                    self.assertTrue((self.output / "myaccount" / "alice" / f"alice_{tweet}_1.jpg").is_file())
                self.assertEqual(self.runs()[-1]["status"], "finished")

    def test_a_cooldown_a_picture_left_is_not_shortened_later_in_the_same_save(self):
        from gallery_dl.downloader.http import HttpDownloader

        self.answer = lambda request: _response(request.url, 429, headers={
            "Retry-After": "3600" if "name=small" in request.url else "60"})
        self.exclude(("100", 1, "photo"))
        started = time.time()
        code, finished, events = self.save([("100", 1, "photo", "alice"), ("101", 1, "photo", "alice")],
                                           options=replace(self.options, unsaved_previews=True),
                                           downloader=HttpDownloader)
        # The photo's own 429 still ends the save, with the later of the two times.
        self.assertEqual((code, finished["status"]), (1, "error"))
        self.assertEqual(self.sent, ["https://pbs.twimg.com/media/100_1?format=jpg&name=small",
                                     "https://pbs.twimg.com/media/101_1?format=jpg&name=orig"])
        self.assertGreater(datetime.fromisoformat(finished["retry_at"]).timestamp(), started + 3000)
        self.assertIn(datetime.fromisoformat(finished["retry_at"]).strftime("%Y-%m-%d %H:%M"), finished["message"])
        (state,) = (self.base / "protection").glob("*.json")
        self.assertGreater(json.loads(state.read_text(encoding="utf-8"))["until"], started + 3000)

    def test_previews_stop_for_the_rest_of_the_run_after_the_first_error(self):
        self.answer = lambda request: _response(request.url, 503)
        self.exclude(("100", 1, "photo"), ("102", 1, "photo"))
        QuietDownloader.missing = {"103"}
        rows = [("100", 1, "photo", "alice"), ("101", 1, "photo", "alice"),
                ("102", 1, "photo", "alice"), ("103", 1, "photo", "alice"), ("104", 1, "photo", "alice")]
        code, finished, events = self.save(rows, options=replace(self.options, unsaved_previews=True))
        self.assertEqual((code, finished["status"], finished["saved"], finished["failed_media"]),
                         (0, "finished", 2, 1))
        self.assertEqual(self.sent, ["https://pbs.twimg.com/media/100_1?format=jpg&name=small"])
        # A later save asks again: the error ended only that run's pictures.
        self.answer = lambda request: _response(request.url, 404)
        self.sent = []
        code, finished, events = self.save(rows, options=replace(self.options, unsaved_previews=True))
        self.assertEqual(code, 0)
        self.assertEqual(self.sent, ["https://pbs.twimg.com/media/100_1?format=jpg&name=small",
                                     "https://pbs.twimg.com/media/102_1?format=jpg&name=small",
                                     "https://pbs.twimg.com/media/103_1?format=jpg&name=small"])


if __name__ == "__main__":
    unittest.main()
