"""Unsaved media get one small, bounded picture from X's image host, and only when asked to."""
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from tests import app_data_isolation  # noqa: F401  Never open the user's app data.
from tests import test_core, test_media
from luvbox import thumbcache
from luvbox.core import JobOptions, run_worker
from luvbox.unsaved_preview import MAX_BYTES, UnsavedPreviews, fetch, preview_source


def _png(color=(200, 40, 60), size=(64, 48)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


class Response:
    def __init__(self, body=b"", *, status=200, kind="image/png", length=None, chunks=None):
        self.status_code = status
        self.headers = {"Content-Type": kind}
        if length is not None:
            self.headers["Content-Length"] = str(length)
        self._chunks = chunks if chunks is not None else [body]
        self.closed = False

    def iter_content(self, size):
        yield from self._chunks

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **options):
        self.calls.append((url, options))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class PreviewSourceTests(unittest.TestCase):
    def test_a_photo_is_asked_for_at_its_small_size(self):
        photo = {"type": "photo"}
        self.assertEqual(preview_source("https://pbs.twimg.com/media/Abc_1-x?format=jpg&name=orig", photo),
                         "https://pbs.twimg.com/media/Abc_1-x?format=jpg&name=small")
        self.assertEqual(preview_source("https://pbs.twimg.com/media/Abc.PNG", photo),
                         "https://pbs.twimg.com/media/Abc?format=png&name=small")
        self.assertEqual(preview_source("https://pbs.twimg.com/media/Abc?format=jpeg&name=4096x4096", {}),
                         "https://pbs.twimg.com/media/Abc?format=jpg&name=small")

    def test_a_video_uses_the_poster_the_post_carries(self):
        video = {"type": "video", "_luvbox_poster": "https://pbs.twimg.com/ext_tw_video_thumb/12/pu/img/Xy-z.jpg"}
        self.assertEqual(preview_source("https://video.twimg.com/ext_tw_video/12/pu/vid/a.mp4", video),
                         "https://pbs.twimg.com/ext_tw_video_thumb/12/pu/img/Xy-z?format=jpg&name=small")
        gif = {"type": "animated_gif", "_luvbox_poster": "https://pbs.twimg.com/tweet_video_thumb/Q.jpg"}
        self.assertEqual(preview_source("https://video.twimg.com/tweet_video/Q.mp4", gif),
                         "https://pbs.twimg.com/tweet_video_thumb/Q?format=jpg&name=small")
        self.assertEqual(preview_source("https://video.twimg.com/a.mp4", {"type": "video"}), "")

    def test_nothing_but_the_image_host_over_https_is_returned(self):
        photo = {"type": "photo"}
        for url in ("http://pbs.twimg.com/media/A?format=jpg&name=orig",
                    "https://pbs.twimg.com.example.org/media/A?format=jpg",
                    "https://user:secret@pbs.twimg.com/media/A?format=jpg",
                    "https://pbs.twimg.com:8443/media/A?format=jpg",
                    "https://x.com/media/A?format=jpg", "https://pbs.twimg.com/media/../A?format=jpg",
                    "https://pbs.twimg.com/media/A?format=svg", "https://pbs.twimg.com/media/A",
                    "https://pbs.twimg.com/media/A b?format=jpg", "file:///C:/secret.jpg", "", None,
                    "https://pbs.twimg.com/media/" + "A" * 3000 + "?format=jpg"):
            with self.subTest(url=url):
                self.assertEqual(preview_source(url, photo), "")
        self.assertEqual(preview_source("https://pbs.twimg.com/media/A?format=jpg", None), "")


