"""Media destinations and account-specific local records.

Media may share an output folder; authentication-independent records never do.
Legacy databases are copied with SQLite's backup API, never opened for writing.

The live record databases are on this PC (records_root), never in the media
folder: cloud-sync tools upload and download SQLite files and their WAL
sidecars at different moments, which corrupted a catalog. After changes, a
verified snapshot of each database is placed in the media folder's
.luvbox/<account> as a complete file (no WAL), so records still travel
with the folder; a PC without local records starts from that snapshot.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import hashlib
import json
import shutil
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}
# The sync ledger belonged to the removed "new likes only" snapshot mode. It is
# still recognized and carried along unchanged so no user file is discarded,
# but it is never created, copied from legacy folders, or merged any more.
_LEGACY_SYNC = ".luvbox-sync.sqlite3"
_DATABASES = (".luvbox-history.sqlite3", ".luvbox-archive.sqlite3",
              ".luvbox-archive.date.sqlite3", ".luvbox-archive.flat.sqlite3",
              _LEGACY_SYNC)
# Compatibility inputs stay untouched; every new database uses the LuvBox name.
_OLD_DATABASES = {name: name.replace(".luvbox-", ".xlikesaver-", 1) for name in _DATABASES}
_RECORD_FOLDERS = (".luvbox", ".xlikesaver")
# Sizes and times of the snapshot files this PC last wrote or read; a
# snapshot that differs was written elsewhere and is merged in.
_SNAPSHOT_STATE = "snapshot.json"


@dataclass(frozen=True)
class StoragePaths:
    output_dir: Path
    media_dir: Path
    record_dir: Path
    legacy_dir: Path
    account: str
    separate_accounts: bool
    snapshot_dir: Path


class MigrationError(ValueError):
    """A local relocation failed; existing records remain authoritative."""


class _DamagedDatabase(MigrationError):
    """An integrity check positively identified a damaged database."""


@dataclass(frozen=True)
class MigrationResult:
    source_dir: Path
    destination_dir: Path
    databases: int
    media_records: int
    changed: bool


def _linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _legacy_source(path: Path, output_dir: Path) -> bool:
    """Reject links at every level before inspecting a legacy database."""
    current = path
    while current != output_dir:
        if not current.is_relative_to(output_dir) or _linked(current):
            return False
        current = current.parent
    return path.resolve().is_relative_to(output_dir) and path.is_file()


def _database_aliases(directory: Path, name: str) -> tuple[Path, ...]:
    return directory / name, directory / _OLD_DATABASES[name]


def _snapshot_source(paths: StoragePaths, name: str) -> Path | None:
    for folder in _RECORD_FOLDERS:
        directory = paths.output_dir / folder / paths.record_dir.name
        for source in _database_aliases(directory, name):
            if _legacy_source(source, paths.output_dir):
                return source
    return None


def _database_sources(paths: StoragePaths, name: str) -> tuple[Path, ...]:
    directories = [paths.record_dir]
    directories.extend(paths.output_dir / folder / paths.record_dir.name for folder in _RECORD_FOLDERS)
    directories.append(paths.legacy_dir)
    directories.extend(paths.legacy_dir / folder / paths.record_dir.name for folder in _RECORD_FOLDERS)
    return tuple(source for directory in directories for source in _database_aliases(directory, name))


def _copy_database_once(source: Path, destination: Path, *, shared_history: bool = False) -> bool:
    """Publish a SQLite backup without overwriting another process's database."""
    descriptor, filename = tempfile.mkstemp(prefix=".migrate-", suffix=".sqlite3", dir=destination.parent)
    os.close(descriptor)
    staged = Path(filename)
    try:
        _backup_database(source, staged)
        if shared_history:
            _sanitize_shared_history(staged)
        try:
            if os.name == "nt":
                staged.rename(destination)
            else:
                os.link(staged, destination)
            return True
        except FileExistsError:
            return False
    finally:
        staged.unlink(missing_ok=True)


def migrate_legacy_databases(directory: Path) -> None:
    """Read old local filenames before an empty replacement can be created."""
    directory = Path(directory)
    for name in _DATABASES:
        if name == _LEGACY_SYNC:
            continue  # Obsolete sync state is retained, never used by LuvBox.
        destination, source = _database_aliases(directory, name)
        if destination.exists():
            continue
        if _linked(source):
            raise MigrationError("이전 기록 파일이 다른 위치에 연결되어 있어 가져오지 못했습니다.")
        if source.exists():
            if not _legacy_source(source, directory):
                raise MigrationError(_MIGRATION_HELP)
            try:
                _copy_database_once(source, destination)
            except _DamagedDatabase as exc:
                # The records window offers repair only for the catalog itself.
                exc.database = name
                raise


def records_root() -> Path:
    """Where live record databases are kept: this PC's application data."""
    from .protection import state_directory
    return state_directory().parent / "records"


def _output_key(output: Path) -> str:
    """A readable, unique folder name for one media folder's records."""
    name = re.sub(r"[^\w.-]+", "_", output.name).strip("._")[:40] or "root"
    if name.lower() in _RESERVED:
        name = "_" + name
    digest = hashlib.sha256(os.path.normcase(str(output)).encode("utf-8")).hexdigest()[:12]
    return f"{name}-{digest}"


def resolve_storage(output_dir: str | Path, account: str, separate_accounts: bool = False) -> StoragePaths:
    """Resolve paths without creating folders or changing existing records."""
    account = account.strip().lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9_]{1,15}", account) or type(separate_accounts) is not bool:
        raise ValueError("X 아이디와 계정별 폴더 설정을 확인해 주세요.")
    output = Path(output_dir).expanduser().resolve()
    folder = ("_" if account in _RESERVED else "") + account
    legacy = output / folder
    snapshot = output / ".luvbox" / folder
    for record_folder in _RECORD_FOLDERS:
        for path in (output / record_folder, output / record_folder / folder):
            if _linked(path) or not path.resolve().is_relative_to(output):
                raise ValueError("기록 폴더가 다른 위치에 연결되어 있습니다. 다른 저장 경로를 선택해 주세요.")
    if separate_accounts and (_linked(legacy) or not legacy.resolve().is_relative_to(output)):
        raise ValueError("계정 폴더가 다른 위치에 연결되어 있습니다. 다른 저장 경로를 선택해 주세요.")
    root = records_root()
    if output.is_relative_to(root.resolve()) or root.resolve().is_relative_to(output / ".luvbox"):
        raise ValueError("앱의 기록 폴더 안에는 저장 경로를 지정할 수 없습니다. 다른 저장 경로를 선택해 주세요.")
    record = root / _output_key(output) / folder
    for path in (root, record.parent, record):
        if _linked(path):
            raise ValueError("이 PC의 기록 폴더가 다른 위치에 연결되어 있습니다. 앱 데이터 폴더를 확인해 주세요.")
    return StoragePaths(output, legacy if separate_accounts else output, record, legacy,
                        account, separate_accounts, snapshot)


