# Windows 로컬 세션 이어받기

2026-10-22 기준. 클라우드의 소스 검증 결과와 Windows 실제 화면·실행 파일 검증을 구분합니다. 최신 검사 수와 측정 조건은 [검증 기록](verification.md)과 [성능 근거](performance.md)에 있습니다.

최상위 `WINDOWS_CONTINUE.md`에 이전 요청·반영한 변경·미해결 사항·수정 지점·다음 작업을 정리했습니다. 새 로컬 세션은 이 파일부터 읽으면 됩니다.

## 소스 전달

Windows에서 이어받을 브랜치는 `main`입니다. 이번 수정의 소스 커밋은 `5f26b14`이며, 최초 인수 브랜치는 `codex/ui-ux-windows-handoff`였습니다. 새 작업 폴더를 만들 상위 폴더에서 다음을 실행합니다. 기존 폴더를 덮어쓰지 않으며, 같은 이름의 폴더가 있으면 다른 새 이름을 사용합니다.

```powershell
$ErrorActionPreference = 'Stop'
git clone --branch main https://github.com/chobby-f/LuvBox.git LuvBox-windows-handoff
if ($LASTEXITCODE -ne 0) { throw 'main 브랜치 복제 실패' }
Set-Location -LiteralPath 'LuvBox-windows-handoff'
git lfs pull
if ($LASTEXITCODE -ne 0) { throw '고정 글꼴·FFmpeg Git LFS 복원 실패' }
git log -1 --oneline
```

기존 저장소를 사용한다면 먼저 `git status`로 로컬 변경을 확인하고 보존한 뒤 `main`을 갱신합니다. 새 폴더에서 복제하면 기존 수정과 섞이지 않습니다. 위 폴더를 Windows 로컬 세션의 작업 폴더로 열고 `WINDOWS_CONTINUE.md`부터 읽습니다.

전달받은 소스 ZIP은 새 빈 폴더에 풉니다. `entry.py`와 `build.ps1`이 있는 최상위 폴더를 로컬 세션의 작업 폴더로 엽니다. 동봉된 SHA256과 ZIP의 `Get-FileHash -Algorithm SHA256` 값을 대조합니다. 기존 로컬 저장소의 미커밋 수정 위에 덮어쓰거나 강제 checkout하지 않습니다.

소스 전달 ZIP에는 변경이 이미 반영돼 있으므로 패치를 다시 적용하지 않습니다. ZIP 전용 `HANDOFF_MANIFEST.json`에는 전달 파일의 크기·SHA256과 기준 커밋을 기록합니다. `validation` 폴더에는 클라우드의 관련 회귀 검사 결과, 재생 비교, 갤러리 스케줄러 추적 요약을 동봉합니다. Git 체크아웃에는 이 전달용 파일 대신 검증 문서를 포함합니다.

이전 문서의 `.test-output/...` 원시 로그·진단 스크립트는 클라우드 작업 폴더의 경로이며 ZIP에 전체를 포함하지 않았습니다. 전달된 결과는 `validation/*.json`에서 확인하고 Windows 측정은 동봉된 `tools` 도구와 새 로컬 로그를 사용합니다.

이 전달본에는 `.git`·가상환경·사용자 설정·로그인 세션·쿠키·기록 DB·사용자 미디어가 없습니다. Windows용 Python 3.12 이상과 Tkinter를 로컬에서 사용하며, 클라우드의 Linux 가상환경은 복사하지 않습니다.

Git 체크아웃으로 이어받은 경우에만 `git lfs pull`로 글꼴 두 개와 FFmpeg 실행 파일을 복원합니다. 소스 ZIP에는 복원된 파일이 포함되므로 Git이 없어도 됩니다. `.gitattributes`는 FFmpeg의 두 빌드 레시피를 CRLF, Pretendard의 `OFL.txt`를 LF로 고정합니다. ZIP 전달은 현재 파일 바이트를 보존하며, 모든 고정 체크섬은 아래 준비 검사에서 다시 확인합니다.

## 준비와 소스 검사

PowerShell에서 작업 폴더를 열고 다음 순서로 실행합니다. 어느 검사든 실패하면 원인을 해결한 뒤 다음 단계로 진행합니다.

