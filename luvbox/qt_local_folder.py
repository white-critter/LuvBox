"""The "폴더 정리" tab: a folder on this PC, browsed, cleared of duplicates and given AI tags.

It is the gallery's controller (``LibraryController``) with another source of
photos: ``LocalFolderRepository`` reads a ``LocalFolderStore`` (one database
per folder, in the app data folder) instead of the X catalog. Selection, the
preview, tags, the duplicate view with its "not duplicates" marks, the AI menu
and teaching therefore behave exactly as in the gallery. Recycling goes through
``LocalManagementController``, which checks each file again like the gallery's
deletion does, without the X records' bookkeeping.

A tile key is ``g<generation>_lf_<row>``: the gallery's form, and never a path.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time

from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot

from .ai_profile import valid_tag
from .clip_model import ACTIVE_MODEL_ID
from .local_store import LocalFolderError, LocalFolderStore, SUFFIXES, check_root, folder_key, forget_folder_record
from .qt_library import (MAX_SELECTION, LibraryController, LibraryRepository, ManifestError, _active_ai_names,
                         _format_size, _open_profile, _shown_tags)
from .qt_management import ManagementController
from .recycling import file_signature
from .thumbcache import cloud_only
from .user_errors import UserRuntimeError

_KEY = re.compile(r"g([0-9]+)_lf_([0-9]{1,12})\Z")
FOLDER_ORDERS = ("modified", "name", "size")
_FOLDER_MEDIA_TYPES = ("all", "photo", "animated_gif")
_FOLDER_CHANGES = ("added", "changed", "gone", "restored", "renamed", "cloud_changed")
RECENT_LIMIT = 10
_MISSING = "파일이 없습니다. 폴더에서 옮겨졌거나 지워졌을 수 있습니다."
_CLOUD = "이 파일은 클라우드에만 있습니다. 탐색기에서 받은 뒤 다시 열 수 있습니다."


@dataclass(frozen=True)
class _FolderRequest:
    generation: int
    root: str
    include_subfolders: bool = True
    query: str = ""
    media_type: str = "all"
    order: str = "modified"
    favorite_only: bool = False
    user_tag: str = ""
    duplicate_only: bool = False
    ai_category: str = ""
    ai_origin: str = ""
    # What the gallery's shared code reads of a request; a folder has none of these.
    author: str = ""
    hashtag: str = ""
    date_field: str = "posted"
    date_from: str = ""
    date_to: str = ""

    @property
    def account(self):
        return "folder"

    @property
    def output_dir(self):
        return self.root

    @property
    def separate_accounts(self):
        return False

    def store(self):
        return LocalFolderStore(self.root)

    def filters(self):
        return dict(query=self.query, favorite_only=self.favorite_only, user_tag=self.user_tag,
                    ai_category=self.ai_category, duplicate_only=self.duplicate_only,
                    media_type="all" if self.duplicate_only else self.media_type)


def _modified(mtime_ns) -> str:
    from datetime import datetime

    try:
        return datetime.fromtimestamp(int(mtime_ns) / 1e9).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError, TypeError):
        return ""


class LocalFolderRepository(LibraryRepository):
    """Off-thread reads and jobs for one opened folder."""

    def __init__(self):
        super().__init__()
        self.share_learning = True
        self._profile_lock = threading.Lock()

    @staticmethod
    def _catalog_moved(previous, request):
        if previous is None or request is None:
            return True
        return previous.root != request.root

    # -- keys -------------------------------------------------------------------------------
    @staticmethod
    def _rowid(key, request):
        match = _KEY.fullmatch(key) if isinstance(key, str) else None
        if match is None or request is None or int(match[1]) != request.generation:
            return None
        return int(match[2])

    def _rows_for_keys(self, store, keys, request):
        rowids = {key: self._rowid(key, request) for key in keys}
        if any(value is None for value in rowids.values()):
            return None
        found = store.rows_for(rowids.values())
        return {key: found.get(rowid) for key, rowid in rowids.items()}

    @staticmethod
    def _locate_row(store, row):
        """``(path, kind, details)``: ready, cloud or missing."""
        if row is None or not row.get("present"):
            return None, "missing", None
        path = store.path_of(row["rel_path"])
        if path is None:
            return None, "missing", None
        try:
            details = path.stat()
        except OSError:
            return None, "missing", None
        if not stat.S_ISREG(details.st_mode):
            return None, "missing", None
        # A cloud service can free or hydrate a listed photo at any time: live attributes decide,
        # never a cached cloud flag or an operation that triggers a Windows cloud download.
        if cloud_only(path, status=details):
            return path, "cloud", details
        return path, "ready", details

    def _example_scope(self, request):
        key = folder_key(request.root)
        return "local", key, (lambda rel: f"local:{key}:{rel}")

    # -- the list ---------------------------------------------------------------------------
    def read(self, token, offset, limit):
        request = self.current()
        if request is None or token != str(request.generation):
            return 0, []
        try:
            with closing(request.store()) as store:
                store._folder_cache = {}
                if request.duplicate_only:
                    total, page = store.duplicate_page(offset, limit, **request.filters())
                else:
                    total, page = store.records(offset, limit, order=request.order, **request.filters())
                rows, served = [], {}
                for row in page:
                    key = f"g{request.generation}_lf_{int(row['rowid'])}"
                    path, kind, info = self._locate_row(store, row)
                    stamp = ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                             if kind == "ready" and info is not None else None)
                    served[key] = (str(path) if kind == "ready" else "", stamp)
                    with self._condition:
                        if self._request is not request:
                            return 0, []
                        if kind == "ready":
                            self.provider.register(key, path)
                    folder = os.path.dirname(row["rel_path"])
                    caption = (folder + "/" if folder else "") + row["filename"]
                    if kind == "cloud":
                        caption += " · 클라우드에만 있음"
                    elif kind != "ready":
                        caption += " · 파일 없음"
                    entry = {"key": key, "caption": caption, "missing": kind != "ready",
                             "favorite": bool(row.get("favorite")), "media_type": row["media_type"]}
                    if request.duplicate_only:
                        entry.update(group=int(row["cluster_id"]), keeper=int(row["cluster_rank"]) == 1,
                                     copy=int(row["hash_rank"]) > 1, group_size=int(row["cluster_size"]),
                                     detail=_format_size(row.get("filesize")))
                    else:
                        entry["detail"] = _format_size(row.get("filesize"))
                    rows.append(entry)
                with self._condition:
                    if self._request is not request:
                        return 0, []
                    for key, item in served.items():
                        self._served.setdefault(key, item)
                return total, rows
        except LocalFolderError:
            return 0, []

    def authors(self, generation=None):
        return []

    def hashtags(self, generation=None):
        return []

    def user_tags(self, generation=None):
        request = self.current(generation)
        if request is None:
            return []
        try:
            with closing(request.store()) as store:
                return store.user_tags(200)
        except LocalFolderError:
            return []

    def duplicate_counts(self, generation=None):
        request = self.current(generation)
        if request is None:
            return 0
        try:
            with closing(request.store()) as store:
                return store.duplicate_count()
        except LocalFolderError:
            return 0

    def distinct_counts(self, generation=None):
        request = self.current(generation)
        if request is None:
            return 0
        try:
            with closing(request.store()) as store:
                return store.distinct_duplicate_count()
        except LocalFolderError:
            return 0

    def ai_counts(self, generation=None):
        request = self.current(generation)
        profile = _open_profile() if request is not None else None
        if profile is None:
            return []
        with closing(profile):
            names = profile.active_tag_names()
        try:
            with closing(request.store()) as store:
                found = store.ai_tag_counts(names)
        except LocalFolderError:
            found = {}
        return [{"category": name, "count": int(found.get(name, 0))} for name in names]

    def overview(self, generation=None):
        return None

    def selected(self, key, generation):
        request = self.current(generation)
        if request is None:
            return None
        rowid = self._rowid(key, request)
        if rowid is None:
            return None
        with closing(request.store()) as store:
            row = store.row(rowid)
            if row is None:
                return None
            path, kind, _info = self._locate_row(store, row)
            width = height = raw_width = raw_height = 0
            if kind == "ready" and row["media_type"] == "photo":
                from .preview import photo_info

                try:
                    signature = file_signature(path)
                    dimensions = photo_info(path)
                    width, height = dimensions.get("pixel_width", 0), dimensions.get("pixel_height", 0)
                    raw_width, raw_height = dimensions.get("source_width", 0), dimensions.get("source_height", 0)
                    if file_signature(path) != signature:
                        path, kind = None, "missing"
                except OSError:
                    path, kind = None, "missing"
            elif kind == "ready":
                from .preview import gif_screen_size

                try:
                    width, height = gif_screen_size(path)
                    raw_width, raw_height = width, height
                except OSError:
                    path, kind = None, "missing"
            parts = [row["rel_path"]]
            if width and height:
                parts.append(f"{width}×{height}" + (f" (원본 {raw_width}×{raw_height})"
                                                    if raw_width and (raw_width, raw_height) != (width, height) else ""))
            if row.get("filesize"):
                parts.append(_format_size(row["filesize"]))
            modified = _modified(row.get("mtime_ns"))
            if modified:
                parts.append("수정 " + modified)
            active = _active_ai_names()
            tags = _shown_tags(store.item_tags(row["rel_path"]), active)
            ai_tags = _shown_tags(store.ai_tags_of(row["rel_path"]), active)
            ready = kind == "ready"
            return {"key": key, "author": "", "body": "", "source": "", "hashtags": [],
                    "path": str(path) if ready else "", "missing": not ready,
                    "missing_note": "" if ready else (_CLOUD if kind == "cloud" else _MISSING),
                    "preview": QUrl.fromLocalFile(str(path)).toString() if ready else "",
                    "media_type": row["media_type"], "public_info": " · ".join(parts),
                    "pixel_width": width, "pixel_height": height, "source_width": raw_width,
                    "source_height": raw_height, "favorite": bool(row.get("favorite")), "tags": tags,
                    "ai_category": ai_tags[0]["tag"] if ai_tags else "",
                    "ai_confirmed": bool(ai_tags and ai_tags[0]["confirmed"]), "ai_tags": ai_tags,
                    "ai_confidence": -1.0}

    def toggle_favorite(self, key, generation):
        request = self.current(generation)
        rowid = self._rowid(key, request)
        if rowid is None:
            return None
        try:
            with closing(request.store()) as store:
                return store.toggle_favorite(rowid)
        except LocalFolderError:
            return None

    # -- tags -------------------------------------------------------------------------------
    def _teach_profile(self, store, request, tag):
        """A tag typed or taken away: if it is an AI tag and learning is shared, its examples follow."""
        from . import ai_pipeline

        clean = valid_tag(tag)
        if not clean or not self.share_learning:
            return
        with self._profile_lock:
            if not self.share_learning:
                return
            profile = _open_profile()
            if profile is None:
                return
            try:
                with closing(profile):
                    if clean in profile.known_tag_names():
                        kind, scope, example_id = self._example_scope(request)
                        ai_pipeline.sync_examples(store, profile, kind, scope, example_id, [clean])
            except Exception:
                pass

    def _rel_paths(self, store, keys, request):
        found = self._rows_for_keys(store, keys, request)
        if not found:
            return []
        return [row["rel_path"] for row in found.values() if row is not None]

    def add_tag(self, key, generation, tag):
        return bool(self.add_tag_batch([key], generation, tag))

    def add_tag_batch(self, keys, generation, tag):
        request = self.current(generation)
        if request is None or not keys:
            return 0
        try:
            with closing(request.store()) as store:
                rels = self._rel_paths(store, keys, request)
                count = store.add_tag(rels, tag)
                if count:
                    self._teach_profile(store, request, tag)
                return count
        except LocalFolderError:
            return 0

    def remove_tag_detailed(self, key, generation, tag):
        request = self.current(generation)
        if request is None:
            return False, False
        try:
            with closing(request.store()) as store:
                rels = self._rel_paths(store, [key], request)
                if not rels:
                    return False, False
                source, confirmed_restored = store.remove_tag_result(rels[0], tag)
                removed = bool(source)
                if removed:
                    self._teach_profile(store, request, tag)
                return removed, confirmed_restored
        except LocalFolderError:
            return False, False

    def remove_tag(self, key, generation, tag):
        return self.remove_tag_detailed(key, generation, tag)[0]

    def sync_tag_examples(self, generation, tags, epoch=None):
        from . import ai_pipeline

        names = [valid_tag(tag) for tag in tags if valid_tag(tag)]
        if not names:
            return False
        with self._profile_lock:
            request = self._catalog_request(generation, epoch) if epoch is not None else self.current(generation)
            if request is None or not self.share_learning:
                return False
            profile = _open_profile()
            if profile is None:
                return False
            try:
                with closing(profile), closing(request.store()) as store:
                    known = set(profile.known_tag_names())
                    names = [tag for tag in names if tag in known]
                    kind, scope, example_id = self._example_scope(request)
                    return ai_pipeline.sync_examples(store, profile, kind, scope, example_id, names) if names else False
            except Exception:
                return False

    def set_share_learning(self, enabled, generation=None):
        """Sharing switched off takes this folder's examples out of the shared profile; on brings them back."""
        from . import ai_pipeline

        with self._profile_lock:
            self.share_learning = bool(enabled)
            request = self.current(generation)
            profile = _open_profile() if request is not None else None
            if profile is None:
                return
            try:
                with closing(profile), closing(request.store()) as store:
                    kind, scope, example_id = self._example_scope(request)
                    if self.share_learning:
                        ai_pipeline.sync_examples(store, profile, kind, scope, example_id)
                    else:
                        profile.remove_scope_examples(kind, scope)
            except Exception:
                pass

    # -- the folder itself ------------------------------------------------------------------
    def sync_folder(self, generation, epoch=None, progress_callback=None):
        """Read the folder again (a quick look at sizes and times; nothing is opened)."""
        from .duplicates import ScanCancelled

        request = self._catalog_request(generation, epoch)
        if request is None:
            return None

        def check_cancel():
            if self._catalog_request(generation, epoch) is None:
                raise ScanCancelled("폴더가 바뀌어 읽기를 멈췄습니다.")

        with closing(request.store()) as store:
            return store.sync_folder(request.include_subfolders, check_cancel)

    # -- duplicates -------------------------------------------------------------------------
    def scan_duplicates(self, generation, progress_callback=None, epoch=None, near=False, compare_callback=None):
        from .duplicates import ScanCancelled, cluster_images, compute_image_hashes

        request = self._catalog_request(generation, epoch)
        if request is None:
            return 0

        def check_cancel():
            if self._duplicate_cancel.is_set():
                raise ScanCancelled("중복 검사를 중단했습니다.")
            if self._catalog_request(generation, epoch) is None:
                raise ScanCancelled("폴더가 바뀌어 작업을 멈췄습니다.")

        self._dup_synced_changes = False
        try:
            with closing(request.store()) as store:
                store._folder_cache = {}
                synced = store.sync_folder(request.include_subfolders, check_cancel)
                self._dup_synced_changes = any(synced[key] for key in _FOLDER_CHANGES)
                pending = store.photos_to_hash()
                total = len(pending)
                hashed = []
                for index, row in enumerate(pending):
                    check_cancel()
                    path = store.path_of(row["rel_path"])
                    if path is None:
                        continue
                    try:
                        before = file_signature(path)
                        file_hash, dhash = compute_image_hashes(path)
                        if file_signature(path) != before:
                            continue
                    except (OSError, ValueError):
                        continue
                    if not file_hash:
                        continue
                    values = (row["rel_path"], file_hash, dhash or "", ":".join(map(str, before)))
                    if before[2:] != (row["filesize"], row["mtime_ns"]):
                        values += before[2:]
                        self._dup_synced_changes = True
                    hashed.append(values)
                    if len(hashed) >= 64:
                        store.save_hashes(hashed)
                        hashed.clear()
                    if progress_callback and (index % 16 == 0 or index == total - 1):
                        progress_callback(index + 1, total + 1)
                store.save_hashes(hashed)
                seen_files = set()
                items = []
                stale_updates = []
                for row in store.hashed_photos():
                    check_cancel()
                    path = store.path_of(row["rel_path"])
                    if path is None:
                        continue
                    try:
                        info = path.stat()
                        if not stat.S_ISREG(info.st_mode) or cloud_only(path, status=info):
                            continue
                        sig = file_signature(path)
                    except OSError:
                        continue
                    volume, number = sig[:2]
                    if number != 0:
                        if (volume, number) in seen_files:
                            continue
                        seen_files.add((volume, number))
                    sig_str = ":".join(map(str, sig))
                    file_hash, dhash, filesize = row["file_hash"], row["dhash"], row["filesize"]
                    if row.get("file_signature") != sig_str:
                        try:
                            file_hash, dhash = compute_image_hashes(path)
                            after = file_signature(path)
                        except (OSError, ValueError):
                            continue
                        if after != sig or not file_hash:
                            continue
                        filesize = after[2]
                        stale_updates.append((row["rel_path"], file_hash, dhash or "", sig_str, after[2], after[3]))
                        self._dup_synced_changes = True
                        if len(stale_updates) >= 64:
                            store.save_hashes(stale_updates)
                            stale_updates.clear()
                    items.append({"key": row["rel_path"], "file_hash": file_hash, "filesize": filesize,
                                  "dhash": dhash, "path": str(path), "favorite": bool(row["favorite"])})
                if stale_updates:
                    store.save_hashes(stale_updates)
                groups = cluster_images(items, near=bool(near), check_cancel=check_cancel,
                                        progress=compare_callback)
                check_cancel()
                grouped = store.save_clusters(([item["key"] for item in group] for group in groups),
                                              "near" if near else "exact", check_cancel=check_cancel)
                if progress_callback:
                    progress_callback(total + 1, total + 1)
                return grouped
        except (ScanCancelled, LocalFolderError):
            raise
        except Exception as exc:
            raise UserRuntimeError("중복 검사를 끝내지 못했습니다. 폴더를 확인한 뒤 다시 시도해 주세요.") from exc

    def duplicate_copy_keys(self, generation):
        request = self.current(generation)
        if request is None:
            return []
        try:
            with closing(request.store()) as store:
                return [f"g{generation}_lf_{int(row['rowid'])}" for row in store.duplicate_copies(**request.filters())]
        except LocalFolderError:
            return []

    def mark_group_distinct(self, key, generation):
        request = self.current(generation)
        if request is None:
            return None
        try:
            with closing(request.store()) as store:
                rels = self._rel_paths(store, [key], request)
                return store.set_group_distinct(rels[0]) if rels else None
        except LocalFolderError:
            return None

    def mark_photos_distinct(self, keys, generation):
        request = self.current(generation)
        if request is None or not keys:
            return None
        try:
            with closing(request.store()) as store:
                rels = self._rel_paths(store, keys, request)
                return store.set_photos_distinct(rels) if len(rels) == len(keys) else None
        except LocalFolderError:
            return None

    def undo_photos_distinct(self, mark, generation):
        request = self.current(generation)
        if request is None or not mark:
            return False
        try:
            with closing(request.store()) as store:
                return bool(store.undo_photos_distinct(mark))
        except LocalFolderError:
            return False

    def undo_group_distinct(self, members, generation):
        request = self.current(generation)
        if request is None or not members:
            return False
        try:
            with closing(request.store()) as store:
                return bool(store.undo_group_distinct(members))
        except LocalFolderError:
            return False

    def clear_distinct(self, generation):
        request = self.current(generation)
        if request is None:
            return None
        try:
            with closing(request.store()) as store:
                return int(store.clear_distinct())
        except LocalFolderError:
            return None

    # -- AI ---------------------------------------------------------------------------------
    def auto_ai_pending(self, generation):
        """Whether to classify by itself. A folder nobody classified yet waits for the person.

        Its first classification reads every photo, which in a big folder takes long
        and holds the tab's other work. Once the person ran it, new and changed
        photos and changed AI tags are taken care of without being asked.
        """
        request = self.current(generation)
        if request is None:
            return False
        from .clip_model import model_file_present

        try:
            with closing(request.store()) as store:
                if store.ai_cleared():
                    return False
                if not store.count_embeddings(ACTIVE_MODEL_ID):
                    return False
                if store.count_embeddings(ACTIVE_MODEL_ID, unlabeled=True):
                    return True
                profile = _open_profile()
                if profile is not None:
                    with closing(profile):
                        version = profile.rules_version()
                    if store.ai_scored_state() != (str(version), ACTIVE_MODEL_ID):
                        return True
                if not model_file_present():
                    return False
                signatures = store.unreadable_signatures()
                for row in store.photos_without_embeddings():
                    if self.current(generation) is None:
                        return False
                    path = store.path_of(row["rel_path"])
                    if path is None:
                        continue
                    try:
                        info = path.stat()
                    except OSError:
                        continue
                    if not stat.S_ISREG(info.st_mode) or cloud_only(path, status=info):
                        continue
                    if signatures.get(row["rel_path"]) != (int(info.st_size), int(info.st_mtime_ns)):
                        return True
                return False
        except Exception:
            return False

    def scan_ai_tags(self, generation, progress_callback=None, status_callback=None, epoch=None, download=True,
                     encode=True):
        from contextlib import ExitStack
        from . import ai_pipeline
        from .duplicates import ScanCancelled

        self._scan_note = ""
        self._ai_wrote = False
        self._ai_changed = 0
        self._ai_synced_changes = False
        request = self._catalog_request(generation, epoch)
        if request is None:
            return 0
        run = ai_pipeline.Run()
        release_session = None
        session_use = None
        if encode:
            from .clip_classifier import is_available, release_session, session_use

        def check_cancel():
            if self._ai_cancel.is_set():
                kept = f" 이미 분류된 {run.tagged:,}장은 유지됩니다." if run.tagged else ""
                raise ScanCancelled(f"AI 자동분류를 중단했습니다.{kept}")
            if self._catalog_request(generation, epoch) is None:
                raise ScanCancelled("폴더가 바뀌어 작업을 멈췄습니다.")

        profile = _open_profile()
        if profile is None:
            raise UserRuntimeError("AI 태그 설정을 열지 못했습니다. LuvBox 기록 폴더의 권한을 확인해 주세요.")
        synced_changes = False
        try:
            with closing(profile), closing(request.store()) as store:
                items, skipped = [], 0
                if encode:
                    synced = store.sync_folder(request.include_subfolders, check_cancel)
                    synced_changes = any(synced[key] for key in _FOLDER_CHANGES)
                    signatures = store.unreadable_signatures()
                    for row in store.photos_without_embeddings():
                        check_cancel()
                        path = store.path_of(row["rel_path"])
                        if path is None:
                            continue
                        try:
                            info = path.stat()
                        except OSError:
                            continue
                        if not stat.S_ISREG(info.st_mode) or cloud_only(path, status=info):
                            continue
                        sig = (int(info.st_size), int(info.st_mtime_ns))
                        if signatures.get(row["rel_path"]) == sig:
                            skipped += 1
                            continue
                        items.append((row["rel_path"], path))
                    if items and not download and not is_available():
                        items = []
                if skipped:
                    self._scan_note = f" 읽을 수 없는 사진 {skipped:,}장은 제외했습니다."
                kind, scope, example_id = self._example_scope(request)
                with ExitStack() as scope_guard:
                    if session_use is not None and items:
                        scope_guard.enter_context(session_use())
                    ai_pipeline.classify(store, profile, kind=kind, scope=scope, example_id=example_id, items=items,
                                         skipped=skipped, check_cancel=check_cancel, progress=progress_callback,
                                         status=status_callback, download=download, run=run,
                                         shared=self.share_learning)
                if run.unreadable:
                    self._scan_note = f" 읽을 수 없는 사진 {skipped + run.unreadable:,}장은 제외했습니다."
                return run.tagged if encode else run.changed
        except (RuntimeError, ScanCancelled):
            raise
        except Exception as exc:
            raise UserRuntimeError("AI 자동분류를 끝내지 못했습니다. 폴더를 확인한 뒤 다시 시도해 주세요.") from exc
        finally:
            self._ai_synced_changes = synced_changes
            self._ai_wrote = run.wrote
            self._ai_changed = run.changed
            if release_session is not None:
                release_session()

    def reclassify(self, generation, progress_callback=None, status_callback=None, epoch=None):
        request = self._catalog_request(generation, epoch)
        if request is None:
            return 0
        self._ai_had_embeddings = False
        try:
            with closing(request.store()) as store:
                self._ai_had_embeddings = store.count_embeddings(ACTIVE_MODEL_ID) > 0
                store.set_ai_scored_state(-1, ACTIVE_MODEL_ID)
        except LocalFolderError as exc:
            raise UserRuntimeError("AI 태그를 다시 매기지 못했습니다. 폴더를 확인한 뒤 다시 시도해 주세요.") from exc
        return self.scan_ai_tags(generation, progress_callback=progress_callback, status_callback=status_callback,
                                 epoch=epoch, download=False, encode=False)

    def clear_ai_tags(self, generation, epoch=None):
        request = self._catalog_request(generation, epoch)
        if request is None:
            return 0
        try:
            with closing(request.store()) as store:
                return store.clear_ai_tags()
        except LocalFolderError as exc:
            raise UserRuntimeError("AI 태그를 초기화하지 못했습니다. 폴더를 확인한 뒤 다시 시도해 주세요.") from exc

    def teach_labels(self, generation, identities, category, epoch=None, progress_callback=None, status_callback=None):
        return self.teach_tag(generation, identities, category, bool(category), epoch=epoch,
                              progress_callback=progress_callback, status_callback=status_callback)

    def teach_tag(self, generation, identities, tag, positive, epoch=None, progress_callback=None,
                  status_callback=None):
        """As the gallery's: ``identities`` are row numbers of this folder's list."""
        from . import ai_pipeline
        from .clip_classifier import release_session
        from .duplicates import ScanCancelled

        request = self._catalog_request(generation, epoch)
        if request is None:
            return 0, 0
        clean = valid_tag(tag)
        if not clean and (tag or positive):
            return 0, 0

        def check_cancel():
            if self._ai_cancel.is_set() or self._catalog_request(generation, epoch) is None:
                raise ScanCancelled("분류 가르치기를 중단했습니다.")

        profile = _open_profile()
        if profile is None:
            raise UserRuntimeError("AI 태그 설정을 열지 못했습니다. LuvBox 기록 폴더의 권한을 확인해 주세요.")
        try:
            with closing(profile), closing(request.store()) as store:
                rows = store.rows_for(int(item) for item in identities)
                self._teach_skipped_media = sum(
                    1 for row in rows.values() if row["present"] and row["media_type"] != "photo"
                )
                present = [row["rel_path"] for row in rows.values()
                           if row["present"] and not row["cloud"] and row["media_type"] == "photo"]
                known = store.embeddings_for(present)
                missing = []
                for rel in present:
                    if rel in known:
                        continue
                    path = store.path_of(rel)
                    if path is not None and not cloud_only(path):
                        missing.append((rel, path))
                ai_pipeline.encode_missing_for_teach(
                    store, missing, total=len(present), check_cancel=check_cancel,
                    progress_callback=progress_callback, status_callback=status_callback,
                )
                check_cancel()
                refreshed = store.embeddings_for(present) if present else {}
                teachable = [rel for rel in present if rel in refreshed]
                if not teachable:
                    return 0, 0
                kind, scope, example_id = self._example_scope(request)
                return ai_pipeline.teach(
                    store, profile, kind=kind, scope=scope, example_id=example_id,
                    idents=teachable, tag=clean, positive=positive,
                    check_stop=lambda: self._ai_cancel.is_set() or self._catalog_request(generation, epoch) is None,
                    shared=self.share_learning,
                )
        except (RuntimeError, ScanCancelled):
            raise
        except Exception as exc:
            raise UserRuntimeError("분류를 저장하지 못했습니다. 폴더를 확인한 뒤 다시 시도해 주세요.") from exc
        finally:
            release_session()

    # -- recycling --------------------------------------------------------------------------
    def deletion_manifest(self, keys):
        if (not isinstance(keys, (list, tuple)) or not 1 <= len(keys) <= MAX_SELECTION
                or any(not isinstance(key, str) for key in keys)):
            raise ManifestError("휴지통으로 보낼 사진을 1~2,000개 선택해 주세요.")
        keys = list(dict.fromkeys(keys))
        with self._condition:
            request = self._request
            if request is None or any(key not in self._served for key in keys):
                raise ManifestError("목록이 바뀌었습니다. 사진을 다시 선택해 주세요.")
            displayed = {key: self._served[key] for key in keys}
        with closing(request.store()) as store:
            found = self._rows_for_keys(store, keys, request) or {}
            rows = []
            for key in keys:
                row = found.get(key)
                path, kind, _info = self._locate_row(store, row)
                previous_path, previous_signature = displayed[key]
                if kind != "ready" or str(path) != previous_path or previous_signature is None:
                    raise ManifestError("선택한 파일을 확인하지 못했습니다. 목록을 새로고침해 주세요.")
                signature = file_signature(path)
                if signature != previous_signature:
                    raise ManifestError("선택한 뒤 파일이 바뀌었습니다. 목록을 새로고침해 주세요.")
                rows.append({"path": str(path), "filename": row["filename"], "rel_path": row["rel_path"],
                             "filesize": row["filesize"], "_delete_signature": signature, "_key": key})
            kept = 0
            if request.duplicate_only:
                rows, kept = self._copies_only(store, rows)
        with self._condition:
            if self._request is not request:
                raise ManifestError("목록이 바뀌었습니다. 사진을 다시 선택해 주세요.")
        return {"location": request.root, "rows": rows, "generation": request.generation, "kept": kept}

    @staticmethod
    def _copies_only(store, rows):
        """As the gallery's: a copy goes only while another file of its group with its bytes stays."""
        chosen = {row["rel_path"] for row in rows}
        groups, member_of, copies = {}, {}, set()
        for member in store.ranked_duplicate_members(sorted(chosen)):
            rel, number = member["rel_path"], member["cluster_id"]
            groups.setdefault(number, []).append(rel)
            member_of[rel] = number
            if member["file_hash"] and member["hash_rank"] > 1:
                copies.add(rel)
        first = {members[0] for members in groups.values()}
        spared = {members[0] for members in groups.values() if all(rel in chosen for rel in members)}
        rule = store.duplicate_rule() or "exact"
        marked = []
        for row in rows:
            rel = row["rel_path"]
            if rel in spared:
                continue
            keepers = []
            for member in groups.get(member_of.get(rel), ()):
                if member == rel:
                    continue
                if member not in chosen or member in spared or (rel in copies and member not in copies):
                    path = store.path_of(member)
                    if path is not None:
                        keepers.append(str(path))
            near = rel in member_of and rel not in first and rel not in copies
            marked.append(dict(row, _copy_keepers=keepers, _copy_rule=rule, _copy_near=near))
        return marked, len(spared)

    def alike_manifest(self, key):
        from .duplicates import nearly_identical, pixel_vector, same_in_detail

        if not isinstance(key, str) or not key:
            raise ManifestError("비교할 사진을 다시 골라 주세요.")
        with self._condition:
            request = self._request
            if request is None or not request.duplicate_only or key not in self._served:
                raise ManifestError("목록이 바뀌었습니다. 사진을 다시 골라 주세요.")
            previous_path, previous_signature = self._served[key]
        with closing(request.store()) as store:
            row = (self._rows_for_keys(store, [key], request) or {}).get(key)
            path, kind, _info = self._locate_row(store, row)
            if kind != "ready" or str(path) != previous_path or previous_signature is None:
                raise ManifestError("선택한 파일을 확인하지 못했습니다. 목록을 새로고침해 주세요.")
            signature = file_signature(path)
            if signature != previous_signature:
                raise ManifestError("선택한 뒤 파일이 바뀌었습니다. 목록을 새로고침해 주세요.")
            rel = row["rel_path"]
            pair = store.alike_pair(rel)
            if pair is None:
                raise ManifestError("‘비슷함’으로 표시된 사진만 남길 사진과 비교해 휴지통으로 보낼 수 있습니다.")
            row, keeper_row = pair
            keeper = store.path_of(keeper_row["rel_path"])
            if keeper is None or not keeper.is_file():
                raise ManifestError("남길 사진 파일이 없어 이 사진을 보낼 수 없습니다.")
            if cloud_only(keeper):
                raise ManifestError("남길 사진이 클라우드에만 있어 비교할 수 없습니다. 탐색기에서 받은 뒤 다시 시도해 주세요.")
            keeper_signature = file_signature(keeper)
            if (os.path.normcase(os.path.realpath(keeper)) == os.path.normcase(os.path.realpath(path))
                    or (keeper_signature[1] and keeper_signature[:2] == signature[:2])):
                raise ManifestError("남길 사진이 이 사진과 같은 파일이라 보낼 수 없습니다.")
            if not (nearly_identical(pixel_vector(path), pixel_vector(keeper)) and same_in_detail(path, keeper)):
                raise ManifestError("남길 사진과 같은 사진으로 다시 확인되지 않아 보낼 수 없습니다. "
                                    "‘중복 아님’으로 표시하거나 목록에서 휴지통으로 보내세요.")
            if file_signature(path) != signature or file_signature(keeper) != keeper_signature:
                raise ManifestError("비교하는 동안 파일이 바뀌었습니다. 목록을 새로고침해 주세요.")
            shown = {"alike": self._compared_local(path, row, signature),
                     "keeper": self._compared_local(keeper, keeper_row, keeper_signature)}
        with self._condition:
            if self._request is not request:
                raise ManifestError("목록이 바뀌었습니다. 사진을 다시 골라 주세요.")
        marked = {"path": str(path), "filename": row["filename"], "rel_path": row["rel_path"],
                  "filesize": row["filesize"], "_delete_signature": signature, "_key": key,
                  "_alike_keeper": str(keeper), "_alike_keeper_signature": keeper_signature}
        return {"location": request.root, "rows": [marked], "generation": request.generation,
                "kept": 0, "alike": shown}

    @staticmethod
    def _compared_local(path, row, signature):
        from .preview import photo_info

        try:
            size = photo_info(path)
        except OSError:
            size = {}
        width, height = size.get("pixel_width", 0), size.get("pixel_height", 0)
        return {"url": QUrl.fromLocalFile(str(path)).toString(), "name": path.name,
                "pixels": f"{width}×{height}" if width and height else "",
                "bytes": _format_size(signature[2]), "caption": _modified(row.get("mtime_ns")),
                "favorite": bool(row.get("favorite"))}

    def forget(self, keys, generation):
        with self._condition:
            request = self._request
            if request is None or request.generation != generation:
                return False
            for key in keys:
                self._served.pop(key, None)
            return True

    def action(self, key, generation, action):
        selection = self.selected(key, generation)
        if selection is None or self.current(generation) is None:
            return "사진을 다시 선택해 주세요."
        if action == "source":
            return "이 사진에는 원본 게시물이 없습니다."
        if not selection["path"]:
            return "파일을 찾지 못했습니다. 폴더를 확인해 주세요."
        if action == "open":
            if Path(selection["path"]).suffix.lower() not in SUFFIXES:
                return "지원하는 사진 파일만 열 수 있습니다. 폴더에서 파일을 확인해 주세요."
            os.startfile(selection["path"])
        elif action == "reveal":
            from .qt_browser_setup import explorer_select_command

            command = explorer_select_command(selection["path"])
            if command is None:
                return "폴더를 열지 못했습니다."
            subprocess.Popen(command, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return ""


class LocalManagementController(ManagementController):
    """The gallery's recycling, for the folder tab: the same confirmations, the folder's own checks."""

    def __init__(self, library, parent=None):
        super().__init__(library, parent)
        if hasattr(library, "folderChanged"):
            library.folderChanged.connect(self._sync_folder_context)
        self.changed.connect(self._sync_busy)
        self._sync_folder_context()

    def _sync_folder_context(self):
        self.setLibrary("folder", getattr(self._library, "folderPath", ""),
                        bool(getattr(self._library, "includeSubfolders", True)))

    def _sync_busy(self):
        setter = getattr(self._library, "_set_deletion_busy", None)
        if callable(setter):
            setter(self._mutating)

    def _recycle(self, manifest, keep_deleted, generation):
        from .local_deletion import run_local_deletion

        return run_local_deletion(manifest["location"], manifest["rows"], self._stop,
                                  lambda event: self._delivered.emit(("progress", generation, event, "")))


class LocalFolderController(LibraryController):
    """The gallery's controller over a folder of this PC."""

    folderChanged = Signal()
    folderOpeningChanged = Signal()
    recentFoldersChanged = Signal()
    shareLearningChanged = Signal()
    overlapChanged = Signal()
    # A folder was taken off the recent list (its path), with its record when asked.
    folderForgotten = Signal(str)
    # A property of this class must name a signal of this class as its notify: one that names
    # LibraryController.changed gives PySide6 a meta-object that crashes QML on the first read.
    # So `changed` is relayed here.
    stateChanged = Signal()
    _folderChecked = Signal(int, object, str)
    _overlapChecked = Signal(int, bool)
    image_provider = "localthumbs"
    lazy_workers = True                 # made at every start; its threads start with the first folder

    def __init__(self, parent=None):
        super().__init__(parent)
        self.changed.connect(self.stateChanged)
        self.overlapChanged.connect(self.stateChanged)
        self._folderChecked.connect(self._finish_open_folder)
        self._overlapChecked.connect(self._finish_overlap_check)
        self._order = "modified"
        self._root = ""
        self._include_subfolders = True
        self._recent = []
        self._share_learning = True
        self._overlaps = False
        self._checking_overlap = False
        self._overlap_request = 0
        self._overlap_inputs = None
        self._x_folders = ()
        self._folder_error = ""
        self._last = ""
        self._opening_path = None
        self._folder_request = 0
        self._deletion_busy = False
        self._aux_threads = []
        self._erasing_folders = set()

    def _set_deletion_busy(self, busy):
        busy = bool(busy)
        if self._deletion_busy != busy:
            self._deletion_busy = busy
            self.changed.emit()
            if not busy and self._auto_ai_wanted and not self._exclusive:
                self._auto_ai_timer.start()

    def _track_thread(self, thread):
        self._aux_threads = [item for item in self._aux_threads if item.is_alive()]
        self._aux_threads.append(thread)

    def _begin_exclusive(self, kind, action, status):
        if self._deletion_busy:
            self._status = "휴지통 이동이 끝난 뒤 다시 시도해 주세요."
            self.changed.emit()
            return False
        return super()._begin_exclusive(kind, action, status)

    @Slot()
    def _poll_shutdown(self):
        if not self._closing or self._shutdown_complete:
            return
        self._aux_threads = [item for item in self._aux_threads if item.is_alive()]
        if any(item.name == "qt-local-folder-forget" for item in self._aux_threads):
            return
        super()._poll_shutdown()

    # -- what differs from the gallery (see LibraryController) -----------------------------
    def _make_repository(self):
        return LocalFolderRepository()

    def _make_request(self):
        root = getattr(self, "_root", "")
        if not root:
            return None
        return _FolderRequest(self._generation, root, self._include_subfolders, self._query, self._media_type,
                              self._folder_order(), self._favorite_only, self._user_tag, self._duplicate_only,
                              self._ai_category, self._ai_origin)

    def _folder_order(self):
        # "필터 초기화" puts the gallery's own order back; a folder lists by modification time then.
        return self._order if self._order in FOLDER_ORDERS else "modified"

    def _teach_identity(self, key):
        match = _KEY.fullmatch(key) if isinstance(key, str) else None
        if match is None or int(match[1]) != self._generation:
            return None, False
        if self._selected.get("key") == key:
            return int(match[2]), self._selected.get("media_type", "photo") == "photo"
        pos = self.model._positions.get(key)
        if pos is not None and 0 <= pos < len(self.model._rows):
            row = self.model._rows[pos]
            if isinstance(row, dict) and row.get("media_type"):
                return int(match[2]), row["media_type"] == "photo"
        return int(match[2]), True

    # -- properties -------------------------------------------------------------------------
    @Property(QObject, constant=True)
    def folderModel(self):
        """The tab's list (the gallery's is handed to the window as galleryModel)."""
        return self.model

    @Property(str, notify=folderChanged)
    def folderPath(self):
        return self._root

    @Property(str, notify=folderChanged)
    def folderName(self):
        return (Path(self._root).name or self._root) if self._root else ""

    @Property(bool, notify=folderChanged)
    def hasFolder(self):
        return bool(self._root)

    @Property(bool, notify=folderOpeningChanged)
    def openingFolder(self):
        return self._opening_path is not None

    @Property("QVariantList", notify=recentFoldersChanged)
    def recentFolders(self):
        return list(self._recent)

    @Property(bool, notify=folderChanged)
    def includeSubfolders(self):
        return self._include_subfolders

    @Property(bool, notify=shareLearningChanged)
    def shareLearning(self):
        return self._share_learning

    @Property(bool, notify=overlapChanged)
    def overlapsXLibrary(self):
        return self._overlaps

    @Property(bool, notify=overlapChanged)
    def checkingXOverlap(self):
        return self._checking_overlap

    @property
    def may_overlap_x_library(self):
        """While paths are checked, protect the files as if the folders overlap."""
        return self._overlaps or self._checking_overlap

    @Property(bool, notify=stateChanged)
    def scanningFolder(self):
        return self._exclusive == "folder"

    @Property(str, notify=folderChanged)
    def folderError(self):
        return self._folder_error

    @Property(str, notify=stateChanged)
    def folderOrder(self):
        return self._folder_order()

    @Property(str, notify=stateChanged)
    def folderMediaType(self):
        return self._media_type if self._media_type in _FOLDER_MEDIA_TYPES else "all"

    @Property(bool, notify=stateChanged)
    def busy(self):
        return bool(self._exclusive or self._busy or self._deletion_busy)

    # -- the folder -------------------------------------------------------------------------
    def restore_preferences(self, last, recent, subfolders, share):
        """What the settings remember. No folder opens by itself (see openLastFolder)."""
        self._last = last if isinstance(last, str) else ""
        self._recent = [str(item) for item in recent if isinstance(item, str) and item][:RECENT_LIMIT]
        self._include_subfolders = bool(subfolders)
        self._share_learning = bool(share)
        self._repository.share_learning = self._share_learning
        self.recentFoldersChanged.emit()
        self.shareLearningChanged.emit()
        self.folderChanged.emit()

    @Slot(result=bool)
    def openLastFolder(self):
        """When the tab first opens, request the last folder if none is open."""
        if self._root or not self._last or self._closed or self.openingFolder:
            return False
        last, self._last = self._last, ""          # asked once; a folder gone since says so once
        return self._request_open_folder(last)

    @Slot(str, result=bool)
    def openFolder(self, path):
        """Start opening a path (or a file URL) without waiting for the drive to respond."""
        if self._closed:
            return False
        if self._deletion_busy or self._exclusive:
            self._refuse_folder("진행 중인 작업이 끝난 뒤 다른 폴더를 열 수 있습니다.")
            return False
        self._last = ""
        text = QUrl(path).toLocalFile() if isinstance(path, str) and path.startswith("file:") else path
        return self._request_open_folder(text)

    def _request_open_folder(self, path):
        text = str(path) if isinstance(path, (str, Path)) else ""
        if text:
            norm = os.path.normcase(os.path.normpath(text))
            if norm in self._erasing_folders:
                self._refuse_folder("이전 정리 기록을 지우는 중입니다. 잠시 뒤 다시 열어 주세요.")
                return False
        if self._opening_path == text:
            return False
        self._folder_request += 1
        request = self._folder_request
        self._opening_path = text
        self.folderOpeningChanged.emit()

        def look():
            root, message = None, ""
            try:
                root = str(check_root(text))
            except LocalFolderError as exc:
                message = str(exc)
            except Exception:
                message = "폴더를 열지 못했습니다. 위치를 확인해 주세요."
            try:
                self._folderChecked.emit(request, root, message)
            except RuntimeError:
                pass  # The window may have closed while the drive was unavailable.

        thread = threading.Thread(target=look, name="qt-local-folder-open", daemon=True)
        self._track_thread(thread)
        thread.start()
        return True

    @Slot(int, object, str)
    def _finish_open_folder(self, request, root, message):
        if self._closed or request != self._folder_request or not self.openingFolder:
            return
        if root is not None:
            try:
                self._open_root(root)
            finally:
                if request == self._folder_request:
                    self._cancel_folder_open()
        else:
            self._cancel_folder_open()
            self._refuse_folder(message)

    def _cancel_folder_open(self):
        if self.openingFolder:
            self._opening_path = None
            self.folderOpeningChanged.emit()

    def _refuse_folder(self, message):
        self._folder_error = message
        self._status = message
        self.folderChanged.emit()
        self.changed.emit()

    def _open_root(self, root_text):
        """Open a folder check_root has accepted."""
        if os.path.normcase(os.path.normpath(root_text)) in self._erasing_folders:
            self._refuse_folder("이전 정리 기록을 지우는 중입니다. 잠시 뒤 다시 열어 주세요.")
            return False
        if self._exclusive or self._deletion_busy:
            self._status = "진행 중인 작업이 끝난 뒤 다른 폴더를 열 수 있습니다."
            self.changed.emit()
            return False
        self._folder_error = ""
        self._recent = [root_text] + [item for item in self._recent
                                      if os.path.normcase(item) != os.path.normcase(root_text)]
        del self._recent[RECENT_LIMIT:]
        self.recentFoldersChanged.emit()
        if os.path.normcase(root_text) != os.path.normcase(self._root):
            self._reset_filters_quietly()
        self._root = root_text
        self._check_overlap()
        self._repository.share_learning = self._share_learning
        self.setLibrary("folder", root_text, self._include_subfolders)
        self.folderChanged.emit()
        self.refreshFolder()
        return True

    def _reset_filters_quietly(self):
        """Another folder: its list starts unfiltered (the next refresh reads it)."""
        self._query = ""
        self._user_tag = ""
        self._ai_category = self._ai_origin = ""
        self._favorite_only = False
        self._duplicate_only = False
        self._media_type = "all"
        self.userTagFilterChanged.emit()
        self.favoriteFilterChanged.emit()
        self.duplicateFilterChanged.emit()

    @Slot(str, bool, result=bool)
    def forgetFolder(self, path, erase):
        """Take a folder off the recent list; with ``erase`` also delete its record.

        The record is what LuvBox keeps about the folder in the app data folder
        (tags, stars, "not duplicates" marks, features) and the folder's examples
        in the shared AI profile. The open folder's record is never deleted; the
        photos never are.
        """
        if self._closed or not isinstance(path, str) or not path:
            return False
        norm = os.path.normcase(os.path.normpath(path))
        if erase and self._root and norm == os.path.normcase(os.path.normpath(self._root)):
            self._status = "열려 있는 폴더의 기록은 지울 수 없습니다. 다른 폴더를 연 뒤 지워 주세요."
            self.changed.emit()
            return False
        self._recent = [item for item in self._recent if os.path.normcase(os.path.normpath(item)) != norm]
        if self.openingFolder and os.path.normcase(os.path.normpath(self._opening_path)) == norm:
            self._cancel_folder_open()
        if self._last and os.path.normcase(os.path.normpath(self._last)) == norm:
            self._last = ""
        self.recentFoldersChanged.emit()
        if not erase:
            self.folderForgotten.emit(path)
            return True
        if norm in self._erasing_folders:
            return False
        self._erasing_folders.add(norm)
        generation, serial = self._generation, self._selection_serial

        def erase_record():
            # A folder on a drive that sleeps can take long to name: away from the window.
            removed = False
            try:
                removed = forget_folder_record(path)
                profile = _open_profile()
                if profile is not None:
                    with closing(profile):
                        profile.remove_scope_examples("local", folder_key(path))
            except Exception:
                pass
            self._emit(generation, serial, "forget_folder", (path, removed), "", None)

        thread = threading.Thread(target=erase_record, name="qt-local-folder-forget", daemon=True)
        self._track_thread(thread)
        thread.start()
        return True

    @Slot()
    def closeFolder(self):
        if self._closed or self._exclusive or self._deletion_busy:
            return
        self._cancel_folder_open()
        if not self._root:
            return
        self._root = ""
        self._check_overlap()
        self.setLibrary("", "", False)
        self.folderChanged.emit()

    @Slot(bool)
    def setIncludeSubfolders(self, enabled):
        enabled = bool(enabled)
        if enabled == self._include_subfolders or self._exclusive or self._deletion_busy:
            return
        self._include_subfolders = enabled
        self.folderChanged.emit()
        if self._root:
            self.setLibrary("folder", self._root, enabled)
            self.refreshFolder()

    @Slot()
    def refreshFolder(self):
        """Read the folder again: new, changed and removed files; no photo is opened."""
        if not self._root or self._closed:
            return
        self._begin_exclusive("folder", "sync_folder", "폴더를 읽는 중…")

    @Slot(bool)
    def setShareLearning(self, enabled):
        if self._closed:
            return
        enabled = bool(enabled)
        if enabled == self._share_learning:
            return
        if self.filesLocked or self._deletion_busy:
            self._status = "진행 중인 작업이 끝난 뒤 학습 공유를 바꿀 수 있습니다."
            self.shareLearningChanged.emit()
            self.changed.emit()
            return
        self._share_learning = enabled
        self._repository.set_share_learning(enabled, self._generation)
        self.shareLearningChanged.emit()
        self._reload_ai_rules()
        self.classifyNewPhotos()

    def checkXOverlap(self, output_dirs):
        """Whether the open folder and an X save folder (any account's: a path or a list) are one inside the other."""
        folders = [output_dirs] if isinstance(output_dirs, (str, Path)) else list(output_dirs or ())
        self._x_folders = tuple(dict.fromkeys(str(folder) for folder in folders
                                             if isinstance(folder, (str, Path)) and str(folder)))
        self._check_overlap()

    def _check_overlap(self):
        inputs = (self._root, self._x_folders)
        same_inputs = inputs == self._overlap_inputs
        if self._closed or (same_inputs and self._checking_overlap):
            return
        self._overlap_inputs = inputs
        self._overlap_request += 1
        request = self._overlap_request
        root, folders = inputs
        if not root or not folders:
            self._set_overlap(False, False)
            return
        self._set_overlap(True, same_inputs and self._overlaps)

        def check():
            mine = Path(root)  # check_root already resolved the selected folder.
            overlaps = False
            for folder in folders:
                try:
                    theirs = Path(folder).expanduser().resolve()
                except (OSError, ValueError, RuntimeError):
                    continue
                if mine.is_relative_to(theirs) or theirs.is_relative_to(mine):
                    overlaps = True
                    break
            try:
                self._overlapChecked.emit(request, overlaps)
            except RuntimeError:
                pass  # The window may have closed while the drive was unavailable.

        thread = threading.Thread(target=check, name="qt-local-folder-overlap", daemon=True)
        self._track_thread(thread)
        thread.start()

    @Slot(int, bool)
    def _finish_overlap_check(self, request, overlaps):
        if not self._closed and request == self._overlap_request:
            self._set_overlap(False, overlaps)

    def _set_overlap(self, checking, overlaps):
        if (checking, overlaps) != (self._checking_overlap, self._overlaps):
            self._checking_overlap, self._overlaps = checking, overlaps
            self.overlapChanged.emit()

    @Slot(str)
    def setFolderOrder(self, order):
        if self._closed or order not in FOLDER_ORDERS or order == self._folder_order():
            return
        self._order = order
        self._refresh(reload_authors=False)

    @Slot(str)
    def setFolderMediaType(self, kind):
        if self._closed or kind not in _FOLDER_MEDIA_TYPES or kind == self.folderMediaType:
            return
        self._media_type = kind
        self.changed.emit()
        self._refresh(reload_authors=False)

    def _job_finished(self, action, selection, message):
        """The folder was read: the list shows what is there now, and new photos are classified."""
        if action == "forget_folder":
            path, removed = selection
            self._erasing_folders.discard(os.path.normcase(os.path.normpath(path)))
            self._status = ("최근 폴더에서 빼고 그 폴더의 정리 기록을 지웠습니다." if removed
                            else "최근 폴더에서 뺐습니다. 지울 정리 기록은 없었습니다.")
            self._reload_ai_rules()
            self.changed.emit()
            self.folderForgotten.emit(path)
            return True
        if action != "sync_folder":
            return False
        self._release_exclusive()
        if isinstance(selection, dict):
            parts = [f"사진 {selection.get('found', 0):,}장"]
            if selection.get("added"):
                parts.append(f"새 파일 {selection['added']:,}개")
            if selection.get("gone"):
                parts.append(f"없어진 파일 {selection['gone']:,}개")
            message = "폴더를 읽었습니다: " + " · ".join(parts)
        self._counts_loaded = False
        self._user_tags_loaded = False
        self._refresh(reload_authors=True, status=message, keep_place=True)
        if isinstance(selection, dict):
            self.classifyNewPhotos()
        return True
