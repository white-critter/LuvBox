$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw '먼저 .\setup.ps1을 실행하세요.' }
& $taskPython 'tools\exiftool\download.py'
if ($LASTEXITCODE -ne 0) { throw 'ExifTool 준비에 실패했습니다.' }
& $taskPython 'tools\ffmpeg\build.py' '--check'
if ($LASTEXITCODE -ne 0) { throw '번들 GIF 변환 도구 확인에 실패했습니다. tools\ffmpeg\README.md를 확인하세요.' }

$version = (& $taskPython -c "from luvbox import __version__; print(__version__)" | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw '버전을 읽지 못했습니다. luvbox\__init__.py를 확인하세요.' }
$name = "LuvBox-$version"
# A versioned name lets an older running build stay open. The same version
# cannot be replaced while it runs, so stop with a clear message instead.
$target = Join-Path $PSScriptRoot "release\$name\$name.exe"
if (Test-Path -LiteralPath $target) {
    try { [System.IO.File]::Open($target, 'Open', 'ReadWrite', 'None').Close() }
    catch { throw "$name.exe가 실행 중이라 덮어쓸 수 없습니다. 프로그램을 닫고 다시 빌드하세요." }
}

# Collect only application dependencies and our pinned metadata helper. Never
# collect the workspace, saved settings, account records, or authentication.
# A folder build starts in well under a second; a one-file exe unpacked about
# 40MB to a temporary folder on every launch, including each save's worker.
& $taskPython -m PyInstaller --noconfirm --clean --windowed --onedir --name $name `
    --icon 'assets\icons\luvbox.ico' --add-data 'assets\icons;assets/icons' `
    --distpath release --workpath build\onedir `
    --collect-all gallery_dl --collect-binaries PIL --collect-data PIL --exclude-module imageio_ffmpeg `
    --copy-metadata pillow --copy-metadata requests --copy-metadata urllib3 `
    --copy-metadata certifi --copy-metadata charset-normalizer --copy-metadata idna `
    --add-data 'tools\exiftool\runtime;tools/exiftool/runtime' `
    --add-data 'tools\ffmpeg\runtime;tools/ffmpeg/runtime' `
    --add-data 'assets\fonts\Pretendard;assets/fonts/Pretendard' `
    --add-data 'README.md;.' --add-data 'LICENSE;.' --add-data 'THIRD_PARTY_NOTICES.md;.' entry.py
if ($LASTEXITCODE -ne 0) { throw '실행 파일 빌드에 실패했습니다.' }
& $taskPython 'tools\package_release.py'
if ($LASTEXITCODE -ne 0) { throw '소스와 라이선스가 포함된 배포 ZIP 준비에 실패했습니다.' }
Write-Host "빌드 완료: release\$name\$name.exe (같은 폴더의 _internal과 함께 실행)"
Write-Host "배포 ZIP: release\$name.zip (사용 안내, 소스, 라이선스 포함)"
Write-Host "체크섬: release\SHA256-$version.txt"
