"""A '비슷함' photo goes to the Recycle Bin only alone, compared with the photo its group keeps.

The duplicate view never offers a photo grouped as "the same picture at another
size or quality" as a copy: its bytes are its own, and no comparison of pixels
tells every such photo from another picture on the same template with one
character of small text changed. A person may still look at it beside the
photo its group keeps and send that one photo. These tests hold that path to
what it promises: one photo per batch, never a group, a selection or a bulk
choice; the photo kept must be the very file that was shown, another file
than this one, still there and still the same picture right before the move;
and on the shape of the real library (one file recorded under two folder
layouts with the same path) no file without another picture of it can go.
"""

from contextlib import closing
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from tests import app_data_isolation  # noqa: F401  Never open the user's app data.
from tests.test_near_duplicate_text import NOTICE, _notice, _photo, _rows, _schedule, _template
from luvbox import deletion, duplicates
from luvbox.history import HistoryStore
from luvbox.qt_library import LibraryRepository, _Request
from luvbox.qt_management import ManagementController
from luvbox.recycling import file_signature
from luvbox.storage import resolve_storage

ACCOUNT = "offline"
_SHOWN = object()          # the photo kept's signature as it is now


class AlikeCatalog(unittest.TestCase):
    """A catalog of its own, a fake Recycle Bin and the duplicate view's repository."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="luvbox-alike-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "appdata")})
        environment.start()
        self.addCleanup(environment.stop)
        self.paths = resolve_storage(self.root / "Likes", ACCOUNT)
        with closing(HistoryStore.for_storage(self.paths)):
            pass                                    # makes the folders and the catalog
        self.files = {}
        self.bin = self.root / "fake-recycle-bin"
        self.bin.mkdir()

    def add(self, tweet, image=None, *, source=None, modes=("flat",), **options):
        """One photo: ``image`` saved, or the bytes of ``source``; one record per folder layout, one path."""
        path = self.paths.output_dir / f"writer_{tweet}_1.jpg"
        if source is not None:
            path.write_bytes(self.files[source].read_bytes())
        else:
            image.save(path, "JPEG", **options)
        self.files[tweet] = path
        details = {"tweet_id": tweet, "num": 1, "type": "photo", "author": {"name": "writer"},
                   "date": f"2026-10-{1 + int(tweet) % 28:02d}T12:00:00+09:00"}
        with closing(HistoryStore.for_storage(self.paths)) as store:
            for index, mode in enumerate(modes):
                # The real library: saved by author folder, moved, met again by a flat save.
                store.record_media(mode, details, path, **({"status": "existing"} if index else {}))
        return path

    def repository(self, duplicate_only=True):
        repository = LibraryRepository()
        self.addCleanup(repository.configure, None)
        repository.provider = self
        repository.configure(_Request(generation=1, account=ACCOUNT, output_dir=str(self.paths.output_dir),
                                      separate_accounts=False, duplicate_only=duplicate_only))
        return repository

    def register(self, key, path):
        pass

    def shown(self, repository):
        """The duplicate view's rows as served: {tweet: row}, each key registered for later choices."""
        _total, rows = repository.read("1", 0, 100)
        return {row["key"].split("_")[2]: row for row in rows}

    def key(self, repository, tweet, mode=None):
        rows = self.shown(repository)
        key = rows[tweet]["key"]
        if mode is not None:
            key = key.replace("_flat_", f"_{mode}_").replace("_author_", f"_{mode}_")
        return key

    def recycle(self, path, expected=None):
        path = Path(path)
        self.assertEqual(file_signature(path), tuple(expected))
        destination = self.bin / path.name
        path.rename(destination)
        return str(destination)

    def delete(self, rows):
        with patch.object(deletion, "recycle_file", side_effect=self.recycle) as recycle:
            result = deletion.run_deletion(self.paths, rows, True, threading.Event(), lambda _: None)
        self.recycled = recycle.call_count
        return result

    def record(self, tweet, mode="flat"):
        with closing(HistoryStore.for_storage(self.paths, create_media=False)) as store:
            return store.media_record(mode, {"tweet_id": tweet, "num": 1, "type": "photo"})

    def alike_row(self, tweet, keeper, mode="flat", keeper_signature=_SHOWN):
        """A row as the comparison marks it, made by hand: ``keeper`` is the path offered as the photo kept."""
        row = self.record(tweet, mode)
        signature = file_signature(row["path"])
        if keeper_signature is _SHOWN:
            keeper_signature = file_signature(keeper)
        return dict(row, _delete_signature=signature, _alike_keeper=str(keeper),
                    _alike_keeper_signature=keeper_signature)

    def bin_names(self):
        return sorted(path.name for path in self.bin.iterdir())


