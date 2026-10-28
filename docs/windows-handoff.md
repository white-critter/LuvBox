# LuvBox 1.3.0 Windows 실행 순서

Windows 10·11 64비트, Python 3.12 이상과 PowerShell을 사용합니다. 앱은 PySide6 + Qt Quick으로 실행합니다. [개발 안내](development.md), [보안 검토](security-review.md), [검증 기록](verification.md)이 현재 동작과 검사 범위의 기준입니다.

## 소스 준비

새 폴더에서 받는 경우:

```powershell
git clone --branch main https://github.com/white-critter/LuvBox.git LuvBox
Set-Location -LiteralPath 'LuvBox'
git lfs pull
```

기존 `main` 작업 복사본은 `git status --short --branch`로 변경을 확인하고 보존한 뒤 갱신합니다. 변경이 없는 경우 `git fetch --prune origin`, `git merge --ff-only origin/main`, `git lfs pull` 순서로 진행할 수 있습니다. 충돌·실패가 있으면 다음 단계로 넘기지 않습니다. 소스 ZIP은 새 폴더에 풀고, 포함된 소스 최상위의 `entry.py`·`setup.ps1`을 기준으로 작업합니다. 기존 사용자 폴더 위에 풀지 않습니다.

## 의존성과 고정 입력

```powershell
.\setup.ps1
.\.venv\Scripts\python.exe tools\check_build_inputs.py
.\.venv\Scripts\python.exe -m pip check
```

Python 경로를 지정하려면 `setup.ps1 -PythonPath 'C:\경로\python.exe'`를 사용합니다. 가상환경은 로컬에서 만들고 다른 운영체제의 `.venv`를 복사하지 않습니다. 고정 버전은 `requirements-lock.txt`를 따릅니다. 초기 LFS 검사만 필요하면 `python tools\check_build_inputs.py --assets-only`를 사용할 수 있습니다.

준비 검사는 글꼴·FFmpeg의 실제 파일, 크기·체크섬과 ExifTool 런타임을 확인합니다. Qt 원본 소스·라이선스 준비는 `build.ps1`이 수행합니다. 원본 URL·체크섬을 임의로 바꾸거나 실행 검사 실패를 통과로 처리하지 않습니다.

## 소스 실행과 격리 검사

```powershell
.\run.bat
```

테스트는 앱을 닫은 뒤 다음처럼 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
New-Item -ItemType Directory -Force '.test-output' | Out-Null
.\.venv\Scripts\python.exe entry.py --self-test '.test-output\source-selftest.json'
.\.venv\Scripts\python.exe entry.py --download-smoke '.test-output\download-smoke.json'
```

`--self-test`는 임시 데이터로 Qt 창과 저장 엔진·미디어·메타데이터·기록을 확인합니다. `--download-smoke`는 합성 쿠키로 실제 X 추출기의 첫 요청을 준비한 직후 중단하며 DNS·연결·전송을 차단합니다. 두 보고서의 `ok: true`와 프로세스 종료 코드 0을 함께 확인합니다. 테스트의 생략은 이유를 기록합니다. 선택적 실제 휴지통 검사는 임시 테스트 파일에만 적용되며 기본 전체 검사에서는 생략될 수 있습니다.

이 명령들은 기존 X 연결·미디어·예약을 시험 대상으로 사용하지 않습니다. 실제 사용자 서비스 연결·다운로드·예약 변경의 성공은 별도 결과가 있을 때만 보고합니다.

연결 화면에서는 Chrome·Edge·Firefox를 선택하고 같은 브라우저의 일반 프로필에 확장을 설치합니다. 관리 화면 주소 복사·X 열기·실제 연결이 같은 선택을 사용합니다. 확장 단계 표시를 갱신하려면 연결창을 한 번 연 뒤 확장 관리에서 새로고침해 확장을 1.1.0으로 바꿉니다. 이전 1.0.0의 최종 계정 전달은 호환되지만 중간 단계는 없습니다. 설치 도움말은 기본으로 접으며 8초 무응답 안내는 미설치 확정이 아닙니다. [연결 안내](browser-login.md)에 승인·재연결·검증 상태와 전용 로그인 창 실험의 범위를 기록합니다.

미리보기 확인: 갤러리 사진과 저장 전 확인창이 앱 창을 넓게 사용하는지 확인합니다. **창 최대화 / 창 복원**은 앱 전체 크기를 바꾸며, 갤러리 **본문 보기 / 본문 접기**는 사진 아래 게시물 본문을 펼치고 접습니다. 작은 창에서도 닫기·저장·건너뛰기 동작이 보여야 합니다.

## EXE·ZIP 빌드

```powershell
.\build.ps1
```

이 스크립트는 고정 입력을 확인하고 앱을 빌드한 뒤 실제 frozen EXE에 다음 검사를 수행합니다.

- Qt 네이티브 창, 갤러리 썸네일·사진 미리보기, 설정·연결창과 종료.
- 실제 X 추출기의 SSL·쿠키 초기화와 네트워크 없는 첫 요청 준비.

둘 중 하나라도 실패하면 ZIP을 만들지 않습니다. `tools/check_qt_startup.py`와 `tools/check_download_startup.py`가 남긴 버전·EXE SHA-256 보고서는 직접 패키징에도 필요합니다.

산출물:

- `release/LuvBox-1.3.0/LuvBox-1.3.0.exe`
- 같은 폴더의 `_internal/`
- `release/LuvBox-1.3.0.zip`
- `release/SHA256-1.3.0.txt`

ZIP의 EXE·소스·라이선스와 체크섬을 확인하고 새 경로에 풀어 실행합니다. EXE만 옮기지 않습니다. ZIP의 소스에는 사용자 설정·세션·쿠키·미디어·기록 DB가 없어야 합니다. Qt/PySide6와 재생 구성요소의 소스·고지는 [Qt 배포 자료](../tools/qt/README.md)를 함께 보관합니다.

최종 실행·검사 수·배포 결과는 [검증 기록](verification.md)에 갱신합니다. 이전 검증 문서는 당시 버전의 기록으로 보존하며 현재 실행 결과로 대신하지 않습니다.
