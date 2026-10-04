"""Unattended save with the app's saved settings, for Windows Task Scheduler.

`XlikeSaver-<version>.exe --run-saved` checks only new likes (quick scan) and falls back
to a complete scan by itself when the previous run did not finish. It uses the
encrypted token connection only; a cookies.txt path is never stored. Output
goes to a log file because a windowed executable has no console.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .settings import app_directory, load_settings


TASK_NAME = "XlikeSaver 자동 저장"
_KEEP_LOGS = 30
_KOREA = timezone(timedelta(hours=9))


def log_directory() -> Path:
    return app_directory() / "logs"


def launch_command() -> str:
    """The command line Task Scheduler runs, quoted for paths with spaces."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --run-saved'
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    entry = Path(__file__).resolve().parents[1] / "entry.py"
    return f'"{windowless if windowless.is_file() else executable}" "{entry}" --run-saved'


def schtasks_command(time_of_day: str = "09:00") -> str:
    """A once-a-day registration command the user can review and run."""
    action = launch_command().replace('"', '\\"')
    return f'schtasks /Create /TN "{TASK_NAME}" /SC DAILY /ST {time_of_day} /TR "{action}" /F'


def _options_from_settings(settings: dict, full: bool):
    from .core import JobOptions, _normal_account

    if settings.get("auth_mode", "managed") != "managed":
        raise ValueError("예약 실행은 ‘인증 토큰 연결’ 방식에서만 사용할 수 있습니다. 앱에서 인증 토큰을 연결해 주세요.")
    account, output = settings.get("account"), settings.get("output_dir")
    if not isinstance(account, str) or not account.strip() or not isinstance(output, str) or not output.strip():
        raise ValueError("저장된 설정이 없습니다. 앱에서 한 번 저장을 실행한 뒤 예약 실행을 사용해 주세요.")
    locations = settings.get("record_locations")
    if isinstance(locations, dict):
        recorded = locations.get(_normal_account(account))
        if (isinstance(recorded, str) and recorded.strip() and
                Path(recorded).expanduser().resolve() != Path(output).expanduser().resolve()):
            # The window moves an account's records when its folder changes.
            # Never start in a folder whose records have not been moved yet.
            raise ValueError("저장 폴더 변경이 끝나지 않았습니다. 앱에서 저장 폴더를 확인한 뒤 다시 시도해 주세요.")
    size = settings.get("max_video_mb", 200)
    folder_mode = settings.get("folder_mode", "flat")
    return JobOptions(
        account=account, output_dir=output, auth_mode="managed",
        folder_mode=folder_mode if isinstance(folder_mode, str) else "",
        scan_mode="full" if full else "quick",
        include_videos=settings.get("include_videos", True) is not False,
        include_gifs=settings.get("include_gifs", True) is not False,
        max_video_mb=size if isinstance(size, int) and not isinstance(size, bool) else -1,
        separate_accounts=settings.get("separate_accounts", False) is True,
    ).validate()


def _prune_logs(directory: Path) -> None:
    logs = sorted(directory.glob("scheduled-*.log"))
    for old in logs[:-_KEEP_LOGS]:
        try:
            old.unlink()
        except OSError:
            pass


def run_saved(*, full: bool = False) -> int:
    """Run once and return the worker's exit code; 1 when nothing could start."""
    directory = log_directory()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(_KOREA).strftime("%Y%m%d-%H%M%S")
    path = directory / f"scheduled-{stamp}.log"
    with path.open("a", encoding="utf-8") as log:
        def write(text: str) -> None:
            log.write(datetime.now(_KOREA).strftime("%H:%M:%S") + "  " + text + "\n")
            log.flush()

        def emit(event: dict) -> None:
            # Events are already secret-free; progress and per-file skips are
            # omitted so a large library does not produce a huge log.
            kind = event.get("type")
            if kind == "log":
                write(str(event.get("message", "")))
            elif kind == "saved":
                write("저장 · " + str(event.get("path", "")))
            elif kind == "size-skipped":
                write("용량 초과로 건너뜀 · " + str(event.get("filename", "")))
            elif kind == "finished":
                write(f"종료 · {event.get('status')} · 저장 {event.get('saved', 0)}개 · 기존 {event.get('skipped', 0)}개 · "
                      f"용량 초과 {event.get('size_skipped', 0)}개")
                write(str(event.get("message", "")))
                if event.get("retry_at"):
                    write("요청을 다시 시도할 수 있는 시각 (한국 시간) · " + str(event["retry_at"]))

        write("예약 실행 시작 · " + ("전체 목록 다시 확인" if full else "새 Like만 빠르게 확인"))
        try:
            options = _options_from_settings(load_settings(), full)
        except ValueError as exc:
            # Only this app's fixed validation messages reach this handler.
            write(str(exc))
            code = 1
        except OSError:
            write("저장된 설정의 폴더 경로를 확인하지 못했습니다. 앱에서 저장 폴더를 확인해 주세요.")
            code = 1
        else:
            from .core import run_worker

            code = run_worker(options, emit)
    _prune_logs(directory)
    return code
