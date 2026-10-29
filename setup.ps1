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
    & $taskPython 'tools\check_build_inputs.py' '--python-only'
    if ($LASTEXITCODE -ne 0) { throw '보안 업데이트된 Python이 필요합니다. 위의 버전 안내를 확인해 주세요.' }
    & $taskPython -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '가상환경 생성에 실패했습니다.' }
}
& '.\.venv\Scripts\python.exe' 'tools\check_build_inputs.py' '--python-only'
if ($LASTEXITCODE -ne 0) { throw '기존 가상환경의 Python 보안 업데이트가 필요합니다. 위의 버전으로 새 가상환경을 준비해 주세요.' }
& '.\.venv\Scripts\python.exe' -m pip install -r requirements-dev.txt -c requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw '패키지 설치에 실패했습니다.' }
& '.\.venv\Scripts\python.exe' 'tools\exiftool\download.py'
if ($LASTEXITCODE -ne 0) { throw '파일 출처 기록에 필요한 ExifTool 준비에 실패했습니다.' }
& '.\.venv\Scripts\python.exe' 'tools\ffmpeg\build.py' '--check'
if ($LASTEXITCODE -ne 0) { throw 'GIF 변환 도구 확인에 실패했습니다. tools\ffmpeg\README.md를 확인하세요.' }
Write-Host '준비 완료: run.bat를 더블클릭하거나 .\.venv\Scripts\python.exe entry.py를 실행하세요.'
