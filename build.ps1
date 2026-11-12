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
& $taskPython 'tools\check_build_inputs.py'
if ($LASTEXITCODE -ne 0) { throw '필수 빌드 파일 확인에 실패했습니다. setup.ps1의 준비 과정을 완료하세요.' }

$version = (& $taskPython -c "from luvbox import __version__; print(__version__)" | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') { throw '버전은 숫자로 된 주.부.수정 형식이어야 합니다. luvbox\__init__.py를 확인하세요.' }
& $taskPython 'tools\package_release.py' '--check-guides'
if ($LASTEXITCODE -ne 0) { throw 'README·사용 안내·개발 안내·변경 사항의 버전이 제품 버전과 다릅니다. 문서를 먼저 고친 뒤 다시 빌드하세요.' }
& $taskPython 'tools\windows_version.py'
if ($LASTEXITCODE -ne 0) { throw 'Windows 파일 버전 정보를 준비하지 못했습니다.' }
# The application is built as a folder (build\app\LuvBox), then packed with the
# launcher into one program (build\single\LuvBox.exe) and, once that exact file
# passed the startup checks, published as release\LuvBox.exe. None of the three
# can be replaced while it runs, so stop with a clear message instead.
$application = Join-Path $PSScriptRoot 'build\app\LuvBox\LuvBox.exe'
$single = Join-Path $PSScriptRoot 'build\single\LuvBox.exe'
foreach ($target in @($application, $single, (Join-Path $PSScriptRoot 'release\LuvBox.exe'))) {
    if (Test-Path -LiteralPath $target) {
        try { [System.IO.File]::Open($target, 'Open', 'ReadWrite', 'None').Close() }
        catch { throw "LuvBox.exe가 실행 중이라 덮어쓸 수 없습니다. 프로그램을 닫고 다시 빌드하세요. ($target)" }
    }
}

# Collect only application dependencies and our pinned metadata helper. Never
# collect the workspace, saved settings, account records, or authentication.
# PyInstaller still makes a folder: the launcher unpacks it once per version, so
# Qt DLLs stay replaceable on disk and no start pays to extract the runtime
# again (a PyInstaller one-file program would, including each save's worker).
$buildOriginalPath = $env:PATH
& $taskPython 'tools\build_snapshot.py' 'record'
if ($LASTEXITCODE -ne 0) { throw '빌드 입력 기록에 실패했습니다. 소스와 배포 입력을 확인하세요.' }
try {
    $env:PATH = "$(Join-Path $env:SystemRoot 'System32');$buildOriginalPath"
    & $taskPython -m PyInstaller --noconfirm --clean --windowed --onedir --name LuvBox `
    --version-file "$PSScriptRoot\build\windows-version.txt" `
    --icon "$PSScriptRoot\assets\icons\luvbox.ico" --add-data "$PSScriptRoot\assets\icons;assets/icons" `
    --distpath build\app --workpath build\onedir --specpath build\spec `
    --additional-hooks-dir "$PSScriptRoot\tools\pyinstaller" `
    --collect-all gallery_dl --exclude-module imageio_ffmpeg `
    --exclude-module tkinter --exclude-module _tkinter --exclude-module turtle `
    --exclude-module PIL.ImageTk --exclude-module PIL._imagingtk --exclude-module PIL._tkinter_finder `
    --exclude-module PIL._avif `
    --hidden-import PySide6.QtCore --hidden-import PySide6.QtGui --hidden-import PySide6.QtQml `
    --hidden-import PySide6.QtQuick --hidden-import PySide6.QtQuickControls2 `
    --hidden-import PySide6.QtMultimedia --hidden-import PySide6.QtNetwork `
    --copy-metadata PySide6 --copy-metadata PySide6_Essentials --copy-metadata PySide6_Addons `
    --copy-metadata shiboken6 --add-data "$PSScriptRoot\assets\qt;assets/qt" `
    --copy-metadata pillow --copy-metadata requests --copy-metadata urllib3 `
    --copy-metadata certifi --copy-metadata charset-normalizer --copy-metadata idna `
    --add-data "$PSScriptRoot\tools\exiftool\runtime;tools/exiftool/runtime" `
    --add-data "$PSScriptRoot\tools\ffmpeg\runtime;tools/ffmpeg/runtime" `
    --add-data "$PSScriptRoot\assets\fonts\Pretendard;assets/fonts/Pretendard" `
    --add-data "$PSScriptRoot\assets\browser-extension\README.md;assets/browser-extension" `
    --add-data "$PSScriptRoot\assets\browser-extension\PRIVACY.md;assets/browser-extension" `
    --add-data "$PSScriptRoot\assets\browser-extension\chromium\manifest.json;assets/browser-extension/chromium" `
    --add-data "$PSScriptRoot\assets\browser-extension\chromium\background.js;assets/browser-extension/chromium" `
    --add-data "$PSScriptRoot\assets\browser-extension\chromium\profile.js;assets/browser-extension/chromium" `
    --add-data "$PSScriptRoot\assets\browser-extension\chromium\connect.js;assets/browser-extension/chromium" `
    --add-data "$PSScriptRoot\assets\browser-extension\chromium\connect.html;assets/browser-extension/chromium" `
    --add-data "$PSScriptRoot\assets\browser-extension\chromium\connect.css;assets/browser-extension/chromium" `
    --add-data "$PSScriptRoot\assets\browser-extension\chromium\extension-page.js;assets/browser-extension/chromium" `
    --add-data "$PSScriptRoot\assets\browser-extension\firefox\manifest.json;assets/browser-extension/firefox" `
    --add-data "$PSScriptRoot\assets\browser-extension\firefox\background.js;assets/browser-extension/firefox" `
    --add-data "$PSScriptRoot\assets\browser-extension\firefox\profile.js;assets/browser-extension/firefox" `
    --add-data "$PSScriptRoot\assets\browser-extension\firefox\connect.js;assets/browser-extension/firefox" `
    --add-data "$PSScriptRoot\assets\browser-extension\firefox\connect.html;assets/browser-extension/firefox" `
    --add-data "$PSScriptRoot\assets\browser-extension\firefox\connect.css;assets/browser-extension/firefox" `
    --add-data "$PSScriptRoot\assets\browser-extension\firefox\extension-page.js;assets/browser-extension/firefox" `
    --add-data "$PSScriptRoot\docs\browser-login.md;docs" `
    --add-data "$PSScriptRoot\README.md;." --add-data "$PSScriptRoot\CHANGELOG.md;." `
    --add-data "$PSScriptRoot\LICENSE;." --add-data "$PSScriptRoot\THIRD_PARTY_NOTICES.md;." entry.py
    if ($LASTEXITCODE -ne 0) { throw '실행 파일 빌드에 실패했습니다.' }
}
finally {
    $env:PATH = $buildOriginalPath
}
& $taskPython 'tools\build_snapshot.py' 'seal'
if ($LASTEXITCODE -ne 0) { throw '빌드 중 소스나 입력이 변경되었습니다. 다시 빌드하세요.' }
& $taskPython 'tools\build_launcher.py'
if ($LASTEXITCODE -ne 0) { throw '실행기를 컴파일하거나 앱을 LuvBox.exe 하나로 묶지 못했습니다.' }
# Both checks start the single program the way a person does. Its launcher
# unpacks into this private folder, never into the developer's own LuvBox data;
# the second check reuses the unpacked copy, as a second start would.
$checkData = Join-Path $PSScriptRoot "build\startup-check-$version"
if (Test-Path -LiteralPath $checkData) { Remove-Item -LiteralPath $checkData -Recurse -Force }
try {
    & $taskPython 'tools\check_qt_startup.py' $single (Join-Path $PSScriptRoot "build\qt-startup-$version.json") $checkData
    if ($LASTEXITCODE -ne 0) { throw '실제 LuvBox.exe의 Qt 시작 점검에 실패했습니다. 배포 파일을 만들지 않습니다.' }
    & $taskPython 'tools\check_download_startup.py' $single (Join-Path $PSScriptRoot "build\download-startup-$version.json") $checkData
    if ($LASTEXITCODE -ne 0) { throw '실제 LuvBox.exe의 오프라인 저장 시작 점검에 실패했습니다. 배포 파일을 만들지 않습니다.' }
}
finally {
    if (Test-Path -LiteralPath $checkData) { Remove-Item -LiteralPath $checkData -Recurse -Force }
}
& $taskPython 'tools\package_browser_extension.py'
if ($LASTEXITCODE -ne 0) { throw '브라우저 확장 제출 자료 준비에 실패했습니다.' }
& $taskPython 'tools\package_release.py'
if ($LASTEXITCODE -ne 0) { throw '배포 파일(LuvBox.exe, 체크섬, 소스·라이선스 ZIP) 준비에 실패했습니다.' }
Write-Host "빌드 완료: release\LuvBox.exe (이 파일 하나로 실행)"
Write-Host "체크섬: release\SHA256-$version.txt"
Write-Host "소스·라이선스: release\LuvBox-$version-source-licenses.zip (사용 안내 포함)"
