"""Isolated gallery-dl media worker.

Run one worker per process. gallery-dl uses process-global configuration and
caches, so this module deliberately does not support concurrent in-process jobs.
"""

from __future__ import annotations

import copy
import inspect
import json
import logging
import os
import re
import sqlite3
import stat
import tempfile
import time
from contextlib import closing, nullcontext
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from datetime import datetime as _Datetime
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlsplit

from .protection import API_INTERVAL, MEDIA_INTERVAL, AccountGuard, ProtectionError
from .history import CatalogDamaged, HistoryError, HistoryStore, _author_fields
from .storage import OutputGuard, missing_media_help, resolve_storage, saved_media_missing
from .media import GIF_SIZES, ConversionError, ConversionSizeLimit, convert_mp4_to_gif
from .media import move_without_overwrite as _move_without_overwrite


EventSink = Callable[[dict], None]


class SaveReview(Protocol):
    """A manual run's live option and cancellable, secret-free preview prompt."""

    def enabled(self) -> bool: ...

    def ask(self, path: Path, media: dict, check_stop: Callable[[], None]) -> str:
        """'save', 'skip' (this run only) or 'never' (do not ask again)."""


_ACCOUNT = re.compile(r"[A-Za-z0-9_]{1,15}\Z")
_ROUTES = {"i", "home", "explore", "search", "settings", "notifications",
           "messages", "compose", "intent", "login", "logout", "signup",
           "hashtag", "account", "privacy", "tos", "oauth"}
_AUTH_MODES = {"file", "managed"}
_FOLDER_MODES = {"author", "date", "flat"}
_SCAN_MODES = {"full", "quick"}
# Missing files chosen for downloading again, per run. Each tweet costs one
# paced X request, so a larger choice is better split across runs.
RETRY_LIMIT = 2000
_RETRY_KEY = re.compile(r"([0-9]{1,20})_(?:(video|animated_gif)_)?([0-9]{1,5})\Z")
# A quick scan ends after this many already-saved files in a row. Likes are
# listed newest-like first, so this is the point where the previous complete
# run left off. Unsaved files never count toward the streak.
QUICK_STOP_AFTER = 100
_KOREA = timezone(timedelta(hours=9))
_RECORD_ERROR = ("저장 기록을 읽거나 갱신하지 못했습니다. "
                 "저장 폴더의 권한과 다른 실행 중인 프로그램을 확인해 주세요.")
_COMPATIBILITY_ERROR = ("내장 gallery-dl이 예상과 달라 요청 전에 멈췄습니다. "
                        "실행 파일을 다시 받거나 setup.ps1로 다시 설치해 주세요.")
_HISTORY_ERROR = "저장 이력을 기록하지 못했습니다. 저장 폴더의 쓰기 권한과 디스크 여유 공간을 확인해 주세요."
_METADATA_ERROR = "출처 또는 작성자를 기록하지 못했습니다. 미디어 원본은 그대로 보존했습니다."
_SNAPSHOT_ERROR = "저장 폴더에 기록 사본을 쓰지 못했습니다. 기록은 이 PC에 있고 다음 저장 때 다시 씁니다."
_DATE_MOVE_ERROR = "저장 완료일 폴더로 옮기지 못했습니다. 기존 파일 위치와 중복 방지 기록은 보존했습니다."
_LOCAL_WRITE_ERROR = ("파일을 쓰지 못해 멈췄습니다. 폴더 쓰기 권한, 디스크 공간, 파일을 쓰는 다른 프로그램을 "
                      "확인한 뒤 다시 실행해 주세요. 받은 파일은 그대로 있습니다.")
_REVIEW_ERROR = ("저장 여부 확인을 완료하지 못해 멈췄습니다. 다시 실행해 주세요. "
                 "받은 임시 파일은 보존했습니다.")


def _save_day() -> date:
    """The download's local saving day, independent of tweet publication."""
    return datetime.now(_KOREA).date()


def _completion_paths(source: Path) -> tuple[Path, ...]:
    return tuple(Path(str(source) + suffix) for suffix in
                 (".luvbox-complete", ".xlikesaver-complete"))


def _transfer_complete(source: Path, *, marker_path: Path | None = None) -> dict | None:
    """The record our downloader left on completing this .part, or None.

    Only such a file is used again without a request. Asking the server to
    resume a complete file is answered with 416, which stops the run. Sync
    tools and FAT drives round modification times, so up to two seconds of
    difference still describe the same file; its size must match exactly.
    """
    paths = (marker_path,) if marker_path is not None else _completion_paths(source)
    for path in paths:
        try:
            signature = _review_file_signature(path)
            with path.open(encoding="utf-8") as stream:
                marker = json.loads(stream.read(1024))
            if _review_file_signature(path) != signature:
                continue
            source_signature = _review_file_signature(source)
            modified = marker.get("mtime_http")
            if (marker.get("version") != 1 or type(marker.get("size")) is not int or
                    marker["size"] != source_signature[2] or type(marker.get("mtime_ns")) is not int or
                    abs(marker["mtime_ns"] - source_signature[3]) > 2_000_000_000 or
                    not (modified is None or isinstance(modified, str) and len(modified) <= 100)):
                continue
            return marker
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return None


def _valid_completion_markers(source: Path) -> list[tuple[Path, tuple]]:
    result = []
    for marker in _completion_paths(source):
        try:
            signature = _review_file_signature(marker)
            if (_transfer_complete(source, marker_path=marker) is not None and
                    _review_file_signature(marker) == signature):
                result.append((marker, signature))
        except OSError:
            pass
    return result


def _clear_completion_markers(markers) -> None:
    for marker, signature in markers:
        try:
            if _review_file_signature(marker) == signature:
                marker.unlink()
        except OSError:
            pass