class FetchTests(unittest.TestCase):
    def test_a_small_image_is_returned_without_following_redirects(self):
        body = _png()
        session = Session(Response(body, length=len(body)))
        self.assertEqual(fetch(session, "https://pbs.twimg.com/media/A?format=jpg&name=small"), body)
        url, options = session.calls[0]
        self.assertFalse(options["allow_redirects"])
        self.assertTrue(options["stream"])
        self.assertEqual(options["timeout"], (5.0, 8.0))

    def test_other_answers_are_refused_and_always_closed(self):
        cases = (Response(status=302), Response(status=404), Response(b"<html>", kind="text/html"),
                 Response(_png(), length=MAX_BYTES + 1), Response(_png(), length="many"),
                 Response(chunks=[b"x" * 200_000, b"x" * 200_000]), Response(b""))
        for response in cases:
            with self.subTest(status=response.status_code, kind=response.headers["Content-Type"]):
                self.assertIsNone(fetch(Session(response), "https://pbs.twimg.com/media/A?format=jpg&name=small"))
                self.assertTrue(response.closed)


class KeeperTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="luvbox-unsaved-preview-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        directory = patch("luvbox.thumbcache.directory", return_value=self.root / "thumbnails")
        directory.start()
        self.addCleanup(directory.stop)
        self.recorder = thumbcache.Recorder("offline")
        self.addCleanup(self.recorder.close)

    def media(self, tweet="100", kind="photo"):
        return {"tweet_id": tweet, "num": 1, "type": kind}

    def url(self, tweet="100"):
        return f"https://pbs.twimg.com/media/{tweet}_1?format=jpg&name=orig"

    def test_one_request_fills_the_preview_and_the_list_can_show_it(self):
        keeper = UnsavedPreviews(self.recorder)
        session = Session(Response(_png(), length=len(_png())))
        self.assertTrue(keeper.keep(session, self.url(), self.media()))
        self.assertEqual(session.calls[0][0], "https://pbs.twimg.com/media/100_1?format=jpg&name=small")
        self.assertEqual((keeper.requested, keeper.stored), (1, 1))
        self.assertTrue(self.recorder.has(("100", 1, "photo")))
        # Met again (a full check passes every like): no second request.
        self.assertTrue(keeper.keep(Session(), self.url(), self.media()))
        self.recorder.close()
        # The list learns there is a picture without reading it; a tile reads it when shown.
        self.assertEqual(thumbcache.kept("offline", [("100", 1, "photo"), ("101", 1, "photo"), ("bad",)]),
                         {("100", 1, "photo")})
        picture = thumbcache.recall("offline", ("100", 1, "photo"))
        self.assertLessEqual(picture.size, thumbcache.SIZE)
        self.assertEqual(thumbcache.kept("not an account", [("100", 1, "photo")]), set())
        self.assertEqual(thumbcache.kept("nobody", [("100", 1, "photo")]), set())
        self.assertEqual(list((self.root / "thumbnails").glob("unsaved-preview-*")), [])

    def test_failures_stop_the_requests_for_the_rest_of_the_run(self):
        keeper = UnsavedPreviews(self.recorder)
        session = Session(OSError("offline"), Response(status=500), Response(b"not an image", kind="image/jpeg"))
        for tweet in ("101", "102", "103"):
            self.assertFalse(keeper.keep(session, self.url(tweet), self.media(tweet)))
        self.assertEqual(len(session.calls), 3)
        self.assertFalse(keeper.keep(session, self.url("104"), self.media("104")))
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(list((self.root / "thumbnails").glob("unsaved-preview-*")), [])

    def test_the_run_limit_and_unusable_media_make_no_request(self):
        keeper = UnsavedPreviews(self.recorder, limit=1)
        session = Session(Response(_png()), Response(_png()))
        self.assertTrue(keeper.keep(session, self.url("201"), self.media("201")))
        self.assertFalse(keeper.keep(session, self.url("202"), self.media("202")))
        self.assertEqual(len(session.calls), 1)
        fresh = UnsavedPreviews(self.recorder)
        self.assertFalse(fresh.keep(session, "https://example.org/a.jpg", self.media("203")))
        self.assertFalse(fresh.keep(session, self.url("204"), {"tweet_id": "x", "num": 1, "type": "photo"}))
        self.assertFalse(fresh.keep(session, "https://video.twimg.com/a.mp4", self.media("205", "video")))
        self.assertEqual(len(session.calls), 1)


