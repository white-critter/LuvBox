# LuvBox 1.3.1 개발 안내

현재 앱은 Windows용 PySide6 + Qt Quick 단일 UI입니다. 기본 실행은 저장 화면이며, 같은 앱에서 X 연결·저장·갤러리·설정을 사용합니다. 최신 검사 결과는 [검증 기록](verification.md), 유지하는 보안 경계는 [보안 검토](security-review.md), 측정 방법은 [성능 안내](performance.md)를 참고하세요.

## 준비와 실행

Windows 10·11 64비트와 Python 3.12 이상을 사용합니다. 저장소를 새로 받았다면 Git LFS의 글꼴·FFmpeg 실제 파일을 복원합니다.

```powershell
git lfs pull
.\setup.ps1
.\run.bat
```

Python 위치를 지정하려면 `setup.ps1 -PythonPath 'C:\경로\python.exe'`를 사용합니다. 준비 스크립트는 `.venv`에 `requirements-dev.txt`를 설치하되 `requirements-lock.txt`의 고정 버전을 적용하고, ExifTool을 준비해 번들 FFmpeg를 확인합니다. UI·다운로드 실행에 별도 서버나 X 개발자 API 키는 필요하지 않습니다. Node.js는 선택적 브라우저 확장 호환 검사에만 사용합니다.

콘솔에서 실행하려면 `.\.venv\Scripts\python.exe entry.py`를 사용합니다. 제품 버전의 기준은 `luvbox/__init__.py`입니다. Pillow 13.0.0, certifi 2026.10.19, setuptools 84.1.0의 보안 수정은 고정 의존성과 함께 유지합니다.

## 코드 구조

| 위치 | 역할 |
| --- | --- |
| `entry.py`, `luvbox/qt_app.py`, `assets/qt/` | 실행 진입점, Qt 앱 제어와 QML 화면 |
| `qt_accounts.py`, `qt_browser_setup.py`, `browser_discovery.py`, `login.py`, `browser_bridge.py` | 연결 승인·확장 설치 안내·DPAPI 계정 보관 |
| `qt_download.py`, `qt_saved_preview.py`, `download_service.py`, `core.py`, `review.py` | 저장 상태·선택적 첫 장면, 별도 worker와 IPC, 실제 다운로드와 저장 여부 확인 |
| `qt_library.py`, `qt_gallery.py`, `qt_wheel.py` | 읽기 전용 갤러리, 썸네일·스크롤·휠 입력 |
| `app_settings.py`, `settings.py`, `file_mutex.py`, `qt_schedule.py`, `scheduled.py`, `activity.py` | 설정·일일 예약·수동/자동 실행 결과와 프로세스 간 변경 보호 |
| `history.py`, `storage.py`, `recovery.py`, `deletion.py`, `recycling.py`, `management_store.py`, `qt_management.py` | 기록·갤러리 필터·관리 화면의 백그라운드 스냅샷, 경로 승인·이전·사본·복구와 휴지통 이동 |
| `preview.py`, `thumbcache.py`, `media.py` | 로컬 미리보기·캐시와 GIF 변환 |
| `metadata.py`, `process_limits.py`, `protection.py` | 출처·작성자 기록, 외부 도구 제한과 X 요청 보호 |
| `selftest.py`, `qt_smoke.py`, `tools/check_*startup.py`, `tools/package_release.py` | 격리 검사와 소스·라이선스 배포 패키징 |

Qt 객체와 QML 상태는 UI 스레드에서 다룹니다. 파일·DB·네트워크 작업은 작업 스레드나 별도 worker에서 처리하고, 신호·큐·JSON 메시지로 결과를 전달합니다. 이전 작업의 썸네일 결과는 generation으로 구분하며, 진행 이벤트는 제어 이벤트의 순서를 보존하면서 합칩니다. 종료 시 실행 중인 작업과 파일 핸들을 정리합니다.