def _mark_transfer_complete(source: Path, modified=None) -> None:
    stat = source.stat()
    record = {"version": 1, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    if isinstance(modified, str) and len(modified) <= 100:
        # The server's Last-Modified, which a saved file is stamped with.
        record["mtime_http"] = modified
    fd, name = tempfile.mkstemp(prefix=".luvbox-complete-", dir=source.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
        os.replace(temporary, str(source) + ".luvbox-complete")
    finally:
        temporary.unlink(missing_ok=True)


def _review_file_signature(path: Path) -> tuple[int, int, int, int]:
    """Identify an ordinary temporary file before and after a preview wait."""
    details = path.lstat()
    if (not stat.S_ISREG(details.st_mode) or
            getattr(details, "st_file_attributes", 0) & 0x400):
        raise OSError("unsafe review temporary file")
    return details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns


def _review_path(value: str) -> Path:
    # gallery-dl writes through Windows extended paths; the catalog's allowed
    # roots and the GUI use their ordinary drive/UNC spelling.
    if os.name == "nt":
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
    return Path(value)


def _media_details(metadata: dict) -> dict:
    posted = metadata.get("date")
    if isinstance(posted, _Datetime) and posted:
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        posted = posted.astimezone(_KOREA).isoformat()
    else:
        posted = ""
    author, author_id = _author_fields(metadata.get("author"))
    tweet_id = str(metadata.get("tweet_id", ""))
    actual_path = metadata.get("_luvbox_path")
    prefix = metadata.get("_luvbox_filename_prefix", author + "_" if author else "")
    part = metadata.get("_luvbox_filename_part", metadata.get("num", 0))
    filename = (_review_path(actual_path).name if actual_path else
                f"{prefix}{tweet_id}_{part}.{metadata.get('extension', '')}")
    return {"tweet_id": tweet_id, "num": metadata.get("num", 0),
            "media_type": metadata.get("type", ""),
            "author": author, "author_id": author_id,
            "posted_at": posted,
            "filename": filename,
            "source_url": f"https://x.com/i/web/status/{tweet_id}" if re.fullmatch(r"[0-9]{1,20}", tweet_id) else "",
            "metadata_status": metadata.get("_luvbox_metadata_status", "pending"),
            "text_metadata_status": metadata.get("_luvbox_text_metadata_status", "unknown"),
            "author_metadata_status": metadata.get("_luvbox_author_metadata_status", "pending")}


class GalleryCompatibilityError(RuntimeError):
    """The pinned gallery-dl internals this worker replaces have changed."""


# run_worker replaces these private gallery-dl members. Their names and leading
# parameters are checked before any request, so a gallery-dl upgrade that
# renames or reshapes one fails loudly instead of silently skipping a hook.
_GALLERY_HOOKS = (
    ("gallery_dl.extractor.twitter", "TwitterExtractor", "_extract_media", ("self", "tweet", "entities", "files")),
    ("gallery_dl.extractor.twitter", "TwitterExtractor", "_extract_media_source", ("self", "dest", "media")),
    ("gallery_dl.extractor.common", "Extractor", "_init_session", ("self",)),
    ("gallery_dl.extractor.common", "Extractor", "_init_cookies", ("self",)),
    ("gallery_dl.extractor.common", "Extractor", "cookies_check", ("self", "cookies_names")),
    ("gallery_dl.extractor.common", "Extractor", "sleep", ("self", "seconds", "reason")),
    ("gallery_dl.extractor.common", "Extractor", "wait", ("self",)),
    ("gallery_dl.extractor.common", "Extractor", "input", ("self",)),
    ("gallery_dl.job", "DownloadJob", "handle_url", ("self", "url", "kwdict")),
    ("gallery_dl.job", "DownloadJob", "handle_directory", ("self", "kwdict")),
    ("gallery_dl.job", "DownloadJob", "initialize", ("self", "kwdict")),
    ("gallery_dl.job", "DownloadJob", "download", ("self", "url")),
    ("gallery_dl.job", "DownloadJob", "get_downloader", ("self", "scheme")),
    ("gallery_dl.job", "Job", "_init", ("self",)),
    ("gallery_dl.downloader.http", "HttpDownloader", "download", ("self", "url", "pathfmt")),
    ("gallery_dl.downloader.http", "HttpDownloader", "receive", ("self", "fp", "content", "bytes_total", "bytes_start")),
    ("gallery_dl.output", "NullOutput", "success", ("self", "path")),
    ("gallery_dl.output", "NullOutput", "skip", ("self", "path")),
    ("gallery_dl.path", "PathFormat", "part_enable", ("self", "part_directory")),
    ("gallery_dl.path", "PathFormat", "part_size", ("self",)),
    ("gallery_dl.path", "PathFormat", "set_extension", ("self", "extension")),
    ("gallery_dl.path", "PathFormat", "finalize", ("self",)),
    ("gallery_dl.path", "PathFormat", "set_mtime", ("self",)),
)
_GALLERY_NAMES = (
    ("gallery_dl.extractor.common", ("CACHE_COOKIES", "CACHE_MEMORY")),
    ("gallery_dl.util", ("cookiestxt_load",)),
    ("gallery_dl.exception", ("ControlException", "StopExtraction", "AbortExtraction", "AuthRequired")),
)


def check_gallery_hooks() -> None:
    """Verify the private gallery-dl API surface before sending any request."""
    import importlib

    try:
        for module_name, class_name, member, parameters in _GALLERY_HOOKS:
            function = getattr(getattr(importlib.import_module(module_name), class_name), member)
            names = tuple(inspect.signature(function).parameters)
            if names[:len(parameters)] != parameters:
                raise GalleryCompatibilityError(f"{class_name}.{member}")
        for module_name, names in _GALLERY_NAMES:
            module = importlib.import_module(module_name)
            for name in names:
                getattr(module, name)
    except GalleryCompatibilityError:
        raise
    except (ImportError, AttributeError, TypeError, ValueError):
        raise GalleryCompatibilityError("missing gallery-dl member") from None


def _normal_account(value: str) -> str:
    account = value.strip()
    if "://" in account or re.match(r"(?:www\.|mobile\.)?(?:x|twitter)\.com/", account, re.I):
        try:
            parsed = urlsplit(account if "://" in account else "https://" + account)
        except ValueError:
            raise ValueError("X 프로필 주소 또는 @아이디를 입력해 주세요.") from None
        if (parsed.scheme not in {"http", "https"} or parsed.username or
                parsed.password or parsed.netloc.lower() not in {
                    "x.com", "www.x.com", "mobile.x.com", "twitter.com",
                    "www.twitter.com", "mobile.twitter.com"}):
            raise ValueError("X 프로필 주소 또는 @아이디를 입력해 주세요.")
        parts = parsed.path.strip("/").split("/")
        if len(parts) > 2 or (len(parts) == 2 and parts[1] != "likes"):
            raise ValueError("트윗 주소 대신 본인 X 프로필 주소를 입력해 주세요.")
        account = parts[0]
    elif account.startswith("@"):
        account = account[1:]
    if not _ACCOUNT.fullmatch(account) or account.lower() in _ROUTES:
        raise ValueError("X 아이디는 영문, 숫자, 밑줄로 된 1~15자여야 합니다.")
    return account.lower()


def _validate_cookie_file(path: Path) -> None:
    """Inspect Netscape syntax and auth presence without exposing secret values."""
    try:
        data = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise ValueError("쿠키 파일을 읽을 수 없습니다. UTF-8 cookies.txt 파일을 선택해 주세요.") from None
    lines = data.splitlines()
    if not any(line.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File"))
               for line in lines):
        raise ValueError("Netscape cookies.txt 형식의 쿠키 파일을 선택해 주세요.")
    has_auth = False
    expired_auth = False
    for line in lines:
        if line.startswith("#HttpOnly_"):
            line = line[10:]
        elif not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            raise ValueError("쿠키 파일 형식이 올바르지 않습니다. Netscape 형식으로 다시 내보내 주세요.")
        domain, include_subdomains, cookie_path, secure, expires, name, value = fields
        try:
            expires_at = int(expires or "0")
        except ValueError:
            raise ValueError("쿠키 파일의 만료 시간이 올바르지 않습니다. 다시 내보내 주세요.") from None
        if (not domain or not cookie_path.startswith("/") or expires_at < 0 or
                include_subdomains not in {"TRUE", "FALSE"} or secure not in {"TRUE", "FALSE"}):
            raise ValueError("쿠키 파일 형식이 올바르지 않습니다. Netscape 형식으로 다시 내보내 주세요.")
        if domain.lstrip(".").lower() == "x.com" and name == "auth_token" and value:
            if not expires_at or expires_at > time.time():
                has_auth = True
            else:
                expired_auth = True
    if not has_auth:
        if expired_auth:
            raise ValueError("X 로그인 쿠키가 만료되었습니다. X에 다시 로그인하고 쿠키를 내보내 주세요.")
        raise ValueError("쿠키 파일에 X 로그인 쿠키가 없습니다. 본인 계정으로 x.com에 로그인한 뒤 다시 내보내 주세요.")


@dataclass(frozen=True)
class JobOptions:
    account: str
    output_dir: str
    auth_mode: str = "managed"
    cookies_file: str = ""
    photo_limit: int = 0
    folder_mode: str = "flat"
    scan_mode: str = "full"
    include_videos: bool = True
    include_gifs: bool = True
    max_video_mb: int = 200
    separate_accounts: bool = False
    # Animated GIF tweets: convert to .gif, or keep X's original MP4.
    gif_format: str = "gif"
    gif_size: str = "original"
    # Archive keys (history.archive_key) of missing files to download again by
    # tweet instead of walking the Like list. Empty for a normal save.
    targets: tuple = ()
    # One run's explicit confirmation; never restored from saved settings.
    allow_recreate_media: bool = False
    # Manual GUI runs can change this preference live through SaveReview.
    confirm_each: bool = False

    @property
    def media_signature(self) -> str:
        """Settings that decide which files a previous scan could have skipped."""
        return f"v1:videos={int(self.include_videos)}:gifs={int(self.include_gifs)}:max_mb={self.max_video_mb}"

    def validate(self) -> "JobOptions":
        account = _normal_account(self.account)
        if not isinstance(self.auth_mode, str) or self.auth_mode.strip().lower() not in _AUTH_MODES:
            raise ValueError("로그인 방법을 ‘브라우저 로그인’이나 ‘cookies.txt 파일’ 중에서 다시 골라 주세요.")
        auth_mode = self.auth_mode.strip().lower()
        if not isinstance(self.folder_mode, str) or self.folder_mode.strip().lower() not in _FOLDER_MODES:
            raise ValueError("저장 방식은 작성자별, 날짜별, 구분 없이 중 하나를 선택해 주세요.")
        folder_mode = self.folder_mode.strip().lower()
        if not isinstance(self.scan_mode, str) or self.scan_mode.strip().lower() not in _SCAN_MODES:
            raise ValueError("확인 범위는 ‘새 Like만 빠르게’ 또는 ‘전체 목록 다시 확인’ 중 하나를 선택해 주세요.")
        scan_mode = self.scan_mode.strip().lower()
        if type(self.include_videos) is not bool or type(self.include_gifs) is not bool:
            raise ValueError("동영상과 GIF 저장 여부를 확인해 주세요.")
        if type(self.separate_accounts) is not bool:
            raise ValueError("계정별 폴더 설정을 확인해 주세요.")
        if type(self.allow_recreate_media) is not bool:
            raise ValueError("사라진 저장 폴더를 새로 만드는 선택을 확인해 주세요.")
        if type(self.confirm_each) is not bool:
            raise ValueError("파일마다 저장 여부 확인 설정을 확인해 주세요.")
        if (not isinstance(self.gif_format, str) or self.gif_format not in {"gif", "mp4"} or
                not isinstance(self.gif_size, str) or self.gif_size not in GIF_SIZES):
            raise ValueError("움직이는 GIF 저장 형식과 GIF 크기를 다시 선택해 주세요.")
        if isinstance(self.max_video_mb, bool) or not isinstance(self.max_video_mb, int) or self.max_video_mb < 0:
            raise ValueError("동영상·GIF 크기 한도는 0 이상의 정수 MB로 입력해 주세요. 0은 제한 없음입니다.")
        if isinstance(self.photo_limit, bool) or not isinstance(self.photo_limit, int) or self.photo_limit < 0:
            raise ValueError("파일 저장 한도는 0 이상의 정수여야 합니다.")
        if not isinstance(self.targets, (list, tuple)):
            raise ValueError("다시 받을 파일 목록을 확인해 주세요.")
        targets = tuple(dict.fromkeys(self.targets))
        if len(targets) > RETRY_LIMIT or not all(isinstance(key, str) and _RETRY_KEY.fullmatch(key) and
                                                 1 <= int(_RETRY_KEY.fullmatch(key)[3]) <= 10000 for key in targets):
            raise ValueError(f"다시 받을 파일은 한 번에 {RETRY_LIMIT:,}개까지 고를 수 있습니다.")
        if not self.output_dir.strip():
            raise ValueError("미디어를 저장할 폴더를 선택해 주세요.")
        try:
            output = Path(self.output_dir).expanduser().resolve()
            if output.exists() and not output.is_dir():
                raise ValueError("저장 경로가 파일입니다. 폴더를 선택해 주세요.")
        except (OSError, RuntimeError):
            raise ValueError("저장 폴더 경로를 확인해 주세요.") from None
        cookie_path = ""
        if auth_mode == "file":
            if not self.cookies_file.strip():
                raise ValueError("X 로그인 쿠키가 있는 cookies.txt 파일을 선택해 주세요.")
            try:
                path = Path(self.cookies_file).expanduser().resolve()
            except (OSError, RuntimeError):
                raise ValueError("쿠키 파일 경로를 확인해 주세요.") from None
            _validate_cookie_file(path)
            cookie_path = str(path)
        return replace(self, account=account, output_dir=str(output),
                       auth_mode=auth_mode, cookies_file=cookie_path,
                       folder_mode=folder_mode, scan_mode=scan_mode, targets=targets,
                       photo_limit=0 if targets else self.photo_limit)


def _auth_help(mode: str) -> str:
    if mode == "managed":
        return "X 연결이 만료됐습니다. ‘X 연결’에서 브라우저를 다시 연결하거나 새 토큰을 입력해 주세요."
    return "X에 다시 로그인한 뒤 새 cookies.txt를 내보내 선택해 주세요."


# Match whole terms: a plain substring test would read gallery-dl's
# "Missing 'author' data" as an authentication failure.
_RATE_TERMS = re.compile(r"rate ?limit|\b429\b")
_STORAGE_TERMS = re.compile(r"permission|access is denied|read-only|disk full|no space|winerror 5\b")
_AUTH_TERMS = re.compile(r"\bdecrypt|\bdpapi\b|\bcookies?\b|\bauth(?:enticat\w*|oriz\w*|_token|required)?\b|"
                         r"\blog ?in\b|\bcredentials?\b|\b40[13]\b")
# Ordinary per-post conditions in a likes list. They are informational, never
# the reason a run failed, and never a hint to reconnect the account.
_SKIPPED_POST_NOTICES = (
    (re.compile(r"\Amedia unavailable\b"),
     "볼 수 없는 미디어(저작권·지역 제한 등)는 건너뜁니다."),
    (re.compile(r"\A'.*' \([0-9]+\)\Z", re.S),
     "국가 제한(withheld) 등으로 볼 수 없는 게시물은 건너뜁니다."),
    (re.compile(r"\A\"age-restricted"),
     "연령 제한 게시물은 X 계정의 민감한 콘텐츠 설정에 따라 보이지 않을 수 있습니다."),
    (re.compile(r"\A[0-9]+: missing 'author' data\Z"),
     "작성자 정보가 없는 게시물은 건너뛸 수 있습니다."),
    (re.compile(r"\A[0-9]+: error while extracting media files"),
     "일부 게시물의 미디어 정보를 읽지 못해 건너뛰었습니다."),
)


def _skipped_post_notice(message: str) -> str:
    text = message.strip().lower()
    return next((notice for pattern, notice in _SKIPPED_POST_NOTICES if pattern.search(text)), "")


def _safe_diagnostic(message: str, mode: str) -> str:
    """Map library diagnostics to fixed messages; never forward arbitrary secrets."""
    text = message.lower()
    if "history store unavailable" in text:
        return _HISTORY_ERROR
    if _RATE_TERMS.search(text):
        return "X 요청 한도에 도달해 중단했습니다. 잠시 후 다시 실행하면 저장한 파일은 건너뜁니다."
    if _STORAGE_TERMS.search(text):
        return "파일에 접근할 수 없습니다. 저장 폴더의 쓰기 권한과 디스크 여유 공간을 확인해 주세요."
    if _AUTH_TERMS.search(text):
        return _auth_help(mode)
    if "not found" in text or re.search(r"\b404\b", text):
        return "계정 또는 미디어를 찾지 못했습니다. 본인 X 아이디와 게시물의 공개 상태를 확인해 주세요."
    if any(part in text for part in ("timeout", "connection", "network", "ssl", "certificate")):
        return "X에 연결하지 못했습니다. 인터넷 연결을 확인한 뒤 다시 시도해 주세요."
    if "archive" in text:
        return "중복 저장 기록을 열지 못했습니다. 저장 폴더의 권한과 다른 실행 중인 프로그램을 확인해 주세요."
    if "download" in text:
        return "일부 파일을 저장하지 못했습니다. 연결과 저장 폴더를 확인한 뒤 다시 실행해 주세요."
    return "X 목록을 읽지 못했습니다. 잠시 뒤 다시 시도해 주세요."


# Worker aborts that end a run regardless of which post was being handled.
_FATAL_TERMS = ("history store unavailable", "unsafe", "download archive unavailable")


class _GalleryLogHandler(logging.Handler):
    def __init__(self, emit: EventSink, mode: str):
        super().__init__(logging.INFO)
        self._emit = emit
        self.mode = mode
        self.last_error = ""
        self._error_priority = 0
        self._last_message = ""
        self.rate_limited = False
        # After the user's stop, gallery-dl reports the interrupted request or
        # download as a failure ("None" text). Those are not real problems.
        self.stopping = False
        # Re-downloading by tweet reports each unavailable tweet itself; only
        # problems that must end the whole run are logged then.
        self.per_tweet = False
        self.fatal = False

    def emit(self, record: logging.LogRecord) -> None:
        # INFO can contain URLs, arbitrary server text or other user metadata.
        # Progress is emitted separately, so only mapped diagnostics are useful.
        if record.levelno < logging.WARNING or self.stopping:
            return
        raw = record.getMessage()
        if raw == "media size exceeded":
            return
        text = raw.lower()
        # gallery-dl uses AuthRequired for these two per-tweet visibility
        # results even with valid cookies. Keep every other authentication
        # error fatal, including either message accompanied by 401/403.
        if self.per_tweet and text in {"authrequired: protected tweet", "authrequired: nsfw tweet"}:
            return
        notice = _skipped_post_notice(raw) if record.levelno < logging.ERROR else ""
        if notice:
            level, message, priority = "info", notice, 0
        else:
            if _RATE_TERMS.search(text):
                self.rate_limited = True
            level = "error" if record.levelno >= logging.ERROR else "warning"
            message = _safe_diagnostic(raw, self.mode)
            priority = (3 if _RATE_TERMS.search(text) or _STORAGE_TERMS.search(text) or _AUTH_TERMS.search(text)
                        or any(term in text for term in _FATAL_TERMS)
                        else 1 if record.levelno >= logging.ERROR else 0)
            if priority == 3:
                self.fatal = True
            elif self.per_tweet:
                return
        if priority and priority >= self._error_priority:
            self.last_error = message
            self._error_priority = priority
        if message != self._last_message:
            self._last_message = message
            media = getattr(getattr(record, "job", None), "current_media", None)
            details = (_media_details(media) if record.name in {"download", "http"} and
                       isinstance(media, dict) and media.get("tweet_id") else {})
            self._emit({"type": "log", "level": level, "message": message, **details})


def _retry_groups(keys) -> list[tuple[str, list[str]]]:
    """Chosen archive keys grouped by tweet, in the order they were chosen."""
    groups = {}
    for key in keys:
        groups.setdefault(_RETRY_KEY.fullmatch(key)[1], []).append(key)
    return list(groups.items())


def _key_identity(key: str) -> tuple[str, int, str]:
    match = _RETRY_KEY.fullmatch(key)
    return match[1], int(match[3]), match[2] or "photo"


def _entry_key(entry: str) -> str:
    """An archive entry's key in the current spelling (no prefix, 'animated_gif')."""
    key = entry.removeprefix("twitter")
    return key.replace("_gif_", "_animated_gif_") if "_animated_gif_" not in key else key


def _archive_entries(archive: Path, keys) -> list[str]:
    """The archive's entries for these media, in every spelling it has used."""
    candidates = []
    for key in keys:
        spellings = [key] + ([key.replace("_animated_gif_", "_gif_")] if "_animated_gif_" in key else [])
        candidates.extend(prefix + spelling for spelling in spellings for prefix in ("twitter", ""))
    if not archive.is_file() or not candidates:
        return []
    with closing(sqlite3.connect(archive, timeout=5)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive'").fetchone():
            return []
        marks = ",".join("?" for _ in candidates)
        return [row[0] for row in db.execute("SELECT entry FROM archive WHERE entry IN (" + marks + ")", candidates)]


def _set_archive_entries(archive: Path, entries, *, present: bool) -> None:
    if not entries:
        return
    with closing(sqlite3.connect(archive, timeout=5)) as db, db:
        statement = ("INSERT OR IGNORE INTO archive(entry) VALUES (?)" if present
                     else "DELETE FROM archive WHERE entry=?")
        db.executemany(statement, [(entry,) for entry in entries])


def _retry_message(saved: int, found: int, unavailable: int, absent: int, limited: int = 0) -> str:
    parts = [f"사라진 파일 다시 받기를 마쳤습니다 · 받은 파일 {saved:,}개"]
    if found:
        parts.append(f"제자리에 있던 파일 {found:,}개")
    if unavailable:
        parts.append(f"볼 수 없는 트윗 {unavailable:,}개(삭제·비공개·제한)")
    if absent:
        parts.append(f"받지 못한 미디어 {absent:,}개")
    if limited:
        parts.append(f"용량 제한으로 건너뛴 파일 {limited:,}개")
    text = " · ".join(parts) + "."
    if unavailable or absent or limited:
        text += " 받지 못한 파일은 ‘사라진 파일’ 목록에 남아 있으며, 필요 없으면 ‘다시 받지 않기’로 숨길 수 있습니다."
    return text


def _configure_gallery(options: JobOptions, archive_path: Path, account_folder: str) -> None:
    from gallery_dl import config

    config.clear()
    config.set(("cache",), "file", ":memory:")
    config.set(("output",), "mode", "null")
    directory = [account_folder] if options.separate_accounts else []
    if options.folder_mode == "author":
        directory.append("{author[name]}")
    elif options.folder_mode == "date":
        directory.append("{_luvbox_date}")
    values = {
        "base-directory": options.output_dir,
        "directory": directory,
        "filename": "{_luvbox_filename_prefix}{tweet_id}_{_luvbox_filename_part}.{extension}",
        "archive": str(archive_path),
        "archive-format": "{_luvbox_archive_key}",
        "archive-event": ["file", "skip"],
        "archive-mode": "disk", "archive-reuse": False,
        "cookies-update": False, "netrc": False,
        # Chosen missing videos are fetched even when new ones are not saved.
        "videos": options.include_videos or options.include_gifs or bool(options.targets),
        "previews": False, "cards": False,
        # One TweetResultByRestId request per tweet, without its conversation.
        "tweet-endpoint": "restid",
        "quoted": False, "quoted-expand": False, "expand": False,
        "articles": False, "twitpic": False, "ads": False,
        "retweets": True, "replies": True,
        "children": False, "postprocess": False, "async": False, "reverse": False,
        "unique": True, "size": ["orig", "4096x4096", "large", "medium", "small"], "fallback": True,
        "ratelimit": "abort", "locked": "abort",
        "sleep-request": API_INTERVAL, "timeout": 10.0, "retries": 0, "retries-api": 0,
        "cookies": options.cookies_file if options.auth_mode == "file" else None,
    }
    for key, value in values.items():
        config.set(("extractor", "twitter"), key, value)
    config.set(("downloader", "http"), "timeout", 10.0)
    config.set(("downloader", "http"), "retries", 0)
    config.set(("downloader", "http"), "progress", None)
    # The shared-session protection aborts a 429 before the downloader's retry
    # or image-size fallback can start. These settings are defense in depth.
    config.set(("downloader", "http"), "retry-codes", [])


def run_worker(options: JobOptions, emit: EventSink, stop_file: Path | str | None = None,
               *, review: SaveReview | None = None) -> int:
    """Download available liked media and emit structured, secret-free events.

    A successful traversal covers the list X exposes to this authenticated
    session; it is not evidence that every historical like was available.
    """
    from gallery_dl import config, exception, job, output, util
    from gallery_dl.extractor import common
    from requests.exceptions import RequestException
    from .metadata import available, embed_source, failure_message, persistent

    saved = skipped = size_skipped = review_skipped = review_never = 0
    metadata_failed = 0
    metadata_available = False
    metadata_warned = False
    author_deferred = 0
    stop_reason = ""
    status = "error"
    message = ""
    exit_code = 1
    normalized = options
    active_job = None
    quick_scan = False
    existing_streak = 0
    guard = None
    output_guard = None
    previous_output_guards = []
    history = None
    previews = None
    run_id = None
    likes = None
    targets = frozenset()
    ignored = set()
    ignored_encountered = False
    recovered = {}
    size_limited = set()
    review_declined = set()
    # Files skipped in an earlier review: (tweet_id, num, type). A quick scan
    # continues until it reaches them; resolved ones are then forgotten.
    review_pending = set()
    review_reached = set()
    review_resolved = set()
    review_declined_ids = set()
    review_unknown = False
    unavailable_tweets = absent_files = 0
    tweet_texts = []
    visited_ids = set()
    last_tweet_id = last_posted_at = ""
    flag = Path(stop_file) if stop_file is not None else None
    root_logger = logging.getLogger()
    previous_handlers, previous_level = root_logger.handlers[:], root_logger.level
    handler = _GalleryLogHandler(emit, options.auth_mode)
    root_logger.handlers = [handler]
    root_logger.setLevel(logging.INFO)
    logger_states = []
    for name in ("twitter", "download", "cookies", "archive", "cache", "http"):
        logger = logging.getLogger(name)
        logger_states.append((logger, logger.handlers[:], logger.level, logger.propagate))
        logger.handlers = []
        logger.setLevel(logging.NOTSET)
        logger.propagate = True

    def check_stop() -> None:
        nonlocal stop_reason
        if flag is not None and flag.exists():
            stop_reason = "user"
            handler.stopping = True
            raise exception.StopExtraction()

    def sleep(seconds: float, reason: str = "") -> None:
        check_stop()
        if seconds >= 30:
            guard.record_limit({"Retry-After": seconds})
        until = time.monotonic() + max(0, seconds)
        while (remaining := until - time.monotonic()) > 0:
            check_stop()
            time.sleep(min(remaining, 0.1))
        check_stop()

    def flush_tweet_texts():
        if tweet_texts and history is not None:
            batch = tweet_texts[:]
            tweet_texts.clear()
            history.record_tweets(batch)

    progress_fields = {}
    progress_written = 0.0

    def history_update(*, force=False, **fields):
        """Record run progress at most every two seconds.

        Every commit flushes to disk (slow in cloud-synced folders), and a
        quick scan visits hundreds of known tweets. The final counts are
        always written when the run finishes.
        """
        nonlocal progress_written
        progress_fields.update(fields)
        if history is None or run_id is None:
            return
        now = time.monotonic()
        if not force and now - progress_written < 2.0:
            return
        progress_written = now
        try:
            history.update_run(run_id, **progress_fields)
        except HistoryError:
            raise exception.AbortExtraction("history store unavailable") from None
        progress_fields.clear()

    def record_source(kwdict, media_path, media_status, previous):
        nonlocal metadata_failed, metadata_warned, author_deferred
        # handle_url already reads an archive hit's record to recover its path.
        # Reuse that same snapshot for metadata decisions instead of querying
        # the catalog twice for every already saved file.
        previous = previous or {}
        previous_status = previous.get("metadata_status", "pending")
        previous_author_status = previous.get("author_metadata_status", "pending")
        author, author_id = _author_fields(kwdict.get("author"))
        # A prior catalog can supply a known author when a current API result
        # omits it. The account whose Likes are being saved is never used.
        old_author, old_author_id = _author_fields({"name": previous.get("author", ""), "id": previous.get("author_id", "")})
        author = author or old_author
        author_id = author_id or old_author_id
        if author or author_id:
            details = kwdict.get("author") if isinstance(kwdict.get("author"), dict) else {}
            kwdict["author"] = {**details, "name": author, "id": author_id}
        known_author = bool(author or author_id)
        # Handles identify accounts without regard to case, but their latest
        # spelling must still reach the file through the explicit update.
        same_author = author == old_author and author_id == old_author_id
        author_done = not known_author or (previous_author_status == "embedded" and same_author)
        recording_required = False
        metadata_reason = "unknown"
        text_status = "unknown"
        if previous_status == "unsupported" or (previous_status == "embedded" and author_done):
            metadata_status = previous_status
            author_status = previous_author_status if known_author else "unknown"
        elif previous_status == "embedded" and media_status == "existing":
            # Retry a missing source, but keep an already saved file intact
            # when only its author changed. Updating the catalog avoids
            # rewriting a whole library or making cloud sync re-upload it.
            metadata_status = "embedded"
            author_status = previous_author_status if same_author else "pending"
            author_deferred += 1
        elif metadata_available:
            recording_required = True
            try:
                content = kwdict.get("content")
                result = embed_source(media_path, str(kwdict["tweet_id"]), author=author, author_id=author_id,
                                      text=content if isinstance(content, str) else None)
                text_status = result.get("text_status", "unknown")
                if text_status not in {"embedded", "preserved", "not_requested", "error", "unknown"}:
                    text_status = "error"
                metadata_reason = result.get("reason", "unknown")
                metadata_status = result.get("status")
                if metadata_status not in {"embedded", "failed", "unsupported"}:
                    metadata_status = "failed"
                author_status = result.get("author_status", "error" if known_author and metadata_status != "unsupported" else "unknown")
                if author_status not in {"embedded", "unknown", "error"}:
                    author_status = "error"
            except Exception:
                metadata_status = "failed"
                author_status = "error" if known_author else "unknown"
                text_status = "error"
        else:
            recording_required = True
            metadata_reason = "unavailable"
            metadata_status = "failed"
            author_status = "error" if known_author else "unknown"
            text_status = "error"
        kwdict["_luvbox_metadata_status"] = metadata_status
        kwdict["_luvbox_text_metadata_status"] = text_status
        kwdict["_luvbox_author_metadata_status"] = author_status
        # Deferred work keeps any prior author error for retry; it is not a
        # recording failure from this run.
        if recording_required and (metadata_status == "failed" or author_status == "error"):
            metadata_failed += 1
            if not metadata_warned:
                metadata_warned = True
                emit({"type": "log", "level": "warning",
                      "message": f"{_METADATA_ERROR} {failure_message(metadata_reason)}",
                      **_media_details(kwdict)})

    class SizeLimit(exception.ControlException):
        default = "media size exceeded"

    class ReviewSkip(exception.ControlException):
        default = "user declined media"

    class ReviewNever(ReviewSkip):
        default = "user chose not to save media"
        path = None

    class EventsOutput(output.NullOutput):
        def success(self, path):
            active_job.last_output = ("saved", str(path))

        def skip(self, path):
            active_job.last_output = ("existing", str(path))

    class PhotoJob(job.DownloadJob):
        def __init__(self, url):
            super().__init__(url)
            self.out = EventsOutput()
            self.current_media = {}
            self.last_output = None
            self.progress_bytes = self.progress_total = 0
            self.progress_started = self.progress_last = None
            self.progress_offset = 0
            self.size_exceeded = False
            self.gif_source = self.gif_temporary = None
            self.gif_finalize = None
            self.review_finalize = None
            self.transfer_marker = None
            self.photo_positions = {}
            self.photo_counts = {}
            extract_media = getattr(self.extractor, "_extract_media", None)
            if extract_media is not None:
                extract_source = self.extractor._extract_media_source
                def numbered_source(dest, media):
                    extract_source(dest, media)
                    if "_luvbox_attachment_number" in media:
                        dest["_luvbox_attachment_number"] = media["_luvbox_attachment_number"]
                self.extractor._extract_media_source = numbered_source

                def direct_media(tweet, media_items, files):
                    selected = []
                    for attachment_number, media in enumerate(media_items, 1):
                        media = dict(media, _luvbox_attachment_number=attachment_number)
                        if "video_info" in media:
                            # Keep both types until handle_url so disabling one
                            # type cannot renumber the other's attachment key.
                            variants = [variant for variant in media["video_info"].get("variants", ())
                                        if variant.get("content_type", "").lower() == "video/mp4" and
                                        urlsplit(variant.get("url", "")).scheme in {"https", "http"} and
                                        urlsplit(variant.get("url", "")).path.lower().endswith(".mp4")]
                            if not variants:
                                continue
                            media = dict(media, video_info=dict(media["video_info"], variants=variants))
                        selected.append(media)
                    return extract_media(tweet, selected, files)
                self.extractor._extract_media = direct_media
            self.extractor.sleep = sleep

            def abort_wait(**kwargs):
                headers = {}
                if kwargs.get("until") is not None:
                    headers["x-rate-limit-reset"] = kwargs["until"]
                if kwargs.get("seconds") is not None:
                    headers["Retry-After"] = kwargs["seconds"]
                guard.record_limit(headers)

            self.extractor.wait = abort_wait
            self.extractor.input = lambda *args, **kwargs: (_ for _ in ()).throw(
                exception.AuthRequired("authenticated cookies"))
            initialize_session = self.extractor._init_session

            def protected_session():
                initialize_session()
                guard.protect_session(self.extractor.session)

            self.extractor._init_session = protected_session
            def normalize_cookies():
                if normalized.auth_mode == "managed":
                    from requests.cookies import create_cookie
                    from .login import load_session_cookies

                    try:
                        session_cookies = load_session_cookies(account=normalized.account)
                    except (ValueError, OSError):
                        raise exception.AuthRequired("managed authenticated session") from None
                    for item in session_cookies:
                        if item.get("domain", "").lstrip(".").lower() != "x.com":
                            continue
                        expiry = item.get("expires")
                        cookie = create_cookie(
                            name=item["name"], value=item["value"],
                            domain=item["domain"], path=item.get("path") or "/",
                            expires=int(expiry) if expiry and expiry > 0 else None,
                            secure=bool(item.get("secure", False)),
                            rest={"HttpOnly": bool(item.get("httpOnly", False))},
                        )
                        self.extractor.cookies.set_cookie(cookie)
                elif normalized.auth_mode == "file":
                    # Read in place, accept a UTF-8 BOM, and load only X cookies.
                    # No copied cookie jar or updates are ever written.
                    with open(normalized.cookies_file, encoding="utf-8-sig") as fp:
                        for cookie in util.cookiestxt_load(fp):
                            if cookie.domain.lstrip(".").lower() == "x.com":
                                self.extractor.cookies.set_cookie(cookie)
                # Some Netscape exporters use the host-only x.com domain.
                # gallery-dl's X API looks up .x.com, so normalize in memory.
                jar = self.extractor.cookies
                for cookie in list(jar):
                    if cookie.domain.lower() == "x.com":
                        copied = copy.copy(cookie)
                        copied.domain = ".x.com"
                        copied.domain_specified = copied.domain_initial_dot = True
                        if not any(existing.domain == ".x.com" and existing.path == copied.path
                                   and existing.name == copied.name for existing in jar):
                            jar.set_cookie(copied)
                        jar.clear(cookie.domain, cookie.path, cookie.name)

            self.extractor._init_cookies = normalize_cookies

        def _init(self):
            check_stop()
            super()._init()
            if not self.extractor.cookies_check(("auth_token",)):
                raise exception.AuthRequired("authenticated cookies")
            check_stop()

        def initialize(self, kwdict=None):
            super().initialize(kwdict)
            if self.archive is None:
                raise exception.AbortExtraction("download archive unavailable")

        def handle_url(self, url, kwdict):
            nonlocal stop_reason, saved, skipped, size_skipped, review_skipped, review_never, existing_streak
            nonlocal ignored_encountered
            check_stop()
            media_type = kwdict.get("type")
            if media_type not in {"photo", "video", "animated_gif"}:
                return
            if not targets and ((media_type == "video" and not normalized.include_videos) or
                                (media_type == "animated_gif" and not normalized.include_gifs)):
                return
            if media_type != "photo" and not urlsplit(url).path.lower().endswith(".mp4"):
                return
            kwdict = dict(kwdict)
            tweet_id = str(kwdict["tweet_id"])
            author, _ = _author_fields(kwdict.get("author"))
            kwdict["_luvbox_filename_prefix"] = author + "_" if author else ""
            attachment_number = kwdict.get("_luvbox_attachment_number", kwdict["num"])
            if media_type == "photo":
                position_key = (tweet_id, attachment_number)
                if position_key not in self.photo_positions:
                    self.photo_counts[tweet_id] = self.photo_counts.get(tweet_id, 0) + 1
                    self.photo_positions[position_key] = self.photo_counts[tweet_id]
                kwdict["num"] = self.photo_positions[position_key]
                kwdict["_luvbox_filename_part"] = str(kwdict["num"])
                kwdict["_luvbox_archive_key"] = f"{tweet_id}_{kwdict['num']}"
            else:
                kind = "gif" if media_type == "animated_gif" else "video"
                kwdict["num"] = attachment_number
                kwdict["extension"] = "mp4"
                kwdict["_luvbox_filename_part"] = f"{kind}_{kwdict['num']}"
                kwdict["_luvbox_archive_key"] = f"{tweet_id}_{media_type}_{kwdict['num']}"
            identity = (tweet_id, kwdict["num"], media_type)
            if identity in review_pending:
                review_reached.add(identity)
            if targets:
                # Only the chosen files; the tweet's other media are untouched.
                if kwdict["_luvbox_archive_key"] not in targets:
                    return
            elif identity in ignored:
                # The user deleted this file on purpose, in whichever layout.
                ignored_encountered = True
                if identity in review_pending:
                    review_resolved.add(identity)
                return
            self.current_media = kwdict
            self.last_output = None
            self.progress_started = self.progress_last = None
            self.progress_bytes = self.progress_total = 0
            self.progress_offset = 0
            self.size_exceeded = False
            self.gif_source = self.gif_temporary = None
            self.gif_finalize = None
            self.review_finalize = None
            self.transfer_marker = None
            if normalized.folder_mode == "date":
                kwdict = dict(kwdict, _luvbox_date=_save_day().isoformat())
            self.current_media = kwdict
            # Real X file metadata contains its tweet's directory metadata.
            # Delay folder/archive setup until media is selected for this run.
            super().handle_directory(kwdict)
            self.pathfmt.set_filename(kwdict)
            self.pathfmt.build_path()
            destination = _review_path(self.pathfmt.path)
            if kwdict["_luvbox_filename_prefix"]:
                current_candidates = [destination, Path(str(destination) + ".part")]
                if media_type == "animated_gif":
                    current_candidates.append(destination.with_suffix(".gif"))
                if not any(candidate.is_file() for candidate in current_candidates):
                    legacy = destination.with_name(f"{tweet_id}_{kwdict['_luvbox_filename_part']}.{kwdict['extension']}")
                    legacy_candidates = [legacy, Path(str(legacy) + ".part")]
                    if media_type == "animated_gif":
                        legacy_candidates.append(legacy.with_suffix(".gif"))
                    if any(candidate.is_file() for candidate in legacy_candidates):
                        kwdict["_luvbox_filename_prefix"] = ""
                        self.pathfmt.set_filename(kwdict)
                        self.pathfmt.build_path()
            # Keep existing MP4 GIFs untouched. New GIF downloads still use a
            # resumable MP4 .part file, but a completed .gif is also a file hit
            # when a ledger was missing or has just moved to another folder.
            if media_type == "animated_gif" and Path(self.pathfmt.path).with_suffix(".gif").is_file():
                self.pathfmt.set_filename(dict(kwdict, extension="gif"))
                kwdict = self.pathfmt.kwdict
                self.current_media = kwdict
                self.pathfmt.build_path()
            if (not history.is_download_path(self.pathfmt.path) or
                    not history.is_download_path(self.pathfmt.path + ".part")):
                raise exception.AbortExtraction("permissionerror unsafe media output")
            try:
                super().handle_url(url, kwdict)
            except ReviewNever as declined:
                review_never += 1
                review_declined.add(kwdict["_luvbox_archive_key"])
                existing_streak = 0
                if identity in review_pending:
                    review_resolved.add(identity)
                try:
                    if targets:
                        # A chosen missing file keeps its record and archive entry.
                        history.set_missing_ignored([identity], True)
                    else:
                        # Like a file deleted with 'do not download again': known
                        # to the archive, listed with the hidden missing files.
                        history.record_declined(normalized.folder_mode, kwdict, declined.path)
                        self.archive.add(kwdict)
                except (HistoryError, sqlite3.Error):
                    raise exception.AbortExtraction("history store unavailable") from None
                self.progress(self.progress_bytes, self.progress_total, force=True, final=True)
                emit({"type": "review-never", "path": "", **_media_details(kwdict)})
            except ReviewSkip:
                review_skipped += 1
                review_declined.add(kwdict["_luvbox_archive_key"])
                review_declined_ids.add(identity)
                existing_streak = 0
                self.progress(self.progress_bytes, self.progress_total, force=True, final=True)
                emit({"type": "review-skipped", "path": "", **_media_details(kwdict)})
            except SizeLimit:
                size_skipped += 1
                if identity in review_pending:
                    review_resolved.add(identity)
                if targets:
                    size_limited.add(kwdict["_luvbox_archive_key"])
                if self.pathfmt.temppath:
                    Path(self.pathfmt.temppath).unlink(missing_ok=True)
                self.progress(self.progress_bytes, self.progress_total, force=True, final=True)
                emit({"type": "size-skipped", "path": "", "max_video_mb": normalized.max_video_mb,
                      **_media_details(kwdict)})
            else:
                if self.last_output and identity in review_pending:
                    # Saved now, or already known: no longer waiting for a choice.
                    review_resolved.add(identity)
                if self.last_output:
                    media_status, media_path = self.last_output
                    previous = None
                    if media_status == "saved":
                        if self.gif_source is not None:
                            # The GIF is finalized and archived now. Until this
                            # point the source remains recoverable on any error.
                            markers = _valid_completion_markers(self.gif_source)
                            self.gif_source.unlink(missing_ok=True)
                            _clear_completion_markers(markers)
                            self.gif_source = None
                        if self.transfer_marker is not None:
                            # The .part it described is saved now. A leftover
                            # record matches no .part and is harmless.
                            _clear_completion_markers(self.transfer_marker)
                            self.transfer_marker = None
                        saved += 1
                        if normalized.folder_mode == "date":
                            completed_day = _save_day().isoformat()
                            if completed_day != kwdict["_luvbox_date"]:
                                original_path = Path(media_path)
                                completed_path = account_dir / completed_day / original_path.name
                                try:
                                    if (not original_path.resolve().is_relative_to(account_dir.resolve()) or
                                            not completed_path.resolve().is_relative_to(account_dir.resolve())):
                                        raise OSError("date folder is outside the account")
                                    completed_path.parent.mkdir(parents=True, exist_ok=True)
                                    if not completed_path.resolve().is_relative_to(account_dir.resolve()):
                                        raise OSError("date folder changed outside the account")
                                    _move_without_overwrite(original_path, completed_path)
                                except OSError:
                                    emit({"type": "log", "level": "warning", "message": _DATE_MOVE_ERROR,
                                          **_media_details(kwdict)})
                                else:
                                    media_path = str(completed_path)
                                    kwdict["_luvbox_date"] = completed_day
                    else:
                        skipped += 1
                        # An archive hit may belong to an older saving day or
                        # the previous account-folder setting. Keep its actual
                        # location rather than today's generated destination.
                        try:
                            previous = history.media_record(normalized.folder_mode, kwdict)
                        except HistoryError:
                            raise exception.AbortExtraction("history store unavailable") from None
                        # A selected missing file may have been copied to the
                        # current output since the library was refreshed. Its
                        # absent former location must not mask that file hit.
                        known_path = previous.get("path") if previous is not None else media_path
                        candidates = [known_path]
                        if targets and media_path and media_path != known_path:
                            candidates.append(media_path)
                        media_path = ""
                        for known_path in candidates:
                            if not known_path:
                                continue
                            candidate = Path(known_path)
                            if history.is_media_path(candidate) and candidate.is_file():
                                media_path = str(candidate)
                                if media_type == "animated_gif":
                                    kwdict["extension"] = candidate.suffix.lstrip(".")
                                if previous is not None and (
                                        os.path.normcase(os.path.abspath(previous.get("path") or "")) !=
                                        os.path.normcase(os.path.abspath(media_path))):
                                    # Verified tags belong to the old physical
                                    # file. Retain its author information, but
                                    # check/embed tags for this different copy.
                                    previous = dict(previous, metadata_status="pending",
                                                    author_metadata_status="pending")
                                break
                    self.last_output = (media_status, media_path)
                    kwdict["_luvbox_path"] = media_path
                    media_stat = None
                    if media_path:
                        try:
                            media_stat = Path(media_path).stat()
                        except (FileNotFoundError, NotADirectoryError):
                            pass
                    media_exists = media_stat is not None and stat.S_ISREG(media_stat.st_mode)
                    original_size = media_stat.st_size if media_exists else 0
                    if media_exists:
                        record_source(kwdict, media_path, media_status, previous)
                    emit({"type": "saved" if media_status == "saved" else "skipped", "path": media_path,
                          **_media_details(kwdict)})
                    if media_exists:
                        # Keep the preview independently of catalog writes:
                        # the file is already finalized even if recording it
                        # fails. Visiting an old file fills only a missing
                        # preview, without rescanning the rest of the folder.
                        preview_key = (kwdict.get("tweet_id"), kwdict.get("num"), kwdict.get("type", "photo"))
                        if media_status == "saved":
                            previews.add(preview_key, media_path)
                        else:
                            previews.add(preview_key, media_path, only_missing=True)
                        try:
                            history.record_media(normalized.folder_mode, kwdict, media_path, status=media_status)
                        except HistoryError:
                            raise exception.AbortExtraction("history store unavailable") from None
                        if targets:
                            recovered[kwdict["_luvbox_archive_key"]] = media_status
                    if media_status == "saved":
                        payload_size = self.progress_bytes if media_type == "animated_gif" else original_size
                        self.progress(payload_size, self.progress_total, force=True, final=True)
            finally:
                if self.gif_finalize is not None:
                    self.pathfmt.finalize = self.gif_finalize
                    self.gif_finalize = None
                if self.review_finalize is not None:
                    self.pathfmt.finalize = self.review_finalize
                    self.review_finalize = None
                if self.gif_temporary is not None:
                    self.gif_temporary.unlink(missing_ok=True)
                    self.gif_temporary = None
            history_update(saved=saved, skipped=skipped, size_skipped=size_skipped, force=bool(self.last_output and self.last_output[0] == "saved"))
            # Archive insertion happens after out.success; stop only once the
            # completed file has been recorded, so reruns remain consistent.
            if normalized.photo_limit and saved >= normalized.photo_limit:
                stop_reason = "limit"
                raise exception.StopExtraction()
            if quick_scan and self.last_output:
                # Size-limited and failed files are neither saved nor known,
                # so they neither extend nor break the already-saved streak.
                existing_streak = 0 if self.last_output[0] == "saved" else existing_streak + 1
                # A file skipped earlier is not known yet; look until it is reached.
                if existing_streak >= QUICK_STOP_AFTER and review_pending <= review_reached:
                    stop_reason = "caught-up"
                    raise exception.StopExtraction()

        def handle_directory(self, kwdict):
            nonlocal last_tweet_id, last_posted_at
            check_stop()
            if "tweet_id" in kwdict:
                last_tweet_id = str(kwdict["tweet_id"])
                last_posted_at = _media_details(kwdict)["posted_at"]
                visited_ids.add(last_tweet_id)
                # Every liked tweet with media, saved or not, keeps Like order
                # and its text, which the records window can search.
                if isinstance(kwdict.get("content"), str):
                    tweet_texts.append((last_tweet_id, kwdict["content"], kwdict.get("hashtags") or ()))
                try:
                    if likes is not None:
                        likes.visit(last_tweet_id)
                    if len(tweet_texts) >= 50:
                        flush_tweet_texts()
                except HistoryError:
                    raise exception.AbortExtraction("history store unavailable") from None
                history_update(visited=len(visited_ids), last_tweet_id=last_tweet_id,
                               last_posted_at=last_posted_at)

        def progress(self, downloaded, total, *, force=False, final=False):
            now = time.monotonic()
            if self.progress_started is None:
                self.progress_started = now
            self.progress_bytes, self.progress_total = downloaded, total or 0
            if not force and self.progress_last is not None and now - self.progress_last < 0.25:
                return
            self.progress_last = now
            speed = max(0, downloaded - self.progress_offset) / max(now - self.progress_started, 0.001)
            emit({"type": "progress", **_media_details(self.current_media), "downloaded": downloaded,
                  "total": total or 0, "speed": speed, "final": final,
                  "path": self.current_media.get("_luvbox_path", "")})

        def download(self, url):
            # gallery-dl's media downloader catches exceptions from request()
            # and returns False. Re-raise a protection failure before its parent
            # attempts other image sizes or the next photo.
            media_type = self.current_media.get("type")
            is_gif = media_type == "animated_gif"
            # gallery-dl's HTTP downloader writes every transfer to its .part
            # file (its default). Name it now: a conversion failure must never
            # leave a completed-looking MP4 that gallery-dl would archive as a
            # success on the next run, and a transfer completed by an earlier
            # run is used again without a request.
            self.pathfmt.part_enable()
            local_source = Path(self.pathfmt.temppath)
            completed = _transfer_complete(local_source)
            locally_complete = completed is not None
            if locally_complete:
                self.transfer_marker = _valid_completion_markers(local_source)
                if completed.get("mtime_http"):
                    # Stamp the saved file as the original transfer would have.
                    self.pathfmt.kwdict["_mtime_http"] = completed["mtime_http"]
                cap = normalized.max_video_mb * 1024 * 1024 if media_type != "photo" else 0
                if cap and local_source.stat().st_size > cap:
                    # The limit was lowered after this file finished. Treat it
                    # like any other oversized video and drop the temporary file.
                    _clear_completion_markers(self.transfer_marker)
                    raise SizeLimit()
                self.progress(local_source.stat().st_size, local_source.stat().st_size, force=True)
                result = True
            else:
                result = super().download(url)
            if guard.failure:
                raise exception.AbortExtraction(guard.failure)
            if self.size_exceeded:
                raise SizeLimit()
            if not result:
                # The downloader turns a stop during streaming or request pacing
                # into a plain failure. Stop here, before gallery-dl requests a
                # fallback image size and removes the resumable .part file. A
                # completed download is left to finalize and be archived.
                check_stop()
            if result and self.pathfmt.temppath:
                self.review_download()
            if result and is_gif and self.pathfmt.temppath:
                source = Path(self.pathfmt.temppath)
                self.gif_source = source
                if normalized.gif_format == "mp4":
                    # Keep X's original MP4; the normal finalize saves it and
                    # handle_url removes a marker left by an earlier GIF run.
                    return result
                if not locally_complete:
                    # A stop during conversion keeps the MP4 for a local retry.
                    self.keep_transfer(source, replace=True)
                # Completion describes the MP4 transfer. Conversion can make
                # the GIF much larger; it must not inflate downloaded bytes.
                source_bytes = source.stat().st_size
                self.progress(source_bytes, self.progress_total or source_bytes, force=True)
                emit({"type": "converting", "stage": "started", "message": "GIF로 변환하고 있습니다.",
                      **_media_details(self.current_media)})
                emit({"type": "log", "level": "info", "message": "GIF로 변환하고 있습니다.",
                      **_media_details(self.current_media)})
                try:
                    converted = convert_mp4_to_gif(source, check_stop=check_stop,
                                                  max_bytes=normalized.max_video_mb * 1024 * 1024,
                                                  size=normalized.gif_size)
                except ConversionError as exc:
                    # One unconvertible GIF must not block every later Like.
                    # Keep the original MP4 path; the normal finalize saves and
                    # archives it, and handle_url then removes the marker.
                    if isinstance(exc, ConversionSizeLimit):
                        message = f"GIF로 바꾸면 {normalized.max_video_mb}MB를 넘어 원본 MP4로 저장합니다."
                    else:
                        message = "GIF로 바꾸지 못해 원본 MP4로 저장합니다."
                    emit({"type": "converting", "stage": "fallback", "message": message,
                          **_media_details(self.current_media)})
                    emit({"type": "log", "level": "warning", "message": message,
                          **_media_details(self.current_media)})
                    return result
                self.gif_temporary = converted
                self.pathfmt.set_extension("gif")
                self.pathfmt.build_path()
                self.pathfmt.temppath = str(converted)
                self.gif_finalize = self.pathfmt.finalize

                def finalize_gif():
                    check_stop()
                    destination = Path(self.pathfmt.realpath)
                    if not history.is_download_path(self.pathfmt.path):
                        raise exception.AbortExtraction("permissionerror unsafe GIF output")
                    try:
                        _move_without_overwrite(converted, destination)
                    except OSError:
                        guard.failure = "GIF 파일을 저장하지 못했습니다. 같은 이름의 파일이나 저장 폴더를 확인해 주세요. MP4 임시 파일은 보존했습니다."
                        raise exception.AbortExtraction(guard.failure) from None
                    self.pathfmt.part = False
                    self.pathfmt.temppath = self.pathfmt.realpath
                    self.pathfmt.set_mtime()
                    return True

                self.pathfmt.finalize = finalize_gif
                emit({"type": "converting", "stage": "finished", "message": "GIF 변환을 마쳤습니다.",
                      **_media_details(self.current_media)})
            return result

        def keep_transfer(self, source, signature=None, *, replace=False):
            """Leave a complete .part for the next run when this one stops first.

            Without the record the next run asks the server to resume the
            complete file, which is refused (416) and stops that run. Only
            exits that keep the file call this, and it never fails the run.
            An existing record is replaced only for a transfer just made; one
            changed by another program meanwhile is left as it is.
            """
            try:
                if signature is not None and _review_file_signature(source) != signature:
                    return  # changed meanwhile: not the transfer we completed
                if _transfer_complete(source) is None and (
                        replace or not os.path.lexists(str(source) + ".luvbox-complete")):
                    _mark_transfer_complete(source, self.pathfmt.kwdict.get("_mtime_http"))
                # GIF fallback may move this .part to MP4 before handle_url
                # can inspect it again. Remember only markers validated now.
                self.transfer_marker = _valid_completion_markers(source)
            except OSError:
                pass

        def review_download(self):
            """Protect finalization, optionally asking before conversion/archiving."""
            source = _review_path(self.pathfmt.temppath)
            destination = _review_path(self.pathfmt.realpath)
            # A downloader may change paths. Only this run's .part file can be
            # displayed, discarded, or finalized; a pre-existing file is never
            # the subject of a prompt or of the user's discard operation.
            if (not self.pathfmt.part or source != Path(str(destination) + ".part") or
                    not history.is_download_path(source) or not history.is_download_path(destination)):
                guard.failure = _REVIEW_ERROR
                raise exception.AbortExtraction(guard.failure)
            signature = None
            try:
                signature = _review_file_signature(source)
                markers = _valid_completion_markers(source)
                check_stop()
                enabled = review.enabled() if review is not None else False
                if type(enabled) is not bool:
                    raise ValueError("invalid review state")
                decision = review.ask(source, {**_media_details(self.current_media),
                                              "type": self.current_media.get("type", "")}, check_stop) if enabled else "save"
                check_stop()
                if decision not in {"save", "skip", "never"}:
                    raise ValueError("invalid review response")
                if (not history.is_download_path(source) or
                        _review_file_signature(source) != signature):
                    raise OSError("review temporary file changed")
                if decision in {"skip", "never"}:
                    for marker, marker_signature in markers:
                        if (not history.is_download_path(marker) or
                                _review_file_signature(marker) != marker_signature):
                            raise OSError("review completion marker changed")
                    if decision == "never" and previews is not None:
                        # What the file looked like, for the hidden missing list.
                        previews.add((self.current_media.get("tweet_id"), self.current_media.get("num"),
                                      self.current_media.get("type", "photo")), source, suffix=destination.suffix)
                    # Creating a kept preview may decode a video for several
                    # seconds. A stop or an outside edit in that interval must
                    # not discard a different file or record a new choice.
                    check_stop()
                    if (not history.is_download_path(source) or
                            _review_file_signature(source) != signature):
                        raise OSError("review temporary file changed")
                    for marker, marker_signature in markers:
                        if (not history.is_download_path(marker) or
                                _review_file_signature(marker) != marker_signature):
                            raise OSError("review completion marker changed")
                    source.unlink()
                    for marker, _ in markers:
                        marker.unlink()
                    if decision == "skip":
                        raise ReviewSkip()
                    declined = ReviewNever()
                    # The name it would have been saved under.
                    declined.path = (destination.with_suffix(".gif")
                                     if self.current_media.get("type") == "animated_gif" and
                                     normalized.gif_format == "gif" else destination)
                    raise declined
            except ReviewSkip:
                raise
            except exception.ControlException:
                # Stopped before a choice: the complete transfer is kept.
                if signature is not None:
                    self.keep_transfer(source, signature)
                raise
            except Exception:
                if signature is not None:
                    self.keep_transfer(source, signature)
                guard.failure = _REVIEW_ERROR
                raise exception.AbortExtraction(guard.failure) from None

            original_finalize = self.pathfmt.finalize
            self.review_finalize = original_finalize

            def finalize_reviewed():
                try:
                    check_stop()
                except exception.StopExtraction:
                    self.keep_transfer(source, signature)
                    raise
                try:
                    if (not history.is_download_path(source) or not history.is_download_path(destination) or
                            _review_file_signature(source) != signature):
                        raise OSError("review temporary file changed")
                    _move_without_overwrite(source, destination)
                except OSError:
                    self.keep_transfer(source, signature)
                    guard.failure = _LOCAL_WRITE_ERROR
                    raise exception.AbortExtraction(guard.failure) from None
                self.pathfmt.part = False
                self.pathfmt.temppath = self.pathfmt.realpath
                self.pathfmt.set_mtime()
                return True

            self.pathfmt.finalize = finalize_reviewed

        def get_downloader(self, scheme):
            instance = super().get_downloader(scheme)
            if instance is not None and not getattr(instance, "_luvbox_stop", False):
                original_download = instance.download

                def tracked_download(url, pathfmt):
                    cap = normalized.max_video_mb * 1024 * 1024 if self.current_media.get("type") != "photo" else 0
                    self.progress(0, 0, force=True)
                    if cap and getattr(instance, "part", False) and not getattr(instance, "metadata", False):
                        pathfmt.part_enable(instance.partdir)
                        existing_bytes = pathfmt.part_size()
                        if existing_bytes > cap:
                            self.size_exceeded = True
                            self.progress_bytes = self.progress_offset = existing_bytes
                            raise SizeLimit()
                    session = getattr(instance, "session", None)
                    original_request = session.request if session is not None else None
                    response = None
                    successful = False

                    def bounded_request(*args, **kwargs):
                        nonlocal response
                        response = original_request(*args, **kwargs)
                        if cap and response.status_code in (200, 206, 416):
                            header = (response.headers.get("Content-Range", "").rpartition("/")[2]
                                      if response.status_code in (206, 416) else response.headers.get("Content-Length"))
                            try:
                                total = int(header)
                            except (ValueError, TypeError):
                                total = 0
                            if total > cap:
                                self.size_exceeded = True
                                self.progress_total = total
                                response.close()
                                raise SizeLimit()
                            # Also protect the header chunk consumed before
                            # receive(), and count resumed bytes toward the cap.
                            range_header = (kwargs.get("headers") or {}).get("Range", "")
                            try:
                                offset = int(range_header.removeprefix("bytes=").partition("-")[0]) if response.status_code == 206 else 0
                            except ValueError:
                                offset = 0
                            self.progress_offset = offset
                            if offset > cap:
                                self.size_exceeded = True
                                response.close()
                                raise SizeLimit()
                            iter_content = response.iter_content
                            received = 0

                            def bounded_content(*content_args, **content_kwargs):
                                nonlocal received
                                for chunk in iter_content(*content_args, **content_kwargs):
                                    if offset + received + len(chunk) > cap:
                                        self.size_exceeded = True
                                        raise SizeLimit()
                                    received += len(chunk)
                                    yield chunk
                            response.iter_content = bounded_content
                        return response

                    try:
                        if session is not None:
                            session.request = bounded_request
                        result = original_download(url, pathfmt)
                        if result and cap and pathfmt.temppath and Path(pathfmt.temppath).is_file() and Path(pathfmt.temppath).stat().st_size > cap:
                            self.size_exceeded = True
                        if self.size_exceeded:
                            raise SizeLimit()
                        successful = bool(result)
                        return result
                    except RequestException:
                        # Requests exceptions can inherit OSError. Preserve the
                        # existing network protection rather than call them a
                        # local storage failure.
                        raise
                    except OSError:
                        # gallery-dl treats most local OSErrors as a failed
                        # download and then tries another size. A locked or
                        # unwritable file needs user action, not more requests.
                        guard.failure = guard.failure or _LOCAL_WRITE_ERROR
                        if response is not None:
                            try:
                                response.close()
                            except OSError:
                                pass
                        raise exception.AbortExtraction(guard.failure) from None
                    finally:
                        if session is not None:
                            session.request = original_request
                        if self.size_exceeded and response is not None:
                            response.close()
                        if not successful and not self.size_exceeded:
                            self.progress(self.progress_bytes, self.progress_total, force=True, final=True)

                instance.download = tracked_download

            if instance is not None and hasattr(instance, "receive") and not getattr(instance, "_luvbox_stop", False):
                receive = instance.receive

                def checked_receive(fp, content, bytes_total, bytes_start):
                    cap = normalized.max_video_mb * 1024 * 1024 if self.current_media.get("type") != "photo" else 0
                    downloaded = bytes_start
                    if cap and (downloaded > cap or (bytes_total and bytes_total > cap)):
                        self.size_exceeded = True
                        raise SizeLimit()
                    def chunks():
                        nonlocal downloaded
                        try:
                            for chunk in content:
                                check_stop()
                                if cap and downloaded + len(chunk) > cap:
                                    self.size_exceeded = True
                                    raise SizeLimit()
                                downloaded += len(chunk)
                                yield chunk
                                self.progress(downloaded, bytes_total)
                        except RequestException:
                            guard.mark_media_error()
                            raise
                    try:
                        return receive(fp, chunks(), bytes_total, bytes_start)
                    except RequestException:
                        guard.mark_media_error()
                        raise

                instance.receive = checked_receive
            if instance is not None:
                instance._luvbox_stop = True
            return instance

    try:
        normalized = options.validate()
        if normalized.confirm_each and review is None:
            raise ValueError("파일마다 저장 여부 확인은 프로그램에서 직접 저장을 시작할 때 사용할 수 있습니다.")
        handler.mode = normalized.auth_mode
        check_gallery_hooks()
        check_stop()
        guard = AccountGuard(normalized.account, sleep=sleep)
        guard.acquire()
        output_guard = OutputGuard(normalized.output_dir)
        output_guard.acquire()
        storage = resolve_storage(normalized.output_dir, normalized.account, normalized.separate_accounts)
        # The folder can disappear after the window or scheduler checks it.
        # Recheck while owning the write guards, before creating an empty copy.
        if not normalized.allow_recreate_media and saved_media_missing(storage):
            raise ValueError(missing_media_help(storage))
        account_dir = storage.media_dir
        account_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=account_dir):
            pass
        history = HistoryStore.for_storage(storage, protected=True)
        from .thumbcache import Recorder
        # A small preview on this PC still shows a file deleted later.
        previews = Recorder(normalized.account)
        # Previously saved files may still be tagged at their old location.
        # Exclude another account writing into those same selected roots too.
        for previous_root in history.media_roots[1:]:
            previous_guard = OutputGuard(previous_root, group=output_guard)
            previous_guard.acquire()
            previous_output_guards.append(previous_guard)
        history.import_legacy()
        history.recover_interrupted_runs()
        # Account/output locks protect reconciliation. Never infer a successful
        # recycle from absence, and never retry an interrupted file deletion.
        history.reconcile_deletions()
        targets = frozenset(normalized.targets)
        ignored = history.ignored_media()
        if not targets:
            remembered = history.review_pending()
            review_unknown = remembered is None
            review_pending = remembered or set()
        scan_note = ""
        if targets:
            scan_note = (f"사라진 파일 {len(targets):,}개(트윗 {len(_retry_groups(normalized.targets)):,}개)를 다시 받습니다. "
                         "Like 목록 대신 트윗마다 X 요청을 1번씩 보냅니다.")
        elif normalized.scan_mode == "quick":
            readiness = history.quick_scan_readiness(normalized.folder_mode, normalized.media_signature)
            if readiness == "ready" and review_unknown:
                readiness = "incomplete"
            quick_scan = readiness == "ready"
            scan_note = {
                "ready": f"새 Like만 확인합니다. 이미 저장한 파일이 {QUICK_STOP_AFTER}개 이어지면 멈춥니다.",
                "settings-changed": "동영상·GIF·용량 설정이 바뀌어 이번만 전체 목록을 확인합니다.",
                "records-changed": "다른 계정의 저장 기록을 합쳐 이번만 전체 목록을 확인합니다.",
            }.get(readiness, "이전 확인이 끝까지 되지 않아(첫 실행·중단·오류·시험 저장) 이번만 전체 목록을 확인합니다.")
            if quick_scan and review_pending:
                scan_note += f" 지난번에 건너뛴 파일 {len(review_pending):,}개가 나올 때까지는 이어서 확인합니다."
        run_id = history.start_run(normalized.folder_mode,
                                   scan_mode="retry" if targets else "quick" if quick_scan else "full",
                                   media_signature=normalized.media_signature)
        # Only walking the Like list tells where a Like belongs in its order.
        likes = None if targets else history.like_order(run_id)
        metadata_available = available()
        # Preserve the original author archive so existing installations resume.
        # Other layouts need their own record, otherwise switching folders would
        # skip photos saved only in the previous destination.
        archive_name = (".luvbox-archive.sqlite3" if normalized.folder_mode == "author"
                        else f".luvbox-archive.{normalized.folder_mode}.sqlite3")
        archive_path = storage.record_dir / archive_name
        _configure_gallery(normalized, archive_path, storage.legacy_dir.name)
        common.CACHE_COOKIES.clear()
        common.CACHE_MEMORY.clear()
        emit({"type": "log", "level": "info", "message": "사진은 원본, 동영상은 최고 화질로 저장합니다. GIF는 받은 뒤 이 PC에서 변환합니다."})
        emit({"type": "log", "level": "info", "message": f"목록은 {API_INTERVAL:g}초, 미디어는 {MEDIA_INTERVAL:g}초 간격으로 요청합니다. 요청 한도가 거의 차거나 계정 제한이 오면 멈춥니다."})
        if scan_note:
            emit({"type": "log", "level": "info", "message": scan_note})
        # One resident ExifTool for the run; the availability check above stays the only one.
        with persistent() if metadata_available else nullcontext():
            if targets:
                handler.per_tweet = True
                exit_code = 0
                for tweet_id, keys in _retry_groups(normalized.targets):
                    check_stop()
                    # Release just this tweet's archive entries, so a stop or an
                    # unavailable tweet leaves every other choice as it was.
                    entries = _archive_entries(archive_path, keys)
                    _set_archive_entries(archive_path, entries, present=False)
                    active_job = PhotoJob(f"https://x.com/i/web/status/{tweet_id}")
                    try:
                        active_job.run()
                    finally:
                        _set_archive_entries(archive_path, [entry for entry in entries
                                                            if _entry_key(entry) not in recovered], present=True)
                        session = getattr(active_job.extractor, "session", None)
                        if session is not None:
                            session.cookies.clear()
                            session.close()
                    if guard.failure or handler.fatal or handler.rate_limited:
                        exit_code = 1
                        break
                    # A stop while this tweet was handled is not its outcome.
                    check_stop()
                    # Files over the size limit are reported as size-skipped.
                    left = [key for key in keys if key not in recovered and key not in size_limited and
                            key not in review_declined]
                    if not left:
                        continue
                    if tweet_id in visited_ids:
                        absent_files += len(left)
                        notice = "트윗에서 이 미디어를 찾지 못했거나 받지 못했습니다."
                    else:
                        unavailable_tweets += 1
                        notice = "트윗을 볼 수 없어(삭제·비공개·제한) 다시 받지 못했습니다."
                    # The address lets the user check the tweet in a browser.
                    emit({"type": "log", "level": "info", "message": f"{notice} https://x.com/i/web/status/{tweet_id}"})
            else:
                active_job = PhotoJob(f"https://x.com/{normalized.account}/likes")
                exit_code = active_job.run()
        if handler.rate_limited and not guard.failure:
            guard.record_limit()
        if guard.failure:
            exit_code = 1
        check_stop()
        if stop_reason == "user":
            status, exit_code = "stopped", 0
            message = "저장을 중단했습니다. 다시 실행하면 저장한 파일은 건너뜁니다."
        elif exit_code:
            message = guard.failure or handler.last_error or "미디어를 모두 저장하지 못했습니다. 로그인과 저장 폴더를 확인한 뒤 다시 실행해 주세요."
        elif targets:
            status = "finished"
            message = _retry_message(saved, sum(value == "existing" for value in recovered.values()),
                                     unavailable_tweets, absent_files, size_skipped)
        elif stop_reason == "limit":
            status = "finished"
            message = f"시험 저장 {saved}개를 마쳤습니다. 시험 저장을 끄고 다시 실행하면 이어서 저장합니다."
        elif stop_reason == "caught-up":
            status = "finished"
            message = (f"새 Like 확인을 마쳤습니다. {saved:,}개를 저장했고, 이미 저장한 파일이 "
                       f"{QUICK_STOP_AFTER}개 이어져 멈췄습니다.")
        elif not saved and not skipped and not size_skipped and not review_skipped and not review_never and not ignored_encountered:
            exit_code = 1
            message = ("저장할 미디어를 찾지 못했습니다. 본인 계정으로 로그인했는지, "
                       "좋아요에 사진·영상이 있는지 확인해 주세요.")
        else:
            message = ("X가 보여 주는 좋아요 목록을 모두 확인했습니다. "
                       "삭제된 게시물이나 X가 더는 보여 주지 않는 오래된 좋아요는 빠질 수 있습니다.")
            status = "finished"
        if review_skipped:
            message += (f" 이번엔 건너뛴 파일 {review_skipped:,}개는 " +
                        ("‘다시 받기’에서 다시 선택할 수 있습니다." if targets else "다음 저장 때 다시 보여 드립니다."))
        if review_never:
            message += (f" ‘저장 안 함’으로 고른 파일 {review_never:,}개는 다시 묻지 않습니다. "
                        "필요하면 ‘사라진 파일’의 숨긴 파일에서 다시 받을 수 있습니다.")
    except exception.StopExtraction:
        status, exit_code = "stopped", 0
        message = "저장을 중단했습니다."
    except ProtectionError as exc:
        status, exit_code, message = "error", 1, str(exc)
    except ValueError as exc:
        # Only our validation messages are permitted here.
        if active_job is None:
            message = str(exc)
        else:
            message = _safe_diagnostic(str(exc), normalized.auth_mode)
        exit_code = 1
    except PermissionError:
        message = "저장 폴더에 쓸 수 없습니다. 문서 또는 사진 폴더 안의 다른 폴더를 선택해 주세요."
        exit_code = 1
    except GalleryCompatibilityError:
        status, exit_code, message = "error", 1, _COMPATIBILITY_ERROR
    except sqlite3.Error:
        status, exit_code, message = "error", 1, _RECORD_ERROR
    except CatalogDamaged as exc:
        status, exit_code, message = "error", 1, str(exc)
    except HistoryError:
        status, exit_code, message = "error", 1, _HISTORY_ERROR
    except OSError as exc:
        message = _safe_diagnostic(str(exc), normalized.auth_mode)
        exit_code = 1
    except Exception as exc:
        message = _safe_diagnostic(str(exc), normalized.auth_mode)
        exit_code = 1
    finally:
        if history is not None:
            try:
                # Persist completed retries even if a later tweet was stopped
                # or failed. Unprocessed choices keep their hidden state.
                restored = [identity for identity in map(_key_identity, recovered) if identity in ignored]
                if restored:
                    history.set_missing_ignored(restored, False)
                if likes is not None:
                    likes.flush()
                flush_tweet_texts()
                if run_id is not None:
                    if not targets:
                        # A declined file must not disappear behind a later
                        # saved-file streak: remember it for the next quick run.
                        remaining = review_pending - review_resolved
                        if status == "finished" and stop_reason in {"", "caught-up"}:
                            # The whole list was walked, or every remembered
                            # file was reached; unreached ones are not Liked now.
                            remaining &= review_reached
                        remaining |= review_declined_ids
                        if review_unknown or remaining != review_pending:
                            history.set_review_pending(remaining)
                    run_status = "limited" if stop_reason == "limit" and status == "finished" else status
                    history.finish_run(run_id, run_status, saved=saved, skipped=skipped, size_skipped=size_skipped,
                                       visited=len(visited_ids), last_tweet_id=last_tweet_id,
                                       last_posted_at=last_posted_at, message=message)
            except HistoryError:
                status, exit_code, message = "error", 1, _HISTORY_ERROR
            finally:
                if previews is not None:
                    previews.close()
                    if previews.failed:
                        emit({"type": "log", "level": "warning", "message":
                              f"작은 미리보기 {previews.failed:,}개를 이 PC에 보관하지 못했습니다. "
                              "다운로드한 원본은 그대로이며 라이브러리에서 파일을 열면 자동으로 다시 시도합니다."})
                try:
                    history.close()
                except HistoryError:
                    status, exit_code, message = "error", 1, _HISTORY_ERROR
            # A verified copy on this PC, so a damaged catalog can be restored.
            try:
                from .recovery import backup_catalog
                backup_catalog(history.account_dir, normalized.account)
            except (OSError, sqlite3.Error):
                pass
            # The media folder keeps a complete copy, so records travel with it.
            try:
                from .storage import MigrationError, publish_snapshot
                publish_snapshot(storage)
            except (MigrationError, OSError, sqlite3.Error, ValueError):
                emit({"type": "log", "level": "warning", "message": _SNAPSHOT_ERROR})
        if guard is not None:
            guard.close()
        if output_guard is not None:
            output_guard.close()
        for previous_guard in reversed(previous_output_guards):
            previous_guard.close()
        if active_job is not None:
            session = getattr(active_job.extractor, "session", None)
            if session is not None:
                session.cookies.clear()
                session.close()
        common.CACHE_COOKIES.clear()
        common.CACHE_MEMORY.clear()
        config.clear()
        root_logger.handlers = previous_handlers
        root_logger.setLevel(previous_level)
        for logger, handlers, level, propagate in logger_states:
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate
    if author_deferred:
        emit({"type": "log", "level": "info",
              "message": f"이미 있던 파일 {author_deferred:,}개의 작성자 정보를 저장 기록에 반영했습니다. "
                         "기존 파일의 메타데이터는 유지했습니다."})
    if status == "error":
        emit({"type": "log", "level": "error", "message": message})
    emit({"type": "finished", "status": status, "saved": saved, "skipped": skipped,
          "size_skipped": size_skipped, "review_skipped": review_skipped, "review_never": review_never,
          "metadata_failed": metadata_failed, "author_deferred": author_deferred,
          "visited": len(visited_ids), "last_tweet_id": last_tweet_id,
          "last_posted_at": last_posted_at,
          "scan_mode": "retry" if targets else "quick" if quick_scan else "full",
          "recovered": len(recovered), "unavailable_tweets": unavailable_tweets, "absent_files": absent_files,
          "retry_at": guard.retry_at if guard is not None else "", "message": message})
    return exit_code