class ComparedPhotoTests(AlikeCatalog):
    """A photo, the photo saved smaller, a file with the photo's bytes and a photo of its own."""

    def setUp(self):
        super().setUp()
        photo = _photo(7)
        self.add("200", photo, quality=95)
        self.add("201", photo.resize((900, 675), Image.Resampling.LANCZOS), quality=85)
        self.add("300", _photo(23, (800, 600)), quality=92)
        self.add("301", source="300")
        self.add("500", _photo(41, (800, 600)), quality=90)

    def scanned(self):
        repository = self.repository()
        self.assertEqual(repository.scan_duplicates(1, near=True), 4)
        rows = self.shown(repository)
        self.assertEqual({tweet: (row["keeper"], row["copy"]) for tweet, row in rows.items()},
                         {"200": (True, False), "201": (False, False), "301": (True, False), "300": (False, True)})
        return repository

    def test_the_compared_photo_goes_alone_and_the_photo_kept_stays(self):
        repository = self.scanned()
        manifest = repository.alike_manifest(self.key(repository, "201"))
        self.assertEqual(len(manifest["rows"]), 1)
        row = manifest["rows"][0]
        self.assertEqual((row["tweet_id"], row["_alike_keeper"]), ("201", str(self.files["200"])))
        self.assertEqual(tuple(row["_alike_keeper_signature"]), file_signature(self.files["200"]))
        self.assertNotIn("_copy_keepers", row)
        # What the comparison shows: both pictures at their sizes.
        shown = manifest["alike"]
        self.assertEqual((shown["keeper"]["pixels"], shown["alike"]["pixels"]), ("1200×900", "900×675"))
        self.assertEqual(shown["keeper"]["name"], "writer_200_1.jpg")
        self.assertTrue(shown["alike"]["url"].startswith("file:///") and shown["alike"]["url"].endswith("writer_201_1.jpg"))
        self.assertTrue(shown["keeper"]["bytes"] and shown["alike"]["bytes"])
        result = self.delete(manifest["rows"])
        self.assertEqual((result["recycled"], result["protected"], result["failed"]), (1, 0, 0))
        self.assertEqual(self.bin_names(), ["writer_201_1.jpg"])
        self.assertTrue(all(self.files[tweet].is_file() for tweet in ("200", "300", "301", "500")))

    def test_only_a_photo_shown_as_alike_can_be_compared(self):
        repository = self.scanned()
        for tweet, reason in (("200", "‘비슷함’으로 표시된"), ("301", "‘비슷함’으로 표시된"),
                              ("300", "‘비슷함’으로 표시된")):
            with self.subTest(tweet=tweet), self.assertRaisesRegex(ValueError, reason):
                repository.alike_manifest(self.key(repository, tweet))
        # A key the view never served, or one from another list.
        for key in ("", "g1_flat_500_1_photo", "g2_flat_201_1_photo", None):
            with self.subTest(key=key), self.assertRaises(ValueError):
                repository.alike_manifest(key)
        # Outside the duplicate view nothing is compared.
        gallery = self.repository(duplicate_only=False)
        _total, rows = gallery.read("1", 0, 100)
        key = next(row["key"] for row in rows if row["key"].split("_")[2] == "201")
        with self.assertRaisesRegex(ValueError, "목록이 바뀌었습니다"):
            gallery.alike_manifest(key)
        self.assertEqual(self.bin_names(), [])

    def test_a_stored_group_of_two_other_pictures_is_refused_before_anything_is_shown(self):
        """A group an earlier scan or version stored, of two pictures that are not one: no comparison, no move."""
        repository = self.scanned()
        with closing(HistoryStore.for_storage(self.paths, create_media=False)) as store:
            store.save_clusters([[("flat", "200", 1, "photo"), ("flat", "500", 1, "photo")]])
            store.set_duplicate_rule("near")
        repository = self.repository()
        rows = self.shown(repository)
        self.assertEqual({tweet: (row["keeper"], row["copy"]) for tweet, row in rows.items()},
                         {"200": (True, False), "500": (False, False)})
        with self.assertRaisesRegex(ValueError, "같은 사진으로 다시 확인되지 않아"):
            repository.alike_manifest(rows["500"]["key"])
        result = self.delete([self.alike_row("500", self.files["200"])])
        self.assertEqual((result["recycled"], result["protected"]), (0, 1))
        self.assertEqual(self.bin_names(), [])

    def test_the_photo_kept_must_be_the_file_that_was_shown(self):
        repository = self.scanned()
        manifest = repository.alike_manifest(self.key(repository, "201"))
        # Saved again (the same picture, new bytes) after the two were shown side by side.
        keeper = self.files["200"]
        stamp = keeper.stat().st_mtime_ns
        with Image.open(keeper) as source:
            picture = source.convert("RGB")
        picture.save(keeper, "JPEG", quality=80)
        os.utime(keeper, ns=(stamp + 10_000_000, stamp + 10_000_000))
        result = self.delete(manifest["rows"])
        self.assertEqual((result["recycled"], result["protected"]), (0, 1))
        self.assertIn("남길 사진이 그대로 있고 같은 사진인지", result["message"])
        self.assertNotIn("같은 내용의 다른 파일이 남는지", result["message"])
        self.assertTrue(self.files["201"].is_file())

    def test_a_photo_kept_that_is_gone_keeps_the_other(self):
        repository = self.scanned()
        manifest = repository.alike_manifest(self.key(repository, "201"))
        self.files["200"].rename(self.root / "moved-away.jpg")
        result = self.delete(manifest["rows"])
        self.assertEqual((result["recycled"], result["protected"]), (0, 1))
        self.assertTrue(self.files["201"].is_file())
        # Asked again with the photo kept gone: refused before anything is shown.
        repository = self.repository()
        with self.assertRaisesRegex(ValueError, "남길 사진 파일이 없어"):
            repository.alike_manifest(self.key(repository, "201"))

    def test_a_photo_changed_after_it_was_shown_stays(self):
        repository = self.scanned()
        manifest = repository.alike_manifest(self.key(repository, "201"))
        path = self.files["201"]
        stamp = path.stat().st_mtime_ns
        os.utime(path, ns=(stamp + 10_000_000, stamp + 10_000_000))
        result = self.delete(manifest["rows"])
        self.assertEqual((result["recycled"], result["failed"]), (0, 1))
        self.assertTrue(path.is_file())

    def test_never_with_other_rows(self):
        repository = self.scanned()
        alike = repository.alike_manifest(self.key(repository, "201"))["rows"][0]

        def ordinary(tweet, seed):
            self.add(tweet, _photo(seed, (640, 480)), quality=90)
            return dict(self.record(tweet), _delete_signature=file_signature(self.files[tweet]))

        # Beside an ordinary row (before or after it), beside itself, or carrying a copy mark: it stays.
        for rows, recycled in (([alike, ordinary("600", 43)], ["writer_600_1.jpg"]),
                               ([ordinary("601", 47), alike], ["writer_600_1.jpg", "writer_601_1.jpg"]),
                               ([alike, dict(alike)], ["writer_600_1.jpg", "writer_601_1.jpg"]),
                               ([dict(alike, _copy_keepers=[str(self.files["200"])])],
                                ["writer_600_1.jpg", "writer_601_1.jpg"])):
            with self.subTest(rows=[row["tweet_id"] for row in rows]):
                result = self.delete([dict(row) for row in rows])
                self.assertEqual(result["protected"], 1)
                self.assertEqual(self.bin_names(), recycled)
                self.assertTrue(self.files["201"].is_file())

    def test_the_copy_choices_still_keep_the_alike_photo(self):
        """'사본 N장 선택', '사본 일괄 선택' and a hand pick still never send it."""
        repository = self.scanned()
        self.assertEqual(sorted(key.split("_")[2] for key in repository.duplicate_copy_keys(1)), ["300"])
        keys = [self.key(repository, tweet) for tweet in ("200", "201", "300", "301")]
        manifest = repository.deletion_manifest(keys)
        self.assertFalse(any("_alike_keeper" in row for row in manifest["rows"]))
        result = self.delete(manifest["rows"])
        # Two whole groups chosen: each keeps its first photo; the copy goes, the alike photo stays.
        self.assertEqual((result["recycled"], result["protected"]), (1, 1))
        self.assertEqual(self.bin_names(), ["writer_300_1.jpg"])
        self.assertTrue(self.files["201"].is_file() and self.files["200"].is_file())

    def test_the_confirmation_says_one_photo_and_what_is_checked(self):
        repository = self.scanned()
        manifest = repository.alike_manifest(self.key(repository, "201"))
        shown = type("Shown", (), {"_delete": manifest, "deleteCount": 1})()
        summary = ManagementController.deleteSummary.fget(shown)
        self.assertIn("‘비슷함’ 사진 한 장만", summary)
        self.assertIn("남길 사진이 그대로 있는지, 같은 사진인지 다시 확인", summary)
        self.assertEqual(ManagementController.deleteAlike.fget(shown), manifest["alike"])


