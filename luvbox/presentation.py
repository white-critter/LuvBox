"""Small display formatters shared by windows without loading their UI."""

from datetime import datetime


MEDIA_LABELS = {"all": "전체", "photo": "사진", "video": "동영상", "animated_gif": "움직이는 GIF"}


def format_bytes(value: int | float | None) -> str:
    if value is None:
        return "알 수 없음"
    size = max(0, float(value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:,.1f} {unit}" if unit != "B" else f"{size:,.0f} B"
        size /= 1024
    return ""


def format_date(value: str | None) -> str:
    if not value:
        return "기록 없음"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return "기록 없음"


def format_short(value: str | None) -> str:
    if not value:
        return "–"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return "–"