미리보기·저장 전 확인창은 부모 오버레이에서 24px 여백을 남기고 확장합니다. 헤더의 창 최대화·복원은 호스트 앱 창을 제어합니다. 갤러리 본문은 기본으로 접고 필요할 때 펼치며, 이미지 비율과 미디어 해제·저장 승인 순서는 유지합니다.

휠·버튼 확대/축소, 드래그 이동, 화면 맞춤과 지원하는 정지 사진의 원본 배율을 제공합니다. 이전·다음은 현재 검색·정렬 순서를 따르고 끝에서 순환하지 않습니다. 입력란의 방향키를 가로채지 않으며 새 파일·닫기·숨김에서 확대·이동을 초기화합니다. 저장 화면의 자세히 보기는 최근 실제 저장 파일의 첫 프레임·작성자·본문·게시 시각·원문을 표시합니다. 숨겨진 동안 디코딩하지 않고 현재 요청 하나와 가장 최근 대기 하나만 유지하며 추가 X 요청을 보내지 않습니다.

## 데이터와 인증 호환성

설정·암호화 세션·기록·백업·미리보기는 `%LOCALAPPDATA%\LuvBox`에 있습니다. 미디어 저장 폴더의 `.luvbox` 사본과 이전 파일명·archive·암호화 형식·예약 이름을 계속 인식합니다. UI 변경을 이유로 기존 파일·DB·계정을 초기화하지 않습니다. 폴더 이전의 명시적 승인 경로를 보존하고, 다른 PC의 기록 사본이 주장하는 외부 경로에 관리 권한을 부여하지 않습니다.

계정은 브라우저 확장의 코드·계정 확인 후 승인하거나 토큰을 직접 입력해 연결합니다. 수동 토큰 보관은 X 인증 성공을 뜻하지 않습니다. 토큰·쿠키·브라우저 프로필·사용자 DB를 로그·Git·배포 파일에 넣지 않습니다. 계정 연결과 Qt 설정 변경은 Windows 경로별 mutex와 원자적 파일 교체로 보호합니다. 설정 읽기에 실패하면 변경 저장을 중단해 기존 파일을 보존합니다.

`BrowserSetupController`는 지원 브라우저 목록·선택을 UI 밖에서 읽고 선택 저장이 성공한 뒤 화면에 반영합니다. `connection_browser`에 Chrome·Edge·Firefox의 고정 ID만 보관하며 설치·X 열기·연결은 같은 브라우저 실행 도우미를 사용합니다. 선택한 제품의 일반 프로필은 브라우저가 정하므로 앱이 특정 프로필을 알아냈다고 표시하지 않습니다. Firefox에는 별도 확장 변형을 준비합니다.

확장 1.1.0은 경로가 분리된 암호화 `/progress`로 허용된 연결 단계만 보냅니다. 요청 세대·취소 상태를 확인해 늦은 이전 작업의 진행 신호를 버리고, 실제 응답 전에는 확장 설치 성공으로 표시하지 않습니다. 약 8초 무응답 안내는 설치·프로필 확인을 돕는 표시이며 미설치 판정이 아닙니다. 이전 1.0.0 확장의 `/session` 형식은 보존합니다.

계정 상태는 로컬 보관(`saved`), 브라우저 확인(`browser_confirmed`), 인증된 Likes 목록의 정상 응답(`verified`), 명시적 인증 거부(`reconnect_required`)를 구분합니다. 통신·요청 제한·게시물 접근 실패로 계정을 지우지 않으며 수동 토큰 보관만으로 성공·자동 닫기를 발생시키지 않습니다. [로그인 연결](browser-login.md)에 상세 경계와 확장 없는 WebEngine 실험의 미검증 범위를 기록합니다.