class SameFileTests(AlikeCatalog):
    """The photo kept is never this photo under another record, name or link."""

    def setUp(self):
        super().setUp()
        photo = _photo(7)
        self.add("200", photo, quality=95)
        self.add("201", photo.resize((900, 675), Image.Resampling.LANCZOS), quality=85)

    def test_this_file_under_another_name_or_link_is_no_photo_kept(self):
        path = self.files["201"]
        link = self.paths.output_dir / "linked_201_1.jpg"
        os.link(path, link)
        spellings = [str(path), str(path).upper(), str(path.parent) + os.sep + "." + os.sep + path.name, str(link)]
        for keeper in spellings:
            with self.subTest(keeper=keeper):
                row = self.alike_row("201", keeper)
                self.assertEqual(deletion.kept_alike(_Store(), row["path"], file_signature(row["path"]), keeper,
                                                     row["_alike_keeper_signature"]), "")
                result = self.delete([row])
                self.assertEqual((result["recycled"], result["protected"]), (0, 1))
        self.assertTrue(path.is_file())
        self.assertEqual(self.bin_names(), [])

    def test_on_a_volume_without_file_numbers_the_path_itself_decides(self):
        """Some volumes report no file number; another spelling of this very path is still this file."""
        real = deletion.file_signature

        def numberless(path):
            device, _number, size, stamp = real(path)
            return device, 0, size, stamp

        path = self.files["201"]
        for keeper in (str(path).upper(), str(path.parent) + os.sep + "." + os.sep + path.name):
            with self.subTest(keeper=keeper), patch.object(deletion, "file_signature", side_effect=numberless):
                row = self.alike_row("201", keeper, keeper_signature=numberless(keeper))
                self.assertEqual(deletion.kept_alike(_Store(), row["path"], numberless(row["path"]), keeper,
                                                     row["_alike_keeper_signature"]), "")
        # The photo kept, another file, still passes there.
        with patch.object(deletion, "file_signature", side_effect=numberless):
            keeper = str(self.files["200"])
            self.assertEqual(deletion.kept_alike(_Store(), str(path), numberless(path), keeper, numberless(keeper)),
                             keeper)
        self.assertTrue(path.is_file())

    def test_a_group_whose_first_photo_is_this_file_is_refused_before_anything_is_shown(self):
        """Forged: the group's first photo is a hard link to this very file. The catalog cannot make this
        (a file with this photo's bytes would make it a copy), so the stored groups are replaced here."""
        link = self.paths.output_dir / "writer_202_1.jpg"
        os.link(self.files["201"], link)
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store.record_media("flat", {"tweet_id": "202", "num": 1, "type": "photo", "author": {"name": "writer"},
                                        "date": "2026-10-03T12:00:00+09:00"}, link)
        repository = self.repository()
        # The scan already takes the link and its file for one photo: only one of them is in the group.
        self.assertEqual(repository.scan_duplicates(1, near=True), 2)
        rows = self.shown(repository)
        target = "201" if "201" in rows else "202"
        other = "202" if target == "201" else "201"
        paths = {"201": str(self.files["201"]), "202": str(link)}
        first = {"identity": ("flat", other, 1, "photo"), "path": paths[other]}
        own = {"identity": ("flat", target, 1, "photo"), "path": paths[target]}
        with patch.object(HistoryStore, "duplicate_members", return_value={1: [first, own]}), \
                patch.object(HistoryStore, "duplicate_copy_identities", return_value=[]):
            with self.assertRaisesRegex(ValueError, "같은 파일"):
                repository.alike_manifest(rows[target]["key"])
        self.assertTrue(self.files["201"].is_file())

    def test_the_check_reads_the_pictures_now(self):
        """The near rule's two looks run right before the move, on the files as they are."""
        row = self.alike_row("201", self.files["200"])
        with patch.object(duplicates, "same_in_detail", wraps=duplicates.same_in_detail) as detail, \
                patch.object(duplicates, "nearly_identical", wraps=duplicates.nearly_identical) as coarse:
            result = self.delete([row])
        self.assertEqual(result["recycled"], 1)
        self.assertEqual(coarse.call_count, 1)
        self.assertEqual([tuple(map(str, call.args)) for call in detail.call_args_list],
                         [(str(self.files["201"]), str(self.files["200"]))])

    def test_a_signature_that_is_not_one_is_no_proof(self):
        shown = file_signature(self.files["200"])
        for signature in ([], [1, 2, 3], ["1", 2, 3, 4], None, "abcd", [1.0, 2, 3, 4], [True, 2, 3, 4],
                          [float(value) for value in shown], [str(value) for value in shown]):
            with self.subTest(signature=signature):
                row = self.alike_row("201", self.files["200"], keeper_signature=signature)
                self.assertEqual(self.delete([row])["recycled"], 0)
        self.assertTrue(self.files["201"].is_file())

    def test_a_photo_kept_outside_the_library_is_no_proof(self):
        """The same picture as a file outside the media folder: not a photo the library keeps."""
        outside = self.root / "outside" / "writer_200_1.jpg"
        outside.parent.mkdir()
        outside.write_bytes(self.files["200"].read_bytes())
        result = self.delete([self.alike_row("201", outside)])
        self.assertEqual((result["recycled"], result["protected"]), (0, 1))
        self.assertTrue(self.files["201"].is_file())


