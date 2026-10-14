"""Salvage rows from a damaged records database without changing it.

SQLite can refuse to read a table whose b-tree pages are out of order (for
example after a cloud-sync tool replaced the file under an open WAL). The rows
themselves are usually intact, so this walks each table's pages from its root
and decodes every cell directly, following the documented file format:
https://www.sqlite.org/fileformat2.html
"""

from __future__ import annotations

import os
import hashlib
import shutil
import sqlite3
import struct
from contextlib import closing
from datetime import datetime
from itertools import groupby
from pathlib import Path


HISTORY_NAME = ".luvbox-history.sqlite3"
_KEEP_BACKUPS = 7


class RecoveryError(RuntimeError):
    pass


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for index in range(8):
        byte = data[offset + index]
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, offset + index + 1
    return (value << 8) | data[offset + 8], offset + 9


def _signed(value: int, bits: int) -> int:
    return value - (1 << bits) if value >= 1 << (bits - 1) else value


def _record(payload: bytes) -> list:
    header_size, offset = _varint(payload, 0)
    if not offset <= header_size <= len(payload):
        raise ValueError("invalid record header")
    types = []
    while offset < header_size:
        serial, offset = _varint(payload, offset)
        if offset > header_size:
            raise ValueError("truncated serial type")
        types.append(serial)
    values, position = [], header_size
    for serial in types:
        if serial in (0, 8, 9):
            values.append(None if serial == 0 else serial - 8)
        elif 1 <= serial <= 6:
            size = (1, 2, 3, 4, 6, 8)[serial - 1]
            if position + size > len(payload):
                raise ValueError("truncated integer")
            values.append(_signed(int.from_bytes(payload[position:position + size], "big"), size * 8))
            position += size
        elif serial == 7:
            if position + 8 > len(payload):
                raise ValueError("truncated float")
            values.append(struct.unpack(">d", payload[position:position + 8])[0])
            position += 8
        elif serial >= 12:
            size = (serial - 12) // 2 if serial % 2 == 0 else (serial - 13) // 2
            raw = payload[position:position + size]
            if len(raw) != size:
                raise ValueError("truncated record")
            values.append(raw if serial % 2 == 0 else raw.decode("utf-8"))
            position += size
        else:
            raise ValueError("reserved serial type")
    return values


class _Pages:
    def __init__(self, path: Path):
        self.data = Path(path).read_bytes()
        if len(self.data) < 100 or not self.data.startswith(b"SQLite format 3\x00"):
            raise RecoveryError("not a SQLite database")
        size = int.from_bytes(self.data[16:18], "big")
        self.size = 65536 if size == 1 else size
        self.usable = self.size - self.data[20]
        if int.from_bytes(self.data[56:60], "big") not in (0, 1):
            raise RecoveryError("only UTF-8 databases are supported")
        self.count = len(self.data) // self.size

    def page(self, number: int) -> bytes:
        if not 1 <= number <= self.count:
            raise ValueError("page out of range")
        return self.data[(number - 1) * self.size:number * self.size]

    def payload(self, page: bytes, offset: int, size: int) -> bytes:
        """A table-leaf cell's payload, following overflow pages."""
        usable = self.usable
        local_max = usable - 35
        if size <= local_max:
            return page[offset:offset + size]
        minimum = (usable - 12) * 32 // 255 - 23
        local = minimum + (size - minimum) % (usable - 4)
        if local > local_max:
            local = minimum
        parts = [page[offset:offset + local]]
        remaining = size - local
        following = int.from_bytes(page[offset + local:offset + local + 4], "big")
        seen = set()
        while remaining > 0 and following and following not in seen:
            seen.add(following)
            overflow = self.page(following)
            chunk = overflow[4:4 + min(remaining, usable - 4)]
            parts.append(chunk)
            remaining -= len(chunk)
            following = int.from_bytes(overflow[:4], "big")
        if remaining > 0:
            raise ValueError("overflow chain is incomplete")
        return b"".join(parts)

    def rows(self, root: int):
        """(rowid, values) of every readable cell under a table b-tree root."""
        pending, seen = [root], set()
        while pending:
            number = pending.pop()
            if number in seen:
                continue
            seen.add(number)
            try:
                page = self.page(number)
            except ValueError:
                continue
            start = 100 if number == 1 else 0
            kind = page[start]
            cells = int.from_bytes(page[start + 3:start + 5], "big")
            header = start + (12 if kind == 0x05 else 8)
            if kind == 0x05:
                pending.append(int.from_bytes(page[start + 8:start + 12], "big"))
            elif kind != 0x0D:
                continue
            for index in range(cells):
                try:
                    offset = int.from_bytes(page[header + 2 * index:header + 2 * index + 2], "big")
                    if kind == 0x05:
                        pending.append(int.from_bytes(page[offset:offset + 4], "big"))
                        continue
                    size, offset = _varint(page, offset)
                    rowid, offset = _varint(page, offset)
                    yield rowid, _record(self.payload(page, offset, size))
                except (ValueError, IndexError, UnicodeDecodeError):
                    continue


