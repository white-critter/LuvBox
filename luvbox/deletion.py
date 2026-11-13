"""Protected, journalled library deletion without UI or X requests."""

from __future__ import annotations

import os
from pathlib import Path
import threading
import uuid

from .history import HistoryStore
from .protection import AccountGuard
from .recovery import backup_catalog
from .recycling import RecyclingError, file_signature, recycle_file
from .storage import OutputGuard, StoragePaths, publish_snapshot


_NO_OTHER_COPY = "같은 내용의 다른 파일이 남는지 확인하지 못해 그대로 두었습니다."


def _same_content(first: Path, second: Path, rule: str) -> bool:
    """The two files hold the same photo, read now rather than taken from stored hashes."""
    from .duplicates import compute_file_hash, nearly_identical, pixel_vector

    digest = compute_file_hash(first)
    if digest and digest == compute_file_hash(second):
        return True
    # Groups made with "the same photo at another size or quality" hold files
    # whose bytes differ; the scan's own pixel comparison decides again.
    return rule == "near" and nearly_identical(pixel_vector(first), pixel_vector(second))


def kept_copy(store: HistoryStore, path: str, signature, keepers, rule: str) -> str:
    """A different file with the same content that exists right now, or ''.

    A file sent away as "a copy" must leave such a file behind. Another record
    of the same file, another spelling of its path or a hard link to it is the
    file itself, however the catalog lists it.
    """
    own = os.path.normcase(os.path.realpath(path))
    for keeper in keepers if isinstance(keepers, (list, tuple)) else ():
        try:
            if not isinstance(keeper, str) or not keeper or not store.is_media_path(keeper):
                continue
            other = file_signature(keeper)          # an ordinary file, not a link
            if os.path.normcase(os.path.realpath(keeper)) == own:
                continue
            if other[1] and signature[1] and other[:2] == tuple(signature[:2]):
                continue                            # the same file number on the same volume
            if file_signature(path) != tuple(signature):
                return ""                           # the file to move changed meanwhile
            if _same_content(Path(path), Path(keeper), rule) and file_signature(keeper) == other:
                return keeper
        except (OSError, ValueError, TypeError):
            continue
    return ""


