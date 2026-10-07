"""Per-account request pacing, local cooldowns, and process exclusion.

These precautions reduce unnecessary requests; they do not make website
automation an approved X API client or guarantee freedom from restrictions.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

from requests.exceptions import RequestException


API_INTERVAL = 10.0
MEDIA_INTERVAL = 2.0
DEFAULT_COOLDOWN = 15 * 60
_KOREA = timezone(timedelta(hours=9))
_STATE_HELP = "요청 보호 기록을 읽거나 저장하지 못해 중단했습니다. Windows 사용자 폴더의 권한을 확인해 주세요."
_BUSY_HELP = "같은 X 계정으로 다른 저장 작업이 실행 중입니다. 먼저 실행한 작업을 종료해 주세요."
_AUTH_HELP = "X가 로그인 또는 접근을 거부해 중단했습니다. X에서 계정 상태를 직접 확인하고 필요하면 다시 로그인해 주세요."
_LOCKED_HELP = "X가 계정 잠금 또는 추가 인증을 알려 중단했습니다. X에서 직접 계정 상태를 확인하고 인증을 완료해 주세요."
_API_ERROR_HELP = "X 목록 서버 오류로 멈췄습니다. 잠시 후 다시 실행해 주세요."
_MEDIA_ERROR_HELP = "사진 서버 접근·연결 오류로 멈췄습니다. 잠시 후 다시 실행해 주세요."
_RESUME_ERROR_HELP = ("서버가 이어받기를 거부해 멈췄습니다. 받다 만 .part 파일은 남겨 두었습니다. "
                      "계속 실패하면 그 .part 파일만 지우고 다시 실행하세요.")


class ProtectionError(RuntimeError):
    """Only fixed, secret-free application messages are used here."""


def state_directory() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "XlikeSaver" / "request-protection"


def _finite_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def cooldown_until(headers, now: float) -> float:
    """Honor the later valid server deadline; otherwise wait 15 minutes.

