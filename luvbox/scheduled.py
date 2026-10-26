"""Unattended save with the app's saved settings, for Windows Task Scheduler.

`LuvBox-<version>.exe --run-saved` checks only new likes (quick scan) and falls back
to a complete scan by itself when the previous run did not finish. It uses the
encrypted token connection only; a cookies.txt path is never stored. Output
goes to a log file because a windowed executable has no console.

The app registers the daily task itself (Windows' ScheduledTasks module), so
it can also run on battery and catch up after the PC was off at that time.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .file_mutex import replace_file
from .settings import AppDataError, app_directory, load_settings


TASK_NAME = "LuvBox 자동 저장"
# Builds before the LuvBox name registered the same task under this name.
_LEGACY_TASKS = ("XlikeSaver 자동 저장",)
_KEEP_LOGS = 30
_KOREA = timezone(timedelta(hours=9))
_TIME = re.compile(r"([01]?[0-9]|2[0-3]):([0-5][0-9])\Z")
_SCHEDULE_HELP = "작업 스케줄러에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요(회사·학교 PC는 관리자 정책 확인)."


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
$name = $env:XLS_TASK
$task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
if (-not $task) {
    foreach ($candidate in ($env:XLS_LEGACY -split '\|')) {
        if ($candidate) { $task = Get-ScheduledTask -TaskName $candidate -ErrorAction SilentlyContinue }
        if ($task) { $name = $candidate; break }
    }
}
if (-not $task) { '{"registered": false}'; exit 0 }
$info = Get-ScheduledTaskInfo -TaskName $name
$action = $task.Actions | Select-Object -First 1
$trigger = $task.Triggers | Select-Object -First 1
function Stamp($value) { if ($value -and $value.Year -gt 2000) { $value.ToString('s') } else { '' } }
[pscustomobject]@{
    registered = $true
    name = $name
    program = [string]$action.Execute
    arguments = [string]$action.Arguments
    start = [string]$trigger.StartBoundary
    enabled = [bool]$trigger.Enabled -and ($task.State -ne 'Disabled')
    running = $task.State -eq 'Running'
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
# One daily task: remove the one registered under the app's former name.
foreach ($old in ($env:XLS_LEGACY -split '\|')) {
    if ($old -and (Get-ScheduledTask -TaskName $old -ErrorAction SilentlyContinue)) {
        Unregister-ScheduledTask -TaskName $old -Confirm:$false
    }
}
"""

_UNREGISTER_SCRIPT = r"""
foreach ($name in @($env:XLS_TASK) + ($env:XLS_LEGACY -split '\|')) {
    if ($name -and (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
}
"""


def task_status() -> dict:
    """What is registered: program, daily time, and whether it is this app."""
    try:
        data = json.loads(_powershell(_STATUS_SCRIPT, task=TASK_NAME, legacy="|".join(_LEGACY_TASKS)) or "{}")
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
                       str(data.get("arguments") or "") == arguments and
                       str(data.get("name") or TASK_NAME) == TASK_NAME,
            "time": f"{found[1]}:{found[2]}" if found else "",
            "enabled": bool(data.get("enabled")), "running": bool(data.get("running")),
            "catch_up": bool(data.get("catch_up")),
            "on_battery": bool(data.get("on_battery")),
            "last_run": str(data.get("last_run") or ""), "next_run": str(data.get("next_run") or "")}


def register_task(time_of_day: str) -> None:
    """Register or replace the daily task for this executable."""
    program, arguments = launch_parts()
    _powershell(_REGISTER_SCRIPT, task=TASK_NAME, program=program, arguments=arguments,
                time=normal_time(time_of_day), description="러브박스(LuvBox): 새 Like를 매일 저장합니다.",
                legacy="|".join(_LEGACY_TASKS))


def unregister_task() -> None:
    _powershell(_UNREGISTER_SCRIPT, task=TASK_NAME, legacy="|".join(_LEGACY_TASKS))