def salvage(path: Path | str) -> dict:
    """Read every table of a (possibly damaged) database.

    Returns {table: {"columns": [...], "rows": [(rowid, {column: value})]}}.
    Tables SQLite can still read are read normally; damaged ones by pages.
    """
    path = Path(path)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        schema = connection.execute(
            "SELECT name,rootpage FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        result = {}
        pages = None
        for name, root in schema:
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')]
            primary = [row[1] for row in connection.execute(f'PRAGMA table_info("{name}")') if row[5] == 1 and row[2].upper() == "INTEGER"]
            try:
                rows = [(row[0], dict(zip(columns, row[1:])))
                        for row in connection.execute(f'SELECT rowid,* FROM "{name}"')]
                method = "sql"
            except sqlite3.DatabaseError:
                pages = pages or _Pages(path)
                rows = []
                for rowid, values in pages.rows(root):
                    if len(values) > len(columns):
                        continue
                    record = dict(zip(columns, values))
                    # An INTEGER PRIMARY KEY is stored as NULL; it is the rowid.
                    for key in primary:
                        if record.get(key) is None:
                            record[key] = rowid
                    rows.append((rowid, record))
                method = "pages"
            result[name] = {"columns": columns, "rows": rows, "method": method}
        return result
    finally:
        connection.close()


def _integrity(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()


def rebuild_history(damaged: Path | str, destination: Path | str, *, fallback: Path | str | None = None) -> dict:
    """Write a new, verified records database at ``destination``.

    Rows come from ``damaged`` (by pages where needed); rows only found in an
    earlier backup (``fallback``) are added. Each file record keeps its most
    recent version (highest rowid). ``destination`` must not exist yet.
    """
    from .history import HistoryStore

    damaged, destination = Path(damaged), Path(destination)
    if destination.exists():
        raise RecoveryError("destination exists")
    sources = [salvage(damaged)]
    if fallback is not None and Path(fallback).is_file():
        try:
            sources.append(salvage(Path(fallback)))
        except (RecoveryError, sqlite3.Error, OSError):
            pass
    workspace = destination.parent / (destination.name + ".build")
    workspace.mkdir()
    try:
        HistoryStore(workspace).close()
        built = workspace / ".luvbox-history.sqlite3"
        counts = {}
        connection = sqlite3.connect(built)
        try:
            with connection:
                for table in ("media", "runs", "catalog_meta", "likes", "tweets", "missing_choices", "deletion_items"):
                    target = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
                    rows = []
                    for source in sources:
                        found = source.get(table)
                        if not found:
                            continue
                        records = found["rows"]
                        if table == "media":
                            latest = {}
                            for rowid, row in records:
                                identity = (row.get("folder_mode"), row.get("tweet_id"), row.get("num"), row.get("media_type"))
                                if identity not in latest or rowid > latest[identity][0]:
                                    latest[identity] = (rowid, row)
                            records = sorted(latest.values(), key=lambda item: item[0])
                        rows.extend(row for _, row in records)
                    if table == "deletion_items":
                        # UUIDs remain stable across folder snapshots. Prefer
                        # a finished/newer receipt to an older pending intent.
                        latest = {}
                        for row in rows:
                            ident = row.get("id")
                            if not isinstance(ident, str) or not ident:
                                continue
                            rank = lambda item: (bool(item.get("finished_at")),
                                                 item.get("finished_at") or item.get("created_at") or "")
                            if ident not in latest or rank(row) > rank(latest[ident]):
                                latest[ident] = row
                        rows = list(latest.values())
                    if not rows:
                        counts[table] = 0
                        continue
                    before = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    # Old backups (and page-salvaged rows predating ALTER
                    # TABLE) omit newer columns. Let SQLite apply defaults;
                    # an explicit corrupt NULL must still fail constraints.
                    # Batch consecutive equal shapes to retain source order:
                    # current rows win over duplicate fallback rows.
                    for columns, batch in groupby(rows, lambda row: tuple(name for name in target if name in row)):
                        if not columns:
                            continue
                        statement = (f'INSERT OR IGNORE INTO "{table}" (' + ",".join(f'"{name}"' for name in columns) +
                                     ") VALUES (" + ",".join("?" for _ in columns) + ")")
                        connection.executemany(statement, ([row[name] for name in columns] for row in batch))
                    counts[table] = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] - before
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
        if _integrity(built) != "ok":
            raise RecoveryError("rebuilt database failed its integrity check")
        built.replace(destination)
        return {"counts": counts, "methods": {name: table["method"] for name, table in sources[0].items()}}
    finally:
        for leftover in sorted(workspace.glob("*"), reverse=True):
            leftover.unlink(missing_ok=True)
        workspace.rmdir()


def backup_directory() -> Path:
    """Backups live on this PC, outside any synced media folder."""
    from .protection import state_directory
    return state_directory().parent / "backups"


def _catalog_backups(account: str, record_dir: Path | str | None = None) -> Path:
    folder = backup_directory() / "catalog" / account
    if record_dir is None:
        return folder  # Retain access to old, unscoped backups.
    identity = os.path.normcase(str(Path(record_dir).expanduser().resolve()))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return folder / ("catalog-" + digest)


def latest_backup(account: str, record_dir: Path | str | None = None) -> Path | None:
    folders = [_catalog_backups(account, record_dir)]
    if record_dir is not None:
        # Renaming the app-data root preserves backup folders, but their scope
        # hash was computed from the original absolute record directory.
        app = backup_directory().parent
        current = Path(record_dir).expanduser().resolve()
        if app.name.lower() == "luvbox" and current.is_relative_to(app / "records"):
            previous = app.with_name("XlikeSaver") / current.relative_to(app)
            identity = os.path.normcase(str(previous))
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
            folders.append(backup_directory() / "catalog" / account / ("catalog-" + digest))
    found = [path for folder in folders for path in folder.glob("history-*.sqlite3")]
    return max(found, key=lambda path: (path.name, path.stat().st_mtime_ns)) if found else None


def backup_catalog(record_dir: Path | str, account: str) -> Path | None:
    """Keep one verified copy of the records per day (the last seven days).

    A damaged database is never copied over a good backup.
    """
    live = Path(record_dir) / HISTORY_NAME
    if not live.is_file():
        return None
    folder = _catalog_backups(account, record_dir)
    folder.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y%m%d")
    target, temporary = folder / f"history-{day}.sqlite3", folder / f".history-{day}.partial"
    temporary.unlink(missing_ok=True)
    try:
        with closing(sqlite3.connect(live.as_uri() + "?mode=ro", uri=True, timeout=5)) as source,                 closing(sqlite3.connect(temporary)) as copy:
            source.backup(copy)
            healthy = copy.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        if not healthy:
            return None
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    for old in sorted(folder.glob("history-*.sqlite3"))[:-_KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return target


def repair_history(record_dir: Path | str, account: str) -> dict:
    """Replace a damaged records database with a rebuilt, verified one.

    The damaged files are kept first; media files and download archives are
    never touched. Returns row counts and where the damaged copy was kept.
    """
    record_dir = Path(record_dir)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    kept = backup_directory() / f"{account}-{stamp}-damaged"
    kept.mkdir(parents=True)
    for suffix in ("", "-wal", "-shm"):
        source = record_dir / (HISTORY_NAME + suffix)
        if source.is_file():
            shutil.copy2(source, kept / source.name)
    work = kept / "work"
    work.mkdir()
    try:
        for source in kept.glob(HISTORY_NAME + "*"):
            shutil.copy2(source, work / source.name)
        # Bring committed changes still in the WAL into the copy's pages.
        try:
            with closing(sqlite3.connect(work / HISTORY_NAME)) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        rebuilt = record_dir / f".luvbox-history.repaired-{stamp}.sqlite3"
        # Old account-only backups cannot identify their output folder. Keep
        # them intact, but never mix an unrelated catalog into this repair.
        result = rebuild_history(work / HISTORY_NAME, rebuilt, fallback=latest_backup(account, record_dir))
        for suffix in ("-wal", "-shm"):
            (record_dir / (HISTORY_NAME + suffix)).unlink(missing_ok=True)
        os.replace(rebuilt, record_dir / HISTORY_NAME)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    result["kept"] = str(kept)
    return result
