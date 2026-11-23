"""One template with other text is not "the same photo at another size or quality".

The near rule ("화질만 다른 사진 포함") compared 32x32 reductions, where text a
few dozen pixels high is a grey smear. Weekly schedules drawn on one
background therefore became one group of "the same photo", and the check
before recycling repeated that test, so the only copy of a schedule could go
to the Recycle Bin as a "copy". The scan now looks again in detail, but one
changed time in small text, saved smaller, still looks like the same picture
to any pixel comparison; so a photo of such a group goes to the Recycle Bin
only while a file with the same bytes stays. These pictures carry real-size
rendered text.
"""

from contextlib import closing
from itertools import combinations
import os
from pathlib import Path
import random
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from tests import app_data_isolation  # noqa: F401  Never open the user's app data.
from luvbox import deletion, duplicates
from luvbox.duplicates import cluster_images, compute_dhash, compute_file_hash, hamming_distance
from luvbox.history import HistoryStore
from luvbox.qt_library import LibraryRepository, _Request
from luvbox.recycling import file_signature
from luvbox.storage import resolve_storage

ACCOUNT = "offline"
DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
TITLES = ("Minecraft", "Karaoke night", "Chatting", "Collab", "Horror game", "Birthday", "Rest day",
          "New song", "Reaction", "Radio", "Duet", "Free talk", "Goods talk", "Event", "Notice", "Special")


def _template(seed=5, size=(1200, 1200)):
    """Background art shared by every image of the series."""
    randomizer = random.Random(seed)
    image = Image.new("RGB", (9, 7))
    image.putdata([tuple(randomizer.randrange(256) for _ in range(3)) for _ in range(63)])
    image = image.resize(size, Image.Resampling.BICUBIC)
    draw = ImageDraw.Draw(image)
    for _ in range(25):
        x, y, radius = randomizer.randrange(size[0]), randomizer.randrange(size[1]), randomizer.randrange(20, 160)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                     fill=tuple(randomizer.randrange(256) for _ in range(3)))
    image = image.filter(ImageFilter.GaussianBlur(2))
    ImageDraw.Draw(image).rounded_rectangle((560, 120, 1160, 1080), 30, fill="white")
    return image


def _rows(seed):
    randomizer = random.Random(seed)
    return [f"{randomizer.randint(18, 23)}:00 {randomizer.choice(TITLES)}" for _ in DAYS]


def _schedule(template, header, rows):
    """One week's schedule: a header and seven rows of 32 px text on the template."""
    font = ImageFont.load_default(size=32)
    image = template.copy()
    draw = ImageDraw.Draw(image)
    draw.text((590, 130), header, font=font, fill=(200, 60, 90))
    for index, (day, text) in enumerate(zip(DAYS, rows)):
        top = 230 + index * 120
        draw.text((590, top), day, font=font, fill=(200, 60, 90))
        draw.text((700, top), text, font=font, fill=(40, 40, 40))
    return image


def _photo(seed, size=(1200, 900)):
    """A busy picture without text."""
    randomizer = random.Random(seed)
    image = Image.new("RGB", (9, 7))
    image.putdata([tuple(randomizer.randrange(256) for _ in range(3)) for _ in range(63)])
    image = image.resize(size, Image.Resampling.BICUBIC)
    draw = ImageDraw.Draw(image)
    for _ in range(25):
        x, y, radius = randomizer.randrange(size[0]), randomizer.randrange(size[1]), randomizer.randrange(20, 120)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                     fill=tuple(randomizer.randrange(256) for _ in range(3)))
    return image.filter(ImageFilter.GaussianBlur(2))


def _notice(template, rows, size=20):
    """A schedule in smaller text, as in the review: one changed time is hard to see once saved smaller."""
    font = ImageFont.load_default(size=size)
    image = template.copy()
    draw = ImageDraw.Draw(image)
    draw.text((600, 130), "THIS WEEK", font=font, fill=(200, 60, 90))
    for index, text in enumerate(rows):
        draw.text((600, 230 + index * 120), text, font=font, fill=(40, 40, 40))
    return image


