$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw '먼저 .\setup.ps1을 실행하세요.' }
& $taskPython 'tools\exiftool\download.py'
if ($LASTEXITCODE -ne 0) { throw 'ExifTool 준비에 실패했습니다.' }

# Collect only application dependencies and our pinned metadata helper. Never
# collect the workspace, saved settings, account records, or authentication.
& $taskPython -m PyInstaller --noconfirm --clean --windowed --onefile --name XlikeSaver `
    --distpath release --workpath build\onefile `
    --collect-all gallery_dl --copy-metadata requests --copy-metadata urllib3 `
    --copy-metadata certifi --copy-metadata charset-normalizer --copy-metadata idna `
    --exclude-module playwright --exclude-module greenlet --exclude-module selenium `
    --add-data 'tools\exiftool\runtime;tools/exiftool/runtime' `
    --add-data 'README.md;.' --add-data 'LICENSE;.' --add-data 'THIRD_PARTY_NOTICES.md;.' entry.py
if ($LASTEXITCODE -ne 0) { throw '실행 파일 빌드에 실패했습니다.' }
& $taskPython 'tools\package_release.py'
if ($LASTEXITCODE -ne 0) { throw '소스와 라이선스가 포함된 배포 ZIP 준비에 실패했습니다.' }
Write-Host '빌드 완료: release\XlikeSaver.exe (실행 파일 하나로 실행 가능)'
Write-Host '배포 ZIP: release\XlikeSaver-<버전>.zip (사용 안내, 소스, 라이선스 포함)'