def prepare_storage(paths: StoragePaths, *, protected: bool = False, create_media: bool = True) -> None:
    """Create storage and copy old account records once, preserving originals.

    Browsing passes ``create_media=False``: a moved or disconnected media
    folder must not be replaced by an empty one just because records opened.
    """
    # Resolve again immediately before filesystem writes to reject junctions
    # introduced after a settings preview was rendered.
    checked = resolve_storage(paths.output_dir, paths.account, paths.separate_accounts)
    _recover_relocation(checked, protected=protected)
    if create_media:
        checked.media_dir.mkdir(parents=True, exist_ok=True)
    checked.record_dir.mkdir(parents=True, exist_ok=True)
    migrate_legacy_databases(checked.record_dir)
    for name in _DATABASES:
        destination = checked.record_dir / name
        if _linked(destination) or not destination.resolve().is_relative_to(checked.record_dir):
            raise ValueError("기록 파일이 다른 위치에 연결되어 있습니다. 다른 저장 경로를 선택해 주세요.")
    legacy = not _linked(checked.legacy_dir) and checked.legacy_dir.resolve().is_relative_to(checked.output_dir)
    imported = {}
    for name in _DATABASES:
        if name == _LEGACY_SYNC:
            continue
        destination = checked.record_dir / name
        if destination.exists():
            continue
        # The media folder's snapshot first (records from earlier versions,
        # which kept them there live, or from another PC). Also recognize an
        # account folder previously selected as the output root; its
        # isolated ledgers can be one account level deeper.
        sources = _database_sources(checked, name)
        source = next((path for path in sources
                       if (path.parent == checked.record_dir and _legacy_source(path, checked.record_dir))
                       or (legacy or not path.is_relative_to(checked.legacy_dir))
                       and _legacy_source(path, checked.output_dir)), None)
        if source is None:
            continue
        copied = _copy_database_once(source, destination,
                                     shared_history=name == _HISTORY_DB and source.parent != checked.record_dir)
        if copied and source == _snapshot_source(checked, name):
            imported[name] = _file_state(source)
    if imported:
        state = _snapshot_state(checked)
        state.update(imported)
        _write_snapshot_state(checked, state)
    if protected:
        _merge_changed_snapshot(checked)


