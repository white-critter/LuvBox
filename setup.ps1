param([string]$PythonPath = "")
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    if ($PythonPath) {
        $taskPython = (Resolve-Path -LiteralPath $PythonPath).Path
    } else {
        $taskPythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $taskPythonCommand) {
            throw 'Python 3.12를 설치하거나 -PythonPath로 python.exe 경로를 지정하세요.'
        }
        $taskPython = $taskPythonCommand.Source
    }
    & $taskPython -c "import sys, tkinter; assert sys.version_info >= (3, 10), 'Python 3.10+ required'"
    if ($LASTEXITCODE -ne 0) { throw 'Tkinter가 포함된 Python 3.10 이상이 필요합니다. Python 3.12를 권장합니다.' }
    & $taskPython -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '가상환경 생성에 실패했습니다.' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw '패키지 설치에 실패했습니다.' }
& '.\.venv\Scripts\python.exe' 'tools\exiftool\download.py'
if ($LASTEXITCODE -ne 0) { throw '파일 출처 기록에 필요한 ExifTool 준비에 실패했습니다.' }
Write-Host '준비 완료: run.bat를 더블클릭하거나 .\.venv\Scripts\python.exe entry.py를 실행하세요.'