NOTICE = ["19:00 Minecraft", "20:00 Karaoke night", "21:00 Chatting", "22:00 Collab",
          "19:00 Horror game", "20:00 Birthday", "21:00 Radio"]


def _same_picture(first, second):
    """Both looks of the near rule for one pair, as the scan takes them."""
    return (duplicates.nearly_identical(duplicates.pixel_vector(first), duplicates.pixel_vector(second))
            and duplicates.same_in_detail(first, second))


def _item(path, key=None):
    return {"key": key or path.stem, "path": path, "file_hash": compute_file_hash(path),
            "dhash": compute_dhash(path), "filesize": path.stat().st_size}


def _keys(groups):
    return sorted(sorted(entry["key"] for entry in group) for group in groups)


class GroupingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="luvbox-near-text-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.template = _template()

    def save(self, name, image, **options):
        path = self.root / name
        image.save(path, **options)
        return path

    def weeks(self, count=6):
        return [self.save(f"week{week}.jpg", _schedule(self.template, f"WEEK {week} SCHEDULE", _rows(week)),
                          quality=(85, 90, 95)[week % 3]) for week in range(1, count + 1)]

    def test_weekly_schedules_on_one_template_are_not_one_photo(self):
        paths = self.weeks()
        # The 32x32 test alone accepts these pairs: without a closer look they would group.
        coarse = [(first, second) for first, second in combinations(paths, 2)
                  if hamming_distance(compute_dhash(first), compute_dhash(second)) <= duplicates.NEAR_CANDIDATE_BITS
                  and duplicates.nearly_identical(duplicates.pixel_vector(first), duplicates.pixel_vector(second))]
        self.assertGreaterEqual(len(coarse), 5)
        self.assertEqual(cluster_images([_item(path) for path in paths], near=True), [])

    def test_one_changed_time_is_another_picture_even_at_another_size(self):
        rows = _rows(11)
        changed = list(rows)
        hour = int(rows[3][:2])
        changed[3] = f"{18 if hour == 23 else hour + 1}{rows[3][2:]}"
        first = self.save("first.jpg", _schedule(self.template, "WEEK 1 SCHEDULE", rows), quality=95)
        other = _schedule(self.template, "WEEK 1 SCHEDULE", changed)
        same_size = self.save("same-size.jpg", other, quality=88)
        smaller = self.save("smaller.jpg", other.resize((900, 900), Image.Resampling.LANCZOS), quality=85)
        for second in (same_size, smaller):
            with self.subTest(second=second.name):
                self.assertEqual(cluster_images([_item(first), _item(second)], near=True), [])

    def test_the_same_picture_resized_or_saved_again_still_groups(self):
        for name, image in (("schedule", _schedule(self.template, "WEEK 1 SCHEDULE", _rows(1))), ("photo", _photo(3))):
            with self.subTest(picture=name):
                width, height = image.size
                paths = [self.save(f"{name}-q95.jpg", image, quality=95),
                         self.save(f"{name}-q70.jpg", image, quality=70),
                         self.save(f"{name}-q80.jpg", image, quality=80),
                         self.save(f"{name}.png", image),
                         self.save(f"{name}-900-q85.jpg", image.resize((width * 3 // 4, height * 3 // 4),
                                                                      Image.Resampling.LANCZOS), quality=85),
                         self.save(f"{name}-900-bicubic-q90.jpg", image.resize((width * 3 // 4, height * 3 // 4),
                                                                              Image.Resampling.BICUBIC), quality=90),
                         self.save(f"{name}-900-box-q75.jpg", image.resize((width * 3 // 4, height * 3 // 4),
                                                                          Image.Resampling.BOX), quality=75)]
                items = [_item(path) for path in paths]
                self.assertEqual(_keys(cluster_images(items, near=True)), [sorted(path.stem for path in paths)])
                # Each copy on its own matches the original, not only through the others.
                for path in paths[1:]:
                    self.assertTrue(_same_picture(paths[0], path), path.name)

    def test_a_picture_stored_on_its_side_is_compared_upright(self):
        upright = _schedule(self.template, "WEEK 1 SCHEDULE", _rows(1)).crop((0, 0, 1200, 900))
        exif = Image.Exif()
        exif[0x0112] = 6                    # "turn 90 degrees clockwise to show"
        turned = self.save("turned.jpg", upright.transpose(Image.Transpose.ROTATE_90), quality=92,
                           exif=exif.tobytes())
        smaller = self.save("smaller.jpg", upright.resize((900, 675), Image.Resampling.LANCZOS), quality=85)
        self.assertTrue(_same_picture(turned, smaller))
        other = _schedule(self.template, "WEEK 2 SCHEDULE", _rows(2)).crop((0, 0, 1200, 900))
        other_smaller = self.save("other.jpg", other.resize((900, 675), Image.Resampling.LANCZOS), quality=85)
        self.assertFalse(_same_picture(turned, other_smaller))

    def test_a_pair_that_cannot_be_read_closely_is_not_grouped(self):
        image = _photo(4)
        first = self.save("big.jpg", image, quality=95)
        second = self.save("big.png", image)
        self.assertTrue(_same_picture(first, second))
        # Far larger than the close comparison handles: no group.
        with patch.object(duplicates, "_DETAIL_MAX_PIXELS", 100 * 100):
            self.assertFalse(duplicates.same_in_detail(first, second))
            self.assertEqual(cluster_images([_item(first), _item(second)], near=True), [])
        broken = self.root / "broken.png"
        broken.write_bytes(second.read_bytes()[:2000])
        self.assertFalse(duplicates.same_in_detail(first, broken))
        self.assertFalse(duplicates.same_in_detail(first, self.root / "missing.png"))

    def test_a_template_series_is_read_a_few_times_not_once_per_pair(self):
        """Every week is a candidate for every other; each picture should still be decoded only a few times."""
        paths = self.weeks(24)
        items = [_item(path) for path in paths]
        candidate_count, _ = duplicates._close_hash_pairs(
            [(int(item["dhash"], 16), index) for index, item in enumerate(items)], duplicates.NEAR_CANDIDATE_BITS)
        self.assertGreater(candidate_count, 6 * len(paths))
        # Room for twelve of these pictures, half of the series.
        with patch.object(duplicates, "_DETAIL_CACHE_PIXELS", 12 * 1200 * 1200), \
                patch.object(duplicates, "_DETAIL_BLOCK", 4), \
                patch.object(duplicates, "_grey", wraps=duplicates._grey) as decoded:
            self.assertEqual(cluster_images(items, near=True), [])
        self.assertLessEqual(decoded.call_count, 4 * len(paths))     # measured 73; 179 in plain pair order


class LibraryTests(unittest.TestCase):
    """The scan and the Recycle Bin check, on a catalog of their own and a fake Recycle Bin."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="luvbox-near-text-library-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "appdata")})
        environment.start()
        self.addCleanup(environment.stop)
        self.paths = resolve_storage(self.root / "Likes", ACCOUNT)
        self.files = {}
        template = _template()
        photo = _photo(7)
        pictures = {str(100 + week): (_schedule(template, f"WEEK {week} SCHEDULE", _rows(week)),
                                      {"quality": (85, 90, 95)[week % 3]}) for week in range(1, 7)}
        pictures["200"] = (photo, {"quality": 95})
        pictures["201"] = (photo.resize((900, 675), Image.Resampling.LANCZOS), {"quality": 85})
        with closing(HistoryStore.for_storage(self.paths)) as store:
            for tweet, (image, options) in pictures.items():
                path = self.paths.output_dir / f"writer_{tweet}_1.jpg"
                image.save(path, "JPEG", **options)
                self.files[tweet] = path
                store.record_media("flat", {"tweet_id": tweet, "num": 1, "type": "photo",
                                            "author": {"name": "writer"}, "date": "2026-11-01T12:00:00+09:00"}, path)
        self.bin = self.root / "fake-recycle-bin"
        self.bin.mkdir()

    def recycle(self, path, expected=None):
        path = Path(path)
        self.assertEqual(file_signature(path), tuple(expected))
        destination = self.bin / path.name
        path.rename(destination)
        return str(destination)

    def row(self, tweet):
        with closing(HistoryStore.for_storage(self.paths, create_media=False)) as store:
            return store.media_record("flat", {"tweet_id": tweet, "num": 1, "type": "photo"})

    def delete(self, rows):
        with patch.object(deletion, "recycle_file", side_effect=self.recycle) as recycle:
            result = deletion.run_deletion(self.paths, rows, True, threading.Event(), lambda _: None)
        self.recycled = recycle.call_count
        return result

    def repository(self):
        repository = LibraryRepository()
        self.addCleanup(repository.configure, None)
        repository.provider = self
        repository.configure(_Request(generation=1, account=ACCOUNT, output_dir=str(self.paths.output_dir),
                                      separate_accounts=False, duplicate_only=True))
        return repository

    def register(self, key, path):
        pass

    def test_a_near_scan_groups_only_the_resized_photo(self):
        repository = self.repository()
        self.assertEqual(repository.scan_duplicates(1, near=True), 2)
        with closing(HistoryStore.for_storage(self.paths, create_media=False)) as store:
            self.assertEqual(sorted(row["tweet_id"] for row in store.duplicate_page(0, 100)["rows"]), ["200", "201"])

    def test_another_week_is_no_proof_for_the_recycle_bin(self):
        others = [self.files[str(100 + week)] for week in (1, 3, 4, 5, 6)]
        # At least one other week passes the 32x32 test, which alone used to be the proof.
        own = duplicates.pixel_vector(self.files["102"])
        self.assertTrue(any(duplicates.nearly_identical(own, duplicates.pixel_vector(path)) for path in others))
        row = dict(self.row("102"), _copy_keepers=[str(path) for path in others], _copy_rule="near")
        self.assertEqual(deletion.kept_copy(_Store(), row["path"], file_signature(row["path"]),
                                            row["_copy_keepers"]), "")
        result = self.delete([row])
        self.assertEqual((result["recycled"], result["protected"], result["failed"]), (0, 1, 0))
        self.assertEqual(self.recycled, 0)
        self.assertTrue(self.files["102"].is_file())
        self.assertEqual(list(self.bin.iterdir()), [])

    def test_every_week_offered_as_a_copy_stays(self):
        """A group of schedules sent whole as copies: every week but the first."""
        weeks = [str(100 + week) for week in range(1, 7)]
        kept = duplicates.pixel_vector(self.files[weeks[0]])
        self.assertTrue(any(duplicates.nearly_identical(kept, duplicates.pixel_vector(self.files[tweet]))
                            for tweet in weeks[1:]))
        rows = [dict(self.row(tweet), _copy_keepers=[str(self.files[weeks[0]])], _copy_rule="near")
                for tweet in weeks[1:]]
        result = self.delete(rows)
        self.assertEqual((result["recycled"], result["protected"]), (0, len(rows)))
        self.assertTrue(all(self.files[tweet].is_file() for tweet in weeks))

    def add(self, tweet, path, image=None, **options):
        """One more photo in the catalog: ``image`` saved to ``path``, or a file already there."""
        if image is not None:
            image.save(path, **options)
        self.files[tweet] = path
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store.record_media("flat", {"tweet_id": tweet, "num": 1, "type": "photo",
                                        "author": {"name": "writer"}, "date": "2026-11-02T12:00:00+09:00"}, path)
        return path

    def copies(self, tweets):
        """These photos chosen in the duplicate view, marked as its deletion manifest marks them."""
        with closing(HistoryStore.for_storage(self.paths, create_media=False)) as store:
            rows = [store.media_record("flat", {"tweet_id": tweet, "num": 1, "type": "photo"}) for tweet in tweets]
            marked, _kept = LibraryRepository._copies_only(store, [dict(row) for row in rows])
        return marked

    def test_a_corrected_time_saved_smaller_stays_even_when_shown_as_one_photo(self):
        """The review's case: 22:00 corrected to 23:00, the corrected schedule saved at 680 px."""
        template = _template()
        corrected = list(NOTICE)
        corrected[3] = "23:00 Collab"
        output = self.paths.output_dir
        self.add("300", output / "writer_300_1.jpg", _notice(template, NOTICE), quality=92)
        self.add("301", output / "writer_301_1.jpg",
                 _notice(template, corrected).resize((680, 680), Image.Resampling.LANCZOS), quality=85)
        self.repository().scan_duplicates(1, near=True)
        rows = self.copies(["301"])
        self.assertEqual([row["tweet_id"] for row in rows], ["301"])
        result = self.delete(rows)
        self.assertEqual((result["recycled"], result["protected"], result["failed"]), (0, 1, 0))
        self.assertTrue(self.files["301"].is_file())
        self.assertEqual(list(self.bin.iterdir()), [])
        # Named as the photo that stays, the original is no proof either.
        self.assertEqual(deletion.kept_copy(_Store(), str(self.files["301"]), file_signature(self.files["301"]),
                                            [str(self.files["300"])]), "")

    def test_a_resized_copy_stays_and_the_result_says_why(self):
        self.assertEqual(self.repository().scan_duplicates(1, near=True), 2)
        rows = self.copies(["201"])
        self.assertEqual([(row["tweet_id"], row["_copy_rule"]) for row in rows], [("201", "near")])
        result = self.delete(rows)
        self.assertEqual((result["recycled"], result["protected"]), (0, 1))
        self.assertTrue(self.files["201"].is_file())
        self.assertIn("그대로 두었습니다", result["message"])
        self.assertIn("비슷한 사진도 검사", result["message"])

    def test_a_file_with_the_same_bytes_in_a_near_group_still_goes(self):
        twin = self.paths.output_dir / "writer_202_1.jpg"
        twin.write_bytes(self.files["200"].read_bytes())
        self.add("202", twin)
        self.assertEqual(self.repository().scan_duplicates(1, near=True), 3)
        rows = self.copies(["201", "202"])
        self.assertEqual(sorted(row["tweet_id"] for row in rows), ["201", "202"])
        result = self.delete(rows)
        self.assertEqual((result["recycled"], result["protected"]), (1, 1))
        self.assertFalse(twin.exists())
        self.assertTrue(self.files["200"].is_file())
        self.assertTrue(self.files["201"].is_file())

    def twin(self, tweet, source="200"):
        """Another post's file with exactly the bytes of ``source``."""
        path = self.paths.output_dir / f"writer_{tweet}_1.jpg"
        path.write_bytes(self.files[source].read_bytes())
        return self.add(tweet, path)

    def offered(self, repository):
        """What the duplicate view offers: (tweet, kept, copy) per shown photo, the bulk choice, and the store's."""
        _total, rows = repository.read("1", 0, 100)
        with closing(HistoryStore.for_storage(self.paths, create_media=False)) as store:
            page = store.duplicate_page(0, 100)["rows"]
            stored = [identity[1] for identity in store.duplicate_copy_identities()]
        self.assertEqual([(row["tweet_id"], row["cluster_rank"] == 1, bool(row["cluster_copy"])) for row in page],
                         [(row["key"].split("_")[2], row["keeper"], row["copy"]) for row in rows])
        bulk = [key.split("_")[2] for key in repository.duplicate_copy_keys(1)]
        return [(row["key"].split("_")[2], row["keeper"], row["copy"]) for row in rows], bulk, stored

    def test_only_a_file_with_the_same_bytes_is_offered_as_a_copy(self):
        """A near group of the photo, a file with its very bytes, and the photo saved smaller."""
        self.twin("202")
        repository = self.repository()
        self.assertEqual(repository.scan_duplicates(1, near=True), 3)
        shown, bulk, stored = self.offered(repository)
        # The same size: the newer post stays and the older file with its bytes is its copy. No
        # photo of the group has the bytes of the smaller file, so it is not offered as one.
        self.assertEqual(shown, [("202", True, False), ("200", False, True), ("201", False, False)])
        self.assertEqual(bulk, ["200"])
        self.assertEqual(stored, ["200"])
        # Chosen by hand all the same, the smaller file is marked as one that stays unless a file
        # with its bytes does, and the confirmation says so before anything moves.
        rows = self.copies(["200", "201"])
        self.assertEqual({row["tweet_id"]: bool(row.get("_copy_near")) for row in rows}, {"200": False, "201": True})
        summary = _summary(rows)
        self.assertIn("‘비슷함’ 1장은 ‘비교’에서", summary)
        result = self.delete(rows)
        self.assertEqual((result["recycled"], result["protected"]), (1, 1))
        self.assertFalse(self.files["200"].exists())
        self.assertTrue(self.files["201"].is_file() and self.files["202"].is_file())

    def test_a_copy_of_the_smaller_file_is_offered_and_the_smaller_file_is_not(self):
        """Two files with the smaller photo's bytes: the second is its copy; the first stays as a picture of its own."""
        self.twin("203", source="201")
        repository = self.repository()
        self.assertEqual(repository.scan_duplicates(1, near=True), 3)
        shown, bulk, _stored = self.offered(repository)
        self.assertEqual(shown, [("200", True, False), ("203", False, False), ("201", False, True)])
        self.assertEqual(bulk, ["201"])
        rows = self.copies(["201"])
        self.assertFalse(rows[0].get("_copy_near"))
        self.assertNotIn("‘비슷함’", _summary(rows))
        self.assertEqual(self.delete(rows)["recycled"], 1)
        self.assertTrue(self.files["203"].is_file() and self.files["200"].is_file())

    def test_an_exact_group_offers_every_photo_but_the_one_kept(self):
        self.twin("202")
        self.twin("203")
        repository = self.repository()
        self.assertEqual(repository.scan_duplicates(1), 3)
        shown, bulk, stored = self.offered(repository)
        self.assertEqual(shown, [("203", True, False), ("202", False, True), ("200", False, True)])
        self.assertEqual(bulk, ["202", "200"])
        self.assertEqual(stored, ["202", "200"])
        rows = self.copies(["202", "200"])
        self.assertFalse(any(row.get("_copy_near") for row in rows))
        summary = _summary(rows)
        self.assertIn("같은 파일이 남는 경우에만", summary)
        self.assertNotIn("‘비슷함’", summary)

    def test_without_stored_groups_only_files_with_the_same_bytes_are_offered(self):
        """A catalog that cannot say which photos the last scan grouped: the groups are made again from hashes."""
        self.twin("202")
        repository = self.repository()
        self.assertEqual(repository.scan_duplicates(1, near=True), 3)
        with patch.object(HistoryStore, "duplicate_copy_identities", return_value=None):
            bulk = [key.split("_")[2] for key in repository.duplicate_copy_keys(1)]
        # Close hashes alone put the smaller photo, and weeks of the schedule, into these groups.
        self.assertEqual(bulk, ["200"])


def _summary(rows):
    """The confirmation the duplicate view shows for these rows."""
    from luvbox.qt_management import ManagementController

    shown = type("Shown", (), {"_delete": {"rows": rows, "kept": 0}, "deleteCount": len(rows)})()
    return ManagementController.deleteSummary.fget(shown)


class _Store:
    """Every keeper counts as a media file; kept_copy decides on content alone."""

    def is_media_path(self, path):
        return True


if __name__ == "__main__":
    unittest.main()