새 파일명은 `작성자ID_게시물ID_번호.ext`이며 기존 파일을 자동으로 이름 변경하지 않습니다. 원본 링크·작성자·본문·숫자 계정 ID를 분리하고 타인이 기록한 메타데이터를 보존합니다. 새 Like 빠른 확인은 이미 저장된 파일이 연속되는 지점까지 확인하며, 이전 실행·옵션·병합 상태에 따라 전체 확인으로 전환합니다.

## 검사

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe tools\check_build_inputs.py
.\.venv\Scripts\python.exe -m unittest discover -s tests
New-Item -ItemType Directory -Force '.test-output' | Out-Null
.\.venv\Scripts\python.exe entry.py --self-test '.test-output\source-selftest.json'
.\.venv\Scripts\python.exe entry.py --download-smoke '.test-output\download-smoke.json'
```

테스트와 자체 점검은 임시 앱 데이터·생성 미디어·합성 인증 정보를 사용합니다. `--self-test`는 Qt 시작·종료와 미디어·메타데이터·기록·worker를, `--download-smoke`는 실제 X 추출기의 SSL 초기화와 첫 요청 준비를 검사합니다. 후자는 DNS·접속·전송을 차단하므로 실제 X 요청을 보내지 않습니다. 통과 수·생략 이유·실제 서비스 검증 범위는 실행 결과와 검증 기록을 구분해 보고합니다.

## 빌드와 배포 자료

```powershell
.\build.ps1
```

`build.ps1`은 고정 입력·ExifTool·FFmpeg와 Qt 원본 소스·라이선스를 준비하고 PyInstaller 폴더형 앱을 만듭니다. Windows System32를 우선해 잘못된 ICU DLL 수집을 막습니다. 빌드 Python이 요구하는 선택적 SSL 보조 파일은 해당 정책을 보존한 채 동봉합니다.

실제 EXE의 Qt 창·갤러리·미리보기·설정·연결창·종료 검사와 오프라인 저장 시작 검사를 모두 통과해야 ZIP을 만듭니다. 직접 패키징도 현재 버전과 EXE SHA-256에 일치하는 두 검사 보고서를 요구합니다. EXE·`_internal`·동봉 소스·라이선스를 함께 유지합니다. Qt와 미디어 구성요소의 버전별 원본 자료는 [Qt 소스 안내](../tools/qt/README.md), [FFmpeg 안내](../tools/ffmpeg/README.md)를 따릅니다. 검사 실패를 피하려고 입력 체크섬·TLS 검증·패키징 허용 목록을 완화하지 않습니다.

산출물은 `release/LuvBox-1.3.1/`, `release/LuvBox-1.3.1.zip`, `release/SHA256-1.3.1.txt`입니다. 빌드 단계의 격리 검사는 실제 X 인증·다운로드나 사용자 예약 변경을 대신하지 않습니다.

`tools/build_snapshot.py`는 PyInstaller 직전 소스·QML·배포 입력과 의존성 버전을 기록하고 빌드 후 EXE·실행 폴더 전체 파일의 상대 경로와 해시에 결합합니다. 패키징도 같은 기록을 검사하므로 소스·문서·의존성·번들 런타임이 바뀌면 다시 빌드합니다. 입력 기록과 시작 보고서는 `build/`, 생성 spec은 `build/spec/`에 두며 소스 최상위에 버전별 spec을 쌓지 않습니다. 프로젝트의 허용 소스 파일과 상위 폴더는 링크·junction·hardlink가 없어야 합니다.

관리 화면은 단일 일일 예약 생성·시간 변경·해제, 휴지통 이동·삭제 이력, 사라진 파일·통계·실행 이력과 갤러리의 작성자·날짜 필터를 제공합니다. Windows 휴지통에서 원본을 직접 복원하는 UI와 확장 스토어 등록은 별도 기능 범위입니다. 확장 없는 WebEngine 로그인은 합성 실험 단계이며 제품 런타임에 포함하지 않습니다. 날짜별 검토 보고서는 현재 안내에 통합하고 원문은 Git 태그·기록에 보존합니다.