def saved_media_missing(paths: StoragePaths) -> bool:
    """Records were saved for this media folder, but the folder is gone.

    A folder moved or renamed in Explorer, or a drive that is not connected,
    looks exactly like every file having been deleted. Callers warn instead.
    """
    if paths.media_dir.is_dir():
        return False
    history, previous = _database_aliases(paths.record_dir, ".luvbox-history.sqlite3")
    if not history.exists() and not _linked(history):
        # This guard runs before storage preparation on the first LuvBox
        # launch. Inspect the old name without creating or migrating records.
        history = previous
    if _linked(history) or not history.is_file():
        return False
    try:
        with closing(sqlite3.connect(history.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            # Merged or relocated records can all belong to another account's
            # subfolder or a former output. They do not prove this media_dir
            # ever existed. If the whole output disappeared, include every
            # known location below it, including merged account subfolders.
            root = paths.media_dir if paths.output_dir.is_dir() else paths.output_dir
            columns = _columns(db, "media")
            locations = [name for name in ("path", "last_path") if name in columns]
            if not locations:
                return False
            for row in db.execute("SELECT " + ",".join(locations) + " FROM media"):
                for value in row:
                    if (isinstance(value, str) and value and Path(value).is_absolute()
                            and Path(os.path.abspath(value)).is_relative_to(root)):
                        return True
            return False
    except (OSError, sqlite3.Error, ValueError):
        return False


def missing_media_help(paths: StoragePaths) -> str:
    """Name the likely cause: a disconnected drive or a moved folder."""
    anchor = Path(paths.media_dir.anchor) if paths.media_dir.anchor else None
    if anchor is not None and not anchor.exists():
        return (f"저장 폴더가 있는 드라이브({paths.media_dir.drive or anchor})가 연결되어 있지 않습니다. "
                "외장·네트워크 드라이브를 연결한 뒤 다시 시도해 주세요.")
    return (f"저장 폴더를 찾을 수 없습니다: {paths.media_dir}\n"
            "폴더를 옮기거나 이름을 바꿨다면 메인 화면의 ‘저장 폴더’에서 새 위치를 골라 주세요.")


def _file_state(path: Path) -> list[int]:
    status = path.stat()
    return [status.st_size, status.st_mtime_ns]


def _snapshot_state(paths: StoragePaths) -> dict:
    location = paths.record_dir / _SNAPSHOT_STATE
    try:
        if _linked(location) or location.stat().st_size > 65536:
            return {}
        value = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = value.get("files") if isinstance(value, dict) else None
    return {name: state for name, state in files.items() if name in _DATABASES and isinstance(state, list)} \
        if isinstance(files, dict) else {}


def _write_snapshot_state(paths: StoragePaths, files: dict) -> None:
    location = paths.record_dir / _SNAPSHOT_STATE
    temporary = paths.record_dir / (".snapshot-" + uuid.uuid4().hex + ".json")
    try:
        temporary.write_text(json.dumps({"version": 1, "output": str(paths.output_dir), "files": files},
                                        ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, location)
    finally:
        temporary.unlink(missing_ok=True)


def _merge_changed_snapshot(paths: StoragePaths) -> set[str]:
    """Merge a snapshot that another PC wrote since this PC last saw it.

    Download records are combined; file records keep the better version.
    A damaged snapshot is skipped, never blocking a save. An unreadable or
    temporarily locked snapshot remains pending so publication cannot replace
    records that have not yet been copied into this PC's database.
    """
    state = _snapshot_state(paths)
    changed = False
    pending = set()
    for name in _DATABASES:
        if name == _LEGACY_SYNC:
            continue
        shared, local = _snapshot_source(paths, name), paths.record_dir / name
        if not local.is_file() or shared is None:
            continue
        current = _file_state(shared)
        if state.get(name) == current:
            continue
        incoming = paths.record_dir.parent / (".incoming-" + uuid.uuid4().hex + ".sqlite3")
        try:
            try:
                _backup_database(shared, incoming)
            except (MigrationError, OSError, sqlite3.Error, ValueError) as error:
                damaged = isinstance(error, _DamagedDatabase) or (
                    isinstance(error, sqlite3.DatabaseError) and
                    (getattr(error, "sqlite_errorcode", 0) or 0) & 0xFF in {11, 26})
                if not damaged:
                    pending.add(name)
                    continue
                # Only a confirmed corrupt source may be replaced without a
                # merge. An access error says nothing about the source's data.
            else:
                try:
                    _merge_database(name, local, incoming)
                except (MigrationError, OSError, sqlite3.Error, ValueError):
                    pending.add(name)
                    continue
            state[name] = current
            changed = True
        finally:
            incoming.unlink(missing_ok=True)
    if changed:
        _write_snapshot_state(paths, state)
    return pending


def _replace(source: Path, target: Path) -> None:
    """os.replace, retrying while a sync tool or scanner briefly holds the file."""
    for attempt in range(10):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.2)


def publish_snapshot(paths: StoragePaths) -> int:
    """Place verified copies of this PC's records in the media folder.

    Each database is copied with SQLite's backup API, checked, switched to a
    single-file journal and swapped in whole, so a sync tool only ever sees
    complete files. Returns how many databases were written.
    """
    checked = resolve_storage(paths.output_dir, paths.account, paths.separate_accounts)
    # A snapshot can arrive during a download, or a previous import can have
    # failed because another connection held a write lock. Merge it first; a
    # still-unreadable valid copy must not be silently overwritten.
    if _merge_changed_snapshot(checked):
        raise MigrationError("저장 폴더의 기록 사본을 아직 합치지 못해 그대로 보존했습니다. 다음 저장 때 다시 시도합니다.")
    snapshot = checked.snapshot_dir
    snapshot.mkdir(parents=True, exist_ok=True)
    if _linked(snapshot) or not snapshot.resolve().is_relative_to(checked.output_dir):
        raise MigrationError(_MIGRATION_HELP)
    state = _snapshot_state(checked)
    written = 0
    for name in _DATABASES:
        source = checked.record_dir / name
        if name == _LEGACY_SYNC or not source.is_file():
            continue
        target = snapshot / name
        if _linked(target):
            raise MigrationError(_MIGRATION_HELP)
        temporary = snapshot / (".snapshot-" + uuid.uuid4().hex + ".sqlite3")
        try:
            _backup_database(source, temporary)
            _replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        # Earlier versions used these as live WAL databases.
        for suffix in ("-wal", "-shm", "-journal"):
            leftover = snapshot / (name + suffix)
            if leftover.is_file() and not _linked(leftover):
                leftover.unlink(missing_ok=True)
        state[name] = _file_state(target)
        written += 1
    _write_snapshot_state(checked, state)
    return written


OUTPUT_BUSY_HELP = "이 저장 경로에서 다른 작업이 실행 중입니다. 먼저 실행한 작업을 종료해 주세요."
_MIGRATION_HELP = "기록을 옮기지 못했습니다. 이전 기록은 보존했습니다. 폴더 권한과 다른 실행 중인 작업을 확인해 주세요."
_HISTORY_DB = ".luvbox-history.sqlite3"


def _record_files(directory: Path) -> None:
    """Only application-owned regular database files may enter a directory swap."""
    if _linked(directory):
        raise MigrationError(_MIGRATION_HELP)
    if not directory.exists():
        return
    names = {name + suffix for name in (*_DATABASES, *_OLD_DATABASES.values())
             for suffix in ("", "-wal", "-shm", "-journal")} | {_SNAPSHOT_STATE}
    for child in directory.iterdir():
        if child.name not in names or _linked(child) or not child.is_file():
            raise MigrationError("기록 폴더에 다른 파일이 있어 자동으로 옮기지 못했습니다. 이전 기록은 보존했습니다.")


def _journal_path(paths: StoragePaths) -> Path:
    return paths.record_dir.parent / ("." + paths.record_dir.name + ".relocation.json")


def _owned_directory(parent: Path, name: str, prefix: str) -> Path:
    if not re.fullmatch(re.escape(prefix) + r"[a-f0-9]{32}", name):
        raise MigrationError(_MIGRATION_HELP)
    candidate = parent / name
    if _linked(candidate) or not candidate.resolve().is_relative_to(parent.resolve()):
        raise MigrationError(_MIGRATION_HELP)
    return candidate


def _remove_owned(directory: Path) -> None:
    if not directory.exists():
        return
    _record_files(directory)
    # The caller resolved this unique staging name inside the selected ledger
    # parent. Validation above excludes links and nested directories entirely.
    shutil.rmtree(directory)


def _recover_relocation(paths: StoragePaths, *, protected: bool = False) -> None:
    """Recover a crash between the two directory renames before opening SQLite."""
    journal = _journal_path(paths)
    if not journal.exists():
        return
    if not protected:
        # A history window in another process must not roll back a relocation
        # that is still running. This guard is released on process crashes.
        guard = OutputGuard(paths.output_dir)
        guard.acquire()
        try:
            _recover_relocation(paths, protected=True)
        finally:
            guard.close()
        return
    if _linked(journal) or journal.stat().st_size > 8192:
        raise MigrationError(_MIGRATION_HELP)
    try:
        value = json.loads(journal.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("invalid relocation journal")
        parent = paths.record_dir.parent
        stage = _owned_directory(parent, value["stage"], ".relocate-" + paths.record_dir.name + "-")
        backup = _owned_directory(parent, value["backup"], ".previous-" + paths.record_dir.name + "-")
        _record_files(paths.record_dir)
        _record_files(backup)
        _record_files(stage)
        if not paths.record_dir.exists() and backup.exists():
            backup.rename(paths.record_dir)
        # Destination present means either the old directory never moved, or
        # publication completed. Destination absent without a backup means no
        # previous directory existed and publication had not happened yet.
        _remove_owned(stage)
        _remove_owned(backup)
        journal.unlink()
    except (OSError, ValueError, KeyError, TypeError):
        raise MigrationError(_MIGRATION_HELP) from None


def _available_databases(paths: StoragePaths) -> dict[str, Path]:
    result = {}
    _record_files(paths.record_dir)
    for name in _DATABASES:
        current = paths.record_dir / name
        if current.is_file():
            result[name] = current
            continue
        candidates = _database_sources(paths, name)
        legacy = next((item for item in candidates
                       if _legacy_source(item, paths.record_dir if item.parent == paths.record_dir else paths.output_dir)), None)
        if legacy is not None:
            result[name] = legacy
    return result


def _merge_snapshot_copies(files: dict[str, Path], paths: StoragePaths, parent: Path):
    """Include records received from another PC without changing live ledgers.

    A folder change may happen before the next protected save imports a newly
    synced snapshot. Merge those records into private copies before publishing
    the destination, so an unreadable valid snapshot cannot be skipped after
    the active catalog has already changed.
    """
    state = _snapshot_state(paths)
    temporary = None
    merged = dict(files)
    try:
        for name, original in files.items():
            shared = _snapshot_source(paths, name)
            if name == _HISTORY_DB and original.parent != paths.record_dir:
                # A shared/legacy catalog carries file records, not approval
                # to manage arbitrary former roots on this PC. Work only on
                # a private copy; local catalogs retain their approved roots.
                if temporary is None:
                    temporary = tempfile.TemporaryDirectory(prefix=".snapshot-union-", dir=parent)
                copied = Path(temporary.name) / name
                _backup_database(original, copied)
                _sanitize_shared_history(copied)
                merged[name] = copied
            if (name == _LEGACY_SYNC or original == shared
                    or shared is None
                    or state.get(name) == _file_state(shared)):
                continue
            if temporary is None:
                temporary = tempfile.TemporaryDirectory(prefix=".snapshot-union-", dir=parent)
            folder = Path(temporary.name)
            incoming = folder / (name + ".incoming")
            try:
                try:
                    _backup_database(shared, incoming)
                except (MigrationError, sqlite3.Error) as error:
                    damaged = isinstance(error, _DamagedDatabase) or (
                        isinstance(error, sqlite3.DatabaseError) and
                        (getattr(error, "sqlite_errorcode", 0) or 0) & 0xFF in {11, 26})
                    if not damaged:
                        raise
                    continue  # only a confirmed corrupt shared copy may be skipped
                copied = folder / name
                if merged[name] != copied:
                    _backup_database(merged[name], copied)
                # _merge_history deliberately keeps the destination's local
                # root approvals and ignores the incoming root declaration.
                _merge_database(name, copied, incoming)
                merged[name] = copied
            finally:
                incoming.unlink(missing_ok=True)
        return temporary, merged
    except BaseException:
        if temporary is not None:
            temporary.cleanup()
        raise


def _integrity(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise _DamagedDatabase("기록 파일이 손상되어 옮기지 못했습니다. 이전 파일은 보존했습니다.")


def _backup_database(source: Path, destination: Path) -> None:
    deadline = time.monotonic() + 5

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise MigrationError("기록 파일이 사용 중이어서 옮기지 못했습니다. 다른 작업을 종료한 뒤 다시 시도해 주세요.")

    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)) as old:
        with closing(sqlite3.connect(destination, timeout=1)) as copied:
            old.backup(copied, pages=256, progress=progress, sleep=0.05)
            copied.execute("PRAGMA journal_mode=DELETE")
            _integrity(copied)


def _sanitize_shared_history(path: Path) -> None:
    """Remove shared root approvals and triggers from a private history copy.

    Only this PC's existing local catalog and an explicit folder relocation
    can approve former roots. Triggers are not part of LuvBox's schema; remove
    them before deleting the declaration so a supplied trigger cannot restore
    it during this import or a later catalog update. The source stays intact.
    """
    with closing(sqlite3.connect(path, timeout=1)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        metadata = db.execute("SELECT type FROM sqlite_master WHERE name='catalog_meta' COLLATE NOCASE").fetchone()
        if metadata is not None and metadata[0] != "table":
            raise MigrationError(_MIGRATION_HELP)
        triggers = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")]
        for name in triggers:
            db.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        if metadata is not None:
            db.execute("DELETE FROM catalog_meta WHERE key='allowed_media_roots'")
            if db.execute("SELECT 1 FROM catalog_meta WHERE key='allowed_media_roots' LIMIT 1").fetchone():
                raise MigrationError(_MIGRATION_HELP)


def _archive_only_catalog(files: dict[str, Path], paths: StoragePaths, parent: Path):
    """Recover file locations for installations that predate the media catalog."""
    if _HISTORY_DB in files or not any("archive" in name for name in files):
        return None, files
    from .history import HistoryStore

    temporary = tempfile.TemporaryDirectory(prefix=".catalog-", dir=parent)
    try:
        snapshot = Path(temporary.name)
        for name, original in files.items():
            if "archive" in name:
                _backup_database(original, snapshot / name)
        catalog = HistoryStore(snapshot, media_dir=paths.media_dir, output_dir=paths.output_dir,
                               legacy_dir=paths.legacy_dir)
        try:
            catalog.import_legacy()
        finally:
            catalog.close()
        result = dict(files)
        result[_HISTORY_DB] = snapshot / _HISTORY_DB
        return temporary, result
    except BaseException:
        temporary.cleanup()
        raise


def _tables(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in db.execute('PRAGMA table_info("' + table + '")')]


def _prepare_table(target: sqlite3.Connection, source: sqlite3.Connection, table: str) -> None:
    if table not in _tables(target):
        sql = source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
        target.execute(sql)
    target_columns = set(_columns(target, table))
    for row in source.execute('PRAGMA table_info("' + table + '")'):
        name, kind, default = row[1], row[2], row[4]
        if name not in target_columns:
            if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
                raise MigrationError(_MIGRATION_HELP)
            target.execute('ALTER TABLE "' + table + '" ADD COLUMN "' + name + '" ' + kind +
                           (" DEFAULT " + default if default is not None else ""))


def _rows(db: sqlite3.Connection, table: str) -> list[dict]:
    columns = _columns(db, table)
    return [dict(zip(columns, row)) for row in db.execute('SELECT * FROM "' + table + '"')]


def _insert(db: sqlite3.Connection, table: str, row: dict) -> int | None:
    columns = tuple(row)
    cursor = db.execute('INSERT INTO "' + table + '" (' + ','.join('"' + name + '"' for name in columns) +
                        ') VALUES (' + ','.join('?' for _ in columns) + ')', tuple(row.values()))
    return cursor.lastrowid


def _valid_roots(db: sqlite3.Connection | None, output: Path) -> list[str]:
    roots = [str(output)]
    if db is not None and "catalog_meta" in _tables(db):
        stored = db.execute("SELECT value FROM catalog_meta WHERE key='allowed_media_roots'").fetchone()
        if stored:
            value = json.loads(stored[0])
            if not isinstance(value, list) or len(value) > 1000:
                raise MigrationError(_MIGRATION_HELP)
            for item in value:
                if not isinstance(item, str) or not Path(item).is_absolute():
                    raise MigrationError(_MIGRATION_HELP)
                root = Path(item)
                if _linked(root) or str(root.resolve()) != str(root):
                    raise MigrationError(_MIGRATION_HELP)
                roots.append(str(root))
    return roots


def _media_score(row: dict) -> tuple:
    path = Path(row["path"]) if row.get("path") else None
    return (bool(path and path.is_file() and not _linked(path)), row.get("record_source") == "saved",
            row.get("posted_source") == "metadata", row.get("metadata_status") == "embedded",
            row.get("author_metadata_status") == "embedded",
            row.get("updated_at") or "")


def _merge_media(previous: dict, incoming: dict) -> dict:
    first, second = (incoming, previous) if _media_score(incoming) > _media_score(previous) else (previous, incoming)
    # Older catalogs timestamp changes to the second. On a tie, an explicit
    # removal must win over a stale path, regardless of merge direction. The
    # present-file check below still preserves a real restored/alternate copy.
    latest = max((previous, incoming), key=lambda row: (
        row.get("updated_at") or "", bool(row.get("last_path") and not row.get("path"))))
    if (latest.get("last_path") and not latest.get("path")
            and not any(_media_score(row)[0] for row in (previous, incoming))):
        # An intentionally removed file has no present path. A stale snapshot
        # must not refill that path and inflate the cached file statistics.
        first, second = latest, previous if latest is incoming else incoming
    result = dict(first)
    if "author_metadata_status" in first or "author_metadata_status" in second:
        # The verified status belongs to the chosen file, not another copy.
        result.setdefault("author_metadata_status", "pending")
    for key, value in second.items():
        if key in {"path", "filesize"} and first.get("last_path") and not first.get("path"):
            continue
        if key not in result or result[key] in (None, ""):
            result[key] = value
    if second.get("posted_source") == "metadata" and first.get("posted_source") != "metadata":
        result["posted_at"], result["posted_source"] = second.get("posted_at"), "metadata"
    if any(row.get("status") == "saved" for row in (first, second)):
        result["status"], result["record_source"] = "saved", "saved"
    identity = lambda row: (row.get("author") or "", row.get("author_id") or "")
    if first.get("path") == second.get("path"):
        if second.get("metadata_status") == "embedded":
            result["metadata_status"] = "embedded"
        if (second.get("author_metadata_status") == "embedded" and
                identity(second) == identity(result)):
            result["author_metadata_status"] = "embedded"
    if result.get("author_metadata_status") == "embedded":
        if identity(result) != identity(first):
            # Enriching catalog information does not embed it into this file.
            result["author_metadata_status"] = "pending"
    return result


def _merge_history(target: sqlite3.Connection, source: sqlite3.Connection) -> None:
    def quick_boundary(db):
        tables = _tables(db)
        stored = db.execute("SELECT value FROM catalog_meta WHERE key='quick_scan_merge_run'").fetchone() \
            if "catalog_meta" in tables else None
        try:
            watermark = int(stored[0]) if stored else -1
        except (ValueError, TypeError, OverflowError):
            watermark = -1
        if watermark < 0:
            return None, False
        latest = {}
        runs = _rows(db, "runs") if "runs" in tables else []
        for row in sorted(runs, key=lambda row: (row["started_at"], row["id"]), reverse=True):
            if row["status"] != "running" and row.get("scan_mode") != "retry":
                latest.setdefault(row["folder_mode"], row)
        pending = not latest or any(row["status"] != "finished" or row["id"] <= watermark
                                    for row in latest.values())
        return watermark, pending

    _, target_pending = quick_boundary(target)
    _, source_pending = quick_boundary(source)
    source_tables = _tables(source)
    if "media" in source_tables:
        _prepare_table(target, source, "media")
        primary = [row[1] for row in sorted(target.execute("PRAGMA table_info(media)"), key=lambda row: row[5]) if row[5]]
        if primary == ["folder_mode", "tweet_id", "num"]:
            target.execute("ALTER TABLE media RENAME TO media_old")
            target.execute("CREATE TABLE media AS SELECT * FROM media_old WHERE 0")
            target.execute("CREATE UNIQUE INDEX media_identity ON media(folder_mode,tweet_id,num,media_type)")
            target.execute("INSERT INTO media SELECT * FROM media_old")
            target.execute("DROP TABLE media_old")
        keys = ("folder_mode", "tweet_id", "num", "media_type")
        existing = {tuple(row[key] for key in keys): row for row in _rows(target, "media")}
        for incoming in _rows(source, "media"):
            identity = tuple(incoming[key] for key in keys)
            if identity in existing:
                merged = _merge_media(existing[identity], incoming)
                target.execute("DELETE FROM media WHERE folder_mode=? AND tweet_id=? AND num=? AND media_type=?", identity)
                _insert(target, "media", merged)
                existing[identity] = merged
            else:
                _insert(target, "media", incoming)
                existing[identity] = incoming
    if "runs" in source_tables:
        _prepare_table(target, source, "runs")
        previous = {(row["folder_mode"], row["started_at"]): row for row in _rows(target, "runs")}
        for incoming in _rows(source, "runs"):
            identity = (incoming["folder_mode"], incoming["started_at"])
            old = previous.get(identity)
            if old is None:
                incoming.pop("id", None)
                incoming["id"] = _insert(target, "runs", incoming)
                previous[identity] = incoming
                continue
            rank = lambda row: (bool(row.get("finished_at")), row.get("finished_at") or "",
                                sum(row.get(key, 0) for key in ("saved", "skipped", "size_skipped", "visited")))
            merged = dict(incoming if rank(incoming) > rank(old) else old)
            merged["id"] = old["id"]
            for key in ("saved", "skipped", "size_skipped", "visited"):
                merged[key] = max(old.get(key, 0), incoming.get(key, 0))
            target.execute("DELETE FROM runs WHERE id=?", (old["id"],))
            _insert(target, "runs", merged)
            previous[identity] = merged
    if "likes" in source_tables:
        # Like order keys are only comparable within one catalog; an existing
        # record at the destination keeps its own.
        _prepare_table(target, source, "likes")
        for row in _rows(source, "likes"):
            target.execute("INSERT OR IGNORE INTO likes(tweet_id,like_key,liked_after,liked_before) VALUES (?,?,?,?)",
                           (row["tweet_id"], row["like_key"], row["liked_after"], row["liked_before"]))
    if "tweets" in source_tables:
        _prepare_table(target, source, "tweets")
        for row in _rows(source, "tweets"):
            target.execute("INSERT OR IGNORE INTO tweets(tweet_id,content,hashtags,updated_at) VALUES (?,?,?,?)",
                           (row["tweet_id"], row["content"], row["hashtags"], row["updated_at"]))
    if "missing_choices" in source_tables:
        # Keep or download again: the later choice wins across PCs.
        _prepare_table(target, source, "missing_choices")
        for row in _rows(source, "missing_choices"):
            target.execute(
                "INSERT INTO missing_choices(tweet_id,num,media_type,ignored,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(tweet_id,num,media_type) DO UPDATE SET ignored=excluded.ignored,updated_at=excluded.updated_at "
                "WHERE excluded.updated_at>missing_choices.updated_at",
                (row["tweet_id"], row["num"], row["media_type"], row["ignored"], row["updated_at"]))
    if "deletion_items" in source_tables:
        _prepare_table(target, source, "deletion_items")
        previous = {row["id"]: row for row in _rows(target, "deletion_items")}
        rank = lambda row: (bool(row.get("finished_at")), row.get("finished_at") or row.get("created_at") or "")
        for row in _rows(source, "deletion_items"):
            old = previous.get(row["id"])
            if old is not None and rank(old) >= rank(row):
                continue
            if old is not None:
                target.execute("DELETE FROM deletion_items WHERE id=?", (row["id"],))
            _insert(target, "deletion_items", row)
            previous[row["id"]] = row
    if "catalog_meta" in source_tables:
        _prepare_table(target, source, "catalog_meta")
        for key, value in source.execute("SELECT key,value FROM catalog_meta WHERE key NOT IN ('allowed_media_roots','quick_scan_merge_run')"):
            if key == "review_pending":
                from .history import _review_pending_items
                # Same-account copies may have skipped different files. Keep
                # both sets reachable rather than letting one PC's quick scan
                # hide the other PC's choices behind a saved-file streak.
                previous = target.execute("SELECT value FROM catalog_meta WHERE key='review_pending'").fetchone()
                incoming = _review_pending_items(value)
                local = _review_pending_items(previous[0]) if previous else set()
                # Damaged memory cannot be interpreted as an empty set; let
                # the worker complete one full scan before repairing it.
                merged = None if incoming is None or local is None else sorted(incoming | local)
                target.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('review_pending',?)",
                               (json.dumps(merged, separators=(",", ":")),))
                continue
            target.execute("INSERT OR IGNORE INTO catalog_meta(key,value) VALUES (?,?)", (key, value))
    if target_pending or source_pending:
        _prepare_table(target, source, "catalog_meta")
        maximum = target.execute("SELECT COALESCE(MAX(id),0) FROM runs").fetchone()[0] \
            if "runs" in _tables(target) else 0
        # Appending an old finished run from another PC does not complete a
        # full scan that either catalog still required. Cleared source markers
        # have no pending work and must not be imported as foreign run IDs.
        target.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('quick_scan_merge_run',?)",
                       (str(maximum),))


def _merge_database(name: str, target_path: Path, source_path: Path) -> None:
    with closing(sqlite3.connect(target_path, timeout=1)) as target:
        with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, timeout=1)) as source:
            _integrity(source)
            with target:
                if "archive" in name:
                    if _columns(target, "archive") != ["entry"] or _columns(source, "archive") != ["entry"]:
                        raise MigrationError(_MIGRATION_HELP)
                    target.executemany("INSERT OR IGNORE INTO archive(entry) VALUES (?)", source.execute("SELECT entry FROM archive"))
                elif name == _HISTORY_DB:
                    _merge_history(target, source)
                else:
                    raise MigrationError(_MIGRATION_HELP)
            _integrity(target)


