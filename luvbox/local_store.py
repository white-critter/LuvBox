"""A folder on this PC opened in the "폴더 정리" tab, and what LuvBox knows about it.

Nothing is written into the folder itself. Every list, hash, image feature, tag,
star and "not duplicates" mark of a folder lives in one SQLite file of the app
data folder (``local-folders/<folder key>.sqlite3``), independent of the X
records (``HistoryStore``): a file here is named by its path relative to the
folder (``rel_path``), never by a tweet.

A scan walks the folder (``os.scandir``, no recursion limit) and keeps what it
knew of a file whose size and modification time did not change: its hashes, its
feature and its AI tags. A file that is gone is kept as ``present = 0`` with its
star and tags, and comes back with them when it is restored. Links, junctions,
the app's own data and a few system folders are never entered; a file that is
only in the cloud (OneDrive placeholder) is listed but never opened.

Only the standard library (and ``storage``'s folder key) is used here: the
window lists a folder without loading numpy or onnxruntime.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading

from .clip_model import ACTIVE_MODEL_ID, embedding_bytes
from .settings import app_directory

PHOTO_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
GIF_SUFFIXES = frozenset({".gif"})
SUFFIXES = PHOTO_SUFFIXES | GIF_SUFFIXES
SKIPPED_FOLDERS = frozenset({".git", ".luvbox", ".xlikesaver", "$recycle.bin", "system volume information"})
TAG_LIMIT = 50
ORDERS = {
    "modified": "f.mtime_ns DESC, f.rel_path ASC",
    "name": "f.filename COLLATE NOCASE ASC, f.rel_path ASC",
    "size": "f.filesize DESC, f.rel_path ASC",
}
_KOREA = timezone(timedelta(hours=9))
_CLOUD_ATTRIBUTES = 0x00400000 | 0x00040000 | 0x00001000      # recall on access, recall on open, offline
# Name-surrogate reparse points (links, junctions, mount points) name another place. A cloud
# service's own kind (OneDrive, Dropbox) holds the file itself, also once it is downloaded,
# so only the first are links (as recycling._linked decides before a file is moved).
_NAME_SURROGATE = 0x20000000
_ERROR = "폴더 정보를 읽거나 저장하지 못했습니다. LuvBox 기록 폴더의 권한을 확인해 주세요."
_FOLDER_READ_ERROR = "폴더를 끝까지 읽지 못했습니다. 폴더의 연결과 읽기 권한을 확인한 뒤 새로고침해 주세요."
_WRITE_LOCK = threading.RLock()
_LABEL_CHUNK = 400


class LocalFolderError(RuntimeError):
    """A fixed, user-facing message; the cause may name a path."""


def _now() -> str:
    return datetime.now(_KOREA).isoformat(timespec="microseconds")


def normalize_tag(tag) -> str:
    if not isinstance(tag, str):
        return ""
    cleaned = "".join(char for char in tag.strip().lstrip("#") if ord(char) >= 32 and ord(char) != 127)
    return cleaned.strip()


def _is_link(details) -> bool:
    """A stat result (``lstat`` or a directory entry's) of a link or junction."""
    return bool(stat.S_ISLNK(details.st_mode) or getattr(details, "st_reparse_tag", 0) & _NAME_SURROGATE)


def _linked(path: Path) -> bool:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    return _is_link(details) or bool(getattr(path, "is_junction", lambda: False)())


def folder_key(root: Path | str) -> str:
    """The database name of a folder: readable, and unique for its full path."""
    from .storage import _output_key

    return _output_key(Path(root))


def check_root(path) -> Path:
    """The folder to open, resolved: an existing folder reached without any link or junction.

    The app's own data folder (and anything inside it) is refused.
    """
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise LocalFolderError("열 폴더를 골라 주세요.")
    try:
        candidate = Path(str(path).strip()).expanduser()
        if not candidate.is_absolute():
            raise LocalFolderError("폴더의 전체 경로를 골라 주세요.")
        for part in (candidate, *candidate.parents):
            if _linked(part):
                raise LocalFolderError("바로 가기나 다른 폴더로 연결된 폴더는 열 수 없습니다. 실제 폴더를 골라 주세요.")
        root = candidate.resolve(strict=True)
    except LocalFolderError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise LocalFolderError("폴더를 찾을 수 없습니다. 위치를 확인해 주세요.") from None
    try:
        is_dir = root.is_dir()
    except OSError:
        is_dir = False
    if not is_dir:
        raise LocalFolderError("폴더를 골라 주세요.")
    try:
        own = app_directory().resolve()
    except OSError:
        own = None
    if own is not None and (root == own or root.is_relative_to(own)):
        raise LocalFolderError("LuvBox가 기록을 보관하는 폴더는 열 수 없습니다.")
    return root


def store_path(root: Path) -> Path:
    return app_directory() / "local-folders" / f"{folder_key(root)}.sqlite3"


def forget_folder_record(root) -> bool:
    """Delete what LuvBox keeps about a folder (its database in the app data folder).

    The folder and its photos are not touched. A database reached through a
    link or junction, or anything that is not an ordinary file, is left alone.
    Returns whether anything was deleted.
    """
    try:
        path = store_path(Path(root))
        top = app_directory()
    except (OSError, ValueError):
        return False
    try:
        for part in (path.parent, *path.parent.parents):
            if _linked(part):
                return False
            if part == top:
                break
    except OSError:
        return False
    removed = False
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            details = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return removed
        if _is_link(details) or not stat.S_ISREG(details.st_mode):
            continue
        try:
            os.remove(candidate)
            removed = True
        except OSError:
            return removed
    return removed


def media_type_for(name: str) -> str:
    suffix = os.path.splitext(name)[1].lower()
    if suffix in PHOTO_SUFFIXES:
        return "photo"
    if suffix in GIF_SUFFIXES:
        return "animated_gif"
    return ""


class LocalFolderStore:
    """One opened folder's database. Every method keeps its own short transaction."""

    def __init__(self, root, *, path: Path | None = None):
        self.root = check_root(root)
        self._tx_lock = threading.RLock()
        self._folder_cache: dict | None = None
        try:
            self.path = Path(path) if path is not None else store_path(self.root)
            top = app_directory()
        except OSError as exc:
            raise LocalFolderError(_ERROR) from exc
        try:
            for part in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm"),
                         Path(str(self.path) + "-journal"), *self.path.parents):
                if _linked(part):
                    raise LocalFolderError(_ERROR)
                if part == top:
                    break
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and (not self.path.is_file() or self.path.stat().st_nlink > 1):
                raise LocalFolderError(_ERROR)
            self._db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        except LocalFolderError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise LocalFolderError(_ERROR) from exc
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA synchronous=FULL")
            # The window opens a folder's list for each small edit, often while a worker writes to
            # it: a list already made opens without writing anything, so it waits for no writer.
            if self._schema_version() != _SCHEMA_VERSION:
                # Made once, by one opener at a time: the list, the folder reader and the window
                # often open a new folder's list together, and two of them switching a new database
                # to WAL at once found it locked (the first list of a folder came back empty).
                with _WRITE_LOCK, self._tx_lock:
                    if self._schema_version() != _SCHEMA_VERSION:
                        self._db.execute("PRAGMA journal_mode=WAL")
                        with self._db:
                            self._db.execute("BEGIN IMMEDIATE")
                            for statement in _SCHEMA:
                                self._db.execute(statement)
                            self._db.execute("INSERT OR REPLACE INTO local_meta(key, value) VALUES ('root_path', ?)",
                                             (str(self.root),))
                            self._db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        except sqlite3.Error as exc:
            self._db.close()
            raise LocalFolderError(_ERROR) from exc

    def _schema_version(self) -> int:
        return int(self._db.execute("PRAGMA user_version").fetchone()[0])

    # -- lifecycle ---------------------------------------------------------------------------
    def close(self):
        try:
            self._db.close()
        except sqlite3.Error:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _read(self, statement, parameters=()) -> list[dict]:
        try:
            with self._tx_lock, closing(self._db.execute(statement, parameters)) as rows:
                return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise LocalFolderError(_ERROR) from exc

    def _read_snapshot(self, queries) -> list[list[dict]]:
        """Run multiple SELECT queries against a single consistent WAL snapshot."""
        results = []
        with self._tx_lock:
            started = not self._db.in_transaction
            try:
                if started:
                    self._db.execute("BEGIN DEFERRED")
                for statement, parameters in queries:
                    with closing(self._db.execute(statement, parameters)) as rows:
                        results.append([dict(row) for row in rows])
                if started and self._db.in_transaction:
                    self._db.rollback()
                return results
            except sqlite3.Error as exc:
                if started and self._db.in_transaction:
                    try:
                        self._db.rollback()
                    except sqlite3.Error:
                        pass
                raise LocalFolderError(_ERROR) from exc

    def _write(self):
        store = self

        class _Transaction:
            def __enter__(self):
                _WRITE_LOCK.acquire()
                store._tx_lock.acquire()
                try:
                    store._db.execute("BEGIN IMMEDIATE")
                except sqlite3.Error as exc:
                    store._tx_lock.release()
                    _WRITE_LOCK.release()
                    raise LocalFolderError(_ERROR) from exc
                return store._db

            def __exit__(self, kind, value, _trace):
                try:
                    if kind is None:
                        store._db.commit()
                    else:
                        store._db.rollback()
                except sqlite3.Error as exc:
                    if kind is None:
                        try:
                            store._db.rollback()
                        except sqlite3.Error:
                            pass
                        raise LocalFolderError(_ERROR) from exc
                finally:
                    store._tx_lock.release()
                    _WRITE_LOCK.release()
                if isinstance(value, sqlite3.Error):
                    raise LocalFolderError(_ERROR) from value
                return False

        return _Transaction()

    def meta(self, key, default=""):
        rows = self._read("SELECT value FROM local_meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def _meta_with(self, db, key, default=""):
        if db is None:
            return self.meta(key, default)
        row = db.execute("SELECT value FROM local_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row is not None else default

    def set_meta(self, key, value):
        with self._write() as db:
            db.execute("INSERT OR REPLACE INTO local_meta(key, value) VALUES (?,?)", (key, str(value)))

    # -- the folder -------------------------------------------------------------------------
    def path_of(self, rel_path: str) -> Path | None:
        """The file of a row, only inside the folder and never through a link."""
        if not isinstance(rel_path, str) or not rel_path or rel_path.startswith(("/", "\\")) or ".." in rel_path.split("/"):
            return None
        path = self.root.joinpath(*rel_path.split("/"))
        cache = getattr(self, "_folder_cache", None)
        if cache is None:
            try:
                for part in (path, *path.parents):
                    if part == self.root:
                        break
                    if _linked(part):
                        return None
                if not path.resolve().is_relative_to(self.root):
                    return None
            except (OSError, ValueError):
                return None
            return path
        try:
            parent = path.parent
            safe_parent = cache.get(parent)
            if safe_parent is None:
                safe_parent = True
                for part in (parent, *parent.parents):
                    if part == self.root:
                        break
                    if _linked(part):
                        safe_parent = False
                        break
                if safe_parent and not parent.resolve().is_relative_to(self.root):
                    safe_parent = False
                cache[parent] = safe_parent
            if not safe_parent or _linked(path) or not path.resolve().is_relative_to(self.root):
                return None
        except (OSError, ValueError):
            return None
        return path

    def is_media_path(self, path) -> bool:
        """For ``deletion.kept_copy`` / ``kept_alike``: a regular file inside this folder."""
        try:
            candidate = Path(os.path.abspath(str(path)))
            root_key = os.path.normcase(str(self.root))
            for part in (candidate, *candidate.parents):
                if _linked(part):
                    return False
                if os.path.normcase(str(part)) == root_key:
                    break
            rel = candidate.resolve().relative_to(self.root).as_posix()
        except (OSError, ValueError, TypeError):
            return False
        found = self.path_of(rel)
        return found is not None and found.is_file() and not _linked(found)

    def walk(self, include_subfolders: bool, check_cancel=None):
        """Every photo or GIF under the folder: ``{rel_path: (size, mtime_ns, cloud, media_type, name)}``."""
        try:
            own = app_directory().resolve()
        except OSError:
            own = None
        found = {}
        stack = [self.root]
        while stack:
            if check_cancel:
                check_cancel()
            folder = stack.pop()
            try:
                with os.scandir(folder) as entries:
                    for index, entry in enumerate(entries, 1):
                        if check_cancel and index % 128 == 0:
                            check_cancel()
                        try:
                            if entry.is_symlink() or entry.is_junction():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if not include_subfolders or entry.name.lower() in SKIPPED_FOLDERS:
                                    continue
                                path = Path(entry.path)
                                if _is_link(entry.stat(follow_symlinks=False)) or path == own:
                                    continue
                                stack.append(path)
                                continue
                            kind = media_type_for(entry.name)
                            if not kind or not entry.is_file(follow_symlinks=False):
                                continue
                            info = entry.stat(follow_symlinks=False)
                            if _is_link(info):
                                continue
                            cloud = bool(getattr(info, "st_file_attributes", 0) & _CLOUD_ATTRIBUTES)
                            rel = Path(entry.path).relative_to(self.root).as_posix()
                            found[rel] = (int(info.st_size), int(info.st_mtime_ns), cloud, kind, entry.name)
                        except FileNotFoundError:
                            continue                    # entry disappeared during the scan
            except OSError as exc:
                # An incomplete scan cannot establish which files were removed.
                raise LocalFolderError(_FOLDER_READ_ERROR) from exc
        return found

    @staticmethod
    def _invalidate_file_analysis(db, rel: str) -> None:
        db.execute("DELETE FROM local_ai_embeddings WHERE rel_path=?", (rel,))
        db.execute("DELETE FROM local_unreadable WHERE rel_path=?", (rel,))
        db.execute("DELETE FROM local_tags WHERE rel_path=? AND source='ai'", (rel,))

    @staticmethod
    def _prune_singleton_clusters(db) -> None:
        """Clear positive cluster_id when fewer than two present files remain and no distinct twin holds -cid."""
        db.execute(
            "UPDATE local_files SET cluster_id=0 WHERE present=1 AND cluster_id>0 "
            "AND cluster_id IN ("
            "SELECT cluster_id FROM local_files WHERE present=1 AND cluster_id>0 "
            "GROUP BY cluster_id HAVING COUNT(*) < 2) "
            "AND NOT EXISTS (SELECT 1 FROM local_files AS neg "
            "WHERE neg.present=1 AND neg.cluster_id = -local_files.cluster_id)"
        )

    def sync_folder(self, include_subfolders: bool = True, check_cancel=None) -> dict:
        """Bring the list up to date with the folder. Unchanged files keep everything known of them.

        Returns counts: found, added, changed, gone, restored.
        """
        found = self.walk(include_subfolders, check_cancel)
        if check_cancel:
            check_cancel()
        known = {row["rel_path"]: row for row in self._read(
            "SELECT rel_path, filesize, mtime_ns, present, cloud FROM local_files")}
        # Windows sees IMG_01.jpg and img_01.jpg as one file: a listed file missing only because its
        # name's case changed takes the new name, with its star, tags, answers, feature and marks.
        by_case = {}
        for rel in known:
            by_case.setdefault(os.path.normcase(rel), []).append(rel)
        renamed = {}
        claimed_olds = set()
        for rel in found:
            if rel in known:
                continue
            olds = [old for old in by_case.get(os.path.normcase(rel), ())
                    if old not in found and old not in claimed_olds]
            if len(olds) == 1:
                renamed[rel] = olds[0]
                claimed_olds.add(olds[0])
        now = _now()
        added = changed = gone = restored = 0
        with self._write() as db:
            if renamed:
                self._rename_rows(db, renamed)
                for rel, old in renamed.items():
                    known[rel] = known.pop(old)
            for rel, (size, mtime, cloud, kind, name) in found.items():
                old = known.get(rel)
                if old is None:
                    db.execute(
                        "INSERT INTO local_files(rel_path, filename, media_type, filesize, mtime_ns, cloud, present, "
                        "updated_at) VALUES (?,?,?,?,?,?,1,?)", (rel, name, kind, size, mtime, int(cloud), now))
                    added += 1
                    continue
                if old["filesize"] != size or old["mtime_ns"] != mtime:
                    # The bytes may differ: what was computed from them is computed again.
                    db.execute(
                        "UPDATE local_files SET filesize=?, mtime_ns=?, cloud=?, present=1, file_hash='', dhash='', "
                        "file_signature='', cluster_id=0, updated_at=? WHERE rel_path=?",
                        (size, mtime, int(cloud), now, rel))
                    self._invalidate_file_analysis(db, rel)
                    changed += 1
                elif not old["present"] or bool(old["cloud"]) != cloud:
                    db.execute("UPDATE local_files SET present=1, cloud=?, updated_at=? WHERE rel_path=?",
                               (int(cloud), now, rel))
                    restored += 0 if old["present"] else 1
            for rel, old in known.items():
                if rel not in found and old["present"]:
                    db.execute("UPDATE local_files SET present=0, cluster_id=0, updated_at=? WHERE rel_path=?",
                               (now, rel))
                    gone += 1
            if changed or gone:
                self._prune_singleton_clusters(db)
            db.execute("INSERT OR REPLACE INTO local_meta(key, value) VALUES ('include_subfolders', ?)",
                       ("1" if include_subfolders else "0",))
        return {"found": len(found), "added": added, "changed": changed, "gone": gone, "restored": restored}

    def _rename_rows(self, db, renamed):
        """``{new rel_path: old rel_path}``: everything kept under the old name moves to the new one."""
        for rel, old in renamed.items():
            db.execute("UPDATE local_files SET rel_path=?, filename=? WHERE rel_path=?",
                       (rel, rel.rsplit("/", 1)[-1], old))
            for table in ("local_tags", "local_ai_embeddings", "local_ai_feedback", "local_unreadable"):
                db.execute(f"UPDATE {table} SET rel_path=? WHERE rel_path=?", (rel, old))
        name = {old: rel for rel, old in renamed.items()}
        sets = [frozenset(name.get(member, member) for member in group) for group in self._distinct_sets()]
        marks = [(frozenset(name.get(member, member) for member in photos),
                  frozenset(name.get(member, member) for member in group)) for photos, group in self._photo_marks()]
        self._store_marks(db, sets, marks)

    def mark_gone(self, rel_paths) -> int:
        """Files sent to the Recycle Bin: no longer present, out of their group, everything else kept."""
        rels = [rel for rel in rel_paths if isinstance(rel, str)]
        if not rels:
            return 0
        now = _now()
        with self._write() as db:
            removed = sum(db.execute("UPDATE local_files SET present=0, cluster_id=0, updated_at=? WHERE rel_path=?",
                                     (now, rel)).rowcount for rel in rels)
            if removed:
                self._prune_singleton_clusters(db)
            return removed

    # -- listing ----------------------------------------------------------------------------
    def _filter(self, *, query="", favorite_only=False, user_tag="", ai_category="", duplicate_only=False,
                media_type="all"):
        clauses, params = ["f.present=1"], []
        if media_type in ("photo", "animated_gif"):
            clauses.append("f.media_type=?")
            params.append(media_type)
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("f.rel_path LIKE ? ESCAPE '\\'")
            params.append("%" + escaped + "%")
        if favorite_only:
            clauses.append("f.favorite=1")
        if user_tag:
            tag = normalize_tag(user_tag)
            clauses.append("EXISTS (SELECT 1 FROM local_tags t WHERE t.rel_path=f.rel_path AND t.source='user' "
                           "AND t.tag=?)")
            params.append(tag)
        if ai_category:
            tag = normalize_tag(ai_category)
            clauses.append("EXISTS (SELECT 1 FROM local_tags t WHERE t.rel_path=f.rel_path AND t.tag=? "
                           "AND t.source IN ('ai','confirmed','user'))")
            params.append(tag)
        if duplicate_only:
            clauses.append(
                "f.cluster_id>0 AND f.cluster_id IN ("
                "SELECT cluster_id FROM local_files WHERE present=1 AND cluster_id>0 "
                "GROUP BY cluster_id HAVING COUNT(*) >= 2)"
            )
        return " WHERE " + " AND ".join(clauses), params

    def records(self, offset=0, limit=200, *, order="modified", **filters) -> tuple[int, list[dict]]:
        where, params = self._filter(**filters)
        order_by = ORDERS.get(order, ORDERS["modified"])
        count_rows, rows = self._read_snapshot([
            ("SELECT COUNT(*) AS n FROM local_files f" + where, params),
            ("SELECT f.rowid AS rowid, f.* FROM local_files f" + where +
             f" ORDER BY {order_by} LIMIT ? OFFSET ?", (*params, int(limit), int(offset))),
        ])
        total = count_rows[0]["n"]
        return int(total), rows

    def duplicate_page(self, offset=0, limit=200, **filters) -> tuple[int, list[dict]]:
        """Present photos of the stored groups, each group together, the photo it keeps first.

        ``cluster_rank`` 1 is the photo kept ("남김"): a starred one first, then
        the largest file. ``hash_rank`` above 1 is a copy: a photo before it in
        the group has its very bytes. Ranks are over the whole group, whatever
        the filters show of it.
        """
        where, params = self._filter(**{**filters, "duplicate_only": True})
        ranked = ("SELECT f.rowid AS rowid, f.*, ROW_NUMBER() OVER (PARTITION BY f.cluster_id ORDER BY f.favorite DESC, "
                  "f.filesize DESC, f.rel_path ASC) AS cluster_rank, COUNT(*) OVER (PARTITION BY f.cluster_id) "
                  "AS cluster_size, ROW_NUMBER() OVER (PARTITION BY f.cluster_id, f.file_hash ORDER BY f.favorite DESC, "
                  "f.filesize DESC, f.rel_path ASC) AS hash_rank FROM local_files f WHERE f.present=1 AND f.cluster_id>0")
        shown = f"SELECT f.rel_path FROM local_files f{where}"
        count_rows, rows = self._read_snapshot([
            (f"SELECT COUNT(*) AS n FROM ({shown})", params),
            (f"SELECT * FROM ({ranked}) AS g WHERE g.cluster_size >= 2 AND g.rel_path IN ({shown}) "
             "ORDER BY g.cluster_id ASC, g.cluster_rank ASC LIMIT ? OFFSET ?", (*params, int(limit), int(offset))),
        ])
        total = count_rows[0]["n"]
        for row in rows:
            if not row.get("file_hash"):
                row["hash_rank"] = 1
        return int(total), rows

    def duplicate_members(self) -> dict[int, list[str]]:
        """``{group: [rel_path, ...]}``, the photo kept first."""
        groups: dict[int, list[str]] = {}
        for row in self._read(
                "SELECT rel_path, cluster_id FROM local_files WHERE present=1 AND cluster_id IN ("
                "SELECT cluster_id FROM local_files WHERE present=1 AND cluster_id>0 "
                "GROUP BY cluster_id HAVING COUNT(*) >= 2) "
                "ORDER BY cluster_id, favorite DESC, filesize DESC, rel_path"):
            groups.setdefault(int(row["cluster_id"]), []).append(row["rel_path"])
        return groups

    def duplicate_copies(self, **filters) -> list[dict]:
        """The copies of the shown groups: photos with the bytes of a photo before them."""
        _total, rows = self.duplicate_page(0, 1_000_000, **filters)
        return [row for row in rows if int(row["hash_rank"]) > 1]

    def duplicate_copy_rel_paths(self) -> list[str]:
        return [row["rel_path"] for row in self.duplicate_copies()]

    def row(self, rowid) -> dict | None:
        rows = self._read("SELECT rowid AS rowid, * FROM local_files WHERE rowid=?", (int(rowid),))
        return rows[0] if rows else None

    def row_by_rel(self, rel_path) -> dict | None:
        rows = self._read("SELECT rowid AS rowid, * FROM local_files WHERE rel_path=?", (rel_path,))
        return rows[0] if rows else None

    def rows_for(self, rowids) -> dict[int, dict]:
        found = {}
        ids = [int(item) for item in rowids]
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            for row in self._read(f"SELECT rowid AS rowid, * FROM local_files WHERE rowid IN ({marks})", chunk):
                found[int(row["rowid"])] = row
        return found

    def toggle_favorite(self, rowid) -> bool | None:
        with self._write() as db:
            row = db.execute("SELECT favorite FROM local_files WHERE rowid=?", (int(rowid),)).fetchone()
            if row is None:
                return None
            value = 0 if row["favorite"] else 1
            db.execute("UPDATE local_files SET favorite=? WHERE rowid=?", (value, int(rowid)))
        return bool(value)

    # -- tags -------------------------------------------------------------------------------
    def item_tags(self, rel_path) -> list[dict]:
        return [{"tag": row["tag"], "source": row["source"]} for row in self._read(
            "SELECT tag, source FROM local_tags WHERE rel_path=? AND tag!='' ORDER BY created_at ASC", (rel_path,))]

    def ai_tags_of(self, rel_path) -> list[dict]:
        return [{"tag": row["tag"], "source": row["source"], "confirmed": row["source"] == "confirmed"}
                for row in self._read("SELECT tag, source FROM local_tags WHERE rel_path=? AND tag!='' "
                                      "AND source IN ('ai','confirmed') ORDER BY source='ai', created_at ASC, tag ASC",
                                      (rel_path,))]

    def user_tags(self, limit=200) -> list[dict]:
        return [{"tag": row["tag"], "count": int(row["n"])} for row in self._read(
            "SELECT t.tag AS tag, COUNT(*) AS n FROM local_tags t JOIN local_files f ON f.rel_path=t.rel_path "
            "WHERE t.source='user' AND t.tag!='' AND f.present=1 GROUP BY t.tag ORDER BY n DESC, t.tag ASC LIMIT ?",
            (int(limit),))]

    def add_tag(self, rel_paths, tag) -> int:
        clean = normalize_tag(tag)
        rels = [rel for rel in rel_paths if isinstance(rel, str)]
        if not clean or len(clean) > TAG_LIMIT or not rels:
            return 0
        now = _now()
        with self._write() as db:
            for rel in rels:
                # Typing the tag the AI gave keeps the photo counted as looked at: without the
                # empty AI row the next run would put the AI's guess straight back once the
                # person took the tag away again (as HistoryStore._keep_looked_at).
                if (db.execute("SELECT 1 FROM local_tags WHERE rel_path=? AND tag=? AND source='ai'",
                               (rel, clean)).fetchone() is not None
                        and db.execute("SELECT 1 FROM local_tags WHERE rel_path=? AND source='ai' "
                                       "AND tag NOT IN ('', ?) LIMIT 1", (rel, clean)).fetchone() is None):
                    db.execute("INSERT INTO local_tags(rel_path, tag, source, created_at) VALUES (?,'','ai',?) "
                               "ON CONFLICT(rel_path, tag) DO UPDATE SET source='ai'", (rel, now))
                db.execute("INSERT INTO local_tags(rel_path, tag, source, created_at) VALUES (?,?,'user',?) "
                           "ON CONFLICT(rel_path, tag) DO UPDATE SET source='user'", (rel, clean, now))
                # A tag the person puts on a photo outweighs an earlier "not this".
                db.execute("DELETE FROM local_ai_feedback WHERE rel_path=? AND tag=? AND label<0", (rel, clean))
        return len(rels)

    def remove_tag_result(self, rel_path, tag) -> tuple[str, bool]:
        """Take a tag away; returns ``(source, confirmed_restored)``."""
        clean = normalize_tag(tag)
        if not clean:
            return "", False
        confirmed_restored = False
        with self._write() as db:
            row = db.execute("SELECT source FROM local_tags WHERE rel_path=? AND tag=?", (rel_path, clean)).fetchone()
            if row is None:
                return "", False
            source = row["source"]
            if source in ("ai", "confirmed"):
                self._apply_feedback(db, [rel_path], clean, -1, _now())
                return source, False
            db.execute("DELETE FROM local_tags WHERE rel_path=? AND tag=?", (rel_path, clean))
            taught = db.execute("SELECT label FROM local_ai_feedback WHERE rel_path=? AND tag=?",
                                (rel_path, clean)).fetchone()
            if taught is not None and taught["label"] > 0:
                db.execute("INSERT INTO local_tags(rel_path, tag, source, created_at) VALUES (?,?,'confirmed',?) "
                           "ON CONFLICT DO NOTHING", (rel_path, clean, _now()))
                confirmed_restored = True
        return source, confirmed_restored

    def remove_tag(self, rel_path, tag) -> str:
        """Take a tag away; returns the source it had ('' when there was none).

        An AI tag taken away is a "not this" for that tag, remembered and learned
        from. A tag the person typed simply goes (it may have been a typo).
        """
        return self.remove_tag_result(rel_path, tag)[0]

    @staticmethod
    def _apply_feedback(db, rels, tag, label, stamp):
        for rel in rels:
            db.execute("INSERT INTO local_ai_feedback(rel_path, tag, label, updated_at) VALUES (?,?,?,?) "
                       "ON CONFLICT(rel_path, tag) DO UPDATE SET label=excluded.label, updated_at=excluded.updated_at",
                       (rel, tag, label, stamp))
            if label > 0:
                db.execute("INSERT INTO local_tags(rel_path, tag, source, created_at) VALUES (?,?,'confirmed',?) "
                           "ON CONFLICT(rel_path, tag) DO UPDATE SET "
                           "source=CASE WHEN local_tags.source='user' THEN 'user' ELSE 'confirmed' END",
                           (rel, tag, stamp))
                continue
            db.execute("DELETE FROM local_tags WHERE rel_path=? AND tag=? AND source IN ('ai','confirmed')", (rel, tag))
            if db.execute("SELECT 1 FROM local_tags WHERE rel_path=? AND source='ai' AND tag!='' LIMIT 1",
                          (rel,)).fetchone() is None:
                db.execute("INSERT INTO local_tags(rel_path, tag, source, created_at) VALUES (?,'','ai',?) "
                           "ON CONFLICT(rel_path, tag) DO UPDATE SET source='ai'", (rel, stamp))

    def set_tag_feedback(self, rel_paths, tag, positive) -> int:
        clean = normalize_tag(tag)
        rels = [rel for rel in rel_paths if isinstance(rel, str)]
        if not clean or len(clean) > TAG_LIMIT or not rels:
            return 0
        with self._write() as db:
            self._apply_feedback(db, rels, clean, 1 if positive else -1, _now())
            db.execute("DELETE FROM local_meta WHERE key='ai_cleared'")
        return len(rels)

    def refuse_ai_tags(self, rel_paths, active_tags=None) -> int:
        rels = [rel for rel in rel_paths if isinstance(rel, str)]
        if not rels:
            return 0
        active = ({tag for tag in (normalize_tag(item) for item in active_tags) if tag}
                  if active_tags is not None else None)
        stamp = _now()
        seen_rels = set()
        with self._write() as db:
            for rel in rels:
                if rel in seen_rels:
                    continue
                seen_rels.add(rel)
                shown = sorted({row["tag"] for row in db.execute(
                    "SELECT tag FROM local_tags WHERE rel_path=? AND source IN ('ai','confirmed') AND tag!=''", (rel,))
                    if active is None or row["tag"] in active})
                if shown:
                    for tag in shown:
                        self._apply_feedback(db, [rel], tag, -1, stamp)
                else:
                    db.execute("INSERT INTO local_tags(rel_path, tag, source, created_at) VALUES (?,'','ai',?) "
                               "ON CONFLICT(rel_path, tag) DO UPDATE SET source='ai'", (rel, stamp))
            db.execute("DELETE FROM local_meta WHERE key='ai_cleared'")
        return len(rels)

    def tag_feedback(self, rel_paths=None) -> dict[str, dict[str, int]]:
        found: dict[str, dict[str, int]] = {}
        if rel_paths is None:
            rows = self._read("SELECT rel_path, tag, label FROM local_ai_feedback")
        else:
            rows = []
            rels = list(rel_paths)
            for start in range(0, len(rels), 400):
                chunk = rels[start:start + 400]
                marks = ",".join("?" for _ in chunk)
                rows += self._read(f"SELECT rel_path, tag, label FROM local_ai_feedback WHERE rel_path IN ({marks})",
                                   chunk)
        for row in rows:
            found.setdefault(row["rel_path"], {})[row["tag"]] = 1 if row["label"] > 0 else -1
        return found

    def clear_ai_tags(self) -> int:
        with self._write() as db:
            count = db.execute("SELECT COUNT(*) FROM local_tags WHERE source='ai' AND tag!=''").fetchone()[0]
            db.execute("DELETE FROM local_tags WHERE source='ai'")
            db.execute("DELETE FROM local_meta WHERE key IN ('ai_scored_rules_version','ai_scored_model_id')")
            db.execute("INSERT OR REPLACE INTO local_meta(key, value) VALUES ('ai_cleared', '1')")
        return int(count)

    def ai_cleared(self) -> bool:
        return self.meta("ai_cleared") == "1"

    def ai_tag_counts(self, tags) -> dict[str, int]:
        names = [tag for tag in dict.fromkeys(normalize_tag(tag) for tag in tags) if tag and len(tag) <= TAG_LIMIT]
        if not names:
            return {}
        marks = ",".join("?" for _ in names)
        return {row["tag"]: int(row["n"]) for row in self._read(
            "SELECT t.tag AS tag, COUNT(DISTINCT t.rel_path) AS n FROM local_tags t JOIN local_files f "
            f"ON f.rel_path=t.rel_path WHERE f.present=1 AND t.tag IN ({marks}) "
            "AND t.source IN ('ai','confirmed','user') GROUP BY t.tag", names)}

    # -- AI features (the same interface the X catalog offers the shared pipeline) -----------
    @staticmethod
    def row_identity(row) -> str:
        return row["rel_path"]

    def photos_without_embeddings(self, model_id=ACTIVE_MODEL_ID) -> list[dict]:
        return self._read(
            "SELECT rowid AS rowid, rel_path, filename, filesize, mtime_ns FROM local_files f "
            "WHERE f.present=1 AND f.cloud=0 AND f.media_type='photo' AND NOT EXISTS (SELECT 1 FROM local_ai_embeddings "
            "e WHERE e.rel_path=f.rel_path AND e.model_id=? AND LENGTH(e.embedding)=?) ORDER BY f.rowid",
            (model_id, embedding_bytes(model_id)))

    def _embedding_sql(self, unlabeled: bool) -> str:
        tags = ("AND NOT EXISTS (SELECT 1 FROM local_tags t WHERE t.rel_path=e.rel_path AND t.source='ai') "
                if unlabeled else "")
        return ("FROM local_ai_embeddings e JOIN local_files f ON f.rel_path=e.rel_path WHERE e.model_id=? "
                "AND LENGTH(e.embedding)=? AND f.present=1 AND f.media_type='photo' " + tags)

    def count_embeddings(self, model_id=ACTIVE_MODEL_ID, *, unlabeled=False) -> int:
        return int(self._read("SELECT COUNT(*) AS n " + self._embedding_sql(unlabeled),
                              (model_id, embedding_bytes(model_id)))[0]["n"])

    def iter_embeddings(self, model_id=ACTIVE_MODEL_ID, batch=512, *, unlabeled=False):
        last = 0
        while True:
            rows = self._read("SELECT f.rowid AS rowid, e.rel_path AS rel_path, e.embedding AS embedding " +
                              self._embedding_sql(unlabeled) + " AND f.rowid>? ORDER BY f.rowid LIMIT ?",
                              (model_id, embedding_bytes(model_id), last, max(1, min(int(batch), 1000))))
            if not rows:
                return
            last = int(rows[-1]["rowid"])
            yield rows

    def embeddings_for(self, rel_paths, model_id=ACTIVE_MODEL_ID) -> dict[str, bytes]:
        rels = list(rel_paths)
        found = {}
        for start in range(0, len(rels), 400):
            chunk = rels[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            for row in self._read(f"SELECT rel_path, embedding FROM local_ai_embeddings WHERE model_id=? "
                                  f"AND LENGTH(embedding)=? AND rel_path IN ({marks})",
                                  (model_id, embedding_bytes(model_id), *chunk)):
                found[row["rel_path"]] = bytes(row["embedding"])
        return found

    def save_embeddings(self, rows, model_id=ACTIVE_MODEL_ID) -> int:
        """rows are (rel_path, feature bytes, confidence)."""
        size = embedding_bytes(model_id)
        stored = [(rel, bytes(blob), confidence if isinstance(confidence, (int, float)) else None)
                  for rel, blob, confidence in rows if isinstance(blob, (bytes, bytearray)) and len(blob) == size]
        if not stored:
            return 0
        now = _now()
        with self._write() as db:
            db.executemany("INSERT INTO local_ai_embeddings(rel_path, model_id, embedding, confidence, updated_at) "
                           "VALUES (?,?,?,?,?) ON CONFLICT(rel_path) DO UPDATE SET model_id=excluded.model_id, "
                           "embedding=excluded.embedding, confidence=excluded.confidence, updated_at=excluded.updated_at",
                           [(rel, model_id, blob, confidence, now) for rel, blob, confidence in stored])
        return len(stored)

    def unreadable_signatures(self) -> dict[str, tuple[int, int]]:
        return {row["rel_path"]: (int(row["filesize"]), int(row["mtime_ns"]))
                for row in self._read("SELECT rel_path, filesize, mtime_ns FROM local_unreadable")}

    def remember_unreadable(self, rows) -> int:
        stored = [(rel, int(size), int(mtime), _now()) for rel, size, mtime in rows]
        if not stored:
            return 0
        with self._write() as db:
            db.executemany("INSERT INTO local_unreadable(rel_path, filesize, mtime_ns, updated_at) VALUES (?,?,?,?) "
                           "ON CONFLICT(rel_path) DO UPDATE SET filesize=excluded.filesize, mtime_ns=excluded.mtime_ns, "
                           "updated_at=excluded.updated_at", stored)
        return len(stored)

    def forget_unreadable(self, rel_paths) -> int:
        rels = list(rel_paths)
        if not rels:
            return 0
        with self._write() as db:
            return sum(db.execute("DELETE FROM local_unreadable WHERE rel_path=?", (rel,)).rowcount for rel in rels)

    def replace_ai_tags(self, rows) -> tuple[int, int]:
        """rows are (rel_path, tags). As ``HistoryStore.replace_ai_tags``: never over the person's decisions."""
        wanted = {}
        for rel, tags in rows:
            clean = []
            for tag in tags or ():
                tag = normalize_tag(tag)
                if tag and len(tag) <= TAG_LIMIT and tag not in clean:
                    clean.append(tag)
            wanted[rel] = clean
        changed = tagged = 0
        rels = list(wanted)
        for start in range(0, len(rels), _LABEL_CHUNK):
            chunk = rels[start:start + _LABEL_CHUNK]
            marks = ",".join("?" for _ in chunk)
            stamp = _now()
            with self._write() as db:
                decided: dict[str, set] = {}
                current: dict[str, set] = {}
                for row in db.execute(f"SELECT rel_path, tag FROM local_ai_feedback WHERE rel_path IN ({marks})", chunk):
                    decided.setdefault(row["rel_path"], set()).add(row["tag"])
                for row in db.execute(f"SELECT rel_path, tag, source FROM local_tags WHERE rel_path IN ({marks})", chunk):
                    if row["source"] == "ai":
                        current.setdefault(row["rel_path"], set()).add(row["tag"])
                    elif row["tag"]:
                        decided.setdefault(row["rel_path"], set()).add(row["tag"])
                updates = []
                for rel in chunk:
                    final = {tag for tag in wanted[rel] if tag not in decided.get(rel, set())} or {""}
                    if current.get(rel) != final:
                        updates.append((rel, final))
                for rel, final in updates:
                    db.execute("DELETE FROM local_tags WHERE rel_path=? AND source='ai'", (rel,))
                    for tag in sorted(final):
                        db.execute("INSERT INTO local_tags(rel_path, tag, source, created_at) VALUES (?,?,'ai',?) "
                                   "ON CONFLICT(rel_path, tag) DO UPDATE SET source='ai' WHERE local_tags.tag=''",
                                   (rel, tag, stamp))
                    changed += 1
                    tagged += final != {""}
                if any(final != {""} for _rel, final in updates):
                    db.execute("DELETE FROM local_meta WHERE key='ai_cleared'")
        return changed, tagged

    def store_ai_tags(self, pairs) -> tuple[int, int]:
        return self.replace_ai_tags(pairs)

    def save_features(self, triples) -> int:
        """triples are (rel_path, feature bytes, confidence): the shared pipeline's form."""
        return self.save_embeddings(triples)

    @staticmethod
    def feature_row(ident, blob) -> dict:
        return {"rel_path": ident, "embedding": blob}

    def remember_unreadable_idents(self, triples) -> int:
        return self.remember_unreadable(triples)

    def ai_examples(self, tags, model_id=ACTIVE_MODEL_ID) -> list[tuple]:
        """(rel_path, tag, label, feature): answers, and the person's own tags (a "yes").

        A photo sent to the Recycle Bin (or gone from the folder) keeps teaching with the
        feature it had: clearing a copy is no wrong answer. A photo whose bytes changed
        lost its feature (sync_folder) and with it its examples.
        """
        names = [tag for tag in dict.fromkeys(normalize_tag(tag) for tag in tags) if tag and len(tag) <= TAG_LIMIT]
        if not names:
            return []
        marks = ",".join("?" for _ in names)
        size = embedding_bytes(model_id)
        found = {}
        for row in self._read(
                "SELECT a.rel_path AS rel_path, a.tag AS tag, a.label AS label, e.embedding AS embedding "
                "FROM local_ai_feedback a JOIN local_files f ON f.rel_path=a.rel_path JOIN local_ai_embeddings e "
                f"ON e.rel_path=a.rel_path WHERE e.model_id=? AND LENGTH(e.embedding)=? "
                f"AND a.tag IN ({marks})", (model_id, size, *names)):
            found[(row["rel_path"], row["tag"])] = (row["rel_path"], row["tag"], 1 if row["label"] > 0 else -1,
                                                    bytes(row["embedding"]))
        for row in self._read(
                "SELECT t.rel_path AS rel_path, t.tag AS tag, e.embedding AS embedding FROM local_tags t "
                "JOIN local_files f ON f.rel_path=t.rel_path JOIN local_ai_embeddings e ON e.rel_path=t.rel_path "
                f"WHERE t.source='user' AND e.model_id=? AND LENGTH(e.embedding)=? AND t.tag IN ({marks})",
                (model_id, size, *names)):
            found[(row["rel_path"], row["tag"])] = (row["rel_path"], row["tag"], 1, bytes(row["embedding"]))
        return list(found.values())

    def taught_identities(self, tags) -> set:
        names = [tag for tag in dict.fromkeys(normalize_tag(tag) for tag in tags) if tag]
        found = {row["rel_path"] for row in self._read("SELECT DISTINCT rel_path FROM local_ai_feedback")}
        if names:
            marks = ",".join("?" for _ in names)
            found.update(row["rel_path"] for row in self._read(
                f"SELECT DISTINCT rel_path FROM local_tags WHERE source='user' AND tag IN ({marks})", names))
        return found

    def background_embeddings(self, tag, limit, model_id=ACTIVE_MODEL_ID) -> list[bytes]:
        clean = normalize_tag(tag)
        wanted = int(limit)
        if not clean or wanted <= 0:
            return []
        total = self.count_embeddings(model_id)
        if not total:
            return []
        base = ("SELECT e.embedding AS embedding " + self._embedding_sql(False) +
                "AND NOT EXISTS (SELECT 1 FROM local_tags t WHERE t.rel_path=e.rel_path AND t.tag=? "
                "AND t.source IN ('ai','confirmed','user')) "
                "AND NOT EXISTS (SELECT 1 FROM local_ai_feedback fb WHERE fb.rel_path=e.rel_path AND fb.tag=?) ")
        base_params = (model_id, embedding_bytes(model_id), clean, clean)
        step = max(1, total // max(1, wanted))
        rows = self._read(base + "AND (f.rowid % ?)=0 ORDER BY f.rowid LIMIT ?", (*base_params, step, wanted))
        if step > 1 and len(rows) < wanted:
            rows.extend(self._read(
                base + "AND (f.rowid % ?)!=0 ORDER BY f.rowid LIMIT ?",
                (*base_params, step, wanted - len(rows)),
            ))
        return [bytes(row["embedding"]) for row in rows]

    def ai_scored_state(self) -> tuple[str, str]:
        return self.meta("ai_scored_rules_version"), self.meta("ai_scored_model_id")

    def set_ai_scored_state(self, rules_version, model_id) -> None:
        with self._write() as db:
            db.execute("INSERT OR REPLACE INTO local_meta(key, value) VALUES ('ai_scored_rules_version', ?)",
                       (str(int(rules_version)),))
            db.execute("INSERT OR REPLACE INTO local_meta(key, value) VALUES ('ai_scored_model_id', ?)",
                       (str(model_id),))
            db.execute("DELETE FROM local_meta WHERE key='ai_cleared'")

    # -- duplicates -------------------------------------------------------------------------
    def photos_to_hash(self) -> list[dict]:
        """Present local photos whose hashes are not known for their current size and time."""
        return self._read("SELECT rowid AS rowid, rel_path, filesize, mtime_ns FROM local_files WHERE present=1 "
                          "AND cloud=0 AND media_type='photo' AND file_signature='' AND (file_hash='' OR dhash='')")

    def save_hashes(self, rows) -> int:
        """rows are (rel_path, file_hash, dhash, file_signature[, filesize, mtime_ns])."""
        plain, sized = [], []
        for row in rows:
            rel, file_hash, dhash, signature = row[:4]
            if len(row) >= 6:
                sized.append((file_hash, dhash, signature, int(row[4]), int(row[5]), rel))
            else:
                plain.append((file_hash, dhash, signature, rel))
        if not (plain or sized):
            return 0
        stamp = _now()
        with self._write() as db:
            if plain:
                db.executemany("UPDATE local_files SET file_hash=?, dhash=?, file_signature=? WHERE rel_path=?",
                               plain)
            changed = False
            for file_hash, _dhash, _signature, size, mtime, rel in sized:
                updated = db.execute(
                    "UPDATE local_files SET filesize=?, mtime_ns=?, cluster_id=0, updated_at=? "
                    "WHERE rel_path=? AND (filesize<>? OR mtime_ns<>? OR (file_hash<>'' AND file_hash<>?))",
                    (size, mtime, stamp, rel, size, mtime, file_hash))
                if updated.rowcount:
                    changed = True
                    self._invalidate_file_analysis(db, rel)
            if changed:
                self._prune_singleton_clusters(db)
            if sized:
                db.executemany("UPDATE local_files SET file_hash=?, dhash=?, file_signature=?, filesize=?, mtime_ns=? "
                               "WHERE rel_path=?", sized)
        return len(plain) + len(sized)

    def hashed_photos(self) -> list[dict]:
        return self._read("SELECT rowid AS rowid, rel_path, filesize, file_hash, dhash, file_signature, favorite "
                          "FROM local_files WHERE present=1 AND cloud=0 AND media_type='photo' AND file_hash!=''")

    def duplicate_rule(self) -> str:
        value = self.meta("duplicate_rule")
        return value if value in ("exact", "near") else ""

    def _distinct_sets(self, db=None) -> list[frozenset]:
        try:
            stored = json.loads(self._meta_with(db, "duplicate_distinct", "[]"))
        except (TypeError, ValueError):
            return []
        sets = []
        for group in stored if isinstance(stored, list) else ():
            members = frozenset(item for item in group if isinstance(item, str)) if isinstance(group, list) else frozenset()
            if len(members) >= 2 and members not in sets:
                sets.append(members)
        return sets

    def _photo_marks(self, db=None) -> list[tuple[frozenset, frozenset]]:
        try:
            stored = json.loads(self._meta_with(db, "duplicate_distinct_photos", "[]"))
        except (TypeError, ValueError):
            return []
        marks = []
        for entry in stored if isinstance(stored, list) else ():
            if not isinstance(entry, dict):
                continue
            group = frozenset(item for item in entry.get("group") or () if isinstance(item, str))
            photos = frozenset(item for item in entry.get("photos") or () if isinstance(item, str)) & group
            if photos and len(photos) < len(group) and (photos, group) not in marks:
                marks.append((photos, group))
        return marks

    @staticmethod
    def _distinct_with(sets, marks) -> list:
        pairs, seen = [], set(sets)
        for photos, group in marks:
            for photo in sorted(photos):
                for other in sorted(group):
                    pair = frozenset((photo, other))
                    if other != photo and pair not in seen:
                        seen.add(pair)
                        pairs.append(pair)
        return list(sets) + pairs

    @staticmethod
    def _store_marks(db, sets=None, marks=None):
        if sets is not None:
            if sets:
                db.execute("INSERT OR REPLACE INTO local_meta(key, value) VALUES ('duplicate_distinct', ?)",
                           (json.dumps([sorted(group) for group in sets], ensure_ascii=False),))
            else:
                db.execute("DELETE FROM local_meta WHERE key='duplicate_distinct'")
        if marks is not None:
            if marks:
                db.execute("INSERT OR REPLACE INTO local_meta(key, value) VALUES ('duplicate_distinct_photos', ?)",
                           (json.dumps([{"photos": sorted(photos), "group": sorted(group)} for photos, group in marks],
                                       ensure_ascii=False),))
            else:
                db.execute("DELETE FROM local_meta WHERE key='duplicate_distinct_photos'")

    def save_clusters(self, clusters, rule) -> int:
        """Store the groups of a scan (lists of rel_path), apart where the person said "not duplicates"."""
        from .duplicates import split_distinct

        distinct = self._distinct_with(self._distinct_sets(), self._photo_marks())
        marked = {member for group in distinct for member in group}
        updates, grouped, number = [], 0, 1
        for group in clusters:
            if len(group) < 2:
                continue
            parts = split_distinct(list(group), distinct) if marked.intersection(group) else [list(group)]
            base, placed = number, set()
            for part in parts:
                updates.extend((number, rel) for rel in part)
                placed.update(part)
                grouped += len(part)
                number += 1
            if not parts:
                number += 1
            updates.extend((-base, rel) for rel in group if rel not in placed)
        with self._write() as db:
            db.execute("UPDATE local_files SET cluster_id=0 WHERE cluster_id!=0")
            db.executemany("UPDATE local_files SET cluster_id=? WHERE rel_path=?", updates)
            db.execute("INSERT OR REPLACE INTO local_meta(key, value) VALUES ('duplicate_rule', ?)",
                       (rule if rule in ("exact", "near") else "exact",))
        return grouped

    def _group_members(self, number, db=None) -> list[str]:
        sql = ("SELECT rel_path FROM local_files WHERE cluster_id=? AND present=1 "
               "ORDER BY favorite DESC, filesize DESC, rel_path")
        rows = db.execute(sql, (number,)).fetchall() if db is not None else self._read(sql, (number,))
        return [row["rel_path"] for row in rows]

    def _regroup_numbers(self, db, numbers, sets) -> None:
        from .duplicates import split_distinct

        nums = sorted({abs(int(n)) for n in numbers if n})
        if not nums:
            return
        pool = nums + [-n for n in nums]
        marks = ",".join("?" for _ in pool)
        rows = db.execute(
            f"SELECT rel_path, cluster_id FROM local_files WHERE cluster_id IN ({marks}) AND present=1 "
            "ORDER BY favorite DESC, filesize DESC, rel_path",
            pool,
        ).fetchall()
        members = [row["rel_path"] for row in rows]
        if not members:
            return
        current = {row["rel_path"]: row["cluster_id"] for row in rows}
        base = nums[0]
        wanted, spare = {}, None
        for index, part in enumerate(split_distinct(members, sets)):
            if len(part) < 2:
                continue
            if index == 0:
                target = base
            else:
                if spare is None:
                    spare = db.execute("SELECT COALESCE(MAX(ABS(cluster_id)),0) FROM local_files").fetchone()[0]
                spare += 1
                target = spare
            wanted.update((member, target) for member in part)
        fallback = -base
        updates = [(wanted.get(member, fallback), member) for member in members
                   if current[member] != wanted.get(member, fallback)]
        if updates:
            db.executemany("UPDATE local_files SET cluster_id=? WHERE rel_path=?", updates)

    def _regroup(self, db, number, sets) -> None:
        self._regroup_numbers(db, [number], sets)

    def distinct_duplicate_count(self) -> int:
        return len(self._distinct_sets()) + len(self._photo_marks())

    def duplicate_count(self) -> int:
        return int(self._read(
            "SELECT COUNT(*) AS n FROM local_files WHERE present=1 AND cluster_id IN ("
            "SELECT cluster_id FROM local_files WHERE present=1 AND cluster_id>0 "
            "GROUP BY cluster_id HAVING COUNT(*) >= 2)")[0]["n"])

    def set_group_distinct(self, rel_path) -> list | None:
        with self._write() as db:
            row = db.execute("SELECT cluster_id FROM local_files WHERE rel_path=?", (rel_path,)).fetchone()
            if row is None or row["cluster_id"] <= 0:
                return None
            number = row["cluster_id"]
            members = self._group_members(number, db)
            if len(members) < 2:
                return None
            sets = self._distinct_sets(db)
            if frozenset(members) not in sets:
                sets.append(frozenset(members))
            self._store_marks(db, sets=sets)
            self._regroup_numbers(db, [number], self._distinct_with(sets, self._photo_marks(db)))
            return sorted(members)

    def set_photos_distinct(self, rel_paths) -> dict | None:
        chosen = sorted({rel for rel in rel_paths if isinstance(rel, str)})
        if not chosen:
            return None
        with self._write() as db:
            numbers = set()
            found = 0
            for start in range(0, len(chosen), 500):
                batch = chosen[start:start + 500]
                marks_sql = ",".join("?" for _ in batch)
                rows = db.execute(f"SELECT cluster_id FROM local_files WHERE rel_path IN ({marks_sql})", batch).fetchall()
                found += len(rows)
                for row in rows:
                    if row["cluster_id"] <= 0:
                        return None
                    numbers.add(row["cluster_id"])
            if found != len(chosen) or len(numbers) != 1:
                return None
            number = numbers.pop()
            members = self._group_members(number, db)
            chosen_set = set(chosen)
            photos = [member for member in members if member in chosen_set]
            if len(photos) != len(chosen_set) or len(photos) >= len(members):
                return None
            marks = self._photo_marks(db)
            mark = (frozenset(photos), frozenset(members))
            if mark not in marks:
                marks.append(mark)
            self._store_marks(db, marks=marks)
            self._regroup_numbers(db, [number], self._distinct_with(self._distinct_sets(db), marks))
            return {"photos": sorted(photos), "group": sorted(members)}

    def _numbers_of(self, db, members) -> set[int]:
        items = [member for member in members if isinstance(member, str)]
        if not items:
            return set()
        numbers = set()
        for start in range(0, len(items), 500):
            batch = items[start:start + 500]
            marks_sql = ",".join("?" for _ in batch)
            for row in db.execute(f"SELECT cluster_id FROM local_files WHERE rel_path IN ({marks_sql})", batch):
                if row[0]:
                    numbers.add(abs(row[0]))
        return numbers

    def undo_photos_distinct(self, mark) -> bool:
        try:
            target = (frozenset(mark["photos"]), frozenset(mark["group"]))
        except (TypeError, KeyError):
            return False
        with self._write() as db:
            marks = self._photo_marks(db)
            if target not in marks:
                return False
            marks.remove(target)
            self._store_marks(db, marks=marks)
            effective = self._distinct_with(self._distinct_sets(db), marks)
            numbers = self._numbers_of(db, target[1])
            if numbers:
                self._regroup_numbers(db, numbers, effective)
        return True

    def undo_group_distinct(self, members) -> bool:
        target = frozenset(item for item in members or () if isinstance(item, str))
        with self._write() as db:
            sets = self._distinct_sets(db)
            if target not in sets:
                return False
            sets.remove(target)
            self._store_marks(db, sets=sets)
            effective = self._distinct_with(sets, self._photo_marks(db))
            numbers = self._numbers_of(db, target)
            if numbers:
                self._regroup_numbers(db, numbers, effective)
        return True

    def clear_distinct(self) -> int:
        with self._write() as db:
            sets = self._distinct_sets(db)
            marks = self._photo_marks(db)
            count = len(sets) + len(marks)
            groups = list(sets) + [group for _photos, group in marks]
            components: list[set[int]] = []
            for group in groups:
                nums = self._numbers_of(db, group)
                if not nums:
                    continue
                merged = set(nums)
                kept = []
                for comp in components:
                    if comp & merged:
                        merged |= comp
                    else:
                        kept.append(comp)
                components = kept + [merged]
            for comp in components:
                self._regroup_numbers(db, comp, ())
            self._store_marks(db, sets=[], marks=[])
            db.execute("UPDATE local_files SET cluster_id=-cluster_id WHERE cluster_id<0")
            self._prune_singleton_clusters(db)
        return count


# Raised whenever _SCHEMA changes, so lists made before get the new tables once.
_SCHEMA_VERSION = 1
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS local_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS local_files ("
    " rel_path TEXT PRIMARY KEY, filename TEXT NOT NULL, media_type TEXT NOT NULL, filesize INTEGER NOT NULL,"
    " mtime_ns INTEGER NOT NULL, file_signature TEXT NOT NULL DEFAULT '', file_hash TEXT NOT NULL DEFAULT '',"
    " dhash TEXT NOT NULL DEFAULT '', cluster_id INTEGER NOT NULL DEFAULT 0, favorite INTEGER NOT NULL DEFAULT 0,"
    " present INTEGER NOT NULL DEFAULT 1, cloud INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS local_files_cluster ON local_files(cluster_id)",
    "CREATE INDEX IF NOT EXISTS local_files_present_mtime ON local_files(present, mtime_ns DESC, rel_path ASC)",
    "CREATE INDEX IF NOT EXISTS local_files_hash ON local_files(file_hash)",
    "CREATE TABLE IF NOT EXISTS local_tags (rel_path TEXT NOT NULL, tag TEXT NOT NULL, "
    " source TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL, PRIMARY KEY(rel_path, tag))",
    "CREATE INDEX IF NOT EXISTS local_tags_source_tag ON local_tags(source, tag)",
    "CREATE TABLE IF NOT EXISTS local_ai_embeddings (rel_path TEXT PRIMARY KEY, model_id TEXT NOT NULL,"
    " embedding BLOB NOT NULL, confidence REAL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS local_ai_feedback (rel_path TEXT NOT NULL, tag TEXT NOT NULL, label INTEGER NOT NULL,"
    " updated_at TEXT NOT NULL, PRIMARY KEY(rel_path, tag))",
    "CREATE TABLE IF NOT EXISTS local_unreadable (rel_path TEXT PRIMARY KEY, filesize INTEGER NOT NULL,"
    " mtime_ns INTEGER NOT NULL, updated_at TEXT NOT NULL)",
)