class RealShapeTests(AlikeCatalog):
    """One file recorded under folder_mode 'author' and 'flat' with the same path, as in the real library."""

    def setUp(self):
        super().setUp()
        photo = _photo(7)
        # The photo: two records, one file. Its smaller save: two records as well. A photo of its own
        # with two records, and one with a file of the same bytes elsewhere.
        self.add("400", photo, quality=95, modes=("author", "flat"))
        self.add("401", photo.resize((900, 675), Image.Resampling.LANCZOS), quality=85, modes=("author", "flat"))
        self.add("402", _photo(23, (800, 600)), quality=92, modes=("author", "flat"))
        self.add("403", _photo(31, (800, 600)), quality=92, modes=("author", "flat"))
        self.add("404", source="403")

    def offered(self, repository):
        """Every photo the view shows, tried for a comparison: {tweet: kept path or the refusal}."""
        outcome = {}
        for tweet, row in self.shown(repository).items():
            try:
                outcome[tweet] = Path(repository.alike_manifest(row["key"])["rows"][0]["_alike_keeper"]).name
            except ValueError as error:
                outcome[tweet] = "refused: " + str(error)[:12]
        return outcome

    def test_each_file_is_one_photo_and_only_the_smaller_save_can_go(self):
        repository = self.repository()
        self.assertEqual(repository.scan_duplicates(1, near=True), 4)
        shown = self.shown(repository)
        self.assertEqual(sorted(shown), ["400", "401", "403", "404"])
        outcome = self.offered(repository)
        self.assertEqual(outcome["401"], "writer_400_1.jpg")
        self.assertTrue(all(outcome[tweet].startswith("refused") for tweet in ("400", "403", "404")), outcome)
        result = self.delete(repository.alike_manifest(shown["401"]["key"])["rows"])
        self.assertEqual(result["recycled"], 1)
        self.assertEqual(self.bin_names(), ["writer_401_1.jpg"])
        for tweet in ("400", "402", "403", "404"):
            self.assertTrue(self.files[tweet].is_file(), tweet)
        # Read again: the group is gone, and nothing is left to compare.
        repository = self.repository()
        self.assertEqual(sorted(self.shown(repository)), ["403", "404"])
        self.assertTrue(all(value.startswith("refused") for value in self.offered(repository).values()))

    def test_groups_stored_by_an_earlier_version_still_pair_no_file_with_itself(self):
        """What versions up to 1.9.3 stored: both records of a file in one group, with its smaller save."""
        repository = self.repository()
        repository.scan_duplicates(1, near=True)
        with closing(HistoryStore.for_storage(self.paths, create_media=False)) as store:
            # The scan hashed one record per file; those versions hashed the other record too.
            hashed = store._read_rows("SELECT * FROM image_hashes")
            store.save_image_hashes([(("flat" if row["folder_mode"] == "author" else "author"), row["tweet_id"],
                                      row["num"], row["media_type"], row["filesize"], row["file_hash"], row["dhash"])
                                     for row in hashed])
            store.save_clusters([[("author", "400", 1, "photo"), ("flat", "400", 1, "photo"),
                                  ("author", "401", 1, "photo"), ("flat", "401", 1, "photo")],
                                 [("author", "402", 1, "photo"), ("flat", "402", 1, "photo")]])
            store.set_duplicate_rule("near")
        repository = self.repository()
        shown = self.shown(repository)
        self.assertEqual(sorted(shown), ["400", "401"])
        outcome = self.offered(repository)
        self.assertEqual(outcome["401"], "writer_400_1.jpg")
        self.assertTrue(outcome["400"].startswith("refused"), outcome)
        # The other record of each shown photo, offered by hand, is refused as well: the view never served it.
        for tweet in ("400", "401", "402"):
            for mode in ("author", "flat"):
                key = f"g1_{mode}_{tweet}_1_photo"
                if tweet in shown and shown[tweet]["key"] == key and tweet == "401":
                    continue
                with self.subTest(key=key), self.assertRaises(ValueError):
                    repository.alike_manifest(key)
        self.assertEqual(self.bin_names(), [])

    def test_a_record_of_the_same_file_named_as_the_photo_kept_is_refused(self):
        """Made by hand: each record of a file offered as the photo the other one keeps."""
        for tweet in ("400", "402"):
            for mode, other in (("author", "flat"), ("flat", "author")):
                keeper = self.record(tweet, other)["path"]
                with self.subTest(tweet=tweet, mode=mode):
                    result = self.delete([self.alike_row(tweet, keeper, mode)])
                    self.assertEqual((result["recycled"], result["protected"]), (0, 1))
        self.assertEqual(self.bin_names(), [])
        self.assertTrue(all(path.is_file() for path in self.files.values()))