def run_deletion(location: StoragePaths | Path, rows: list[dict], keep_deleted: bool,
                 stop: threading.Event, emit) -> dict:
    """Recycle only the confirmed physical files, preserving their media history.

    Intent is committed before every native operation. An uncertain receipt or
    catalog write stops the batch; the next protected job settles any remaining
    intent without repeating a deletion. Native code never falls back to erase.

    A row from the duplicate view carries ``_copy_keepers``, the other files of
    its group. It moves only while one of them is, at that moment, a different
    file with the same content; otherwise it stays and is counted in
    ``protected``. Each file is checked when its turn comes, so the last file
    of a group can never go.
    """
    unique = {}
    for row in rows:
        path = row.get("path") if isinstance(row, dict) else None
        key = os.path.normcase(os.path.abspath(path)) if isinstance(path, str) and path else None
        if key is not None:
            unique.setdefault(key, dict(row))
    candidates = list(unique.values())
    result = dict(total=len(candidates), recycled=0, failed=0, cancelled=0, protected=0, paths=[], message="")
    guards, store, account_guard = [], None, None
    roots_locked = False
    processed = 0
    warnings = []
    failures = []
    batch = uuid.uuid4().hex

    def progress(message):
        emit(dict(result, type="progress", processed=processed, message=message))

    def finished():
        result["cancelled"] = result["total"] - processed
        result["message"] = f"휴지통 이동 {result['recycled']:,}개 · 실패 {result['failed']:,}개"
        if result["protected"]:
            result["message"] += f" · 그대로 둔 파일 {result['protected']:,}개"
        if result["cancelled"]:
            result["message"] += f" · 처리하지 않은 파일 {result['cancelled']:,}개"
        if result["protected"]:
            result["message"] += "\n" + _NO_OTHER_COPY
        if failures:
            # The results are not kept in a separate list; name a few here.
            result["message"] += "\n" + "\n".join(failures[:3])
            if len(failures) > 3:
                result["message"] += f"\n외 {len(failures) - 3:,}개"
        if warnings:
            result["message"] += "\n" + "\n".join(warnings)
        emit(dict(result, type="done", processed=processed))
        return result

    try:
        if type(keep_deleted) is not bool:
            raise ValueError("삭제 설정을 확인해 주세요.")
        if not candidates or stop.is_set():
            # Opening a catalog can create local bookkeeping. There is nothing
            # to prepare for a cancelled or empty selection.
            return finished()
        # The account lock also protects catalog preparation/snapshot merging.
        if isinstance(location, StoragePaths):
            account_guard = AccountGuard(location.account, sleep=lambda *_: None)
            account_guard.acquire(respect_cooldown=False)
            initial_root = location.output_dir
        else:
            initial_root = Path(location)
            if not initial_root.is_dir():
                raise ValueError("기록된 저장 폴더를 찾을 수 없습니다. 파일 위치를 확인해 주세요.")
        guard = OutputGuard(initial_root)
        guard.acquire()
        guards.append(guard)
        store = (HistoryStore.for_storage(location, protected=True, create_media=False) if isinstance(location, StoragePaths)
                 else HistoryStore(location))
        for root in store.media_roots:
            if os.path.normcase(str(root)) == os.path.normcase(str(initial_root)):
                continue
            guard = OutputGuard(root, group=guards[0])
            guard.acquire()
            guards.append(guard)
        roots_locked = True
        recovered = store.reconcile_deletions()
        if recovered["interrupted"]:
            warnings.append(f"이전 삭제 작업 {recovered['interrupted']:,}개의 기록을 확인했습니다.")

        # Capture the whole batch before any file moves. Native PreDelete checks
        # it again, including device/file identity, size and nanosecond mtime.
        manifest = []
        for row in candidates:
            if stop.is_set():
                break
            try:
                if not store.is_media_path(row["path"]):
                    raise RecyclingError("기록된 저장 폴더의 일반 파일만 삭제할 수 있습니다.")
                signature = file_signature(row["path"])
                expected = row.get("_delete_signature")
                if expected is not None and tuple(expected) != signature:
                    raise RecyclingError("선택한 뒤 파일이 바뀌었습니다. 목록을 새로 고침해 주세요.")
                manifest.append((row, signature, ""))
            except (OSError, ValueError, TypeError) as exc:
                manifest.append((row, None, str(exc)))

        for row, signature, error in manifest:
            if stop.is_set():
                break
            path = row["path"]
            if error:
                # No native operation is attempted for an invalid manifest.
                # A regular catalog entry still gets a failure history entry.
                # Its zero identity is never used for a native operation.
                if store.is_media_path(path):
                    size = row.get("filesize")
                    failed_signature = (0, 0, size if type(size) is int and size >= 0 else 0, 0)
                    ident = store.begin_deletion(batch, row, keep_deleted, failed_signature)
                    store.finish_deletion(ident, "failed", error)
                result["failed"] += 1
                processed += 1
                failures.append(f"{Path(path).name}: {error}")
                progress(failures[-1])
                continue
            if "_copy_keepers" in row and not kept_copy(store, path, signature, row.get("_copy_keepers"),
                                                        row.get("_copy_rule") or "exact"):
                # Nothing is recorded: the file and its records stay as they are.
                result["protected"] += 1
                processed += 1
                progress(f"{Path(path).name}: {_NO_OTHER_COPY}")
                continue
            ident = store.begin_deletion(batch, row, keep_deleted, signature)
            if stop.is_set():
                store.finish_deletion(ident, "cancelled", "중단 요청으로 파일을 그대로 두었습니다.")
                break
            try:
                destination = recycle_file(path, expected=signature)
            except RecyclingError as exc:
                uncertain = exc.uncertain or store._deletion_path_absent(path)
                result["failed"] += 1
                processed += 1
                store.finish_deletion(ident, "interrupted" if uncertain else "failed", str(exc),
                                      recycle_path=exc.recycled_path)
                failures.append(f"{Path(path).name}: {exc}")
                progress(failures[-1])
                if uncertain:
                    warnings.append("휴지통 이동 결과가 확실하지 않아 중단했습니다. 파일과 Windows 휴지통을 확인해 주세요.")
                    break
            except OSError as exc:
                absent = store._deletion_path_absent(path)
                result["failed"] += 1
                processed += 1
                store.finish_deletion(ident, "interrupted" if absent else "failed", str(exc))
                failures.append(f"{Path(path).name}: 파일을 옮기지 못했습니다.")
                progress(failures[-1])
                if absent:
                    warnings.append("파일 이동 결과를 확인하지 못해 중단했습니다. Windows 휴지통을 확인해 주세요.")
                    break
            else:
                # Count the verified physical outcome even if its final
                # database commit fails. Such a failure still stops the batch
                # and leaves intent for reconciliation, never for a retry.
                result["recycled"] += 1
                result["paths"].append(path)
                processed += 1
                store.finish_deletion(ident, "recycled", "Windows 휴지통으로 이동했습니다.", destination)
                progress(f"{Path(path).name}: 휴지통으로 이동했습니다.")
    except Exception as exc:
        warnings.append(f"작업을 중단했습니다. {exc}")
        # Do not perform more file operations after an unrecorded outcome.
        # Pending intents are settled under the same locks when possible.
        if store is not None and roots_locked:
            try:
                store.reconcile_deletions()
            except Exception:
                warnings.append("휴지통 이동 결과 확인은 다음 실행에서 다시 시도합니다.")
    finally:
        if store is not None:
            closed = False
            try:
                store.close()
                closed = True
            except Exception:
                warnings.append("기록 파일을 닫지 못했습니다. 앱을 다시 시작해 주세요.")
            if closed and isinstance(location, StoragePaths):
                try:
                    # A moved or disconnected output must not be replaced by
                    # an empty folder just to publish its record copy.
                    if location.output_dir.is_dir():
                        publish_snapshot(location)
                except Exception:
                    warnings.append("저장 폴더의 기록 사본은 다음 저장 때 갱신합니다.")
                try:
                    if backup_catalog(location.record_dir, location.account) is None:
                        warnings.append("이 PC의 기록 백업을 만들지 못했습니다.")
                except Exception:
                    warnings.append("이 PC의 기록 백업을 만들지 못했습니다.")
        for guard in reversed(guards):
            try:
                guard.close()
            except Exception:
                warnings.append("저장 폴더의 작업 보호 정리를 마치지 못했습니다. 앱을 다시 시작해 주세요.")
        if account_guard is not None:
            try:
                account_guard.close()
            except Exception:
                warnings.append("계정의 작업 보호 정리를 마치지 못했습니다. 앱을 다시 시작해 주세요.")
    return finished()
