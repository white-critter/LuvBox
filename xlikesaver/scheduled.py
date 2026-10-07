"""Unattended save with the app's saved settings, for Windows Task Scheduler.

`XlikeSaver-<version>.exe --run-saved` checks only new likes (quick scan) and falls back
to a complete scan by itself when the previous run did not finish. It uses the
encrypted token connection only; a cookies.txt path is never stored. Output
goes to a log file because a windowed executable has no console.

The app registers the daily task itself (Windows' ScheduledTasks module), so
it can also run on battery and catch up after the PC was off at that time.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .settings import app_directory, load_settings


TASK_NAME = "XlikeSaver 자동 저장"
_KEEP_LOGS = 30
_KOREA = timezone(timedelta(hours=9))
_TIME = re.compile(r"([01]?[0-9]|2[0-3]):([0-5][0-9])\Z")
_SCHEDULE_HELP = ("Windows 작업 스케줄러에 연결하지 못했습니다. 잠시 후 다시 시도하거나, "
                  "회사·학교 PC라면 관리자 정책을 확인해 주세요.")


class ScheduleError(RuntimeError):
    """A fixed message about Task Scheduler; PowerShell output is never shown."""


def log_directory() -> Path:
    return app_directory() / "logs"


def last_run_path() -> Path:
    return app_directory() / "last-scheduled.json"


def launch_parts() -> tuple[str, str]:
    """The program and arguments Task Scheduler runs for this installation."""
    if getattr(sys, "frozen", False):
        return sys.executable, "--run-saved"
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    entry = Path(__file__).resolve().parents[1] / "entry.py"
    return str(windowless if windowless.is_file() else executable), f'"{entry}" --run-saved'


def app_command() -> list[str]:
    """Open the normal app window."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    program, _ = launch_parts()
    return [program, str(Path(__file__).resolve().parents[1] / "entry.py")]


def normal_time(value: str) -> str:
    match = _TIME.fullmatch(str(value).strip())
    if not match:
        raise ValueError("실행 시각은 09:00처럼 24시간 형식으로 입력해 주세요.")
    return f"{int(match[1]):02d}:{match[2]}"