def last_run() -> dict | None:
    """The last scheduled run's outcome as recorded by run_saved."""
    try:
        data = json.loads(last_run_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("seen"):
        try:
            acknowledged = json.loads(_last_run_seen_path(data).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            acknowledged = None
        if isinstance(acknowledged, dict) and acknowledged.get("outcome") == _outcome_identity(data):
            data["seen"] = True
    return data


def _last_run_seen_path(outcome: dict) -> Path:
    # Distinct runs get distinct markers: a delayed older window cannot
    # replace the acknowledgment already written by a newer window.
    directory = last_run_path().parent / "last-scheduled-seen"
    return directory / (_outcome_identity(outcome) + ".json")


def _outcome_identity(data: dict) -> str:
    # The legacy seen field describes an acknowledgment, not the run itself.
    outcome = {key: value for key, value in data.items() if key != "seen"}
    encoded = json.dumps(outcome, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def mark_last_run_seen(outcome: dict | None = None) -> None:
    """Acknowledge the displayed run without rewriting a newer run's result."""
    data = last_run() if outcome is None else outcome
    if data is not None and not data.get("seen"):
        _write_last_run_json(_last_run_seen_path(data), {"outcome": _outcome_identity(data)})


def _write_last_run(data: dict) -> None:
    _write_last_run_json(last_run_path(), data)


def _write_last_run_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
        replace_file(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _options_from_settings(settings: dict, full: bool):
    from .core import JobOptions, _normal_account

    if settings.get("auth_mode", "managed") != "managed":
        raise ValueError("예약 실행은 브라우저 로그인 연결에서만 쓸 수 있습니다. 앱의 ‘로그인 연결’에서 연결해 주세요.")
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
    from .storage import missing_media_help, resolve_storage, saved_media_missing
    try:
        paths = resolve_storage(output, account, settings.get("separate_accounts", False) is True)
    except ValueError:
        paths = None   # JobOptions.validate below names the invalid setting
    if paths is not None and saved_media_missing(paths):
        # Never start a second, empty library in place of a moved folder.
        raise ValueError("자동 저장을 건너뛰었습니다. " + missing_media_help(paths))
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
        # This worker is unattended; GUI confirmation preferences never apply.
        confirm_each=False,
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
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication, QMessageBox

        application = QApplication.instance() or QApplication([])
        if not isinstance(application, QApplication):
            return
        dialog = QMessageBox()
        dialog.setWindowTitle("러브박스 자동 저장 실패")
        dialog.setWindowIcon(QIcon(str(Path(__file__).resolve().parents[1] / "assets/icons/luvbox.ico")))
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(f"{outcome['finished_at'][:16].replace('T', ' ')} 자동 저장을 마치지 못했습니다.\n\n"
                       f"{outcome['message']}")
        dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        open_button = dialog.addButton("앱 열기", QMessageBox.ButtonRole.AcceptRole)
        close_button = dialog.addButton("닫기", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(close_button)
        dialog.exec()
        if dialog.clickedButton() is open_button:
            subprocess.Popen(app_command(), close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    except Exception:
        pass


def run_saved(*, full: bool = False) -> int:
    """Run once and return the worker's exit code; 1 when nothing could start."""
    try:
        directory = log_directory()
    except AppDataError as exc:
        # Nothing can be logged without the data folder; say why right away
        # instead of ending in an unhandled error.
        _notify_failure({"finished_at": datetime.now(_KOREA).isoformat(timespec="seconds"),
                         "message": f"{exc}\n\n앱을 열어 데이터 폴더를 정리해 주세요.", "log": ""})
        return 1
    try:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(_KOREA).strftime("%Y%m%d-%H%M%S")
        path = directory / f"scheduled-{stamp}.log"
        stream = path.open("a", encoding="utf-8")
    except OSError:
        _notify_failure({"finished_at": datetime.now(_KOREA).isoformat(timespec="seconds"),
                         "message": "자동 저장 기록을 만들지 못했습니다. 앱 데이터 폴더의 저장 권한과 디스크 여유 공간을 확인해 주세요.",
                         "log": ""})
        return 1
    # Even identical failures within one clock tick need separate acknowledgments.
    outcome = {"run_id": uuid.uuid4().hex, "status": "error", "saved": 0, "message": ""}
    with stream as log:
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