def _publish_records(paths: StoragePaths, stage: Path) -> None:
    parent = paths.record_dir.parent
    backup = parent / (".previous-" + paths.record_dir.name + "-" + uuid.uuid4().hex)
    journal = _journal_path(paths)
    if journal.exists() or _linked(journal):
        raise MigrationError(_MIGRATION_HELP)
    with journal.open("x", encoding="utf-8") as file:
        json.dump({"version": 1, "stage": stage.name, "backup": backup.name}, file)
        file.flush()
        os.fsync(file.fileno())
    try:
        if paths.record_dir.exists():
            paths.record_dir.rename(backup)
        stage.rename(paths.record_dir)
    except OSError:
        _recover_relocation(paths, protected=True)
        raise
    # Publication has succeeded. A cleanup failure must not tell the UI to
    # retain old settings; the journal will finish cleanup on the next open.
    try:
        _recover_relocation(paths, protected=True)
    except MigrationError:
        pass


def initialize_storage(paths: StoragePaths) -> None:
    """Prepare a chosen destination while excluding concurrent writers."""
    from .history import HistoryStore
    from .protection import AccountGuard

    account_guard = AccountGuard(paths.account, sleep=time.sleep)
    output_guard = OutputGuard(paths.output_dir)
    try:
        account_guard.acquire(respect_cooldown=False)
        output_guard.acquire()
        store = HistoryStore.for_storage(paths, protected=True)
        store.close()
    finally:
        output_guard.close()
        account_guard.close()