```powershell
$ErrorActionPreference = 'Stop'
python tools\check_build_inputs.py --assets-only
if ($LASTEXITCODE -ne 0) { throw '초기 빌드 입력 검사 실패' }
.\setup.ps1
if ($LASTEXITCODE -ne 0) { throw 'Windows 준비 실패' }
$taskPython = Join-Path $PWD '.venv\Scripts\python.exe'
& $taskPython tools\check_build_inputs.py
if ($LASTEXITCODE -ne 0) { throw '전체 빌드 입력 검사 실패' }
& $taskPython tools\build_theme.py --check
if ($LASTEXITCODE -ne 0) { throw '테마 번들 검사 실패' }
& $taskPython -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { throw 'Windows 회귀 검사 실패' }
New-Item -ItemType Directory -Force '.test-output' | Out-Null
& $taskPython entry.py --self-test '.test-output\windows-source-selftest.json'
if ($LASTEXITCODE -ne 0) { throw '소스 자체 점검 실패' }
$sourceCheck = Get-Content -Raw '.test-output\windows-source-selftest.json' | ConvertFrom-Json
if (-not $sourceCheck.ok) { throw '소스 자체 점검 보고서를 확인하세요.' }
```

`setup.ps1`은 고정된 ExifTool 배포본과 FFmpeg 원본 아카이브를 준비합니다. 이 두 입력은 클라우드 소스 ZIP에서 새로 만들지 않았습니다. 네트워크 준비가 끝난 뒤 전체 입력 검사가 성공해야 합니다. 준비 검사는 다운로드하거나 실행 파일을 실행하지 않습니다. 소스 자체 점검의 `ok`가 `true`인지 확인합니다.

## Windows 실제 동작

- 메인 창·설정·라이브러리·저장 확인창을 반복해서 열고 닫고, 실제 창 테두리를 드래그해 크기를 바꿉니다. 진행바, 버튼 위치·간격, 작은 화면·큰 배율, 밝음·어두움 전환을 확인합니다.
- 생성 사진으로 갤러리를 리사이즈·스크롤하고, 작성자 검색을 빠르게 바꾸며 선택된 작성자와 사진이 유지되는지 확인합니다. 아래 도구는 임시 생성 사진만 사용합니다. 타이머 간격과 실제 디스플레이 FPS는 별도로 판단합니다.
- 로컬 생성 동영상·GIF에서 30fps 미리보기, 긴 GIF 정지 구간, 다음 프레임, 중단·창 닫기와 원본 파일 잠금 해제를 확인합니다.
- 기본 브라우저의 기존 X 로그인 연결, 본인 아이디 자동 입력과 연결 계정 선택을 확인합니다. 확장 설치·일회성 연결 승인은 [브라우저 연결 안내](browser-login.md)를 따릅니다. 실제 Windows DPAPI 보관·앱 재시작 후 연결 유지도 확인합니다. 인증 값은 검사 로그나 전달 ZIP에 넣지 않습니다.

```powershell
& $taskPython tools\benchmark_gallery.py --rows 1000 --unique --unique-captions --scroll |
    Set-Content -Encoding Ascii '.test-output\windows-gallery.json'
if ($LASTEXITCODE -ne 0) { throw 'Windows 갤러리 측정 실패' }
```

## EXE·ZIP 빌드와 자체 점검

```powershell
$ErrorActionPreference = 'Stop'
.\build.ps1
if ($LASTEXITCODE -ne 0) { throw 'EXE 또는 ZIP 빌드 실패' }
$version = (& $taskPython -c 'from luvbox import __version__; print(__version__)').Trim()
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw '앱 버전 확인 실패' }
$exe = Join-Path $PWD "release\LuvBox-$version\LuvBox-$version.exe"
$report = Join-Path $PWD '.test-output\windows-frozen-selftest.json'
$process = Start-Process -FilePath $exe -ArgumentList '--self-test', ('"{0}"' -f $report) -Wait -PassThru
if ($process.ExitCode -ne 0) { throw '실행 파일 자체 점검 실패' }
$check = Get-Content -Raw -LiteralPath $report | ConvertFrom-Json
if (-not $check.ok -or -not $check.frozen -or $check.theme_images -ne 'bundled-light-and-dark') {
    throw '실행 파일 점검 보고서를 확인하세요.'
}
Get-FileHash -Algorithm SHA256 "release\LuvBox-$version.zip"
Get-Content "release\SHA256-$version.txt"
```

창 모드 EXE의 점검이 끝날 때까지 `Start-Process -Wait`로 기다립니다. 캐시 파일 생성 개수 대신 실제 밝음·어두움 테마 번들과 Tk 이미지를 검사합니다.

배포 ZIP의 CRC, 소스와 EXE의 일치, 고정 런타임·글꼴·테마·연결 확장 포함, SHA256 일치를 확인합니다. 공백·한글 경로에 ZIP을 풀어 같은 EXE 자체 점검과 창 동작을 다시 확인합니다. `release`에는 빌드 결과만 두며, 사용자 설정·세션·쿠키·DB·미디어가 포함되지 않았는지 확인합니다. 검사 보고서와 실제 확인 범위를 남긴 뒤 배포합니다.
