"""Verified Windows Recycle Bin operations, without permanent-delete fallback.

Unlike the GIF cleanup helper, library deletion needs a per-item Shell receipt.
IFileOperation uses FOFX_RECYCLEONDELETE and a progress sink rejects delete
callbacks without TSF_DELETE_RECYCLE_IF_POSSIBLE. A successful result additionally
requires the recycled data file and its original-path/size receipt to agree.

API references:
https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-ifileoperation-setoperationflags
https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-ifileoperationprogresssink-predeleteitem
https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-ifileoperationprogresssink-postdeleteitem
"""

from __future__ import annotations

import ctypes
import os
import stat
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


class RecyclingError(OSError):
    """A safe failure, or an explicitly uncertain outcome needing review."""

    def __init__(self, message: str, *, uncertain: bool = False, recycled_path: str = ""):
        super().__init__(message)
        self.uncertain = uncertain
        self.recycled_path = recycled_path


def file_signature(path: Path | str) -> tuple[int, int, int, int]:
    """Identify a regular, non-reparse file; the tuple is JSON serializable."""
    path = Path(os.path.abspath(path))
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or _linked(info):
            raise RecyclingError("일반 파일만 휴지통으로 보낼 수 있습니다. 링크는 건너뜁니다.")
        for parent in path.parents:
            if _linked(parent.lstat()):
                raise RecyclingError("연결된 폴더 안의 파일은 안전하게 확인할 수 없어 건너뜁니다.")
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
    except OSError as exc:
        if isinstance(exc, RecyclingError):
            raise
        raise RecyclingError("파일을 확인하지 못했습니다. 파일 위치와 사용 중인 프로그램을 확인해 주세요.") from exc


# Name-surrogate reparse points (symbolic links, junctions, mount points) name
# another location. Cloud placeholders such as OneDrive or Dropbox files are
# other reparse kinds that hold the file itself: Python's lstat opens those as
# ordinary files with no reparse tag, exactly as the catalog already treats them.
_NAME_SURROGATE = 0x20000000


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_reparse_tag", 0) & _NAME_SURROGATE)


def _signature(value) -> tuple[int, int, int, int]:
    try:
        if len(value) != 4 or any(type(item) is not int or item < 0 for item in value):
            raise ValueError
        return tuple(value)
    except (TypeError, ValueError):
        raise RecyclingError("파일 확인 정보가 올바르지 않습니다. 목록을 새로 고침해 주세요.") from None


def _same_path(left: Path | str, right: Path | str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _receipt_data(path: Path) -> tuple[str, int]:
    """Read the Windows $I record (Vista v1 and Windows 10/11 v2)."""
    # Even the longest supported Windows path fits here. A corrupt record
    # must not allocate arbitrary amounts of memory while confirming a move.
    maximum = 28 + 32768 * 2
    with path.open("rb") as receipt:
        data = receipt.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("oversized recycle receipt")
    if len(data) < 24:
        raise ValueError("short recycle receipt")
    version, size, _deleted = struct.unpack_from("<QQQ", data)
    if version == 1:
        name = data[24:].decode("utf-16-le").split("\0", 1)[0]
    elif version == 2 and len(data) >= 28:
        length, = struct.unpack_from("<I", data, 24)
        if not 1 <= length <= 32768 or len(data) < 28 + length * 2:
            raise ValueError("invalid recycle path length")
        name = data[28:28 + length * 2].decode("utf-16-le").rstrip("\0")
    else:
        raise ValueError("unknown recycle receipt")
    if not name or not Path(name).is_absolute():
        raise ValueError("invalid recycle original path")
    return name, size


def verify_receipt(original: Path | str, destination: Path | str, expected) -> bool:
    """Verify an existing Shell receipt without moving or deleting anything."""
    try:
        expected = _signature(expected)
        original, destination = Path(os.path.abspath(original)), Path(os.path.abspath(destination))
        if (destination.name[:2].upper() != "$R"
                or "$RECYCLE.BIN" not in (part.upper() for part in destination.parts)
                or destination.anchor.upper() != original.anchor.upper()):
            return False
        # Confirm the destination and its ancestors are still ordinary paths,
        # using the same checks made before the source operation.
        signature = file_signature(destination)
        receipt = destination.with_name("$I" + destination.name[2:])
        file_signature(receipt)
        name, size = _receipt_data(receipt)
        return signature == expected and size == expected[2] and _same_path(name, original)
    except (OSError, ValueError, UnicodeError):
        return False


@dataclass
class _Outcome:
    operation_result: int = 0
    aborted: bool = False
    post_result: int | None = None
    destination: str = ""
    pre_error: str = ""


_HRESULT = ctypes.c_int32
_DWORD = ctypes.c_uint32
_PTR = ctypes.c_void_p
_CALLBACK = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
# FOFX_RECYCLEONDELETE | FOFX_EARLYFAILURE | FOFX_ADDUNDORECORD | FOF_ALLOWUNDO |
# FOF_NOERRORUI | FOF_SILENT | FOF_NO_CONNECTED_ELEMENTS, plus FOF_NOCONFIRMATION
# with FOF_WANTNUKEWARNING: the app has already asked, so Explorer's optional
# per-file prompt (which has no owner window here and can hide behind the app)
# is skipped, yet a permanent deletion still warns instead of being answered
# Yes automatically. PreDeleteItem aborts such a deletion either way.
_RECYCLE_FLAGS = 0x00080000 | 0x00100000 | 0x20000000 | 0x0040 | 0x0400 | 0x0004 | 0x2000 | 0x0010 | 0x4000
_E_ABORT = -2147467260
_E_NOINTERFACE = -2147467262


class _Guid(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]

    @classmethod
    def from_string(cls, value):
        return cls((ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le))


_IID_UNKNOWN = _Guid.from_string("00000000-0000-0000-C000-000000000046")
_IID_SINK = _Guid.from_string("04B0F1A7-9490-44BC-96E1-4296A31252E2")
_IID_SHELL_ITEM = _Guid.from_string("43826D1E-E718-42EE-BC55-A1E261C37BFE")
_IID_FILE_OPERATION = _Guid.from_string("947AAB5F-0A5C-4C13-B4D6-4BF7836FC9F8")
_CLSID_FILE_OPERATION = _Guid.from_string("3AD05575-8857-4850-9277-11B85BDB8E09")


def _method(pointer, index, restype, *argtypes):
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(_PTR))).contents
    return _CALLBACK(restype, _PTR, *argtypes)(table[index])


