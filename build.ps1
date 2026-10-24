$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw '먼저 .\setup.ps1을 실행하세요.' }
& $taskPython 'tools\check_build_inputs.py' '--assets-only'
if ($LASTEXITCODE -ne 0) { throw '빌드 입력이 준비되지 않았습니다. 위의 Git LFS·체크섬 안내를 확인하세요.' }
& $taskPython 'tools\exiftool\download.py'
if ($LASTEXITCODE -ne 0) { throw 'ExifTool 준비에 실패했습니다.' }
& $taskPython 'tools\ffmpeg\build.py' '--check'
if ($LASTEXITCODE -ne 0) { throw '번들 GIF 변환 도구 확인에 실패했습니다. tools\ffmpeg\README.md를 확인하세요.' }
& $taskPython 'tools\qt\prepare_sources.py'
if ($LASTEXITCODE -ne 0) { throw 'Qt 배포 소스와 원본 라이선스 준비에 실패했습니다. tools\qt\README.md를 확인하세요.' }
& $taskPython 'tools\build_theme.py'
if ($LASTEXITCODE -ne 0) { throw '테마 이미지 준비에 실패했습니다.' }
& $taskPython 'tools\check_build_inputs.py'
if ($LASTEXITCODE -ne 0) { throw '필수 빌드 파일 확인에 실패했습니다. setup.ps1의 준비 과정을 완료하세요.' }

$version = (& $taskPython -c "from luvbox import __version__; print(__version__)" | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') { throw '버전은 숫자로 된 주.부.수정 형식이어야 합니다. luvbox\__init__.py를 확인하세요.' }
& $taskPython 'tools\windows_version.py'
if ($LASTEXITCODE -ne 0) { throw 'Windows 파일 버전 정보를 준비하지 못했습니다.' }
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
# A folder build keeps Qt DLLs replaceable; a one-file exe would extract its
# runtime again on every launch, including each save's worker.
$buildOriginalPath = $env:PATH
try {
    $env:PATH = "$(Join-Path $env:SystemRoot 'System32');$buildOriginalPath"
    & $taskPython -m PyInstaller --noconfirm --clean --windowed --onedir --name $name `
    --version-file 'build\windows-version.txt' `
    --icon 'assets\icons\luvbox.ico' --add-data 'assets\icons;assets/icons' `
    --distpath release --workpath build\onedir `
    --additional-hooks-dir 'tools\pyinstaller' `
    --collect-all gallery_dl --collect-binaries PIL --collect-data PIL --exclude-module imageio_ffmpeg `
    --hidden-import PySide6.QtCore --hidden-import PySide6.QtGui --hidden-import PySide6.QtQml `
    --hidden-import PySide6.QtQuick --hidden-import PySide6.QtQuickControls2 `
    --hidden-import PySide6.QtMultimedia --hidden-import PySide6.QtNetwork `
    --copy-metadata PySide6 --copy-metadata PySide6_Essentials --copy-metadata PySide6_Addons `
    --copy-metadata shiboken6 --add-data 'assets\qt;assets/qt' `
    --copy-metadata pillow --copy-metadata requests --copy-metadata urllib3 `
    --copy-metadata certifi --copy-metadata charset-normalizer --copy-metadata idna `
    --add-data 'tools\exiftool\runtime;tools/exiftool/runtime' `
    --add-data 'tools\ffmpeg\runtime;tools/ffmpeg/runtime' `
    --add-data 'assets\fonts\Pretendard;assets/fonts/Pretendard' `
    --add-data 'assets\theme\light.zip;assets/theme' `
    --add-data 'assets\theme\dark.zip;assets/theme' `
    --add-data 'assets\browser-extension\README.md;assets/browser-extension' `
    --add-data 'assets\browser-extension\PRIVACY.md;assets/browser-extension' `
    --add-data 'assets\browser-extension\chromium\manifest.json;assets/browser-extension/chromium' `
    --add-data 'assets\browser-extension\chromium\background.js;assets/browser-extension/chromium' `
    --add-data 'assets\browser-extension\chromium\profile.js;assets/browser-extension/chromium' `
    --add-data 'assets\browser-extension\chromium\connect.js;assets/browser-extension/chromium' `
    --add-data 'assets\browser-extension\chromium\connect.html;assets/browser-extension/chromium' `
    --add-data 'assets\browser-extension\chromium\connect.css;assets/browser-extension/chromium' `
    --add-data 'assets\browser-extension\chromium\extension-page.js;assets/browser-extension/chromium' `
    --add-data 'assets\browser-extension\firefox\manifest.json;assets/browser-extension/firefox' `
    --add-data 'assets\browser-extension\firefox\background.js;assets/browser-extension/firefox' `
    --add-data 'assets\browser-extension\firefox\profile.js;assets/browser-extension/firefox' `
    --add-data 'assets\browser-extension\firefox\connect.js;assets/browser-extension/firefox' `
    --add-data 'assets\browser-extension\firefox\connect.html;assets/browser-extension/firefox' `
    --add-data 'assets\browser-extension\firefox\connect.css;assets/browser-extension/firefox' `
    --add-data 'assets\browser-extension\firefox\extension-page.js;assets/browser-extension/firefox' `
    --add-data 'docs\browser-login.md;docs' `
    --add-data 'README.md;.' --add-data 'CHANGELOG.md;.' `
    --add-data 'LICENSE;.' --add-data 'THIRD_PARTY_NOTICES.md;.' entry.py
    if ($LASTEXITCODE -ne 0) { throw '실행 파일 빌드에 실패했습니다.' }
}
finally {
    $env:PATH = $buildOriginalPath
}
& $taskPython 'tools\check_qt_startup.py' $target (Join-Path $PSScriptRoot "build\qt-startup-$version.json")
if ($LASTEXITCODE -ne 0) { throw '실제 Qt EXE의 시작 점검에 실패했습니다. ZIP을 만들지 않습니다.' }
& $taskPython 'tools\package_browser_extension.py'
if ($LASTEXITCODE -ne 0) { throw '브라우저 확장 제출 자료 준비에 실패했습니다.' }
& $taskPython 'tools\package_release.py'
if ($LASTEXITCODE -ne 0) { throw '소스와 라이선스가 포함된 배포 ZIP 준비에 실패했습니다.' }
Write-Host "빌드 완료: release\$name\$name.exe (같은 폴더의 _internal과 함께 실행)"
Write-Host "배포 ZIP: release\$name.zip (사용 안내, 소스, 라이선스 포함)"
Write-Host "체크섬: release\SHA256-$version.txt"