class EngineTests(unittest.TestCase):
    """The real save: a failed photo is pictured only when the option is on."""
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    set_session = test_core.CoreTests.set_session

    class QuietFailure(test_core.FakeHttpDownloader):
        def download(self, url, pathfmt):
            return False

    def save(self, options):
        events = []
        requests = []

        def get(session, url, **arguments):
            requests.append(url)
            return Response(_png(), length=len(_png()))

        rows = [("100", 1, "photo", "alice", None)]
        with patch("gallery_dl.extractor.find", side_effect=lambda url: test_core.FakeLikesExtractor(url, rows)), \
                patch("gallery_dl.downloader.find", return_value=self.QuietFailure), \
                patch("requests.Session.get", new=get), \
                patch("luvbox.unsaved.unsaved_path", return_value=self.base / "unsaved.sqlite3"), \
                patch("luvbox.thumbcache.directory", return_value=self.base / "thumbnails"):
            code = run_worker(options, events.append)
            previews = thumbcache.kept("myaccount", [("100", 1, "photo")])
        self.assertNotIn(test_core.SECRET, json.dumps(events))
        return events, requests, previews

    def test_options_built_without_the_app_make_no_request(self):
        self.assertFalse(JobOptions("a", "b").unsaved_previews)
        events, requests, previews = self.save(self.options)
        self.assertEqual(events[-1]["failed_media"], 1)
        self.assertEqual(requests, [])
        self.assertEqual(previews, set())

    def exclude(self, *identities):
        from contextlib import closing
        from luvbox.history import HistoryStore
        from luvbox.storage import resolve_storage
        paths = resolve_storage(self.options.output_dir, self.options.account, self.options.separate_accounts)
        with closing(HistoryStore.for_storage(paths)) as store:
            store.set_missing_ignored(list(identities), True)

    def test_an_excluded_photo_met_again_gets_its_picture_and_stays_unsaved(self):
        # The person chose not to download it again; the excluded list still wants a picture of it.
        self.exclude(("100", 1, "photo"))
        events, requests, previews = self.save(replace(self.options, unsaved_previews=True))
        self.assertEqual((events[-1]["saved"], events[-1]["failed_media"]), (0, 0))
        self.assertEqual(requests, ["https://pbs.twimg.com/media/100_1?format=jpg&name=small"])
        self.assertIn(("100", 1, "photo"), previews)
        # A second pass meets it again and asks for nothing.
        events, requests, previews = self.save(replace(self.options, unsaved_previews=True))
        self.assertEqual(requests, [])
        self.assertIn(("100", 1, "photo"), previews)

    def test_an_excluded_photo_is_not_pictured_without_the_option(self):
        self.exclude(("100", 1, "photo"))
        events, requests, previews = self.save(self.options)
        self.assertEqual((events[-1]["saved"], events[-1]["failed_media"]), (0, 0))
        self.assertEqual(requests, [])
        self.assertEqual(previews, set())

    def test_a_failed_photo_gets_its_picture_when_the_app_asks_for_it(self):
        events, requests, previews = self.save(replace(self.options, unsaved_previews=True))
        self.assertEqual(events[-1]["failed_media"], 1)
        self.assertEqual(requests, ["https://pbs.twimg.com/media/100_1?format=jpg&name=small"])
        self.assertIn(("100", 1, "photo"), previews)
        # A second pass meets the same media again and asks for nothing.
        events, requests, previews = self.save(replace(self.options, unsaved_previews=True))
        self.assertEqual(requests, [])
        self.assertIn(("100", 1, "photo"), previews)
        with self.assertRaises(ValueError):
            replace(self.options, unsaved_previews="yes").validate()


if __name__ == "__main__":
    unittest.main()