def _display_name(item, ole32) -> str:
    value = _PTR()
    result = _method(item, 5, _HRESULT, _DWORD, ctypes.POINTER(_PTR))(item, 0x80058000, ctypes.byref(value))
    if result < 0 or not value.value:
        return ""
    try:
        return ctypes.wstring_at(value.value)
    finally:
        ole32.CoTaskMemFree(value)


class _Sink:
    """Keep all COM callback functions alive until PerformOperations returns."""

    def __init__(self, path: Path, signature, outcome: _Outcome, ole32):
        self.path, self.signature, self.outcome, self.ole32 = path, signature, outcome, ole32
        self.references = 1
        self.callbacks = []

        def callback(restype, argument_types, function):
            native = _CALLBACK(restype, _PTR, *argument_types)(function)
            self.callbacks.append(native)
            return ctypes.cast(native, _PTR).value

        entries = [
            callback(_HRESULT, (ctypes.POINTER(_Guid), ctypes.POINTER(_PTR)), self.query_interface),
            callback(_DWORD, (), self.add_ref),
            callback(_DWORD, (), self.release),
        ]
        specifications = [
            ((), None), ((_HRESULT,), None),
            ((_DWORD, _PTR, ctypes.c_wchar_p), None),
            ((_DWORD, _PTR, ctypes.c_wchar_p, _HRESULT, _PTR), None),
            ((_DWORD, _PTR, _PTR, ctypes.c_wchar_p), None),
            ((_DWORD, _PTR, _PTR, ctypes.c_wchar_p, _HRESULT, _PTR), None),
            ((_DWORD, _PTR, _PTR, ctypes.c_wchar_p), None),
            ((_DWORD, _PTR, _PTR, ctypes.c_wchar_p, _HRESULT, _PTR), None),
            ((_DWORD, _PTR), self.pre_delete),
            ((_DWORD, _PTR, _HRESULT, _PTR), self.post_delete),
            ((_DWORD, _PTR, ctypes.c_wchar_p), None),
            ((_DWORD, _PTR, ctypes.c_wchar_p, ctypes.c_wchar_p, _DWORD, _HRESULT, _PTR), None),
            ((_DWORD, _DWORD), None), ((), None), ((), None), ((), None),
        ]
        for argument_types, function in specifications:
            entries.append(callback(_HRESULT, argument_types, function or (lambda *_: 0)))
        self.table = (_PTR * len(entries))(*entries)

        class Instance(ctypes.Structure):
            _fields_ = [("vtable", ctypes.POINTER(_PTR))]

        self.instance = Instance(self.table)
        self.pointer = ctypes.cast(ctypes.pointer(self.instance), _PTR)

    def query_interface(self, _this, iid, output):
        output[0] = None
        if bytes(iid.contents.data) not in (bytes(_IID_UNKNOWN.data), bytes(_IID_SINK.data)):
            return _E_NOINTERFACE
        output[0] = self.pointer.value
        self.references += 1
        return 0

    def add_ref(self, _this):
        self.references += 1
        return self.references

    def release(self, _this):
        self.references = max(0, self.references - 1)
        return self.references

    def pre_delete(self, _this, flags, item):
        try:
            # A callback without this flag would allow irreversible deletion.
            if not flags & 0x80:
                raise RecyclingError("Windows 휴지통을 사용할 수 없어 건너뛰었습니다.")
            if not _same_path(_display_name(item, self.ole32), self.path):
                raise RecyclingError("Windows에서 다른 파일을 선택해 작업을 취소했습니다.")
            if file_signature(self.path) != self.signature:
                raise RecyclingError("선택한 뒤 파일이 바뀌어 건너뛰었습니다. 목록을 새로 고침해 주세요.")
            return 0
        except Exception as exc:
            self.outcome.pre_error = str(exc) or "휴지통 이동 확인에 실패했습니다."
            return _E_ABORT

    def post_delete(self, _this, _flags, _item, result, destination):
        self.outcome.post_result = result
        if destination:
            try:
                self.outcome.destination = _display_name(destination, self.ole32)
            except Exception:
                pass
        return 0


