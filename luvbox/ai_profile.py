"""The AI tags a person keeps, and what they taught them, shared by every library.

One small SQLite file in the app data folder (``ai-profile/profile.sqlite3``)
holds the AI tags (the five defaults and any the person adds), the examples a
person gave them (photos marked "yes, this" or "not this", from any saved
library or local folder) as image features, and the per-tag classifiers learned
from those examples. A library's catalog keeps which of its own photos were
taught; this file keeps the copy every library and the folder tab learn from.

Only the standard library is used here, so the window can list the tags at
start without loading numpy or onnxruntime (see ``tests/test_startup_imports``).
The learning itself (``tag_learner``) runs on a worker thread.

A default tag the person deletes is kept as a tombstone row (``deleted=1``):
the defaults are seeded with ``INSERT OR IGNORE`` at every open, and a plain
delete would bring a removed default back at the next start.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import threading

from .clip_model import ACTIVE_MODEL_ID, DEFAULT_CATEGORIES, embedding_bytes
from .file_safety import is_reparse_or_link, normalize_tag as _safe_normalize_tag
from .settings import app_directory
from .user_errors import UserFacingError


SCHEMA_VERSION = "1"
_PROFILE_USER_VERSION = 2
TAG_LIMIT = 50
THRESHOLD_RANGE = (0.05, 0.98)
REGIMES = ("zeroshot", "neighbor", "linear")
SOURCE_KINDS = ("x", "local")
_KOREA = timezone(timedelta(hours=9))
_ERROR = "AI 태그 설정을 읽거나 저장하지 못했습니다. LuvBox 기록 폴더의 권한을 확인해 주세요."
# One writer at a time in this process; SQLite's own lock keeps other processes apart.
_WRITE_LOCK = threading.RLock()


class AiProfileError(UserFacingError, RuntimeError):
    """A fixed message; the cause may name a user path."""


def profile_path() -> Path:
    return app_directory() / "ai-profile" / "profile.sqlite3"


def normalize_tag(tag) -> str:
    """The catalog's rule for a tag: no leading '#', no control characters, trimmed."""
    return _safe_normalize_tag(tag)


def valid_tag(tag) -> str:
    """The normalized tag, or "" when it cannot be an AI tag."""
    clean = normalize_tag(tag)
    return clean if 1 <= len(clean) <= TAG_LIMIT else ""


def _now() -> str:
    return datetime.now(_KOREA).isoformat(timespec="microseconds")


def _linked(path: Path) -> bool:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    return is_reparse_or_link(path, status=details)


def _check_plain(path: Path, top: Path | None) -> None:
    """Refuse a link or junction at the database, its side files, or a folder up to ``top``."""
    for side in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"), Path(str(path) + "-journal")):
        if _linked(side):
            raise AiProfileError(_ERROR)
        try:
            details = side.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(details.st_mode) or details.st_nlink > 1:
            raise AiProfileError(_ERROR)
    for folder in path.parents:
        if _linked(folder):
            raise AiProfileError(_ERROR)
        if top is None or folder == top:
            break