def _powershell(script: str, **values: str) -> str:
    """Run a fixed script; user values only travel as environment variables."""
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    environment = os.environ.copy()
    environment.update({"XLS_" + key.upper(): value for key, value in values.items()})
    script = ("$ErrorActionPreference = 'Stop'\n"
              "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n" + script)
    try:
        result = subprocess.run(
            [str(executable), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
            env=environment, stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        raise ScheduleError(_SCHEDULE_HELP) from None
    if result.returncode:
        raise ScheduleError(_SCHEDULE_HELP)
    return result.stdout.decode("utf-8", errors="replace").strip()


_STATUS_SCRIPT = r"""
$task = Get-ScheduledTask -TaskName $env:XLS_TASK -ErrorAction SilentlyContinue
if (-not $task) { '{"registered": false}'; exit 0 }
$info = Get-ScheduledTaskInfo -TaskName $env:XLS_TASK
$action = $task.Actions | Select-Object -First 1
$trigger = $task.Triggers | Select-Object -First 1
function Stamp($value) { if ($value -and $value.Year -gt 2000) { $value.ToString('s') } else { '' } }
[pscustomobject]@{
    registered = $true
    program = [string]$action.Execute
    arguments = [string]$action.Arguments
    start = [string]$trigger.StartBoundary
    enabled = [bool]$trigger.Enabled -and ($task.State -ne 'Disabled')
    catch_up = [bool]$task.Settings.StartWhenAvailable
    on_battery = -not [bool]$task.Settings.DisallowStartIfOnBatteries
    last_run = Stamp $info.LastRunTime
    next_run = Stamp $info.NextRunTime
} | ConvertTo-Json -Compress
"""

_REGISTER_SCRIPT = r"""
$action = New-ScheduledTaskAction -Execute $env:XLS_PROGRAM -Argument $env:XLS_ARGUMENTS
# Start tomorrow if today's time has passed: with StartWhenAvailable, a start
# in the past would otherwise count as missed and run at once.
$at = [datetime]::ParseExact($env:XLS_TIME, 'HH:mm', $null)
if ($at -le (Get-Date)) { $at = $at.AddDays(1) }
$trigger = New-ScheduledTaskTrigger -Daily -At $at
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 12)
Register-ScheduledTask -TaskName $env:XLS_TASK -Action $action -Trigger $trigger -Settings $settings `
    -Description $env:XLS_DESCRIPTION -Force | Out-Null
"""

_UNREGISTER_SCRIPT = r"""
if (Get-ScheduledTask -TaskName $env:XLS_TASK -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $env:XLS_TASK -Confirm:$false
}
"""


def task_status() -> dict:
    """What is registered: program, daily time, and whether it is this app."""
    try:
        data = json.loads(_powershell(_STATUS_SCRIPT, task=TASK_NAME) or "{}")
    except ValueError:
        raise ScheduleError(_SCHEDULE_HELP) from None
    if not isinstance(data, dict) or not data.get("registered"):
        return {"registered": False}
    program, arguments = launch_parts()
    start = str(data.get("start") or "")
    found = re.search(r"T(\d\d):(\d\d)", start)
    registered = str(data.get("program") or "")
    return {"registered": True, "program": registered,
            "current": os.path.normcase(registered) == os.path.normcase(program) and
                       str(data.get("arguments") or "") == arguments,
            "time": f"{found[1]}:{found[2]}" if found else "",
            "enabled": bool(data.get("enabled")), "catch_up": bool(data.get("catch_up")),
            "on_battery": bool(data.get("on_battery")),
            "last_run": str(data.get("last_run") or ""), "next_run": str(data.get("next_run") or "")}


def register_task(time_of_day: str) -> None:
    """Register or replace the daily task for this executable."""
    program, arguments = launch_parts()
    _powershell(_REGISTER_SCRIPT, task=TASK_NAME, program=program, arguments=arguments,
                time=normal_time(time_of_day), description="XlikeSaver: 새 Like를 매일 저장합니다.")


def unregister_task() -> None:
    _powershell(_UNREGISTER_SCRIPT, task=TASK_NAME)


def last_run() -> dict | None:
    """The last scheduled run's outcome as recorded by run_saved."""
    try:
        data = json.loads(last_run_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def mark_last_run_seen() -> None:
    data = last_run()
    if data is not None and not data.get("seen"):
        _write_last_run(dict(data, seen=True))


def _write_last_run(data: dict) -> None:
    path = last_run_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="last-scheduled-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


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
        gif_format=settings.get("gif_format", "gif"),
        gif_size=settings.get("gif_size", "original"),
    ).validate()


def _prune_logs(directory: Path) -> None:
    logs = sorted(directory.glob("scheduled-*.log"))
    for old in logs[:-_KEEP_LOGS]:
        try:
            old.unlink()
        except OSError:
            pass


def _busy(message: str) -> bool:
    """Another save of the same account or folder was already running."""
    from .protection import _BUSY_HELP
    from .storage import OUTPUT_BUSY_HELP
    return message in {_BUSY_HELP, OUTPUT_BUSY_HELP}


def _notify_failure(outcome: dict) -> None:
    """Tell the signed-in user right away; a scheduled run has no window."""
    try:
        import tkinter as tk
        from . import dialogs, theme
        root = tk.Tk()
        root.withdraw()
        from .app_icon import install
        install(root)
        try:
            theme.apply(root, "dark" if load_settings().get("appearance") == "dark" else "light")
        except (OSError, ValueError, tk.TclError):
            pass
        dialog = dialogs.Dialog(
            root, "XlikeSaver 자동 저장 실패",
            f"{outcome['finished_at'][:16].replace('T', ' ')} 자동 저장을 마치지 못했습니다.\n\n{outcome['message']}\n\n"
            "앱을 열어 안내를 확인해 주세요. 자세한 내용은 ‘자동 저장 → 실행 기록’에 있습니다.",
            icon="warning", buttons=(("앱 열기", "open"), ("닫기", None)))
        dialog.attributes("-topmost", True)
        choice = dialog.run()
        root.destroy()
        if choice == "open":
            subprocess.Popen(app_command(), close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    except Exception:
        pass


def run_saved(*, full: bool = False) -> int:
    """Run once and return the worker's exit code; 1 when nothing could start."""
    directory = log_directory()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(_KOREA).strftime("%Y%m%d-%H%M%S")
    path = directory / f"scheduled-{stamp}.log"
    outcome = {"status": "error", "saved": 0, "message": ""}
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
                outcome.update(status=str(event.get("status") or "error"), saved=int(event.get("saved") or 0),
                               message=str(event.get("message", "")))
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
            outcome["message"] = str(exc)
            code = 1
        except OSError:
            outcome["message"] = "저장된 설정의 폴더 경로를 확인하지 못했습니다. 앱에서 저장 폴더를 확인해 주세요."
            write(outcome["message"])
            code = 1
        else:
            from .core import run_worker

            code = run_worker(options, emit)
    _prune_logs(directory)
    busy = _busy(outcome["message"])
    failed = (code != 0 or outcome["status"] == "error") and not busy
    outcome.update(finished_at=datetime.now(_KOREA).isoformat(timespec="seconds"), code=code,
                   failed=failed, busy=busy, log=str(path), seen=False)
    try:
        _write_last_run(outcome)
    except OSError:
        pass
    if failed:
        _notify_failure(outcome)
    return code