Retry-After can be delta seconds or an HTTP date. An advertised future reset
is never shortened by an arbitrary application maximum.
"""
    deadlines = []
    values = {str(key).lower(): value for key, value in headers.items()}
    reset = _finite_number(values.get("x-rate-limit-reset"))
    if reset is not None and reset > now:
        deadlines.append(reset)
    retry = values.get("retry-after")
    if retry is not None:
        seconds = _finite_number(retry)
        if seconds is not None:
            if seconds >= 0:
                deadlines.append(now + seconds)
        else:
            with suppress(ValueError, TypeError, OverflowError):
                parsed = parsedate_to_datetime(str(retry))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                timestamp = parsed.timestamp()
                if math.isfinite(timestamp) and timestamp > now:
                    deadlines.append(timestamp)
    # Small grace prevents retrying on the reset boundary.
    return max(deadlines) + 5 if deadlines else now + DEFAULT_COOLDOWN


def _role(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return "media" if host in {"pbs.twimg.com", "video.twimg.com"} else "api"


class AccountGuard:
    def __init__(self, account: str, *, sleep, root: Path | None = None):
        self.root = root if root is not None else state_directory()
        key = hashlib.sha256(account.lower().encode("ascii")).hexdigest()
        self.state_path = self.root / (key + ".json")
        self.lock_path = self.root / (key + ".lock")
        self.sleep = sleep
        self.failure = ""
        self.until = 0.0
        self._lock_file = None
        self._last_send = {}
        self._last_limit_headers = {}

    @property
    def retry_at(self) -> str:
        if self.until <= time.time():
            return ""
        try:
            return datetime.fromtimestamp(self.until, _KOREA).isoformat()
        except (ValueError, OverflowError, OSError):
            return ""

    def _cooldown_message(self) -> str:
        stamp = self.retry_at
        try:
            label = datetime.fromisoformat(stamp).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            label = "서버가 정한 요청 재개 시각"
        return f"X 요청 한도 때문에 멈췄습니다. {label}(한국 시간) 이후에 다시 실행해 주세요."

    def acquire(self, *, respect_cooldown: bool = True) -> None:
        """An OS file lock releases automatically when the worker exits."""
        self.failure = ""
        self.until = 0.0
        fp = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            fp = self.lock_path.open("a+b")
            if fp.tell() == 0:
                fp.write(b"\0")
                fp.flush()
            fp.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if fp is not None:
                fp.close()
            # Windows uses EACCES for an already-held byte-range lock.
            if fp is not None and exc.errno in {11, 13, 35}:
                raise ProtectionError(_BUSY_HELP) from None
            raise ProtectionError(_STATE_HELP) from None
        self._lock_file = fp
        # Local record maintenance needs the same account lock, but sends no
        # requests and must not change or bypass the download cooldown record.
        if not respect_cooldown:
            return
        try:
            if self.state_path.exists():
                value = json.loads(self.state_path.read_text(encoding="utf-8"))
                until = _finite_number(value.get("until")) if isinstance(value, dict) else None
                if until is None or until < 0:
                    raise ValueError("invalid cooldown")
                self.until = until
            self.ensure_allowed()
        except ProtectionError:
            self.close()
            raise
        except (OSError, ValueError, TypeError):
            self.close()
            raise ProtectionError(_STATE_HELP) from None

    def close(self) -> None:
        if self._lock_file is not None:
            # Closing the handle releases Windows/POSIX locks, including crashes.
            self._lock_file.close()
            self._lock_file = None

    def ensure_allowed(self) -> None:
        if self.failure:
            raise ProtectionError(self.failure)
        if self.until > time.time():
            self.failure = self._cooldown_message()
            raise ProtectionError(self.failure)

    def mark_media_error(self) -> None:
        """Remember a streamed-body failure before gallery-dl considers fallback."""
        self.failure = _MEDIA_ERROR_HELP

    def protect_media_body(self, response) -> None:
        # HTTP headers have arrived, but stream=True defers network reads. This
        # also covers signature reads performed before the downloader.receive
        # hook, not only chunks consumed while writing the photo body.
        original_content = response.iter_content

        def protected_content(*args, **kwargs):
            try:
                yield from original_content(*args, **kwargs)
            except RequestException:
                self.mark_media_error()
                raise

        response.iter_content = protected_content

    def record_limit(self, headers=None, *, fail: bool = True) -> None:
        if headers is None:
            headers = self._last_limit_headers
        self.until = max(self.until, cooldown_until(headers, time.time()))
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.root,
                                             prefix="cooldown-", suffix=".tmp", delete=False) as fp:
                temporary = Path(fp.name)
                json.dump({"until": self.until}, fp)
                fp.flush()
                os.fsync(fp.fileno())
            temporary.replace(self.state_path)
        except OSError:
            self.failure = _STATE_HELP
            raise ProtectionError(self.failure) from None
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
        if fail:
            self.failure = self._cooldown_message()
            raise ProtectionError(self.failure)

    def inspect_response(self, response, role: str) -> None:
        status = response.status_code
        values = {str(key).lower(): value for key, value in response.headers.items()}
        if role == "api":
            # gallery-dl may preemptively abort while a small positive quota is
            # left. Preserve that response's reset hint for the abort fallback.
            self._last_limit_headers = {key: values[key] for key in (
                "x-rate-limit-reset", "retry-after") if key in values}
        if status == 429:
            response.close()
            self.record_limit(response.headers)
        if role != "api":
            if status == 416:
                # A rejected Range request is not proof that the existing
                # partial file is complete, even when a total size is supplied.
                response.close()
                self.failure = _RESUME_ERROR_HELP
                raise ProtectionError(self.failure)
            if status >= 400 and status != 404:
                response.close()
                self.failure = _MEDIA_ERROR_HELP
                raise ProtectionError(self.failure)
            return
        if status in {401, 403}:
            response.close()
            self.failure = _AUTH_HELP
            raise ProtectionError(self.failure)
        # Some GraphQL limits and account challenges have HTTP 200 envelopes.
        if "json" in str(values.get("content-type", "")).lower():
            try:
                payload = response.json()
            except (ValueError, TypeError):
                payload = None
            errors = payload.get("errors", ()) if isinstance(payload, dict) else ()
            if isinstance(errors, list):
                for error in errors:
                    if not isinstance(error, dict):
                        continue
                    text = str(error.get("message", "")).lower()
                    if error.get("code") in {88, 420} or any(part in text for part in (
                            "rate limit", "too many requests")):
                        response.close()
                        self.record_limit(response.headers)
                    if error.get("code") in {32, 89, 135, 215} or "could not authenticate" in text:
                        response.close()
                        self.failure = _AUTH_HELP
                        raise ProtectionError(self.failure)
                    if error.get("code") == 326 or any(part in text for part in (
                            "account is temporarily locked", "account temporarily locked")):
                        response.close()
                        self.failure = _LOCKED_HELP
                        raise ProtectionError(self.failure)
                    if error.get("code") in {130, 131} or text.startswith("timeout") or error.get("source") == "Server":
                        response.close()
                        self.failure = _API_ERROR_HELP
                        raise ProtectionError(self.failure)
        # Return the successful response, but reserve a small quota margin and
        # stop further network requests before the API allowance is exhausted.
        remaining = _finite_number(values.get("x-rate-limit-remaining"))
        if status < 400 and remaining is not None and remaining <= 5:
            self.record_limit(response.headers, fail=False)

    def protect_session(self, session) -> None:
        original_send = session.send

        def protected_send(request, **kwargs):
            self.ensure_allowed()
            role = _role(request.url)
            interval = MEDIA_INTERVAL if role == "media" else API_INTERVAL
            previous = self._last_send.get(role)
            if previous is not None:
                wait = interval - (time.monotonic() - previous)
                if wait > 0:
                    self.sleep(wait, "request-protection")
            self.ensure_allowed()
            self._last_send[role] = time.monotonic()
            try:
                response = original_send(request, **kwargs)
            except RequestException:
                # A media network failure must not quietly trigger alternative
                # size requests through gallery-dl's fallback loop.
                self.failure = _MEDIA_ERROR_HELP if role == "media" else _API_ERROR_HELP
                raise ProtectionError(self.failure) from None
            self.inspect_response(response, role)
            if role == "media":
                self.protect_media_body(response)
            return response

        session.send = protected_send