def _run_native(path: Path, signature) -> _Outcome:
    ole32, shell32, kernel32 = (ctypes.WinDLL(name) for name in ("ole32", "shell32", "kernel32"))
    ole32.CoInitializeEx.argtypes, ole32.CoInitializeEx.restype = [_PTR, _DWORD], _HRESULT
    ole32.CoUninitialize.argtypes = []
    ole32.CoTaskMemFree.argtypes = [_PTR]
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_Guid), _PTR, _DWORD, ctypes.POINTER(_Guid), ctypes.POINTER(_PTR)]
    ole32.CoCreateInstance.restype = _HRESULT
    shell32.SHCreateItemFromParsingName.argtypes = [ctypes.c_wchar_p, _PTR, ctypes.POINTER(_Guid), ctypes.POINTER(_PTR)]
    shell32.SHCreateItemFromParsingName.restype = _HRESULT
    kernel32.GetDriveTypeW.argtypes, kernel32.GetDriveTypeW.restype = [ctypes.c_wchar_p], _DWORD
    # Network/removable drives may have no usable recycle location. Skip them.
    if kernel32.GetDriveTypeW(path.anchor) != 3:
        raise RecyclingError("이 저장 위치에서는 Windows 휴지통을 사용할 수 없어 건너뛰었습니다.")
    initialized = ole32.CoInitializeEx(None, 2)
    if initialized < 0:
        raise RecyclingError("Windows 휴지통 연결을 시작하지 못했습니다.")
    operation, item = _PTR(), _PTR()
    outcome = _Outcome()

    def checked(result):
        if result < 0:
            raise RecyclingError("Windows 휴지통 이동에 실패했습니다. 파일을 그대로 두었습니다.")

    try:
        checked(ole32.CoCreateInstance(ctypes.byref(_CLSID_FILE_OPERATION), None, 1,
                                      ctypes.byref(_IID_FILE_OPERATION), ctypes.byref(operation)))
        checked(shell32.SHCreateItemFromParsingName(str(path), None, ctypes.byref(_IID_SHELL_ITEM), ctypes.byref(item)))
        sink = _Sink(path, signature, outcome, ole32)
        checked(_method(operation, 5, _HRESULT, _DWORD)(operation, _RECYCLE_FLAGS))
        checked(_method(operation, 18, _HRESULT, _PTR, _PTR)(operation, item, sink.pointer))
        outcome.operation_result = _method(operation, 21, _HRESULT)(operation)
        aborted = ctypes.c_int32()
        result = _method(operation, 22, _HRESULT, ctypes.POINTER(ctypes.c_int32))(operation, ctypes.byref(aborted))
        outcome.aborted = result < 0 or bool(aborted.value)
        return outcome
    finally:
        if item.value:
            _method(item, 2, _DWORD)(item)
        if operation.value:
            _method(operation, 2, _DWORD)(operation)
        ole32.CoUninitialize()


def recycle_file(path: Path | str, expected=None) -> str:
    """Recycle one unchanged file, returning its verified Recycle Bin path."""
    if os.name != "nt":
        raise RecyclingError("파일 삭제는 Windows 휴지통에서만 지원합니다.")
    path = Path(os.path.abspath(path))
    signature = file_signature(path)
    if expected is not None and signature != _signature(expected):
        raise RecyclingError("선택한 뒤 파일이 바뀌어 건너뛰었습니다. 목록을 새로 고침해 주세요.")
    outcome = None
    try:
        outcome = _run_native(path, signature)
        missing = not path.exists()
        if (missing and outcome.post_result is not None and outcome.post_result >= 0 and outcome.destination
                and outcome.operation_result >= 0 and not outcome.aborted):
            # Windows may release the $I handle shortly after PostDeleteItem.
            for delay in (0, 0.05, 0.1, 0.2):
                if delay:
                    time.sleep(delay)
                if verify_receipt(path, outcome.destination, signature):
                    return outcome.destination
        message = outcome.pre_error or "휴지통 이동을 확인하지 못했습니다. 파일과 휴지통을 확인해 주세요."
        raise RecyclingError(message, uncertain=missing, recycled_path=outcome.destination)
    except RecyclingError as exc:
        if not path.exists() and not exc.uncertain:
            raise RecyclingError("파일이 사라졌지만 휴지통 이동을 확인하지 못했습니다. 휴지통을 확인해 주세요.",
                                 uncertain=True, recycled_path=outcome.destination if outcome else "") from exc
        raise
    except (OSError, ValueError) as exc:
        raise RecyclingError("Windows 휴지통 이동을 확인하지 못했습니다.", uncertain=not path.exists(),
                             recycled_path=outcome.destination if outcome else "") from exc
