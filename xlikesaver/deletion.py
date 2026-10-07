"""Protected, journalled library deletion. No Tk or X requests are made here."""

from __future__ import annotations

import os
from pathlib import Path
import threading
import uuid

from .history import HistoryError, HistoryStore
from .protection import AccountGuard
from .recovery import backup_catalog
from .recycling import RecyclingError, file_signature, recycle_file
from .storage import OutputGuard, StoragePaths, publish_snapshot


def run_deletion(location: StoragePaths | Path, rows: list[dict], keep_deleted: bool,
                 stop: threading.Event, emit) -> dict:
    """Recycle only the confirmed physical files, preserving their media history.

    Intent is committed before every native operation. An uncertain receipt or
    catalog write stops the batch; the next protected job settles any remaining
    intent without repeating a deletion. Native code never falls back to erase.
    """
    unique = {}
    for row in rows:
        path = row.get("path") if isinstance(row, dict) else None
        key = os.path.normcase(os.path.abspath(path)) if isinstance(path, str) and path else None
        if key is not None:
            unique.setdefault(key, dict(row))
    candidates = list(unique.values())
    result = dict(total=len(candidates), recycled=0, failed=0, cancelled=0, paths=[], message="")
    guards, store, account_guard = [], None, None
    processed = 0
    warnings = []
    batch = uuid.uuid4().hex

    def progress(message):
        emit(dict(result, type="progress", processed=processed, message=message))

    try:
        if type(keep_deleted) is not bool:
            raise ValueError("삭제 설정을 확인해 주세요.")
        # The account lock also protects catalog preparation/snapshot merging.
        if isinstance(location, StoragePaths):
            account_guard = AccountGuard(location.account, sleep=lambda *_: None)
            account_guard.acquire(respect_cooldown=False)
            initial_root = location.output_dir
        else:
            initial_root = Path(location)
        guard = OutputGuard(initial_root)
        guard.acquire()
        guards.append(guard)
        store = (HistoryStore.for_storage(location, protected=True) if isinstance(location, StoragePaths)
                 else HistoryStore(location))
        for root in store.media_roots:
            if os.path.normcase(str(root)) == os.path.normcase(str(initial_root)):
                continue
            guard = OutputGuard(root)
            guard.acquire()
            guards.append(guard)
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
                progress(f"{Path(path).name}: {error}")
                continue
            ident = store.begin_deletion(batch, row, keep_deleted, signature)
            try:
                destination = recycle_file(path, expected=signature)
            except RecyclingError as exc:
                uncertain = exc.uncertain or store._deletion_path_absent(path)
                store.finish_deletion(ident, "interrupted" if uncertain else "failed", str(exc),
                                      recycle_path=exc.recycled_path)
                result["failed"] += 1
                processed += 1
                progress(f"{Path(path).name}: {exc}")
                if uncertain:
                    warnings.append("휴지통 이동 결과가 확실하지 않아 중단했습니다. 파일과 Windows 휴지통을 확인해 주세요.")
                    break
            except OSError as exc:
                absent = store._deletion_path_absent(path)
                store.finish_deletion(ident, "interrupted" if absent else "failed", str(exc))
                result["failed"] += 1
                processed += 1
                progress(f"{Path(path).name}: 파일을 옮기지 못했습니다.")
                if absent:
                    warnings.append("파일 이동 결과를 확인하지 못해 중단했습니다. Windows 휴지통을 확인해 주세요.")
                    break
            else:
                # A database error here deliberately leaves a pending journal.
                store.finish_deletion(ident, "recycled", "Windows 휴지통으로 이동했습니다.", destination)
                result["recycled"] += 1
                result["paths"].append(path)
                processed += 1
                progress(f"{Path(path).name}: 휴지통으로 이동했습니다.")
    except Exception as exc:
        warnings.append(f"작업을 중단했습니다. {exc}")
        # Do not perform more file operations after an unrecorded outcome.
        # Pending intents are settled under the same locks when possible.
        if store is not None:
            try:
                store.reconcile_deletions()
            except (HistoryError, OSError):
                warnings.append("삭제 기록 확인은 다음 실행에서 다시 시도합니다.")
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                warnings.append("기록 파일을 닫지 못했습니다. 앱을 다시 시작해 주세요.")
            if isinstance(location, StoragePaths):
                try:
                    publish_snapshot(location)
                except Exception:
                    warnings.append("저장 폴더의 기록 사본은 다음 저장 때 갱신합니다.")
                try:
                    if backup_catalog(location.record_dir, location.account) is None:
                        warnings.append("이 PC의 기록 백업을 만들지 못했습니다.")
                except Exception:
                    warnings.append("이 PC의 기록 백업을 만들지 못했습니다.")
        for guard in reversed(guards):
            guard.close()
        if account_guard is not None:
            account_guard.close()
    result["cancelled"] = result["total"] - processed
    result["message"] = f"휴지통 이동 {result['recycled']:,}개 · 실패 {result['failed']:,}개"
    if result["cancelled"]:
        result["message"] += f" · 처리하지 않은 파일 {result['cancelled']:,}개"
    if warnings:
        result["message"] += "\n" + "\n".join(warnings)
    emit(dict(result, type="done", processed=processed))
    return result
