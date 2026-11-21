"""Recycling for the "폴더 정리" tab: the gallery's checks on files of an opened folder.

The gallery's deletion (``deletion.run_deletion``) also keeps the X records,
their journal and snapshots. A folder has none of these, so this moves files
with the same checks and nothing else:

1. each file is still inside the opened folder, an ordinary file reached
   without a link or junction, and not only in the cloud (moving it would
   first download it);
2. it is still the file that was shown (``_delete_signature``);
3. a copy from the duplicate view goes only while another file with its very
   bytes stays (``deletion.kept_copy``), so the last file of a group never goes;
4. a '비슷함' photo goes only alone, beside the photo it was compared with,
   still that file and still the same picture (``deletion.kept_alike``);
5. the move is the Windows Recycle Bin's (``recycling.recycle_file``), with no
   fallback that erases.

The two keep checks are the gallery's own functions, so both tabs hold the
same rule. A moved file stays in the folder's list as gone, with its star,
tags and taught answers: restored from the Recycle Bin, it comes back with them.
"""

from __future__ import annotations

import os
from pathlib import Path

from .local_store import LocalFolderError, LocalFolderStore
from .recycling import RecyclingError, file_signature, recycle_file
from .thumbcache import cloud_only


def run_local_deletion(root, rows, stop, emit) -> dict:
    """Move the confirmed files of the folder ``root``. ``emit`` gets progress and the result."""
    from .deletion import kept_alike, kept_copy

    candidates = []
    seen = set()
    for row in rows if isinstance(rows, (list, tuple)) else ():
        path = row.get("path") if isinstance(row, dict) else None
        key = os.path.normcase(os.path.abspath(path)) if isinstance(path, str) and path else None
        if key is not None and key not in seen:
            seen.add(key)
            candidates.append(dict(row))
    result = dict(total=len(candidates), recycled=0, failed=0, cancelled=0, protected=0, paths=[], message="")
    processed = 0
    failures = []
    protected_reasons = []
    try:
        store = LocalFolderStore(root)
    except LocalFolderError as exc:
        result["failed"] = len(candidates)
        result["message"] = str(exc)
        emit(dict(result, type="done", processed=0))
        return result
    gone = []
    try:
        for row in candidates:
            if stop.is_set():
                break
            path = row["path"]
            name = Path(path).name
            try:
                if not store.is_media_path(path):
                    raise RecyclingError("열린 폴더 안의 일반 파일만 휴지통으로 옮길 수 있습니다.")
                if cloud_only(path):
                    raise RecyclingError("클라우드에만 있는 파일은 옮기지 않습니다. 탐색기에서 받은 뒤 다시 시도해 주세요.")
                signature = file_signature(path)
                expected = row.get("_delete_signature")
                if not isinstance(expected, (list, tuple)) or tuple(expected) != signature:
                    raise RecyclingError("선택한 뒤 파일이 바뀌었습니다. 목록을 새로 고침해 주세요.")
                if "_alike_keeper" in row:
                    if (len(candidates) != 1 or "_copy_keepers" in row
                            or not kept_alike(store, path, signature, row.get("_alike_keeper"),
                                              row.get("_alike_keeper_signature"))):
                        reason = f"{name}: 남길 사진과 같은 사진으로 다시 확인되지 않아 그대로 두었습니다."
                        result["protected"] += 1
                        processed += 1
                        protected_reasons.append(reason)
                        emit(dict(result, type="progress", processed=processed, message=reason))
                        continue
                elif "_copy_keepers" in row and not kept_copy(store, path, signature, row.get("_copy_keepers")):
                    reason = f"{name}: 같은 파일이 남지 않아 그대로 두었습니다."
                    result["protected"] += 1
                    processed += 1
                    protected_reasons.append(reason)
                    emit(dict(result, type="progress", processed=processed, message=reason))
                    continue
                recycle_file(path, expected=signature)
            except RecyclingError as exc:
                result["failed"] += 1
                processed += 1
                failures.append(f"{name}: {exc}")
                emit(dict(result, type="progress", processed=processed, message=failures[-1]))
                if getattr(exc, "uncertain", False):
                    break         # the Recycle Bin did not say what happened: stop before the next file
                continue
            except OSError:
                result["failed"] += 1
                processed += 1
                failures.append(f"{name}: 파일을 옮기지 못했습니다.")
                emit(dict(result, type="progress", processed=processed, message=failures[-1]))
                continue
            result["recycled"] += 1
            result["paths"].append(path)
            gone.append(row.get("rel_path"))
            processed += 1
            emit(dict(result, type="progress", processed=processed, message=f"{name}: 휴지통으로 이동했습니다."))
        if gone:
            try:
                store.mark_gone([rel for rel in gone if isinstance(rel, str)])
            except LocalFolderError:
                pass              # the next reading of the folder finds them gone
    finally:
        store.close()
    result["cancelled"] = result["total"] - processed
    if len(candidates) == 1 and result["protected"] == 1 and protected_reasons and not failures:
        result["message"] = protected_reasons[0]
    else:
        result["message"] = f"휴지통 이동 {result['recycled']:,}개 · 실패 {result['failed']:,}개"
        if result["protected"]:
            result["message"] += f" · 그대로 둔 파일 {result['protected']:,}개"
        if result["cancelled"]:
            result["message"] += f" · 처리하지 않은 파일 {result['cancelled']:,}개"
        notes = [*failures, *protected_reasons]
        if notes:
            result["message"] += "\n" + "\n".join(notes[:3]) + (f"\n외 {len(notes) - 3:,}개" if len(notes) > 3 else "")
    emit(dict(result, type="done", processed=processed))
    return result
