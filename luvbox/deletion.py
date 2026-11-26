"""Protected, journalled library deletion without UI or X requests."""

from __future__ import annotations

import os
from pathlib import Path
import threading
import uuid

from .history import HistoryError, HistoryStore
from .protection import AccountGuard, ProtectionError
from .recovery import backup_catalog
from .recycling import RecyclingError, file_signature, recycle_file
from .storage import OutputGuard, StoragePaths, publish_snapshot
from .thumbcache import cloud_only


class _DeletionMessageError(ValueError):
    """Fixed user-facing validation error inside run_deletion."""


_NO_OTHER_COPY = "같은 내용의 다른 파일이 남는지 확인하지 못해 그대로 두었습니다."
_NEAR_ONLY = ("‘비슷한 사진도 검사’로 묶인 사진은 내용이 완전히 같은 파일이 남을 때만 사본으로 보냅니다. "
              "‘비슷함’ 사진은 한 장만 골라 휴지통으로 보내면 남길 사진과 나란히 비교한 뒤 보낼 수 있습니다.")
_ALIKE_NOT_KEPT = "남길 사진이 그대로 있고 같은 사진인지 다시 확인하지 못해 그대로 두었습니다."
_CLOUD_ONLY = "클라우드에만 있는 파일은 옮기지 않습니다. 파일을 이 PC에 내려받은 뒤 다시 시도해 주세요."


def _same_content(first: Path, second: Path) -> bool:
    """The two files hold the same bytes, read now rather than taken from stored hashes.

    Only that proves a copy. Groups made with "the same photo at another size
    or quality" also hold files whose bytes differ, and no comparison of their
    pixels tells every such copy from another picture on the same template
    with one character of small text changed, so those files stay.
    """
    from .duplicates import compute_file_hash

    digest = compute_file_hash(first)
    return bool(digest) and digest == compute_file_hash(second)


_ORIGINAL_SAME_CONTENT = _same_content


def kept_copy(store: HistoryStore, path: str, signature, keepers) -> str:
    """A different file with the same bytes that exists right now, or ''.

    A file sent away as "a copy" must leave such a file behind. Another record
    of the same file, another spelling of its path or a hard link to it is the
    file itself, however the catalog lists it.
    """
    from .duplicates import compute_file_hash

    own = os.path.normcase(os.path.realpath(path))
    matching = []
    for keeper in keepers if isinstance(keepers, (list, tuple)) else ():
        try:
            if not isinstance(keeper, str) or not keeper or not store.is_media_path(keeper):
                continue
            other = file_signature(keeper)          # an ordinary file, not a link
            if other[2] != signature[2]:
                continue
            if os.path.normcase(os.path.realpath(keeper)) == own:
                continue
            if other[1] and signature[1] and other[:2] == tuple(signature[:2]):
                continue                            # the same file number on the same volume
            matching.append((keeper, other))
        except (OSError, ValueError, TypeError):
            continue
    if not matching:
        return ""
    if _same_content is not _ORIGINAL_SAME_CONTENT or len(matching) == 1:
        for keeper, other in matching:
            try:
                if file_signature(path) != tuple(signature):
                    return ""                       # the file to move changed meanwhile
                if _same_content(Path(path), Path(keeper)) and file_signature(keeper) == other:
                    return keeper
            except (OSError, ValueError, TypeError):
                continue
        return ""
    own_hash = ""
    for keeper, other in matching:
        try:
            if file_signature(path) != tuple(signature):
                return ""                           # the file to move changed meanwhile
            if not own_hash:
                own_hash = compute_file_hash(Path(path))
                if not own_hash or file_signature(path) != tuple(signature):
                    return ""
            if own_hash == compute_file_hash(Path(keeper)) and file_signature(keeper) == other:
                return keeper
        except (OSError, ValueError, TypeError):
            continue
    return ""