def migrate_records(source: StoragePaths, destination: StoragePaths) -> MigrationResult:
    """Copy/merge one account's records when its selected output changes.

    Media and original databases are never moved. Independent account/output
    locks exclude writers; read-only SQLite backups include committed WAL data.
    A staged directory swap publishes all ledgers together and recovers a crash
    before the swap finishes. No authentication or request state is copied.
    """
    from .protection import AccountGuard, ProtectionError

    source = resolve_storage(source.output_dir, source.account, source.separate_accounts)
    destination = resolve_storage(destination.output_dir, destination.account, destination.separate_accounts)
    if source.account != destination.account:
        raise MigrationError("다른 계정의 기록은 합칠 수 없습니다.")
    if source.record_dir == destination.record_dir:
        return MigrationResult(source.record_dir, destination.record_dir, 0, 0, False)
    if any(destination.output_dir.is_relative_to(source.output_dir / folder) or
           source.output_dir.is_relative_to(destination.output_dir / folder) for folder in _RECORD_FOLDERS):
        raise MigrationError("기록 폴더 안에는 저장 경로를 지정할 수 없습니다.")
    guards = []
    stage = None
    old_catalog = None
    target_catalog = None
    snapshot_copies = []
    try:
        account_guard = AccountGuard(source.account, sleep=time.sleep)
        account_guard.acquire(respect_cooldown=False)
        guards.append(account_guard)
        output_group = None
        for output in sorted({source.output_dir, destination.output_dir}, key=lambda item: os.path.normcase(str(item))):
            guard = OutputGuard(output, group=output_group)
            guard.acquire()
            guards.append(guard)
            output_group = guard if output_group is None else output_group
        if not source.output_dir.is_dir():
            raise MigrationError("이전 저장 폴더에 접근할 수 없어 기록을 옮기지 못했습니다. 연결과 폴더 위치를 확인해 주세요.")
        _recover_relocation(source, protected=True)
        _recover_relocation(destination, protected=True)
        old_files, target_files = _available_databases(source), _available_databases(destination)
        destination.record_dir.parent.mkdir(parents=True, exist_ok=True)
        old_snapshot, old_files = _merge_snapshot_copies(old_files, source, destination.record_dir.parent)
        if old_snapshot is not None:
            snapshot_copies.append(old_snapshot)
        target_snapshot, target_files = _merge_snapshot_copies(target_files, destination, destination.record_dir.parent)
        if target_snapshot is not None:
            snapshot_copies.append(target_snapshot)
        old_catalog, old_files = _archive_only_catalog(old_files, source, destination.record_dir.parent)
        target_catalog, target_files = _archive_only_catalog(target_files, destination, destination.record_dir.parent)
        stage = destination.record_dir.parent / (".relocate-" + destination.record_dir.name + "-" + uuid.uuid4().hex)
        stage.mkdir()
        roots = [str(source.output_dir), str(destination.output_dir)]
        for files, output in ((old_files, source.output_dir), (target_files, destination.output_dir)):
            history = files.get(_HISTORY_DB)
            if history is not None:
                with closing(sqlite3.connect(history.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                    roots.extend(_valid_roots(db, output))
        for name in _DATABASES:
            old, target = old_files.get(name), target_files.get(name)
            if target is not None:
                _backup_database(target, stage / name)
            elif old is not None:
                _backup_database(old, stage / name)
            if old is not None and target is not None and name != _LEGACY_SYNC:
                incoming = stage / (name + ".incoming")
                try:
                    _backup_database(old, incoming)
                    _merge_database(name, stage / name, incoming)
                finally:
                    incoming.unlink(missing_ok=True)
        history_path = stage / _HISTORY_DB
        with closing(sqlite3.connect(history_path, timeout=1)) as db:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS catalog_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                db.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('allowed_media_roots',?)",
                           (json.dumps(sorted(set(roots)), ensure_ascii=False),))
                # New current-root media must be considered even when a prior
                # root's importer had already marked its own scan complete.
                db.execute("DELETE FROM catalog_meta WHERE key='legacy_imported'")
            count = db.execute("SELECT COUNT(*) FROM media").fetchone()[0] if "media" in _tables(db) else 0
            _integrity(db)
        databases = sum((stage / name).is_file() for name in _DATABASES)
        _publish_records(destination, stage)
        stage = None
        try:
            publish_snapshot(destination)
        except (MigrationError, OSError, sqlite3.Error, ValueError):
            pass   # the next save writes the snapshot
        return MigrationResult(source.record_dir, destination.record_dir, databases, count, True)
    except (MigrationError, ProtectionError):
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
        raise MigrationError(_MIGRATION_HELP) from None
    finally:
        try:
            for catalog in (old_catalog, target_catalog, *snapshot_copies):
                if catalog is not None:
                    try:
                        catalog.cleanup()
                    except OSError:
                        pass
            if stage is not None and stage.exists() and not _journal_path(destination).exists():
                try:
                    # In-flight SQLite snapshots can leave sidecars on a failed
                    # backup; remove only this validated, private stage directory.
                    for child in stage.iterdir():
                        if _linked(child) or not child.is_file():
                            raise MigrationError(_MIGRATION_HELP)
                    shutil.rmtree(stage)
                except OSError:
                    pass
        finally:
            for guard in reversed(guards):
                guard.close()



_ACCOUNT_NAME = re.compile(r"[a-z0-9_]{1,15}")


def _account_folder(account: str) -> str:
    return ("_" if account in _RESERVED else "") + account


def _folder_account(name: str) -> str | None:
    account = name[1:] if name.startswith("_") and name[1:] in _RESERVED else name
    return account if _ACCOUNT_NAME.fullmatch(account) else None


def _account_ledgers(paths: StoragePaths, account: str) -> Path | None:
    """Another account's records for this same output: this PC first, then
    the copy in the output folder (records saved on another PC)."""
    folder = _account_folder(account)
    directories = (paths.record_dir.parent / folder,
                   *(paths.output_dir / name / folder for name in _RECORD_FOLDERS))
    for directory in directories:
        for history in _database_aliases(directory, _HISTORY_DB):
            parent = paths.record_dir.parent if directory.parent == paths.record_dir.parent else paths.output_dir
            if not _linked(directory) and _legacy_source(history, parent):
                return directory
    return None


def other_account_records(paths: StoragePaths) -> list[dict]:
    """Other X accounts whose saves went into this same output folder.

    Each account keeps its own records, so in a shared folder one account's
    library cannot tell another account's files apart from unrelated ones.
    """
    found = {}
    for parent in (paths.record_dir.parent, *(paths.output_dir / folder for folder in _RECORD_FOLDERS)):
        try:
            if _linked(parent) or not parent.is_dir():
                continue
            children = sorted(parent.iterdir())
        except OSError:
            continue
        for child in children:
            account = _folder_account(child.name)
            if account is None or account == paths.account or account in found:
                continue
            history = next((path for path in _database_aliases(child, _HISTORY_DB)
                            if _legacy_source(path, parent)), None)
            try:
                if _linked(child) or history is None:
                    continue
                with closing(sqlite3.connect(history.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                    records = db.execute("SELECT COUNT(*) FROM media").fetchone()[0]
            except (OSError, sqlite3.Error):
                continue
            if records:
                found[account] = records
    return [{"account": account, "records": records} for account, records in sorted(found.items())]


def _count(path: Path, table: str) -> int:
    if not path.is_file():
        return 0
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        return db.execute('SELECT COUNT(*) FROM "' + table + '"').fetchone()[0] if table in _tables(db) else 0


def merge_account_records(paths: StoragePaths, account: str) -> dict:
    """Combine one account's records; see merge_accounts_records."""
    return merge_accounts_records(paths, [account])


def merge_accounts_records(paths: StoragePaths, accounts) -> dict:
    """Combine selected accounts in one verified directory publication.

    File records, download keys (per folder layout, as for one account),
    hidden choices, tweet texts and old deletion receipts are combined, so
    this account's library manages their files too and does not download them
    again. Like timelines and runs stay with their accounts. Source local
    records and changed shared snapshots are united only in private copies;
    no source record or media file is changed. A copy of this
    account's records is kept first. The caller holds this account's and
    the output's locks. Every selected account must succeed before publication.
    """
    from .history import HistoryStore
    from .recovery import _catalog_backups

    checked = resolve_storage(paths.output_dir, paths.account, paths.separate_accounts)
    if accounts is None or isinstance(accounts, str):
        raise MigrationError("합칠 계정을 확인해 주세요.")
    selected = []
    for value in accounts:
        account = value.strip().lstrip("@").lower() if isinstance(value, str) else ""
        if not _ACCOUNT_NAME.fullmatch(account) or account == checked.account:
            raise MigrationError("합칠 계정을 확인해 주세요.")
        if account not in selected:
            selected.append(account)
    if not selected:
        raise MigrationError("합칠 계정을 확인해 주세요.")
    sources = []
    for account in selected:
        source = _account_ledgers(checked, account)
        if source is None or source == checked.record_dir:
            raise MigrationError("합칠 기록을 찾지 못했습니다. 기록 창을 새로고침한 뒤 다시 시도해 주세요.")
        sources.append(resolve_storage(checked.output_dir, account, checked.separate_accounts))
    _recover_relocation(checked, protected=True)
    _record_files(checked.record_dir)
    checked.record_dir.parent.mkdir(parents=True, exist_ok=True)
    target = checked.record_dir / _HISTORY_DB
    # Never replace an earlier pre-merge copy, even within the same second.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backups = _catalog_backups(checked.account, checked.record_dir)
    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / f"before-merge-{selected[0]}-{stamp}.sqlite3"
    if target.is_file():
        _backup_database(target, backup)
    result = {"records": 0, "added": 0, "keys": 0}
    stage = checked.record_dir.parent / (".relocate-" + checked.record_dir.name + "-" + uuid.uuid4().hex)
    snapshot_copies = []
    try:
        stage.mkdir()
        files = _available_databases(checked)
        copied, files = _merge_snapshot_copies(files, checked, checked.record_dir.parent)
        if copied is not None:
            snapshot_copies.append(copied)
        for name, original in files.items():
            _backup_database(original, stage / name)
        state = checked.record_dir / _SNAPSHOT_STATE
        if state.is_file():
            shutil.copy2(state, stage / state.name)
        staged_history = stage / _HISTORY_DB
        HistoryStore(stage, media_dir=checked.media_dir, output_dir=checked.output_dir,
                     legacy_dir=checked.legacy_dir).close()
        if not target.is_file():
            _backup_database(staged_history, backup)
        before_records = _count(staged_history, "media")
        with closing(sqlite3.connect(staged_history, timeout=1)) as db:
            roots = _valid_roots(db, checked.output_dir)
            stored = db.execute("SELECT value FROM catalog_meta WHERE key='merged_accounts'").fetchone()
            try:
                merged = json.loads(stored[0]) if stored else []
            except (ValueError, TypeError):
                merged = []
            merged = {name for name in merged if isinstance(name, str)} if isinstance(merged, list) else set()
        with tempfile.TemporaryDirectory(prefix=".account-merge-", dir=checked.record_dir.parent) as temporary:
            folder = Path(temporary)
            for source_paths in sources:
                files = _available_databases(source_paths)
                copied, files = _merge_snapshot_copies(files, source_paths, checked.record_dir.parent)
                if copied is not None:
                    snapshot_copies.append(copied)
                incoming = folder / _HISTORY_DB
                _backup_database(files[_HISTORY_DB], incoming)
                with closing(sqlite3.connect(incoming, timeout=1)) as db:
                    roots.extend(_valid_roots(db, checked.output_dir))
                    with db:
                        tables = _tables(db)
                        for table in ("likes", "runs", "catalog_meta"):
                            if table in tables:
                                db.execute('DELETE FROM "' + table + '"')
                    result["records"] += _count(incoming, "media")
                _merge_database(_HISTORY_DB, staged_history, incoming)
                incoming.unlink()
                merged.add(source_paths.account)
                for name, original in files.items():
                    if name in {_HISTORY_DB, _LEGACY_SYNC}:
                        continue
                    copy = folder / name
                    _backup_database(original, copy)
                    ledger = stage / name
                    before = _count(ledger, "archive")
                    if ledger.is_file():
                        _merge_database(name, ledger, copy)
                    else:
                        _backup_database(copy, ledger)
                    result["keys"] += _count(ledger, "archive") - before
                    copy.unlink()
        result["added"] = _count(staged_history, "media") - before_records
        with closing(sqlite3.connect(staged_history, timeout=1)) as db:
            with db:
                db.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('allowed_media_roots',?)",
                           (json.dumps(sorted(set(roots)), ensure_ascii=False),))
                db.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('merged_accounts',?)",
                           (json.dumps(sorted(merged)),))
                if result["added"] or result["keys"]:
                    run = db.execute("SELECT COALESCE(MAX(id),0) FROM runs").fetchone()[0]
                    db.execute("INSERT OR REPLACE INTO catalog_meta(key,value) VALUES ('quick_scan_merge_run',?)",
                               (str(run),))
            _integrity(db)
        _publish_records(checked, stage)
        stage = None
        return result
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        raise MigrationError(_MIGRATION_HELP) from None
    finally:
        for copied in snapshot_copies:
            try:
                copied.cleanup()
            except OSError:
                pass
        if stage is not None and stage.exists() and not _journal_path(checked).exists():
            try:
                if _linked(stage.parent):
                    raise MigrationError(_MIGRATION_HELP)
                _owned_directory(stage.parent, stage.name, ".relocate-" + checked.record_dir.name + "-")
                for child in stage.iterdir():
                    if _linked(child) or not child.is_file():
                        raise MigrationError(_MIGRATION_HELP)
                shutil.rmtree(stage)
            except OSError:
                pass


class OutputGuard:
    """Exclude overlapping roots while allowing independent sibling folders.

    The registry mutex is held only during acquisition. OS lifetime locks,
    rather than process IDs or stale metadata, identify active operations.
    Explicitly grouped guards belong to one job managing several old roots.
    """

    def __init__(self, output_dir: Path | str, *, group: OutputGuard | None = None):
        from .protection import state_directory
        self.output_dir = Path(output_dir).resolve()
        key = hashlib.sha256(os.path.normcase(str(self.output_dir)).encode("utf-8")).hexdigest()
        self.path = state_directory() / ("output-" + key + ".lock")
        self._registry = self.path.parent / "output-registry.lock"
        self._group = group._group if group is not None else uuid.uuid4().hex
        self._file = None

    @staticmethod
    def _open(path):
        # Append mode cannot update the metadata after locked byte zero.
        file = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "r+b")
        if file.seek(0, os.SEEK_END) == 0:
            file.write(b"\0")
            file.flush()
        file.seek(0)
        return file

    @staticmethod
    def _lock(file):
        file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _check_active_roots(self):
        from .protection import ProtectionError
        for path in self.path.parent.glob("output-*.lock"):
            if path == self.path or not re.fullmatch(r"output-[a-f0-9]{64}\.lock", path.name):
                continue
            with path.open("r+b") as other:
                try:
                    self._lock(other)
                except OSError as error:
                    if error.errno not in {11, 13, 35}:
                        raise
                else:
                    continue  # Inactive metadata cannot block future work.
                # Windows locks byte zero against reads too; metadata starts
                # after that byte so another process can inspect its owner.
                other.seek(1)
                try:
                    data = json.loads(other.read(131073).decode("utf-8"))
                    root = Path(data["output"])
                    group = data["group"]
                    if (data.get("version") != 1 or not root.is_absolute()
                            or not isinstance(group, str) or not re.fullmatch(r"[a-f0-9]{32}", group)):
                        raise ValueError
                except (ValueError, TypeError, KeyError, UnicodeError):
                    raise ProtectionError(OUTPUT_BUSY_HELP) from None
                if group != self._group and (root.is_relative_to(self.output_dir)
                                               or self.output_dir.is_relative_to(root)):
                    raise ProtectionError(OUTPUT_BUSY_HELP)

    def acquire(self) -> None:
        from .protection import ProtectionError
        file, registry = None, None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            registry = self._open(self._registry)
            deadline = time.monotonic() + 0.75
            while True:
                try:
                    self._lock(registry)
                    break
                except OSError as error:
                    if error.errno not in {11, 13, 35} or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            self._check_active_roots()
            file = self._open(self.path)
            self._lock(file)
            file.seek(1)
            file.write(json.dumps({"version": 1, "output": str(self.output_dir), "group": self._group}).encode("utf-8"))
            file.truncate()
            file.flush()
        except OSError as error:
            if file is not None:
                file.close()
            if error.errno in {11, 13, 35}:
                raise ProtectionError(OUTPUT_BUSY_HELP) from None
            raise ProtectionError("저장 경로의 작업 보호 기록을 만들지 못했습니다. Windows 사용자 폴더 권한을 확인해 주세요.") from None
        finally:
            if registry is not None:
                registry.close()
        self._file = file

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
