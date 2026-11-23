"""A folder of this PC opened in the "폴더 정리" tab: its list lives in the app data folder.

The scan is incremental, never enters links, junctions or the app's own data,
never opens a cloud-only file, keeps what it knew of unchanged files, and keeps
the star and tags of a file that went away so they come back with it.
"""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox import local_store
from luvbox.local_store import LocalFolderError, LocalFolderStore, check_root

ROOT = Path(__file__).resolve().parents[1]


def picture(path, color=(200, 30, 50), size=(40, 30)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


class FolderCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="luvbox-local-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.base / "appdata")})
        environment.start()
        self.addCleanup(environment.stop)
        self.folder = self.base / "사진 정리"
        self.folder.mkdir()

    def store(self):
        store = LocalFolderStore(self.folder)
        self.addCleanup(store.close)
        return store

    def listed(self, store, **filters):
        _total, rows = store.records(0, 1000, order="name", **filters)
        return [row["rel_path"] for row in rows]


class RootTests(FolderCase):
    def test_the_database_is_in_the_app_data_folder_and_nothing_is_written_into_the_folder(self):
        picture(self.folder / "a.jpg")
        store = self.store()
        store.sync_folder(True)
        self.assertEqual(store.path.parent, self.base / "appdata" / "LuvBox" / "local-folders")
        self.assertTrue(store.path.name.startswith("사진_정리-"), store.path.name)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["a.jpg"])
        self.assertEqual(store.meta("root_path"), str(self.folder))

    def test_what_cannot_be_opened(self):
        for bad in ("", "relative/path", str(self.base / "nowhere")):
            with self.assertRaises(LocalFolderError):
                check_root(bad)
        own = self.base / "appdata" / "LuvBox" / "records"
        own.mkdir(parents=True)
        with self.assertRaises(LocalFolderError):
            check_root(own)
        with self.assertRaises(LocalFolderError):
            check_root(self.base / "appdata" / "LuvBox")
        file = picture(self.base / "file.jpg")
        with self.assertRaises(LocalFolderError):
            check_root(file)

    def test_failed_commit_discards_changes_and_allows_the_next_edit(self):
        store = self.store()

        def reject_commit(action, argument, *_):
            if action == sqlite3.SQLITE_TRANSACTION and argument == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        store._db.set_authorizer(reject_commit)
        try:
            with self.assertRaises(LocalFolderError):
                store.set_meta("edit", "uncommitted")
        finally:
            store._db.set_authorizer(None)

        self.assertEqual(store.meta("edit"), "")
        store.set_meta("edit", "retried")
        self.assertEqual(store.meta("edit"), "retried")

    @unittest.skipUnless(os.name == "nt", "Windows junctions")
    def test_opening_the_list_again_waits_for_no_one_writing_it(self):
        """The window opens a folder's list for each small edit while a worker may be writing to it."""
        import threading
        import time

        self.store().close()                                    # made once
        writer = sqlite3.connect(local_store.store_path(self.folder), timeout=0)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")                       # another connection is writing
        held, release = threading.Event(), threading.Event()

        def hold_lock():                                        # and this process's writer lock is taken
            with local_store._WRITE_LOCK:
                held.set()
                release.wait(5)
        holder = threading.Thread(target=hold_lock)
        holder.start()
        self.addCleanup(holder.join)
        self.addCleanup(release.set)
        held.wait(5)
        started = time.monotonic()
        with LocalFolderStore(self.folder) as store:
            self.assertEqual(store.records(0, 10), (0, []))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_new_folder_list_opened_by_several_workers_at_once_opens_for_each(self):
        """The list, the folder reader and the window open a new folder's list together; none may fail.

        One of them could find it locked while another was making it, and the
        list of a folder opened for the first time came back empty.
        """
        import threading

        failures = []
        for index in range(100):
            folder = self.base / f"새 폴더 {index}"
            folder.mkdir()
            start = threading.Barrier(8)

            def open_it():
                start.wait()
                try:
                    with LocalFolderStore(folder) as store:
                        store.records(0, 10)
                except LocalFolderError as exc:
                    failures.append(repr(exc.__cause__))
            workers = [threading.Thread(target=open_it) for _ in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        self.assertEqual(failures, [])

    def test_a_folder_reached_through_a_junction_is_refused(self):
        import _winapi

        _winapi.CreateJunction(str(self.folder), str(self.base / "link"))
        with self.assertRaises(LocalFolderError):
            check_root(self.base / "link")
        with self.assertRaises(LocalFolderError):
            check_root(self.base / "link" / ".")


class ScanTests(FolderCase):
    def _after_next_walk(self, store, operation):
        begin_write = store._write
        pending = True

        def interleave():
            nonlocal pending
            if pending:
                pending = False
                operation()
            return begin_write()

        return patch.object(store, "_write", side_effect=interleave)

    def test_overlapping_refreshes_only_insert_a_new_file_once(self):
        picture(self.folder / "a.jpg")
        first, second = self.store(), self.store()
        def interleave_refresh():
            self.assertEqual(first.sync_folder()["added"], 1)

        with self._after_next_walk(second, interleave_refresh):
            result = second.sync_folder()
        self.assertEqual(result, {"found": 1, "added": 0, "changed": 0, "gone": 0, "restored": 0})
        self.assertEqual(self.listed(second), ["a.jpg"])

    def test_an_older_walk_does_not_hide_a_new_file_found_by_another_refresh(self):
        picture(self.folder / "initial.jpg")
        earlier, later = self.store(), self.store()
        earlier.sync_folder()

        def newer_refresh():
            picture(self.folder / "created.jpg")
            self.assertEqual(later.sync_folder()["added"], 1)
            later.save_clusters([["initial.jpg", "created.jpg"]], "exact")

        with self._after_next_walk(earlier, newer_refresh):
            result = earlier.sync_folder()
        self.assertEqual((result["found"], result["gone"]), (2, 0))
        self.assertEqual(self.listed(earlier), ["created.jpg", "initial.jpg"])
        self.assertEqual(earlier.duplicate_count(), 2)

    def test_an_older_walk_does_not_restore_stale_file_details_or_rename_a_file_back(self):
        changed = picture(self.folder / "change.jpg")
        original = picture(self.folder / "original.jpg")
        renamed = original.with_name("ORIGINAL.jpg" if os.name == "nt" else "renamed.jpg")
        earlier, later = self.store(), self.store()
        earlier.sync_folder()

        def newer_refresh():
            picture(changed, size=(220, 220))
            original.rename(original.with_suffix(".tmp")).rename(renamed)
            self.assertEqual(later.sync_folder()["changed"], 1)
            later.save_hashes([(changed.name, "new-hash", "new-dhash", "new-signature")])

        with self._after_next_walk(earlier, newer_refresh):
            result = earlier.sync_folder()
        self.assertEqual((result["changed"], result["gone"]), (0, 0))
        self.assertEqual(set(self.listed(earlier)), {changed.name, renamed.name})
        row = earlier.row_by_rel(changed.name)
        self.assertEqual((row["filesize"], row["file_hash"]), (changed.stat().st_size, "new-hash"))

    def test_repeated_catalog_changes_do_not_retry_forever_or_publish_the_walk(self):
        picture(self.folder / "unapplied.jpg")
        store = self.store()
        begin_write = store._write
        attempts = 0

        def another_edit():
            nonlocal attempts
            attempts += 1
            with begin_write() as db:
                db.execute("INSERT OR REPLACE INTO local_meta VALUES ('interleaved', ?)", (attempts,))
            return begin_write()

        with patch.object(store, "_write", side_effect=another_edit):
            with self.assertRaises(LocalFolderError):
                store.sync_folder()
        self.assertEqual(attempts, 3)
        self.assertEqual(self.listed(store), [])

    def test_cancelling_after_the_walk_leaves_the_catalog_unchanged(self):
        from luvbox.duplicates import ScanCancelled

        picture(self.folder / "unapplied.jpg")
        store = self.store()
        cancelled = False

        def cancel():
            nonlocal cancelled
            cancelled = True

        def check_cancel():
            if cancelled:
                raise ScanCancelled("cancelled before writing")

        with self._after_next_walk(store, cancel), self.assertRaises(ScanCancelled):
            store.sync_folder(check_cancel=check_cancel)
        self.assertEqual(self.listed(store), [])

    def test_unreadable_subfolder_does_not_remove_its_photos_or_duplicate_group(self):
        subfolder = self.folder / "sub"
        picture(subfolder / "one.jpg")
        picture(subfolder / "two.jpg")
        store = self.store()
        store.sync_folder(True)
        store.save_clusters([["sub/one.jpg", "sub/two.jpg"]], "exact")
        picture(self.folder / "added.jpg")
        scandir = os.scandir

        def open_directory(folder):
            if Path(folder) == subfolder:
                raise PermissionError("cannot read this subfolder")
            return scandir(folder)

        with patch("luvbox.local_store.os.scandir", side_effect=open_directory):
            with self.assertRaises(LocalFolderError):
                store.sync_folder(True)
        self.assertEqual(self.listed(store), ["sub/one.jpg", "sub/two.jpg"])
        self.assertEqual(store.duplicate_count(), 2)

    def test_photos_and_gifs_are_listed_from_every_subfolder_unless_asked_otherwise(self):
        picture(self.folder / "a.jpg")
        picture(self.folder / "b.PNG")
        picture(self.folder / "sub" / "c.webp")
        picture(self.folder / "sub" / "deep" / "d.gif")
        (self.folder / "notes.txt").write_text("not a photo")
        (self.folder / "sub" / "clip.mp4").write_bytes(b"not listed")
        for skipped in (".git", ".luvbox", "$RECYCLE.BIN", "System Volume Information"):
            picture(self.folder / skipped / "x.jpg")
        store = self.store()
        counts = store.sync_folder(True)
        self.assertEqual(counts["found"], 4)
        self.assertEqual(self.listed(store), ["a.jpg", "b.PNG", "sub/c.webp", "sub/deep/d.gif"])
        kinds = {row["rel_path"]: row["media_type"] for row in store.records(0, 100)[1]}
        self.assertEqual(kinds["sub/deep/d.gif"], "animated_gif")
        self.assertEqual(kinds["a.jpg"], "photo")
        store.sync_folder(False)
        self.assertEqual(self.listed(store), ["a.jpg", "b.PNG"])

    @unittest.skipUnless(os.name == "nt", "Windows junctions")
    def test_links_and_junctions_inside_are_not_entered(self):
        import _winapi

        outside = self.base / "outside"
        picture(outside / "o.jpg")
        _winapi.CreateJunction(str(outside), str(self.folder / "joined"))
        picture(self.folder / "own.jpg")
        os.link(self.folder / "own.jpg", self.folder / "hard.jpg")
        store = self.store()
        store.sync_folder(True)
        # A hard link is listed under both names, as in the gallery; recycling one as a copy of the
        # other is refused (deletion.kept_copy: the same file number is the same file).
        self.assertEqual(self.listed(store), ["hard.jpg", "own.jpg"])

    def test_the_app_data_folder_inside_the_opened_folder_is_skipped(self):
        appdata = self.folder / "data"
        picture(appdata / "LuvBox" / "thumbnails" / "t.jpg")
        picture(self.folder / "real.jpg")
        with patch.dict(os.environ, {"LOCALAPPDATA": str(appdata)}):
            store = LocalFolderStore(self.folder)
            try:
                store.sync_folder(True)
                self.assertEqual(self.listed(store), ["real.jpg"])
            finally:
                store.close()

    def test_a_cloud_only_file_is_listed_but_never_opened(self):
        picture(self.folder / "cloud.jpg")
        picture(self.folder / "here.jpg")
        real_scandir = os.scandir

        class Entry:
            def __init__(self, entry):
                self._entry = entry
                self.name, self.path = entry.name, entry.path

            def __getattr__(self, name):
                return getattr(self._entry, name)

            def stat(self, follow_symlinks=True):
                info = self._entry.stat(follow_symlinks=follow_symlinks)
                if self.name != "cloud.jpg":
                    return info

                class Info:
                    def __getattr__(self, name):
                        return getattr(info, name)
                    st_file_attributes = 0x00400000 | 0x400
                return Info()

        class Listing:
            def __init__(self, folder):
                self._inner = real_scandir(folder)

            def __enter__(self):
                return [Entry(entry) for entry in self._inner]

            def __exit__(self, *args):
                self._inner.close()

        store = self.store()
        with patch("luvbox.local_store.os.scandir", side_effect=Listing):
            store.sync_folder(True)
        rows = {row["rel_path"]: row for row in store.records(0, 10)[1]}
        self.assertEqual(rows["cloud.jpg"]["cloud"], 1)
        self.assertEqual(rows["here.jpg"]["cloud"], 0)
        self.assertEqual([row["rel_path"] for row in store.photos_to_hash()], ["here.jpg"])
        self.assertEqual([row["rel_path"] for row in store.photos_without_embeddings()], ["here.jpg"])

    def test_files_and_folders_a_cloud_service_keeps_are_listed_and_only_links_are_skipped(self):
        """OneDrive keeps its files and folders as reparse points of its own kind, also once downloaded.

        Only a reparse point that names another place (a link, a junction) is
        left out; the cloud kind holds the file itself.
        """
        picture(self.folder / "synced" / "inside.jpg")
        picture(self.folder / "here.jpg")
        picture(self.folder / "linked.jpg")
        tags = {"synced": 0x9000001A, "here.jpg": 0x9000001A, "linked.jpg": 0xA000000C}
        real_scandir = os.scandir

        class Entry:
            def __init__(self, entry):
                self._entry = entry
                self.name, self.path = entry.name, entry.path

            def __getattr__(self, name):
                return getattr(self._entry, name)

            def stat(self, follow_symlinks=True):
                info = self._entry.stat(follow_symlinks=follow_symlinks)
                if self.name not in tags:
                    return info
                tag = tags[self.name]

                class Info:
                    def __getattr__(self, name):
                        return getattr(info, name)
                    st_file_attributes = info.st_file_attributes | 0x400
                    st_reparse_tag = tag
                return Info()

        class Listing:
            def __init__(self, folder):
                self._inner = real_scandir(folder)

            def __enter__(self):
                return [Entry(entry) for entry in self._inner]

            def __exit__(self, *args):
                self._inner.close()

        store = self.store()
        with patch("luvbox.local_store.os.scandir", side_effect=Listing):
            store.sync_folder(True)
        self.assertEqual(self.listed(store), ["here.jpg", "synced/inside.jpg"])
        self.assertEqual(store.records(0, 10, order="name")[1][0]["cloud"], 0)     # downloaded: read as usual

    def test_only_a_reparse_point_that_names_another_place_counts_as_a_link(self):
        from types import SimpleNamespace
        import stat as stat_module

        regular = stat_module.S_IFREG | 0o644
        cloud = SimpleNamespace(st_mode=regular, st_file_attributes=0x400, st_reparse_tag=0x9000001A)
        junction = SimpleNamespace(st_mode=stat_module.S_IFDIR, st_file_attributes=0x410, st_reparse_tag=0xA0000003)
        plain = SimpleNamespace(st_mode=regular, st_file_attributes=0x20, st_reparse_tag=0)
        self.assertEqual([local_store._is_link(item) for item in (cloud, junction, plain)], [False, True, False])

    def test_unchanged_files_keep_what_was_computed_and_changed_ones_lose_it(self):
        picture(self.folder / "a.jpg")
        picture(self.folder / "b.jpg", (10, 10, 10))
        store = self.store()
        store.sync_folder(True)
        store.save_hashes([("a.jpg", "aa", "0" * 16, "sig"), ("b.jpg", "bb", "1" * 16, "sig")])
        store.save_embeddings([("a.jpg", b"\x01" * 3072, 0.5), ("b.jpg", b"\x02" * 3072, 0.5)])
        store.replace_ai_tags([("a.jpg", ["인물"]), ("b.jpg", ["동물"])])
        store.add_tag(["a.jpg", "b.jpg"], "여행")
        picture(self.folder / "b.jpg", (90, 90, 90), size=(60, 60))
        os.utime(self.folder / "b.jpg", ns=(10**18, 10**18))
        counts = store.sync_folder(True)
        self.assertEqual((counts["added"], counts["changed"], counts["gone"]), (0, 1, 0))
        rows = {row["rel_path"]: row for row in store.records(0, 10)[1]}
        self.assertEqual(rows["a.jpg"]["file_hash"], "aa")
        self.assertEqual(rows["b.jpg"]["file_hash"], "")
        self.assertEqual(set(store.embeddings_for(["a.jpg", "b.jpg"])), {"a.jpg"})
        self.assertEqual([t["tag"] for t in store.ai_tags_of("b.jpg")], [])
        self.assertEqual([t["tag"] for t in store.item_tags("b.jpg")], ["여행"])   # typed tags stay

    def test_hashing_a_changed_file_invalidates_only_its_old_analysis(self):
        a = picture(self.folder / "a.jpg")
        b = picture(self.folder / "b.jpg")
        store = self.store()
        store.sync_folder(True)
        store.save_embeddings([("a.jpg", bytes(3072), 0.5), ("b.jpg", bytes(3072), 0.5)])
        store.replace_ai_tags([("a.jpg", ["인물"]), ("b.jpg", ["인물"])])
        store.add_tag(["b.jpg"], "여행")
        store.remember_unreadable([("b.jpg", b.stat().st_size, b.stat().st_mtime_ns)])
        store.save_clusters([["a.jpg", "b.jpg"]], "exact")

        picture(b, size=(120, 120))
        os.utime(b, ns=(10**18, 10**18))
        a_stat, b_stat = a.stat(), b.stat()
        store.save_hashes([("a.jpg", "old", "", "sig_a", a_stat.st_size, a_stat.st_mtime_ns),
                           ("b.jpg", "new", "", "sig_b", b_stat.st_size, b_stat.st_mtime_ns)])

        self.assertEqual(set(store.embeddings_for(["a.jpg", "b.jpg"])), {"a.jpg"})
        self.assertEqual([t["tag"] for t in store.item_tags("b.jpg")], ["여행"])
        self.assertEqual(store.unreadable_signatures(), {})
        self.assertEqual(store.duplicate_count(), 0)
        self.assertEqual(store.sync_folder(True)["changed"], 0)
        self.assertEqual(store.row_by_rel("b.jpg")["file_hash"], "new")

    def test_a_file_renamed_only_in_case_keeps_its_star_tags_answers_and_marks(self):
        """Windows sees IMG_01.jpg and img_01.jpg as one file: renaming only the case is no new file."""
        picture(self.folder / "IMG_01.jpg")
        picture(self.folder / "other.jpg", (1, 2, 3))
        store = self.store()
        store.sync_folder(True)
        row = store.row_by_rel("IMG_01.jpg")
        store.toggle_favorite(row["rowid"])
        store.add_tag(["IMG_01.jpg"], "여행")
        store.save_embeddings([("IMG_01.jpg", bytes(3072), 0.5)])
        store.set_tag_feedback(["IMG_01.jpg"], "인물", False)
        store.save_clusters([["IMG_01.jpg", "other.jpg"]], "near")
        store.set_group_distinct("IMG_01.jpg")
        (self.folder / "IMG_01.jpg").rename(self.folder / "temp.jpg")
        (self.folder / "temp.jpg").rename(self.folder / "img_01.jpg")
        counts = store.sync_folder(True)
        self.assertEqual((counts["added"], counts["gone"]), (0, 0))
        self.assertIsNone(store.row_by_rel("IMG_01.jpg"))
        renamed = store.row_by_rel("img_01.jpg")
        self.assertEqual((renamed["rowid"], renamed["favorite"], renamed["filename"]), (row["rowid"], 1, "img_01.jpg"))
        self.assertEqual([t["tag"] for t in store.item_tags("img_01.jpg")], ["여행"])
        self.assertEqual(store.tag_feedback(), {"img_01.jpg": {"인물": -1}})
        self.assertEqual(set(store.embeddings_for(["img_01.jpg"])), {"img_01.jpg"})
        self.assertEqual(store._distinct_sets(), [frozenset({"img_01.jpg", "other.jpg"})])

    def test_a_file_that_went_away_keeps_its_star_and_tags_until_it_comes_back(self):
        picture(self.folder / "a.jpg")
        store = self.store()
        store.sync_folder(True)
        rowid = store.records(0, 10)[1][0]["rowid"]
        store.toggle_favorite(rowid)
        store.add_tag(["a.jpg"], "고양이")
        moved = self.base / "a.jpg"
        (self.folder / "a.jpg").rename(moved)
        self.assertEqual(store.sync_folder(True)["gone"], 1)
        self.assertEqual(self.listed(store), [])
        self.assertEqual(store.user_tags(), [])
        moved.rename(self.folder / "a.jpg")
        store.sync_folder(True)
        row = store.records(0, 10)[1][0]
        self.assertEqual((row["rowid"], row["favorite"]), (rowid, 1))
        self.assertEqual(store.user_tags(), [{"tag": "고양이", "count": 1}])


class ListTests(FolderCase):
    def setUp(self):
        super().setUp()
        for name, size in (("b.jpg", (40, 30)), ("a.jpg", (80, 60)), ("sub/c.png", (20, 10))):
            picture(self.folder / name, size=size)
        self.s = self.store()
        self.s.sync_folder(True)

    def test_orders_and_search(self):
        self.assertEqual([r["rel_path"] for r in self.s.records(0, 10, order="name")[1]], ["a.jpg", "b.jpg", "sub/c.png"])
        sizes = [r["filesize"] for r in self.s.records(0, 10, order="size")[1]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(self.listed(self.s, query="sub"), ["sub/c.png"])
        self.assertEqual(self.listed(self.s, query="%"), [])
        total, rows = self.s.records(1, 1, order="name")
        self.assertEqual((total, [r["rel_path"] for r in rows]), (3, ["b.jpg"]))

    def test_tags_filters_and_the_ai_list_includes_typed_tags(self):
        self.s.add_tag(["a.jpg"], "#고양이")
        self.s.replace_ai_tags([("b.jpg", ["고양이", "인물"]), ("sub/c.png", [])])
        self.assertEqual(self.listed(self.s, user_tag="고양이"), ["a.jpg"])
        self.assertEqual(self.listed(self.s, ai_category="고양이"), ["a.jpg", "b.jpg"])
        self.assertEqual(self.s.ai_tag_counts(["고양이", "인물"]), {"고양이": 2, "인물": 1})
        self.assertEqual(self.s.remove_tag("b.jpg", "고양이"), "ai")
        self.assertEqual(self.s.tag_feedback(), {"b.jpg": {"고양이": -1}})
        self.s.replace_ai_tags([("b.jpg", ["고양이", "인물"])])
        self.assertEqual([t["tag"] for t in self.s.ai_tags_of("b.jpg")], ["인물"])
        self.assertEqual(self.s.add_tag(["b.jpg"], "고양이"), 1)
        self.assertEqual(self.s.tag_feedback(), {})
        self.s.set_tag_feedback(["sub/c.png"], "동물", True)
        self.assertEqual(self.s.ai_tags_of("sub/c.png"), [{"tag": "동물", "source": "confirmed", "confirmed": True}])
        self.assertEqual(self.s.refuse_ai_tags(["sub/c.png"]), 1)
        self.assertEqual(self.s.tag_feedback(["sub/c.png"]), {"sub/c.png": {"동물": -1}})
        self.assertEqual(self.s.count_embeddings(unlabeled=True), 0)

    def test_typing_the_tag_the_ai_gave_keeps_the_photo_looked_at(self):
        """Taking that typed tag away again is no "not this", and the AI's guess does not come straight back."""
        self.s.save_embeddings([("b.jpg", bytes(3072), 0.5)])
        self.s.replace_ai_tags([("b.jpg", ["인물"])])
        self.assertEqual(self.s.add_tag(["b.jpg"], "인물"), 1)
        self.assertEqual(self.s.remove_tag("b.jpg", "인물"), "user")
        self.assertEqual(self.s.tag_feedback(), {})
        self.assertEqual(self.s.item_tags("b.jpg"), [])
        self.assertEqual(self.s.count_embeddings(unlabeled=True), 0)

    def test_a_photo_sent_to_the_recycle_bin_keeps_teaching(self):
        """Clearing a copy is no wrong answer: what was taught on it stays an example (plan 4-6)."""
        self.s.save_embeddings([("a.jpg", bytes(3072), 0.5), ("b.jpg", bytes(3072), 0.5)])
        self.s.add_tag(["a.jpg"], "고양이")
        self.s.set_tag_feedback(["b.jpg"], "고양이", False)
        self.s.mark_gone(["a.jpg", "b.jpg"])
        found = sorted((rel, tag, label) for rel, tag, label, _blob in self.s.ai_examples(["고양이"]))
        self.assertEqual(found, [("a.jpg", "고양이", 1), ("b.jpg", "고양이", -1)])

    def test_favorites(self):
        rowid = self.s.records(0, 10, order="name")[1][0]["rowid"]
        self.assertTrue(self.s.toggle_favorite(rowid))
        self.assertEqual(self.listed(self.s, favorite_only=True), ["a.jpg"])
        self.assertFalse(self.s.toggle_favorite(rowid))
        self.assertIsNone(self.s.toggle_favorite(10**6))

    def test_paths_never_leave_the_folder(self):
        for bad in ("../x.jpg", "/x.jpg", "", None, "a/../../x.jpg"):
            self.assertIsNone(self.s.path_of(bad), bad)
        self.assertEqual(self.s.path_of("sub/c.png"), self.folder / "sub" / "c.png")
        self.assertTrue(self.s.is_media_path(self.folder / "a.jpg"))
        self.assertFalse(self.s.is_media_path(self.base / "elsewhere.jpg"))


class DuplicateTests(FolderCase):
    def setUp(self):
        super().setUp()
        for name in ("a.jpg", "b.jpg", "c.jpg", "d.jpg", "e.jpg"):
            picture(self.folder / name)
        self.s = self.store()
        self.s.sync_folder(True)

    def test_groups_marks_and_their_undo(self):
        self.s.save_clusters([["a.jpg", "b.jpg", "c.jpg"], ["d.jpg", "e.jpg"]], "exact")
        self.assertEqual(self.s.duplicate_count(), 5)
        self.assertEqual(self.s.duplicate_rule(), "exact")
        members = self.s.set_group_distinct("d.jpg")
        self.assertEqual(members, ["d.jpg", "e.jpg"])
        self.assertEqual(self.s.duplicate_count(), 3)
        mark = self.s.set_photos_distinct(["c.jpg"])
        self.assertEqual(mark, {"photos": ["c.jpg"], "group": ["a.jpg", "b.jpg", "c.jpg"]})
        self.assertEqual(self.s.duplicate_count(), 2)
        self.assertIsNone(self.s.set_photos_distinct(["a.jpg", "b.jpg"]))      # all that is left of it
        self.assertEqual(self.s.distinct_duplicate_count(), 2)
        # A new scan keeps the marks.
        self.s.save_clusters([["a.jpg", "b.jpg", "c.jpg"], ["d.jpg", "e.jpg"]], "exact")
        self.assertEqual(self.s.duplicate_count(), 2)
        self.assertTrue(self.s.undo_photos_distinct(mark))
        self.assertEqual(self.s.duplicate_count(), 3)
        self.assertTrue(self.s.undo_group_distinct(members))
        self.assertEqual(self.s.duplicate_count(), 5)
        self.s.set_group_distinct("a.jpg")
        self.assertEqual(self.s.clear_distinct(), 1)
        self.assertEqual(self.s.duplicate_count(), 5)

    def test_the_duplicate_page_names_the_keeper_and_the_copies(self):
        self.s.save_hashes([("a.jpg", "h1", "0" * 16, ""), ("b.jpg", "h1", "0" * 16, ""), ("c.jpg", "h2", "0" * 16, "")])
        self.s.save_clusters([["a.jpg", "b.jpg", "c.jpg"]], "near")
        rowid = {row["rel_path"]: row["rowid"] for row in self.s.records(0, 10)[1]}
        self.s.toggle_favorite(rowid["c.jpg"])                  # a starred photo is the one kept
        total, rows = self.s.duplicate_page(0, 10)
        self.assertEqual(total, 3)
        shown = [(r["rel_path"], r["cluster_rank"] == 1, r["hash_rank"] > 1) for r in rows]
        self.assertEqual(shown, [("c.jpg", True, False), ("a.jpg", False, False), ("b.jpg", False, True)])
        self.assertEqual(sorted(self.s.duplicate_copy_rel_paths()), ["b.jpg"])
        self.assertEqual({k: v for k, v in self.s.duplicate_members().items()}, {1: ["c.jpg", "a.jpg", "b.jpg"]})
        # The filters choose the groups shown, not which photo a group keeps.
        total, rows = self.s.duplicate_page(0, 10, query="b.jpg")
        self.assertEqual([(r["rel_path"], r["cluster_rank"]) for r in rows], [("b.jpg", 3)])
        self.s.mark_gone(["a.jpg"])
        self.assertEqual(self.s.duplicate_count(), 2)
        # Once only one photo remains in the group, the singleton cluster is pruned.
        self.s.mark_gone(["b.jpg"])
        self.assertEqual(self.s.duplicate_count(), 0)

    def test_split_group_distinct_undo_and_clear_remerge_connected_components(self):
        self.s.save_clusters([["a.jpg", "b.jpg", "c.jpg", "d.jpg"]], "exact")
        first = self.s.set_photos_distinct(["a.jpg", "b.jpg"])
        self.assertEqual(self.s.duplicate_count(), 2)
        second = self.s.set_photos_distinct(["c.jpg"])
        self.assertIsNotNone(second)
        self.assertEqual(self.s.duplicate_count(), 0)
        self.assertTrue(self.s.undo_photos_distinct(second))
        self.assertEqual(self.s.duplicate_count(), 2)
        self.assertEqual(self.s.clear_distinct(), 1)
        self.assertEqual(self.s.duplicate_count(), 4)
        self.assertEqual(self.s.duplicate_members(), {1: ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]})

    def test_pruning_keeps_distinct_undo_when_sibling_goes_and_ai_cleared_persists(self):
        self.s.save_clusters([["a.jpg", "b.jpg", "c.jpg"]], "exact")
        mark = self.s.set_photos_distinct(["c.jpg"])
        self.assertIsNotNone(mark)
        self.s.mark_gone(["b.jpg"])
        self.assertEqual(self.s.duplicate_count(), 0)
        self.assertEqual(self.s.duplicate_members(), {})
        self.assertTrue(self.s.undo_photos_distinct(mark))
        self.assertEqual(self.s.duplicate_count(), 2)
        self.assertEqual(self.s.duplicate_members(), {1: ["a.jpg", "c.jpg"]})

        self.s.replace_ai_tags([("a.jpg", ["인물"])])
        self.assertFalse(self.s.ai_cleared())
        self.assertEqual(self.s.clear_ai_tags(), 1)
        self.assertTrue(self.s.ai_cleared())
        self.s.set_ai_scored_state(1, "siglip2_base_224")
        self.assertFalse(self.s.ai_cleared())


class LightImportTests(unittest.TestCase):
    def test_listing_a_folder_loads_no_ai_library(self):
        code = ("import json, os, sys, tempfile\n"
                "from pathlib import Path\n"
                "base = Path(tempfile.mkdtemp())\n"
                "os.environ['LOCALAPPDATA'] = str(base / 'appdata')\n"
                "(base / 'f').mkdir()\n"
                "from luvbox.local_store import LocalFolderStore\n"
                "with LocalFolderStore(base / 'f') as store:\n"
                "    store.sync_folder(True); store.records(0, 10); store.user_tags()\n"
                "print(json.dumps([n for n in ('numpy', 'onnxruntime', 'luvbox.clip_classifier') if n in sys.modules]))\n")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1]), [])


if __name__ == "__main__":
    unittest.main()
