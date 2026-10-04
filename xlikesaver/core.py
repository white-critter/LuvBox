"""Isolated gallery-dl media worker.

Run one worker per process. gallery-dl uses process-global configuration and
caches, so this module deliberately does not support concurrent in-process jobs.
"""

from __future__ import annotations

import copy
import inspect
import logging
import os
import re
import sqlite3
import tempfile
import time
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from datetime import datetime as _Datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .protection import API_INTERVAL, MEDIA_INTERVAL, AccountGuard, ProtectionError
from .history import HistoryError, HistoryStore, _author_fields
from .storage import OutputGuard, resolve_storage


EventSink = Callable[[dict], None]
_ACCOUNT = re.compile(r"[A-Za-z0-9_]{1,15}\Z")
_ROUTES = {"i", "home", "explore", "search", "settings", "notifications",
           "messages", "compose", "intent", "login", "logout", "signup",
           "hashtag", "account", "privacy", "tos", "oauth"}
_AUTH_MODES = {"file", "managed"}
_FOLDER_MODES = {"author", "date", "flat"}
_SCAN_MODES = {"full", "quick"}
# A quick scan ends after this many already-saved files in a row. Likes are
# listed newest-like first, so this is the point where the previous complete
# run left off. Unsaved files never count toward the streak.
QUICK_STOP_AFTER = 100
_KOREA = timezone(timedelta(hours=9))
_RECORD_ERROR = ("저장 기록을 읽거나 갱신하지 못했습니다. "
                 "저장 폴더의 권한과 다른 실행 중인 프로그램을 확인해 주세요.")
_COMPATIBILITY_ERROR = ("내장 gallery-dl 구성이 이 버전의 XlikeSaver가 예상한 것과 달라 요청을 보내기 전에 중단했습니다. "
                        "배포된 실행 파일을 다시 받거나 setup.ps1로 고정된 버전을 설치해 주세요.")
_HISTORY_ERROR = "저장 이력을 기록하지 못했습니다. 저장 폴더의 쓰기 권한과 디스크 여유 공간을 확인해 주세요."
_METADATA_ERROR = "출처 또는 작성자를 기록하지 못했습니다. 미디어 원본은 그대로 보존했습니다."
_DATE_MOVE_ERROR = "저장 완료일 폴더로 옮기지 못했습니다. 기존 파일 위치와 중복 방지 기록은 보존했습니다."
_LOCAL_WRITE_ERROR = ("미디어 파일을 쓰지 못해 중단했습니다. 저장 폴더의 쓰기 권한, 다른 프로그램의 파일 사용 여부, "
                      "디스크 여유 공간을 확인한 뒤 직접 다시 실행해 주세요. "
                      "저장 완료 파일과 중복 방지 기록은 유지됩니다.")


def _save_day() -> date:
    """The download's local saving day, independent of tweet publication."""
    return datetime.now(_KOREA).date()


