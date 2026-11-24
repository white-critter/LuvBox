"""Fixed user-facing error types and safe UI error formatting."""

from __future__ import annotations


class UserFacingError(Exception):
    """Mixin for exceptions carrying only fixed, secret-free user messages."""


class UserValueError(UserFacingError, ValueError):
    """A ValueError carrying a fixed user-facing message."""


class UserRuntimeError(UserFacingError, RuntimeError):
    """A RuntimeError carrying a fixed user-facing message."""


def safe_ui_error(
    exc: BaseException,
    fallback: str,
    *,
    allowed: tuple[type[BaseException], ...] = (),
) -> str:
    """Return ``str(exc)`` only when ``exc`` is an explicit user-facing error."""
    if isinstance(exc, UserFacingError) or (allowed and isinstance(exc, allowed)):
        text = str(exc).strip()
        if text:
            return text
    return fallback