def kept_alike(store: HistoryStore, path: str, signature, keeper, keeper_signature) -> str:
    """The photo a person compared this one with, still there and still the same picture, or ''.

    A photo the duplicate view shows only as alike ('비슷함': another size or
    quality, other bytes) leaves only when a person chose it alone, looked at
    it beside the photo its group keeps and confirmed. Its bytes prove
    nothing, so what is checked is that the photo they saw beside it is the
    very file still standing (``keeper_signature``, taken when the two were
    shown), that it is another file and not this one under another record,
    name or link, and that both looks of the near rule still find one picture.
    """
    try:
        if not isinstance(keeper, str) or not keeper or not store.is_media_path(keeper):
            return ""
        expected = tuple(keeper_signature) if isinstance(keeper_signature, (list, tuple)) else ()
        other = file_signature(keeper)              # an ordinary file, not a link
        if other != expected:
            return ""                               # not the file that was shown
        if os.path.normcase(os.path.realpath(keeper)) == os.path.normcase(os.path.realpath(path)):
            return ""
        if other[1] and signature[1] and other[:2] == tuple(signature[:2]):
            return ""                               # the same file number on the same volume
        if file_signature(path) != tuple(signature):
            return ""                               # the photo to move changed meanwhile
        from .duplicates import nearly_identical, pixel_vector, same_in_detail

        if not (nearly_identical(pixel_vector(path), pixel_vector(keeper)) and same_in_detail(path, keeper)):
            return ""
        if file_signature(keeper) != expected or file_signature(path) != tuple(signature):
            return ""                               # one of them changed while they were read
        return keeper
    except (OSError, ValueError, TypeError):
        return ""


def settle_records(location: StoragePaths) -> bool:
    """Write the records copy into the media folder and this PC's backup of the catalog.

    A deletion does both itself unless it is told to leave them (``settle=False``),
    which the gallery does so that photos put away one after another do not each
    copy the whole catalog twice (and, in a synced folder, upload it again each
    time). Returns False when it cannot be done now: a save holds the account or
    the folder and writes both at its own end, or the folder is not connected.
    """
    if not isinstance(location, StoragePaths):
        return False
    account_guard, guard = None, None
    try:
        if not location.output_dir.is_dir():
            # A moved or disconnected output must not be recreated for its record copy.
            return False
        account_guard = AccountGuard(location.account, sleep=lambda *_: None)
        account_guard.acquire(respect_cooldown=False)
        guard = OutputGuard(location.output_dir)
        guard.acquire()
        publish_snapshot(location)
        return backup_catalog(location.record_dir, location.account) is not None
    except Exception:
        return False            # the next save writes both again
    finally:
        for held in (guard, account_guard):
            if held is not None:
                try:
                    held.close()
                except Exception:
                    pass