def _move_without_overwrite(source: Path, destination: Path) -> None:
    """Move within the account filesystem without replacing another file."""
    if os.name == "nt":
        # Windows rename is atomic and fails if the destination already exists.
        source.rename(destination)
    else:
        # POSIX rename replaces existing files. Linking is an exclusive create
        # and keeps the original intact until the second operation succeeds.
        os.link(source, destination)
        try:
            source.unlink()
        except OSError:
            try:
                destination.unlink()
            except OSError:
                pass
            raise


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
    return {"tweet_id": tweet_id, "num": metadata.get("num", 0),
            "media_type": metadata.get("type", ""),
            "author": author, "author_id": author_id,
            "posted_at": posted,
            "filename": f"{tweet_id}_{metadata.get('_xlikesaver_filename_part', metadata.get('num', 0))}.{metadata.get('extension', '')}",
            "source_url": f"https://x.com/i/web/status/{tweet_id}" if re.fullmatch(r"[0-9]{1,20}", tweet_id) else "",
            "metadata_status": metadata.get("_xlikesaver_metadata_status", "pending"),
            "author_metadata_status": metadata.get("_xlikesaver_author_metadata_status", "pending")}


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

    @property
    def media_signature(self) -> str:
        """Settings that decide which files a previous scan could have skipped."""
        return f"v1:videos={int(self.include_videos)}:gifs={int(self.include_gifs)}:max_mb={self.max_video_mb}"

    def validate(self) -> "JobOptions":
        account = _normal_account(self.account)
        if not isinstance(self.auth_mode, str) or self.auth_mode.strip().lower() not in _AUTH_MODES:
            raise ValueError("지원하는 인증 방법은 ‘인증 토큰 연결’과 ‘쿠키 파일(cookies.txt)’입니다. 인증 방법을 다시 선택해 주세요.")
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
        if isinstance(self.max_video_mb, bool) or not isinstance(self.max_video_mb, int) or self.max_video_mb < 0:
            raise ValueError("동영상·GIF 크기 한도는 0 이상의 정수 MB로 입력해 주세요. 0은 제한 없음입니다.")
        if isinstance(self.photo_limit, bool) or not isinstance(self.photo_limit, int) or self.photo_limit < 0:
            raise ValueError("파일 저장 한도는 0 이상의 정수여야 합니다.")
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
                       folder_mode=folder_mode, scan_mode=scan_mode)


def _auth_help(mode: str) -> str:
    if mode == "managed":
        return "저장된 인증 토큰이 만료되었거나 유효하지 않습니다. ‘인증 토큰 연결’을 선택하고 auth_token을 다시 입력해 ‘연결 저장’을 눌러 주세요."
    return "X에 다시 로그인한 뒤 본인 계정의 새 cookies.txt 파일을 내보내 ‘쿠키 파일’에서 선택해 주세요."


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
    return "X 목록을 읽는 중 문제가 발생했습니다. 로그인 상태를 확인하고 다시 시도해 주세요."


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

    def emit(self, record: logging.LogRecord) -> None:
        # INFO can contain URLs, arbitrary server text or other user metadata.
        # Progress is emitted separately, so only mapped diagnostics are useful.
        if record.levelno < logging.WARNING or self.stopping:
            return
        raw = record.getMessage()
        if raw == "media size exceeded":
            return
        text = raw.lower()
        notice = _skipped_post_notice(raw) if record.levelno < logging.ERROR else ""
        if notice:
            level, message, priority = "info", notice, 0
        else:
            if _RATE_TERMS.search(text):
                self.rate_limited = True
            level = "error" if record.levelno >= logging.ERROR else "warning"
            message = _safe_diagnostic(raw, self.mode)
            priority = (3 if _RATE_TERMS.search(text) or _STORAGE_TERMS.search(text) or _AUTH_TERMS.search(text)
                        else 1 if record.levelno >= logging.ERROR else 0)
        if priority and priority >= self._error_priority:
            self.last_error = message
            self._error_priority = priority
        if message != self._last_message:
            self._last_message = message
            self._emit({"type": "log", "level": level, "message": message})


