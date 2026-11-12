"""Duplicates are identical files; "nearly identical" needs the pixels to agree as well."""
from contextlib import closing
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFilter

from tests import app_data_isolation  # noqa: F401  Never open the user's app data.
from luvbox.duplicates import (ScanCancelled, cluster_images, compute_dhash, compute_file_hash, hamming_distance,
                               nearly_identical, pixel_vector)
from luvbox.history import HistoryStore
from luvbox.qt_library import LibraryRepository, _Request
from luvbox.storage import resolve_storage


def _photo(seed, size=(900, 700)):
    """A busy synthetic picture: blurred colour blocks with discs on top."""
    import random

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


def _caption(text):
    """The same template with a different line of text: alike on average, different locally."""
    image = Image.new("RGB", (900, 700), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 80, 820, 380), fill=(228, 228, 228))
    for index, letter in enumerate(text):
        draw.rectangle((90 + index * 34, 470, 116 + index * 34, 540), fill="black" if ord(letter) % 2 else "gray")
    return image


class NearIdenticalTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="luvbox-duplicate-rules-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()

    def save(self, name, image, **options):
        path = self.root / name
        image.save(path, **options)
        return path

    def item(self, path, key=None, favorite=False):
        return {"key": key or path.stem, "path": path, "file_hash": compute_file_hash(path),
                "dhash": compute_dhash(path), "filesize": path.stat().st_size, "favorite": favorite}

    def test_the_same_picture_at_another_quality_or_size_matches(self):
        original = _photo(1)
        first = pixel_vector(self.save("a.jpg", original, quality=95))
        for name, image, options in (
                ("b.jpg", original, {"quality": 40}),
                ("c.jpg", original.resize((450, 350), Image.Resampling.LANCZOS), {"quality": 80}),
                ("d.png", original, {}),
                ("e.webp", original.resize((300, 233), Image.Resampling.LANCZOS), {"quality": 75})):
            with self.subTest(name=name):
                self.assertTrue(nearly_identical(first, pixel_vector(self.save(name, image, **options))))

    def test_pictures_that_only_look_alike_do_not_match(self):
        original = _photo(1)
        first = pixel_vector(self.save("a.jpg", original, quality=95))
        marked = original.copy()
        ImageDraw.Draw(marked).rectangle((720, 620, 880, 680), fill="white")
        shifted = original.transform(original.size, Image.Transform.AFFINE, (1, 0, 12, 0, 1, 0))
        for name, image in (("other.jpg", _photo(2)), ("watermark.jpg", marked), ("shift.jpg", shifted),
                            ("crop.jpg", original.crop((30, 20, 870, 680))),
                            ("wide.jpg", original.crop((0, 60, 900, 640))),
                            ("flip.jpg", original.transpose(Image.Transpose.FLIP_LEFT_RIGHT))):
            with self.subTest(name=name):
                self.assertFalse(nearly_identical(first, pixel_vector(self.save(name, image, quality=92))))
        # The case that made a plain average useless: one template, two captions.
        one = pixel_vector(self.save("caption-1.png", _caption("hello world 12345")))
        two = pixel_vector(self.save("caption-2.png", _caption("hello there 67890")))
        self.assertFalse(nearly_identical(one, two))
        self.assertFalse(nearly_identical(first, None))
        self.assertIsNone(pixel_vector(self.root / "missing.jpg"))

    def test_groups_are_identical_files_unless_near_is_asked_for(self):
        original = _photo(3)
        first = self.save("first.jpg", original, quality=95)
        twin = self.root / "twin.jpg"
        twin.write_bytes(first.read_bytes())
        smaller = self.save("smaller.jpg", original.resize((450, 350), Image.Resampling.LANCZOS), quality=80)
        different = self.save("different.jpg", _photo(4), quality=90)
        items = [self.item(path) for path in (first, twin, smaller, different)]
        self.assertLessEqual(hamming_distance(items[0]["dhash"], items[2]["dhash"]), 4)

        exact = cluster_images(items)
        self.assertEqual([sorted(entry["key"] for entry in group) for group in exact], [["first", "twin"]])

        seen = []
        near = cluster_images(items, near=True, progress=lambda done, total: seen.append((done, total)))
        self.assertEqual([sorted(entry["key"] for entry in group) for group in near],
                         [["first", "smaller", "twin"]])
        self.assertEqual(seen[-1][0], seen[-1][1])
        # A favorite is kept first, then the larger file.
        items[2]["favorite"] = True
        self.assertEqual(cluster_images(items, near=True)[0][0]["key"], "smaller")

    def test_a_close_hash_without_matching_pixels_never_groups(self):
        first = self.save("one.png", _caption("hello world 12345"))
        second = self.save("two.png", _caption("hello there 67890"))
        items = [self.item(first), self.item(second)]
        # Force the hashes together: only the pixel check may decide.
        items[1]["dhash"] = items[0]["dhash"]
        self.assertEqual(cluster_images(items, near=True), [])
        # A vector that cannot be made is not a match either.
        self.assertEqual(cluster_images(items, near=True, vector_for=lambda path: None), [])

    def test_the_comparison_stage_can_be_cancelled(self):
        original = _photo(5)
        items = [self.item(self.save(f"copy{index}.jpg", original, quality=95 - index * 5)) for index in range(3)]
        calls = []

        def cancel():
            calls.append(1)
            if len(calls) > 2:
                raise ScanCancelled("stop")

        with self.assertRaises(ScanCancelled):
            cluster_images(items, near=True, check_cancel=cancel)


class StoredGroupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="luvbox-duplicate-groups-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        for name in ("luvbox.storage.records_root", "luvbox.qt_library.records_root"):
            replacement = patch(name, return_value=self.root / "records")
            replacement.start()
            self.addCleanup(replacement.stop)
        self.paths = resolve_storage(self.root / "Likes", "offline")
        self.files = {}
        with closing(HistoryStore.for_storage(self.paths)) as store:
            for tweet, content in (("301", b"same-bytes"), ("302", b"same-bytes"), ("303", b"same-bytes"),
                                   ("304", b"alike-1"), ("305", b"alike-2"), ("306", b"alone")):
                path = self.paths.output_dir / f"user_{tweet}_1.jpg"
                path.write_bytes(content)
                self.files[tweet] = path
                store.record_media("flat", {"tweet_id": tweet, "num": 1, "type": "photo", "author": "user"}, path)
            store.save_image_hashes([
                ("flat", tweet, 1, "photo", self.files[tweet].stat().st_size, digest, perceptual)
                for tweet, digest, perceptual in (
                    ("301", "aa", "0000000000000001"), ("302", "aa", "0000000000000001"),
                    ("303", "aa", "0000000000000001"), ("304", "bb", "00000000000000f0"),
                    ("305", "cc", "00000000000000f1"), ("306", "dd", "ffffffffffffffff"))])
            # What 1.6.x stored: the identical files, plus two photos that only looked alike.
            store.save_clusters([[("flat", "301", 1, "photo"), ("flat", "302", 1, "photo"), ("flat", "303", 1, "photo")],
                                 [("flat", "304", 1, "photo"), ("flat", "305", 1, "photo")]])
            self.assertEqual(store.duplicate_counts(), 5)
            self.assertEqual(store.duplicate_rule(), "")

    def repository(self):
        repository = LibraryRepository()
        self.addCleanup(repository.configure, None)
        repository.provider = self
        repository.configure(_Request(generation=1, account="offline", output_dir=str(self.paths.output_dir),
                                      separate_accounts=False, duplicate_only=True))
        return repository

    def register(self, key, path):
        pass

    def test_groups_from_the_earlier_rule_are_rebuilt_from_stored_hashes(self):
        repository = self.repository()
        self.assertEqual(repository.duplicate_counts(1), 3)
        with closing(HistoryStore.for_storage(self.paths)) as store:
            self.assertEqual(store.duplicate_rule(), "exact")
            self.assertEqual(store.duplicate_counts(), 3)
            self.assertEqual({item[1] for item in store.duplicate_copy_identities()}, {"301", "302"})
        # The second read finds the marker and changes nothing.
        self.assertEqual(repository.duplicate_counts(1), 3)

    def test_the_duplicate_page_keeps_groups_together_with_the_photo_to_keep_first(self):
        with closing(HistoryStore.for_storage(self.paths)) as store:
            store.regroup_exact_duplicates()
            store.toggle_favorite("flat", "302", 1, "photo")
            page = store.duplicate_page(0, 10)
            self.assertEqual([(row["tweet_id"], row["cluster_rank"], row["cluster_size"]) for row in page["rows"]],
                             [("302", 1, 3), ("303", 2, 3), ("301", 3, 3)])
            self.assertEqual((page["total"], page["pages"]), (3, 1))
            # A filter that hides the kept photo must not promote a copy.
            hidden = store.duplicate_page(0, 10, query="303")
            self.assertEqual([(row["tweet_id"], row["cluster_rank"]) for row in hidden["rows"]], [("303", 2)])
            self.assertEqual(store.duplicate_page(0, 2)["pages"], 2)
        repository = self.repository()
        total, rows = repository.read("1", 0, 10)
        self.assertEqual(total, 3)
        self.assertEqual([(row["keeper"], row["group_size"]) for row in rows], [(True, 3), (False, 3), (False, 3)])
        self.assertTrue(all(row["group"] == rows[0]["group"] and row["detail"] for row in rows))
        self.assertTrue(rows[0]["favorite"])

    def test_a_scan_records_which_rule_made_the_groups(self):
        repository = self.repository()
        # The fixture files are not pictures, so only their bytes can match.
        self.assertEqual(repository.scan_duplicates(1), 3)
        with closing(HistoryStore.for_storage(self.paths)) as store:
            self.assertEqual(store.duplicate_rule(), "exact")
        self.assertEqual(repository.scan_duplicates(1, near=True), 3)
        with closing(HistoryStore.for_storage(self.paths)) as store:
            self.assertEqual(store.duplicate_rule(), "near")


if __name__ == "__main__":
    unittest.main()