class TextTests(AlikeCatalog):
    """Pictures with real-size text: what the pixels can and cannot tell, and who decides."""

    def setUp(self):
        super().setUp()
        self.template = _template()

    def test_another_week_on_the_template_is_never_the_photo_kept(self):
        for week in range(1, 7):
            self.add(str(100 + week), _schedule(self.template, f"WEEK {week} SCHEDULE", _rows(week)),
                     quality=(85, 90, 95)[week % 3])
        weeks = [str(100 + week) for week in range(1, 7)]
        # The 32x32 look alone takes some of them for one picture; the close look does not.
        coarse = [(first, second) for first in weeks for second in weeks if first < second
                  and duplicates.nearly_identical(duplicates.pixel_vector(self.files[first]),
                                                  duplicates.pixel_vector(self.files[second]))]
        self.assertGreaterEqual(len(coarse), 1)
        for first, second in coarse:
            with self.subTest(pair=(first, second)):
                result = self.delete([self.alike_row(second, self.files[first])])
                self.assertEqual((result["recycled"], result["protected"]), (0, 1))
        # The scan groups none of them, so the view offers none.
        repository = self.repository()
        repository.scan_duplicates(1, near=True)
        self.assertEqual(self.shown(repository), {})
        self.assertEqual(self.bin_names(), [])

    def test_a_corrected_time_saved_smaller_goes_only_when_a_person_sends_it_alone(self):
        """The review's case: 22:00 corrected to 23:00 and saved at 680 px. The pixels cannot tell it apart.

        No copy choice sends it. The comparison shows the two side by side, and only that
        one photo, sent on its own, goes.
        """
        corrected = list(NOTICE)
        corrected[3] = "23:00 Collab"
        self.add("300", _notice(self.template, NOTICE), quality=92)
        self.add("301", _notice(self.template, corrected).resize((680, 680), Image.Resampling.LANCZOS), quality=85)
        self.add("302", _photo(41, (800, 600)), quality=90)
        repository = self.repository()
        repository.scan_duplicates(1, near=True)
        shown = self.shown(repository)
        self.assertEqual({tweet: (row["keeper"], row["copy"]) for tweet, row in shown.items()},
                         {"300": (True, False), "301": (False, False)})
        self.assertEqual(repository.duplicate_copy_keys(1), [])
        picked = repository.deletion_manifest([shown["301"]["key"]])
        self.assertEqual(self.delete(picked["rows"])["recycled"], 0)
        manifest = repository.alike_manifest(shown["301"]["key"])
        self.assertEqual((manifest["alike"]["keeper"]["pixels"], manifest["alike"]["alike"]["pixels"]),
                         ("1200×1200", "680×680"))
        # Beside another photo it stays and only that photo goes; alone it goes, and the original stays.
        ordinary = dict(self.record("302"), _delete_signature=file_signature(self.files["302"]))
        result = self.delete([manifest["rows"][0], ordinary])
        self.assertEqual((result["recycled"], result["protected"]), (1, 1))
        self.assertEqual(self.bin_names(), ["writer_302_1.jpg"])
        self.assertEqual(self.delete(manifest["rows"])["recycled"], 1)
        self.assertEqual(self.bin_names(), ["writer_301_1.jpg", "writer_302_1.jpg"])
        self.assertTrue(self.files["300"].is_file())


class _Store:
    """Every path counts as a media file; kept_alike decides on the files alone."""

    def is_media_path(self, path):
        return True


if __name__ == "__main__":
    unittest.main()