def _configure_gallery(options: JobOptions, archive_path: Path, account_folder: str) -> None:
    from gallery_dl import config

    config.clear()
    config.set(("cache",), "file", ":memory:")
    config.set(("output",), "mode", "null")
    directory = [account_folder] if options.separate_accounts else []
    if options.folder_mode == "author":
        directory.append("{author[name]}")
    elif options.folder_mode == "date":
        directory.append("{_xlikesaver_date}")
    values = {
        "base-directory": options.output_dir,
        "directory": directory,
        "filename": "{tweet_id}_{_xlikesaver_filename_part}.{extension}",
        "archive": str(archive_path),
        "archive-format": "{_xlikesaver_archive_key}",
        "archive-event": ["file", "skip"],
        "archive-mode": "disk", "archive-reuse": False,
        "cookies-update": False, "netrc": False,
        "videos": options.include_videos or options.include_gifs, "previews": False, "cards": False,
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


def run_worker(options: JobOptions, emit: EventSink, stop_file: Path | str | None = None) -> int:
    """Download available liked media and emit structured, secret-free events.

    A successful traversal covers the list X exposes to this authenticated
    session; it is not evidence that every historical like was available.
    """
    from gallery_dl import config, exception, job, output, util
    from gallery_dl.extractor import common
    from requests.exceptions import RequestException
    from .metadata import available, embed_source, persistent

    saved = skipped = size_skipped = 0
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
    run_id = None
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

    def history_update(**fields):
        if history is not None and run_id is not None:
            try:
                history.update_run(run_id, **fields)
            except HistoryError:
                raise exception.AbortExtraction("history store unavailable") from None

    def record_source(kwdict, media_path, media_status):
        nonlocal metadata_failed, metadata_warned, author_deferred
        try:
            previous = (history.media_record(normalized.folder_mode, kwdict)
                        if media_status == "existing" else None) or {}
        except HistoryError:
            raise exception.AbortExtraction("history store unavailable") from None
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
        if previous_status == "unsupported" or (previous_status == "embedded" and author_done):
            metadata_status = previous_status
            author_status = previous_author_status if known_author else "unknown"
        elif previous_status == "embedded" and media_status == "existing":
            # An already saved file is rewritten during a run only to retry a
            # missing source. Adding or updating just its author is left to the
            # explicit '기존 파일에 출처·작성자 기록' action, so a complete scan
            # never rewrites a whole library (or makes cloud sync re-upload it).
            # The catalog keeps the current author for that action.
            metadata_status = "embedded"
            author_status = previous_author_status if same_author else "pending"
            author_deferred += 1
        elif metadata_available:
            recording_required = True
            try:
                result = embed_source(media_path, str(kwdict["tweet_id"]), author=author, author_id=author_id)
                metadata_status = result.get("status")
                if metadata_status not in {"embedded", "failed", "unsupported"}:
                    metadata_status = "failed"
                author_status = result.get("author_status", "error" if known_author and metadata_status != "unsupported" else "unknown")
                if author_status not in {"embedded", "unknown", "error"}:
                    author_status = "error"
            except Exception:
                metadata_status = "failed"
                author_status = "error" if known_author else "unknown"
        else:
            recording_required = True
            metadata_status = "failed"
            author_status = "error" if known_author else "unknown"
        kwdict["_xlikesaver_metadata_status"] = metadata_status
        kwdict["_xlikesaver_author_metadata_status"] = author_status
        # Deferred work keeps any prior author error for retry; it is not a
        # recording failure from this run.
        if recording_required and (metadata_status == "failed" or author_status == "error"):
            metadata_failed += 1
            if not metadata_warned:
                metadata_warned = True
                emit({"type": "log", "level": "warning", "message": _METADATA_ERROR})

    class SizeLimit(exception.ControlException):
        default = "media size exceeded"

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
            self.photo_positions = {}
            self.photo_counts = {}
            extract_media = getattr(self.extractor, "_extract_media", None)
            if extract_media is not None:
                extract_source = self.extractor._extract_media_source
                def numbered_source(dest, media):
                    extract_source(dest, media)
                    if "_xlikesaver_attachment_number" in media:
                        dest["_xlikesaver_attachment_number"] = media["_xlikesaver_attachment_number"]
                self.extractor._extract_media_source = numbered_source

                def direct_media(tweet, media_items, files):
                    selected = []
                    for attachment_number, media in enumerate(media_items, 1):
                        media = dict(media, _xlikesaver_attachment_number=attachment_number)
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
                        session_cookies = load_session_cookies()
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
            nonlocal stop_reason, saved, skipped, size_skipped, existing_streak
            check_stop()
            media_type = kwdict.get("type")
            if media_type not in {"photo", "video", "animated_gif"}:
                return
            if ((media_type == "video" and not normalized.include_videos) or
                    (media_type == "animated_gif" and not normalized.include_gifs)):
                return
            if media_type != "photo" and not urlsplit(url).path.lower().endswith(".mp4"):
                return
            kwdict = dict(kwdict)
            tweet_id = str(kwdict["tweet_id"])
            attachment_number = kwdict.get("_xlikesaver_attachment_number", kwdict["num"])
            if media_type == "photo":
                position_key = (tweet_id, attachment_number)
                if position_key not in self.photo_positions:
                    self.photo_counts[tweet_id] = self.photo_counts.get(tweet_id, 0) + 1
                    self.photo_positions[position_key] = self.photo_counts[tweet_id]
                kwdict["num"] = self.photo_positions[position_key]
                kwdict["_xlikesaver_filename_part"] = str(kwdict["num"])
                kwdict["_xlikesaver_archive_key"] = f"{tweet_id}_{kwdict['num']}"
            else:
                kind = "gif" if media_type == "animated_gif" else "video"
                kwdict["num"] = attachment_number
                kwdict["extension"] = "mp4"
                kwdict["_xlikesaver_filename_part"] = f"{kind}_{kwdict['num']}"
                kwdict["_xlikesaver_archive_key"] = f"{tweet_id}_{media_type}_{kwdict['num']}"
            self.current_media = kwdict
            self.last_output = None
            self.progress_started = self.progress_last = None
            self.progress_bytes = self.progress_total = 0
            self.progress_offset = 0
            self.size_exceeded = False
            if normalized.folder_mode == "date":
                kwdict = dict(kwdict, _xlikesaver_date=_save_day().isoformat())
            self.current_media = kwdict
            # Real X file metadata contains its tweet's directory metadata.
            # Delay folder/archive setup until media is selected for this run.
            super().handle_directory(kwdict)
            self.pathfmt.set_filename(kwdict)
            self.pathfmt.build_path()
            if (not history.is_download_path(self.pathfmt.path) or
                    not history.is_download_path(self.pathfmt.path + ".part")):
                raise exception.AbortExtraction("permissionerror unsafe media output")
            try:
                super().handle_url(url, kwdict)
            except SizeLimit:
                size_skipped += 1
                if self.pathfmt.temppath:
                    Path(self.pathfmt.temppath).unlink(missing_ok=True)
                self.progress(self.progress_bytes, self.progress_total, force=True, final=True)
                emit({"type": "size-skipped", "path": "", "max_video_mb": normalized.max_video_mb,
                      **_media_details(kwdict)})
            else:
                if self.last_output:
                    media_status, media_path = self.last_output
                    if media_status == "saved":
                        saved += 1
                        if normalized.folder_mode == "date":
                            completed_day = _save_day().isoformat()
                            if completed_day != kwdict["_xlikesaver_date"]:
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
                                    emit({"type": "log", "level": "warning", "message": _DATE_MOVE_ERROR})
                                else:
                                    media_path = str(completed_path)
                                    kwdict["_xlikesaver_date"] = completed_day
                    else:
                        skipped += 1
                        # An archive hit may belong to an older saving day or
                        # the previous account-folder setting. Keep its actual
                        # location rather than today's generated destination.
                        try:
                            previous = history.media_record(normalized.folder_mode, kwdict)
                        except HistoryError:
                            raise exception.AbortExtraction("history store unavailable") from None
                        known_path = (previous.get("path") or "") if previous is not None else media_path
                        media_path = ""
                        if known_path:
                            candidate = Path(known_path)
                            if history.is_media_path(candidate) and candidate.is_file():
                                media_path = str(candidate)
                    self.last_output = (media_status, media_path)
                    kwdict["_xlikesaver_path"] = media_path
                    original_size = Path(media_path).stat().st_size if Path(media_path).is_file() else 0
                    if Path(media_path).is_file():
                        record_source(kwdict, media_path, media_status)
                    emit({"type": "saved" if media_status == "saved" else "skipped", "path": media_path,
                          **_media_details(kwdict)})
                    if Path(media_path).is_file():
                        try:
                            history.record_media(normalized.folder_mode, kwdict, media_path, status=media_status)
                        except HistoryError:
                            raise exception.AbortExtraction("history store unavailable") from None
                    if media_status == "saved":
                        self.progress(original_size, self.progress_total, force=True, final=True)
            history_update(saved=saved, skipped=skipped, size_skipped=size_skipped)
            # Archive insertion happens after out.success; stop only once the
            # completed file has been recorded, so reruns remain consistent.
            if normalized.photo_limit and saved >= normalized.photo_limit:
                stop_reason = "limit"
                raise exception.StopExtraction()
            if quick_scan and self.last_output:
                # Size-limited and failed files are neither saved nor known,
                # so they neither extend nor break the already-saved streak.
                existing_streak = 0 if self.last_output[0] == "saved" else existing_streak + 1
                if existing_streak >= QUICK_STOP_AFTER:
                    stop_reason = "caught-up"
                    raise exception.StopExtraction()

        def handle_directory(self, kwdict):
            nonlocal last_tweet_id, last_posted_at
            check_stop()
            if "tweet_id" in kwdict:
                last_tweet_id = str(kwdict["tweet_id"])
                last_posted_at = _media_details(kwdict)["posted_at"]
                visited_ids.add(last_tweet_id)
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
                  "path": self.current_media.get("_xlikesaver_path", "")})

        def download(self, url):
            # gallery-dl's media downloader catches exceptions from request()
            # and returns False. Re-raise a protection failure before its parent
            # attempts other image sizes or the next photo.
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
            return result

        def get_downloader(self, scheme):
            instance = super().get_downloader(scheme)
            if instance is not None and not getattr(instance, "_xlikesaver_stop", False):
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

            if instance is not None and hasattr(instance, "receive") and not getattr(instance, "_xlikesaver_stop", False):
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
                instance._xlikesaver_stop = True
            return instance

    try:
        normalized = options.validate()
        handler.mode = normalized.auth_mode
        check_gallery_hooks()
        check_stop()
        guard = AccountGuard(normalized.account, sleep=sleep)
        guard.acquire()
        output_guard = OutputGuard(normalized.output_dir)
        output_guard.acquire()
        storage = resolve_storage(normalized.output_dir, normalized.account, normalized.separate_accounts)
        account_dir = storage.media_dir
        account_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=account_dir):
            pass
        history = HistoryStore.for_storage(storage, protected=True)
        # Previously saved files may still be tagged at their old location.
        # Exclude another account writing into those same selected roots too.
        for previous_root in history.media_roots[1:]:
            previous_guard = OutputGuard(previous_root)
            previous_guard.acquire()
            previous_output_guards.append(previous_guard)
        history.import_legacy()
        history.recover_interrupted_runs()
        scan_note = ""
        if normalized.scan_mode == "quick":
            readiness = history.quick_scan_readiness(normalized.folder_mode, normalized.media_signature)
            quick_scan = readiness == "ready"
            scan_note = {
                "ready": f"새 Like만 빠르게 확인합니다. 이미 저장한 파일이 {QUICK_STOP_AFTER}개 연속으로 나오면 이전 확인 지점으로 보고 멈춥니다.",
                "settings-changed": "동영상·GIF·용량 제한 설정이 이전 확인 때와 달라 이번에는 전체 목록을 확인합니다. 다음 실행부터 다시 빠르게 확인합니다.",
            }.get(readiness, "이전 실행이 끝까지 확인되지 않았거나(첫 실행·중단·오류·시험 저장) 이번 방식의 기록이 없어 전체 목록을 확인합니다. "
                             "다음 실행부터 빠르게 확인합니다.")
        run_id = history.start_run(normalized.folder_mode, scan_mode="quick" if quick_scan else "full",
                                   media_signature=normalized.media_signature)
        metadata_available = available()
        # Preserve the original author archive so existing installations resume.
        # Other layouts need their own record, otherwise switching folders would
        # skip photos saved only in the previous destination.
        archive_name = (".xlikesaver-archive.sqlite3" if normalized.folder_mode == "author"
                        else f".xlikesaver-archive.{normalized.folder_mode}.sqlite3")
        archive_path = storage.record_dir / archive_name
        _configure_gallery(normalized, archive_path, storage.legacy_dir.name)
        common.CACHE_COOKIES.clear()
        common.CACHE_MEMORY.clear()
        emit({"type": "log", "level": "info", "message": "본인 계정의 마음에 들어요 목록에서 사진은 원본 크기를 우선하고, 선택한 동영상·GIF는 가장 높은 비트레이트의 MP4를 찾습니다."})
        emit({"type": "log", "level": "info", "message": f"X 목록 요청은 최소 {API_INTERVAL:g}초, 미디어 요청은 최소 {MEDIA_INTERVAL:g}초 간격으로 보냅니다. 남은 요청 한도가 5회 이하이거나 요청 한도·계정 인증 제한을 만나면 중단합니다."})
        if scan_note:
            emit({"type": "log", "level": "info", "message": scan_note})
        active_job = PhotoJob(f"https://x.com/{normalized.account}/likes")
        # One resident ExifTool for the run; the availability check above stays the only one.
        with persistent() if metadata_available else nullcontext():
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
        elif stop_reason == "limit":
            status = "finished"
            message = (f"테스트 파일 {saved}개 저장을 마쳤습니다. 저장한 파일 기록은 유지됩니다. "
                       "한도를 해제하고 다시 실행하면 이미 저장한 파일을 건너뛰고 계속 확인합니다.")
        elif stop_reason == "caught-up":
            status = "finished"
            message = (f"새 Like 확인을 마쳤습니다. 이번에 {saved:,}개를 저장했고, 이미 저장한 파일이 "
                       f"{QUICK_STOP_AFTER}개 연속으로 나와 이전 확인 지점에서 멈췄습니다.")
        elif not saved and not skipped and not size_skipped:
            exit_code = 1
            message = ("저장할 미디어를 찾지 못했습니다. 본인 계정으로 로그인했는지 확인해 주세요. "
                       "마음에 들어요에 선택한 형식의 미디어가 없거나 X가 목록 접근을 제한했을 수 있습니다.")
        else:
            message = ("X에서 현재 읽을 수 있는 목록의 확인을 마쳤습니다. 저장한 파일 기록은 유지됩니다. "
                       "삭제된 게시물이나 X가 제공하지 않은 과거 마음에 들어요는 포함되지 않을 수 있습니다.")
            status = "finished"
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
                if run_id is not None:
                    run_status = "limited" if stop_reason == "limit" and status == "finished" else status
                    history.finish_run(run_id, run_status, saved=saved, skipped=skipped, size_skipped=size_skipped,
                                       visited=len(visited_ids), last_tweet_id=last_tweet_id,
                                       last_posted_at=last_posted_at, message=message)
            except HistoryError:
                status, exit_code, message = "error", 1, _HISTORY_ERROR
            finally:
                try:
                    history.close()
                except HistoryError:
                    status, exit_code, message = "error", 1, _HISTORY_ERROR
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
              "message": f"이미 저장된 파일 {author_deferred:,}개는 작성자 정보를 기록 DB에만 반영했습니다. "
                         "파일 안에도 넣으려면 ‘기록 · 통계 → 기존 파일에 출처·작성자 기록’을 실행하세요."})
    if status == "error":
        emit({"type": "log", "level": "error", "message": message})
    emit({"type": "finished", "status": status, "saved": saved, "skipped": skipped,
          "size_skipped": size_skipped, "metadata_failed": metadata_failed, "author_deferred": author_deferred,
          "visited": len(visited_ids), "last_tweet_id": last_tweet_id,
          "last_posted_at": last_posted_at,
          "scan_mode": "quick" if quick_scan else "full", "retry_at": guard.retry_at if guard is not None else "", "message": message})
    return exit_code