class AiProfileStore:
    """The shared AI profile. Open, use and close; every method keeps its own short transaction."""

    def __init__(self, path: Path | str | None = None):
        top = None
        if path is None:
            try:
                top = app_directory()
                path = top / "ai-profile" / "profile.sqlite3"
            except OSError as exc:
                raise AiProfileError(_ERROR) from exc
        self.path = Path(path)
        try:
            _check_plain(self.path, top if top is not None else self.path.parent)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _check_plain(self.path, top if top is not None else self.path.parent)
            self._db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        except (OSError, sqlite3.Error) as exc:
            raise AiProfileError(_ERROR) from exc
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA synchronous=FULL")
            if self._db.execute("PRAGMA user_version").fetchone()[0] != _PROFILE_USER_VERSION:
                with _WRITE_LOCK:
                    if self._db.execute("PRAGMA user_version").fetchone()[0] != _PROFILE_USER_VERSION:
                        self._db.execute("PRAGMA journal_mode=WAL")
                        with self._db:
                            self._db.execute("BEGIN IMMEDIATE")
                            # One statement at a time: executescript would commit on its own.
                            for statement in _SCHEMA:
                                self._db.execute(statement)
                            now = _now()
                            for key, value in (("schema_version", SCHEMA_VERSION),
                                               ("active_model_id", ACTIVE_MODEL_ID),
                                               ("rules_version", "0")):
                                self._db.execute("INSERT OR IGNORE INTO ai_profile_meta(key, value) VALUES (?,?)",
                                                 (key, value))
                            for order, name in enumerate(DEFAULT_CATEGORIES):
                                self._db.execute(
                                    "INSERT OR IGNORE INTO ai_tags(tag, enabled, built_in, deleted, sort_order, "
                                    "threshold, created_at, updated_at) VALUES (?,1,1,0,?,NULL,?,?)",
                                    (name, order, now, now))
                            self._db.execute(f"PRAGMA user_version={_PROFILE_USER_VERSION}")
        except sqlite3.Error as exc:
            self._db.close()
            raise AiProfileError(_ERROR) from exc
        except Exception:
            self._db.close()
            raise

    # -- lifecycle -------------------------------------------------------------------------
    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    @contextmanager
    def _write(self):
        with _WRITE_LOCK:
            try:
                with self._db:
                    self._db.execute("BEGIN IMMEDIATE")
                    yield self._db
            except sqlite3.Error as exc:
                raise AiProfileError(_ERROR) from exc

    def _read(self, statement: str, parameters=()) -> list[dict]:
        try:
            with closing(self._db.execute(statement, parameters)) as rows:
                return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise AiProfileError(_ERROR) from exc

    # -- versions --------------------------------------------------------------------------
    def meta(self, key: str, default: str = "") -> str:
        rows = self._read("SELECT value FROM ai_profile_meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def rules_version(self) -> int:
        try:
            return int(self.meta("rules_version", "0"))
        except ValueError:
            return 0

    def _bump(self, db) -> int:
        row = db.execute("SELECT value FROM ai_profile_meta WHERE key='rules_version'").fetchone()
        try:
            value = int(row[0]) + 1 if row else 1
        except (TypeError, ValueError):
            value = 1
        db.execute("INSERT INTO ai_profile_meta(key, value) VALUES ('rules_version', ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(value),))
        return value

    def bump_rules_version(self) -> int:
        with self._write() as db:
            return self._bump(db)

    # -- tags ------------------------------------------------------------------------------
    def _tag_row(self, tag: str) -> dict | None:
        rows = self._read("SELECT * FROM ai_tags WHERE tag=?", (tag,))
        return rows[0] if rows else None

    def example_counts(self, model_id: str = ACTIVE_MODEL_ID) -> dict[str, tuple[int, int]]:
        """``{tag: (positives, negatives)}`` of the examples taught for one model."""
        counts: dict[str, list[int]] = {}
        for row in self._read("SELECT tag, label, COUNT(*) AS n FROM ai_examples WHERE model_id=? GROUP BY tag, label",
                              (model_id,)):
            entry = counts.setdefault(row["tag"], [0, 0])
            entry[0 if row["label"] > 0 else 1] += int(row["n"])
        return {tag: (pos, neg) for tag, (pos, neg) in counts.items()}

    def active_tags(self, include_disabled: bool = False, model_id: str = ACTIVE_MODEL_ID) -> list[dict]:
        """The tags in list order, each with how far its learning has come."""
        rows = self._read("SELECT tag, enabled, built_in, threshold FROM ai_tags WHERE deleted=0 "
                          + ("" if include_disabled else "AND enabled=1 ") +
                          "ORDER BY sort_order ASC, created_at ASC, tag ASC")
        counts = self.example_counts(model_id)
        weights = {row["tag"]: row["regime"] for row in self._read(
            "SELECT tag, regime FROM ai_tag_weights WHERE model_id=?", (model_id,))}
        found = []
        for row in rows:
            pos, neg = counts.get(row["tag"], (0, 0))
            built_in = bool(row["built_in"])
            found.append({
                "tag": row["tag"], "enabled": bool(row["enabled"]), "built_in": built_in,
                "threshold": row["threshold"], "pos_count": pos, "neg_count": neg,
                "regime": weights.get(row["tag"], expected_regime(pos, built_in)),
                "needs_examples": not built_in and pos == 0,
            })
        return found

    def active_tag_names(self) -> list[str]:
        return [row["tag"] for row in self._read(
            "SELECT tag FROM ai_tags WHERE deleted=0 AND enabled=1 ORDER BY sort_order ASC, created_at ASC, tag ASC")]

    def known_tag_names(self) -> list[str]:
        """Tags that are not deleted, switched on or off."""
        return [row["tag"] for row in self._read(
            "SELECT tag FROM ai_tags WHERE deleted=0 ORDER BY sort_order ASC, created_at ASC, tag ASC")]

    def deleted_builtin_count(self) -> int:
        rows = self._read("SELECT COUNT(*) AS n FROM ai_tags WHERE built_in=1 AND deleted=1")
        return int(rows[0]["n"]) if rows else 0

    def add_tag(self, tag: str, *, enabled: bool = True) -> dict | None:
        """Add a tag, or bring back one that was deleted. None when the name cannot be a tag."""
        clean = valid_tag(tag)
        if not clean:
            return None
        now = _now()
        with self._write() as db:
            row = db.execute("SELECT deleted, enabled FROM ai_tags WHERE tag=?", (clean,)).fetchone()
            if row is None:
                order = db.execute("SELECT COALESCE(MAX(sort_order), 99) + 1 FROM ai_tags").fetchone()[0]
                db.execute("INSERT INTO ai_tags(tag, enabled, built_in, deleted, sort_order, threshold, created_at, "
                           "updated_at) VALUES (?,?,0,0,?,NULL,?,?)",
                           (clean, 1 if enabled else 0, max(100, int(order)), now, now))
                self._bump(db)
            elif row["deleted"] or bool(row["enabled"]) != bool(enabled):
                db.execute("UPDATE ai_tags SET deleted=0, enabled=?, updated_at=? WHERE tag=?",
                           (1 if enabled else 0, now, clean))
                self._bump(db)
        return self._tag_row(clean)

    def set_tag_enabled(self, tag: str, enabled: bool) -> bool:
        clean = valid_tag(tag)
        if not clean:
            return False
        with self._write() as db:
            changed = db.execute("UPDATE ai_tags SET enabled=?, updated_at=? WHERE tag=? AND deleted=0 AND enabled!=?",
                                 (1 if enabled else 0, _now(), clean, 1 if enabled else 0)).rowcount
            if changed:
                self._bump(db)
            known = db.execute("SELECT 1 FROM ai_tags WHERE tag=? AND deleted=0", (clean,)).fetchone()
        return known is not None

    def set_tag_threshold(self, tag: str, threshold: float | None) -> bool:
        clean = valid_tag(tag)
        if not clean:
            return False
        if threshold is not None:
            try:
                threshold = float(threshold)
            except (TypeError, ValueError):
                return False
            if not math.isfinite(threshold) or not THRESHOLD_RANGE[0] <= threshold <= THRESHOLD_RANGE[1]:
                return False
        with self._write() as db:
            changed = db.execute("UPDATE ai_tags SET threshold=?, updated_at=? WHERE tag=? AND deleted=0",
                                 (threshold, _now(), clean)).rowcount
            if changed:
                self._bump(db)
        return bool(changed)

    def delete_tag(self, tag: str, *, purge_examples: bool = False) -> bool:
        """A default tag stays as a tombstone; a tag of the person's own goes. Its classifier goes with it."""
        clean = valid_tag(tag)
        if not clean:
            return False
        with self._write() as db:
            row = db.execute("SELECT built_in, deleted FROM ai_tags WHERE tag=?", (clean,)).fetchone()
            if row is None or row["deleted"]:
                return False
            if row["built_in"]:
                db.execute("UPDATE ai_tags SET deleted=1, enabled=0, updated_at=? WHERE tag=?", (_now(), clean))
            else:
                db.execute("DELETE FROM ai_tags WHERE tag=?", (clean,))
            db.execute("DELETE FROM ai_tag_weights WHERE tag=?", (clean,))
            if purge_examples:
                db.execute("DELETE FROM ai_examples WHERE tag=?", (clean,))
            self._bump(db)
        return True

    def restore_builtin_tags(self) -> int:
        with self._write() as db:
            restored = db.execute("UPDATE ai_tags SET deleted=0, enabled=1, updated_at=? WHERE built_in=1 AND deleted=1",
                                  (_now(),)).rowcount
            if restored:
                self._bump(db)
        return int(restored)

    # -- examples --------------------------------------------------------------------------
    @staticmethod
    def _example(row) -> tuple | None:
        try:
            example_id, kind, scope, tag, label, model_id, blob = row
        except (TypeError, ValueError):
            return None
        tag = valid_tag(tag)
        if isinstance(blob, memoryview):
            blob = blob.tobytes()
        if (not isinstance(example_id, str) or not example_id or len(example_id) > 2000 or kind not in SOURCE_KINDS
                or not isinstance(scope, str) or not tag or label not in (1, -1)
                or not isinstance(blob, (bytes, bytearray)) or len(blob) != embedding_bytes(model_id)):
            return None
        return example_id, kind, scope, tag, int(label), model_id, bytes(blob)

    def upsert_examples(self, rows) -> int:
        """rows are (example_id, source_kind, source_scope, tag, label, model_id, embedding). Returns rows changed."""
        clean = [item for item in (self._example(row) for row in rows) if item is not None]
        if not clean:
            return 0
        now = _now()
        changed = 0
        with self._write() as db:
            for example_id, kind, scope, tag, label, model_id, blob in clean:
                changed += db.execute(
                    "INSERT INTO ai_examples(example_id, source_kind, source_scope, tag, label, model_id, embedding, "
                    "updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(example_id, tag) DO UPDATE SET "
                    "source_kind=excluded.source_kind, source_scope=excluded.source_scope, label=excluded.label, "
                    "model_id=excluded.model_id, embedding=excluded.embedding, updated_at=excluded.updated_at "
                    "WHERE ai_examples.label!=excluded.label OR ai_examples.model_id!=excluded.model_id "
                    "OR ai_examples.embedding!=excluded.embedding OR ai_examples.source_scope!=excluded.source_scope",
                    (example_id, kind, scope, tag, label, model_id, blob, now)).rowcount
            if changed:
                self._bump(db)
        return changed

    def remove_examples(self, pairs) -> int:
        """pairs are (example_id, tag)."""
        items = [(str(example_id), valid_tag(tag)) for example_id, tag in pairs if valid_tag(tag)]
        if not items:
            return 0
        with self._write() as db:
            removed = sum(db.execute("DELETE FROM ai_examples WHERE example_id=? AND tag=?", item).rowcount
                          for item in items)
            if removed:
                self._bump(db)
        return removed

    def remove_scope_examples(self, source_kind: str, source_scope: str) -> int:
        with self._write() as db:
            tags = [row["tag"] for row in db.execute(
                "SELECT DISTINCT tag FROM ai_examples WHERE source_kind=? AND source_scope=?",
                (source_kind, source_scope))]
            removed = db.execute("DELETE FROM ai_examples WHERE source_kind=? AND source_scope=?",
                                 (source_kind, source_scope)).rowcount
            if tags:
                marks = ",".join("?" for _ in tags)
                db.execute(f"DELETE FROM ai_tag_weights WHERE tag IN ({marks})", tags)
            if removed:
                self._bump(db)
        return int(removed)

    def rekey_scope(self, source_kind: str, old_scope: str, new_scope: str,
                    old_prefix: str = "", new_prefix: str = "") -> int:
        """Move stored examples when a folder relocates, returning 0 without locking if none exist."""
        if old_scope == new_scope and old_prefix == new_prefix:
            return 0
        if not self._read(
                "SELECT 1 FROM ai_examples WHERE source_kind=? AND source_scope=? LIMIT 1",
                (source_kind, old_scope),
        ):
            return 0
        moved = 0
        with self._write() as db:
            rows = db.execute(
                "SELECT example_id, tag, label, model_id, embedding, updated_at "
                "FROM ai_examples WHERE source_kind=? AND source_scope=?",
                (source_kind, old_scope),
            ).fetchall()
            if not rows:
                return 0
            for row in rows:
                old_id = row["example_id"]
                if old_prefix and old_id.startswith(old_prefix):
                    new_id = new_prefix + old_id[len(old_prefix):]
                else:
                    new_id = old_id
                if new_id == old_id:
                    db.execute("UPDATE ai_examples SET source_scope=? WHERE example_id=? AND tag=?",
                               (new_scope, old_id, row["tag"]))
                else:
                    db.execute(
                        "INSERT INTO ai_examples(example_id, source_kind, source_scope, tag, label, model_id, "
                        "embedding, updated_at) VALUES (?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(example_id, tag) DO UPDATE SET "
                        "source_kind=excluded.source_kind, source_scope=excluded.source_scope, "
                        "label=excluded.label, model_id=excluded.model_id, embedding=excluded.embedding, "
                        "updated_at=excluded.updated_at "
                        "WHERE excluded.updated_at>ai_examples.updated_at",
                        (new_id, source_kind, new_scope, row["tag"], row["label"], row["model_id"],
                         bytes(row["embedding"]), row["updated_at"]),
                    )
                    db.execute("DELETE FROM ai_examples WHERE example_id=? AND tag=?", (old_id, row["tag"]))
                moved += 1
            if moved:
                self._bump(db)
        return moved

    def sync_scope(self, source_kind: str, source_scope: str, tags, rows) -> tuple[int, int]:
        """Make one library's (or folder's) examples of ``tags`` exactly ``rows``.

        ``rows`` are (example_id, tag, label, model_id, embedding) of this scope.
        Examples of other tags and other scopes are left alone. Returns
        ``(written, removed)``; the rules version moves only when something changed.
        """
        wanted_tags = {valid_tag(tag) for tag in tags} - {""}
        desired = {}
        for row in rows:
            item = self._example((row[0], source_kind, source_scope, row[1], row[2], row[3], row[4]))
            if item is not None and item[3] in wanted_tags:
                desired[(item[0], item[3])] = item
        if not wanted_tags:
            return 0, 0
        marks = ",".join("?" for _ in wanted_tags)
        now = _now()
        written = removed = 0
        with self._write() as db:
            current = {(row["example_id"], row["tag"]): row for row in db.execute(
                "SELECT example_id, tag, label, model_id, embedding FROM ai_examples "
                f"WHERE source_kind=? AND source_scope=? AND tag IN ({marks})",
                (source_kind, source_scope, *sorted(wanted_tags)))}
            changed_tags: set[str] = set()
            for key in current.keys() - desired.keys():
                removed += db.execute("DELETE FROM ai_examples WHERE example_id=? AND tag=?", key).rowcount
                changed_tags.add(key[1])
            for key, (example_id, kind, scope, tag, label, model_id, blob) in desired.items():
                old = current.get(key)
                if old is not None and old["label"] == label and old["model_id"] == model_id and bytes(old["embedding"]) == blob:
                    continue
                db.execute(
                    "INSERT INTO ai_examples(example_id, source_kind, source_scope, tag, label, model_id, embedding, "
                    "updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(example_id, tag) DO UPDATE SET "
                    "source_kind=excluded.source_kind, source_scope=excluded.source_scope, label=excluded.label, "
                    "model_id=excluded.model_id, embedding=excluded.embedding, updated_at=excluded.updated_at",
                    (example_id, kind, scope, tag, label, model_id, blob, now))
                written += 1
                changed_tags.add(tag)
            if changed_tags:
                marks = ",".join("?" for _ in changed_tags)
                db.execute(f"DELETE FROM ai_tag_weights WHERE tag IN ({marks})", sorted(changed_tags))
            if written or removed:
                self._bump(db)
        return written, removed

    def training_data_for_tag(self, tag: str, model_id: str = ACTIVE_MODEL_ID,
                              *, include_positives: bool = True) -> tuple[list[bytes], list[bytes]]:
        """Embeddings of the photos taught "yes" and "no" for one tag."""
        clean = valid_tag(tag)
        size = embedding_bytes(model_id)
        positive, negative = [], []
        where = "tag=? AND model_id=?" if include_positives else "tag=? AND model_id=? AND label<0"
        for row in self._read(f"SELECT label, embedding FROM ai_examples WHERE {where} "
                              "ORDER BY example_id", (clean, model_id)):
            blob = bytes(row["embedding"])
            if len(blob) != size:
                continue
            (positive if row["label"] > 0 else negative).append(blob)
        return positive, negative

    def other_examples(self, tag: str, model_id: str = ACTIVE_MODEL_ID, limit: int = 512) -> list[bytes]:
        """Positive examples of other tags that are not taught for ``tag``: background for its classifier."""
        clean = valid_tag(tag)
        size = embedding_bytes(model_id)
        rows = self._read(
            "SELECT embedding FROM ai_examples AS other WHERE other.tag!=? AND other.model_id=? AND other.label=1 "
            "AND NOT EXISTS (SELECT 1 FROM ai_examples AS own WHERE own.example_id=other.example_id AND own.tag=?) "
            "GROUP BY other.example_id ORDER BY other.example_id LIMIT ?", (clean, model_id, clean, max(0, int(limit))))
        return [bytes(row["embedding"]) for row in rows if len(bytes(row["embedding"])) == size]

    def examples_for(self, example_ids, model_id: str = ACTIVE_MODEL_ID) -> dict[str, dict[str, int]]:
        """``{example_id: {tag: label}}`` of the given examples."""
        ids = [str(item) for item in example_ids]
        found: dict[str, dict[str, int]] = {}
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            for row in self._read(f"SELECT example_id, tag, label FROM ai_examples WHERE model_id=? "
                                  f"AND example_id IN ({marks})", (model_id, *chunk)):
                found.setdefault(row["example_id"], {})[row["tag"]] = int(row["label"])
        return found

    # -- learned classifiers ---------------------------------------------------------------
    def dirty_tags(self, model_id: str = ACTIVE_MODEL_ID) -> list[str]:
        """Switched-on tags whose classifier is missing or older than their examples."""
        tags = self._read("SELECT tag, built_in FROM ai_tags WHERE deleted=0 AND enabled=1 "
                          "ORDER BY sort_order ASC, created_at ASC, tag ASC")
        counts = self.example_counts(model_id)
        latest = {row["tag"]: row["latest"] for row in self._read(
            "SELECT tag, MAX(updated_at) AS latest FROM ai_examples WHERE model_id=? GROUP BY tag", (model_id,))}
        weights = {row["tag"]: row for row in self._read(
            "SELECT tag, model_id, pos_count, neg_count, trained_at FROM ai_tag_weights")}
        dirty = []
        for row in tags:
            tag = row["tag"]
            pos, neg = counts.get(tag, (0, 0))
            weight = weights.get(tag)
            if weight is None:
                if row["built_in"] or pos:
                    dirty.append(tag)
                continue
            if (weight["model_id"] != model_id or weight["pos_count"] != pos or weight["neg_count"] != neg
                    or (latest.get(tag) or "") > (weight["trained_at"] or "")):
                dirty.append(tag)
        return dirty

    def save_tag_weight(self, tag: str, model_id: str, regime: str, weights: bytes, bias: float,
                        threshold: float, pos_count: int, neg_count: int) -> bool:
        clean = valid_tag(tag)
        if not clean or regime not in REGIMES:
            return False
        if isinstance(weights, memoryview):
            weights = weights.tobytes()
        if not isinstance(weights, (bytes, bytearray)) or (regime == "linear" and len(weights) != embedding_bytes(model_id)):
            return False
        try:
            bias_value = float(bias)
            threshold_value = float(threshold)
        except (TypeError, ValueError):
            return False
        if not (math.isfinite(bias_value) and math.isfinite(threshold_value)):
            return False
        with self._write() as db:
            db.execute(
                "INSERT INTO ai_tag_weights(tag, model_id, regime, weights, bias, threshold, pos_count, neg_count, "
                "trained_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(tag) DO UPDATE SET model_id=excluded.model_id, "
                "regime=excluded.regime, weights=excluded.weights, bias=excluded.bias, threshold=excluded.threshold, "
                "pos_count=excluded.pos_count, neg_count=excluded.neg_count, trained_at=excluded.trained_at",
                (clean, model_id, regime, bytes(weights), bias_value, threshold_value, int(pos_count),
                 int(neg_count), _now()))
            self._bump(db)
        return True

    def delete_tag_weight(self, tag: str) -> bool:
        with self._write() as db:
            removed = db.execute("DELETE FROM ai_tag_weights WHERE tag=?", (valid_tag(tag),)).rowcount
            if removed:
                self._bump(db)
        return bool(removed)

    def load_tag_weights(self, model_id: str = ACTIVE_MODEL_ID) -> dict[str, dict]:
        found = {}
        for row in self._read("SELECT * FROM ai_tag_weights WHERE model_id=?", (model_id,)):
            row["weights"] = bytes(row["weights"])
            found[row["tag"]] = row
        return found

    def scoring_rules(self, model_id: str = ACTIVE_MODEL_ID) -> list[dict]:
        """What scoring a photo needs for each switched-on tag, in list order.

        Each item: tag, built_in, threshold (the person's, or None), and the
        learned weight row (or None). Example embeddings are read separately
        (``training_data_for_tag``) only for tags that compare with examples.
        """
        weights = self.load_tag_weights(model_id)
        return [{"tag": row["tag"], "built_in": bool(row["built_in"]), "threshold": row["threshold"],
                 "weight": weights.get(row["tag"])}
                for row in self._read("SELECT tag, built_in, threshold FROM ai_tags WHERE deleted=0 AND enabled=1 "
                                      "ORDER BY sort_order ASC, created_at ASC, tag ASC")]

    def summary(self) -> str:
        """A short JSON line for diagnostics: tag names and counts only, no embeddings."""
        return json.dumps({"rules_version": self.rules_version(),
                           "tags": [(row["tag"], row["enabled"], row["pos_count"], row["neg_count"])
                                    for row in self.active_tags(include_disabled=True)]}, ensure_ascii=False)


def expected_regime(pos_count: int, built_in: bool) -> str:
    """Which way a tag is decided with this many "yes" examples (see ``tag_learner``)."""
    if pos_count >= 5:
        return "linear"
    if pos_count >= 1:
        return "neighbor"
    return "zeroshot" if built_in else "none"


def example_id_for_x(output_key: str, account: str, folder_mode: str, tweet_id: str, num: int, media_type: str) -> str:
    return f"x:{output_key}:{account}:{folder_mode}:{tweet_id}:{int(num)}:{media_type}"


def output_key(output_dir) -> str:
    """A short, stable name for a save folder, whatever its spelling."""
    import hashlib

    text = str(output_dir or "").strip()
    try:
        folder = os.path.normcase(str(Path(text).expanduser().resolve())) if text else ""
    except (OSError, RuntimeError):
        folder = os.path.normcase(os.path.normpath(text))
    return hashlib.sha256(folder.encode("utf-8", "surrogatepass")).hexdigest()[:16]


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS ai_profile_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS ai_tags ("
    " tag TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, built_in INTEGER NOT NULL DEFAULT 0,"
    " deleted INTEGER NOT NULL DEFAULT 0, sort_order INTEGER NOT NULL DEFAULT 100, threshold REAL,"
    " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS ai_examples ("
    " example_id TEXT NOT NULL, source_kind TEXT NOT NULL, source_scope TEXT NOT NULL, tag TEXT NOT NULL,"
    " label INTEGER NOT NULL, model_id TEXT NOT NULL, embedding BLOB NOT NULL, updated_at TEXT NOT NULL,"
    " PRIMARY KEY(example_id, tag))",
    "CREATE INDEX IF NOT EXISTS ai_examples_tag_model ON ai_examples(tag, model_id, label)",
    "CREATE INDEX IF NOT EXISTS ai_examples_model_tag_label_updated ON ai_examples(model_id, tag, label, updated_at)",
    "CREATE INDEX IF NOT EXISTS ai_examples_scope ON ai_examples(source_kind, source_scope)",
    "CREATE TABLE IF NOT EXISTS ai_tag_weights ("
    " tag TEXT PRIMARY KEY, model_id TEXT NOT NULL, regime TEXT NOT NULL, weights BLOB NOT NULL,"
    " bias REAL NOT NULL DEFAULT 0.0, threshold REAL NOT NULL DEFAULT 0.65, pos_count INTEGER NOT NULL DEFAULT 0,"
    " neg_count INTEGER NOT NULL DEFAULT 0, trained_at TEXT NOT NULL)",
)