def run_deletion(location: StoragePaths | Path, rows: list[dict], keep_deleted: bool,
                 stop: threading.Event, emit, *, settle: bool = True) -> dict:
    """Recycle only the confirmed physical files, preserving their media history.

    Intent is committed before every native operation. An uncertain receipt or
    catalog write stops the batch; the next protected job settles any remaining
    intent without repeating a deletion. Native code never falls back to erase.

    A row from the duplicate view carries ``_copy_keepers``, the other files of
    its group. It moves only while one of them is, at that moment, a different
    file with the same bytes; otherwise it stays and is counted in
    ``protected``. Each file is checked when its turn comes, so the last file
    of a group can never go. A photo grouped only as the same picture at
    another size or quality (``_copy_rule`` "near") therefore stays.

    A row carrying ``_alike_keeper`` is such a photo that a person chose alone
    and compared with the photo its group keeps (``kept_alike``). It moves
    only as the whole batch, one file, and only while that photo still stands
    beside it; never together with other rows.

    With ``settle=False`` the records copy in the media folder and the backup
    of the catalog are left to the caller (``settle_records``); the result then
    says so with ``unsettled``.
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
    near_kept = False
    alike_kept = 0
    batch = uuid.uuid4().hex

    def progress(message):
        emit(dict(result, type="progress", processed=processed, message=message))

    def finished():
        result["cancelled"] = result["total"] - processed
        result["message"] = f"휴지통 이동 {result['recycled']:,}개"
        if result["failed"]:
            result["message"] += f" · 실패 {result['failed']:,}개"
        if result["protected"]:
            result["message"] += f" · 그대로 둔 파일 {result['protected']:,}개"
        if result["cancelled"]:
            result["message"] += f" · 처리하지 않은 파일 {result['cancelled']:,}개"
        if result["protected"]:
            if alike_kept:
                result["message"] += "\n" + _ALIKE_NOT_KEPT
            if result["protected"] > alike_kept:
                result["message"] += "\n" + _NO_OTHER_COPY
            if near_kept:
                result["message"] += "\n" + _NEAR_ONLY
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
            raise _DeletionMessageError("삭제 설정을 확인해 주세요.")
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
                raise _DeletionMessageError("기록된 저장 폴더를 찾을 수 없습니다. 파일 위치를 확인해 주세요.")
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
                if cloud_only(row["path"]):
                    raise RecyclingError(_CLOUD_ONLY)
                signature = file_signature(row["path"])
                expected = row.get("_delete_signature")
                if expected is not None and tuple(expected) != signature:
                    raise RecyclingError("선택한 뒤 파일이 바뀌었습니다. 목록을 새로고침해 주세요.")
                manifest.append((row, signature, ""))
            except (OSError, ValueError, TypeError) as exc:
                message = (str(exc) if isinstance(exc, (RecyclingError, HistoryError, ProtectionError, _DeletionMessageError))
                           else "파일을 확인하지 못했습니다.")
                manifest.append((row, None, message))

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
                    try:
                        ident = store.begin_deletion(batch, row, keep_deleted, failed_signature)
                        store.finish_deletion(ident, "failed", error)
                    except HistoryError:
                        pass
                result["failed"] += 1
                processed += 1
                failures.append(f"{Path(path).name}: {error}")
                progress(failures[-1])
                continue
            if "_alike_keeper" in row:
                # One compared photo is the whole batch: never part of a group, a selection or a bulk choice.
                if (len(rows) != 1 or "_copy_keepers" in row
                        or not kept_alike(store, path, signature, row.get("_alike_keeper"),
                                          row.get("_alike_keeper_signature"))):
                    result["protected"] += 1
                    alike_kept += 1
                    processed += 1
                    progress(f"{Path(path).name}: {_ALIKE_NOT_KEPT}")
                    continue
            elif "_copy_keepers" in row and not kept_copy(store, path, signature, row.get("_copy_keepers")):
                # Nothing is recorded: the file and its records stay as they are.
                result["protected"] += 1
                near_kept = near_kept or row.get("_copy_rule") == "near"
                processed += 1
                progress(f"{Path(path).name}: {_NO_OTHER_COPY}")
                continue
            ident = store.begin_deletion(batch, row, keep_deleted, signature)
            if stop.is_set():
                store.finish_deletion(ident, "cancelled", "중단 요청으로 파일을 그대로 두었습니다.")
                break
            try:
                if cloud_only(path):
                    raise RecyclingError(_CLOUD_ONLY)
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
            except OSError:
                absent = store._deletion_path_absent(path)
                result["failed"] += 1
                processed += 1
                store.finish_deletion(ident, "interrupted" if absent else "failed", "파일을 옮기지 못했습니다.")
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
        detail = (str(exc) if isinstance(exc, (RecyclingError, HistoryError, ProtectionError, _DeletionMessageError))
                  else "파일 상태와 권한을 확인한 뒤 다시 시도해 주세요.")
        warnings.append(f"작업을 중단했습니다. {detail}")
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
            if closed and isinstance(location, StoragePaths) and not settle:
                # The caller writes the records copy and the backup once things are quiet.
                result["unsettled"] = True
            elif closed and isinstance(location, StoragePaths):
                try:
                    # A moved or disconnected output must not be replaced by
                    # an empty folder just to publish its record copy.
                    if location.output_dir.is_dir():
                        publish_snapshot(location)
                except Exception:
                    warnings.append("저장 폴더의 백업 기록은 다음 저장 때 함께 반영합니다.")
                try:
                    if backup_catalog(location.record_dir, location.account) is None:
                        warnings.append("이 PC의 기록 백업을 만들지 못했습니다.")
                except Exception:
                    warnings.append("이 PC의 기록 백업을 만들지 못했습니다.")
        for guard in reversed(guards):
            try:
                guard.close()
            except Exception:
                warnings.append("저장 폴더의 작업 정리를 마치지 못했습니다. 앱을 다시 시작해 주세요.")
        if account_guard is not None:
            try:
                account_guard.close()
            except Exception:
                warnings.append("계정 작업 정리를 마치지 못했습니다. 앱을 다시 시작해 주세요.")
    return finished()
