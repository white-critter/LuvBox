# LuvBox 작업 규칙 (AGENTS)

이 저장소에서 코드·UI·문서·릴리스를 다룰 때 반드시 지키는 기준입니다.

## 1. 일반 사용자 비노출 원칙 (3중 보호)

LuvBox는 비개발자 Windows 사용자를 위한 데스크톱 앱입니다. 다음 세 층위로 내부·시스템·개발 정보를 사용자에게 노출하지 않습니다.

1. **작성 원칙 ([개발 안내](docs/development.md) ‘사용자가 읽는 글은 짧고 쉽게’)**
   - **대상 표면**:
     - 앱 화면 문자열(`assets/qt/*.qml`의 `text`, `title`, `subtitle`, `placeholderText`, `tip`, `Accessible.name`)
     - 컨트롤러·오류 안내(`luvbox/user_errors.py` 경유 사용자 메시지)
     - 실행기 메시지·시작 화면(`tools/launcher/Launcher.cs`의 `Messages`, `tools/launcher/splash.ini`)
     - 브라우저 연결 확장 화면(`assets/browser-extension/{chromium,firefox}/connect.html`, `extension-page.js`)
     - 사용자 문서(`README.md`, `CHANGELOG.md`, `docs/사용안내.txt`, `docs/browser-login.md`, `assets/browser-extension/README.md`, `assets/browser-extension/PRIVACY.md`, `.github/ISSUE_TEMPLATE/bug.yml`)
     - GitHub 릴리스 본문(한 줄 소개, `### 주요 변경 사항` 3~4줄, `### 받는 법`만 포함)
   - **금지 항목**:
     - 내부 프레임워크·라이브러리·도구명(`PySide6`, `PyInstaller`, `SQLite`, `sqlite3`, `ONNX`, `onnxruntime`, `SigLIP`, `gallery-dl`, `ExifTool`, `FFmpeg`, `Pillow`, `QML`, `Qt Quick`, `DPAPI`, `Win32`, `WebCrypto`, `DevTools`, `PRAGMA`)
     - 내부 파일명·DB 표·환경 변수·경로(`%LOCALAPPDATA%`, `LOCALAPPDATA`, `settings.json`, `history.sqlite3`, `unsaved.sqlite3`, `profile.sqlite3`, `last-scheduled.json`, `.luvbox`, `catalog_meta`, `image_hashes`, `missing_choices`, `_internal`, 사용자 Windows 계정 경로 등)
     - 예외 클래스명·스택 트레이스·내부 진단(`Traceback`, `WinError`, `PermissionError`, `OperationalError`, `ReferenceError`, `Binding loop`)
     - 개발 문서 링크(`development.md`, `changelog-detail.md`, `security-review.md`, `verification.md`), 체크섬·암호화 알고리즘 세목(`SHA-256`, `AES-GCM`, `HMAC-SHA-256`), 로컬 루프백 주소(`127.0.0.1`), 테스트·벤치마크 수치
   - Python 예외를 화면에 직접 `.isascii()` 등으로 걸러 노출하지 말고 `luvbox.user_errors.safe_ui_error` 및 `UserFacingError` 계열만 사용합니다.
   - 제어 이름 작은따옴표 표기(`‘…’`)는 실제 화면 컨트롤의 문자열과 일치해야 합니다(`tests/test_regression_1117.py`).

2. **빌드·패키징 게이트 (`tools/package_release.py`)**
   - `require_current_guides`와 `require_user_facing_wording`이 `.\build.ps1` 시작 전(`--check-guides`)과 `package()` 시점에 사용자 문서·QML·실행기·확장 문자열을 검사하고 위반 시 실패합니다.

3. **자동 회귀 테스트 (`tests/`)**
   - `tests/test_user_wording_policy.py`: `require_user_facing_wording` 정·부정 검사, `luvbox/*.py` AST `.isascii()` 예외 통과 금지, 런타임 한글 사용자 경로·예외 마스킹 검증
   - `tests/test_regression_1117.py`, `tests/test_regression_1119.py`: 컨트롤 라벨 일치 및 문서 내부 용어 차단 검증

## 2. 데이터 격리 및 작업 위생

- 모든 테스트 파일은 `luvbox`를 임포트하기 전에 `from tests import app_data_isolation`을 먼저 임포트합니다.
- 실제 `%LOCALAPPDATA%\LuvBox`, 사용자 미디어 폴더, Windows 작업 스케줄러의 사용자 예약을 절대 건드리지 않습니다. Qt/QML 점검 시 `LOCALAPPDATA`와 `QML_DISK_CACHE_PATH`를 모두 임시 폴더로 격리합니다.
- `docs/plans/`, `.test-output/`, `build/`, `release/` 산출물은 Git에 커밋하지 않습니다.

## 3. 빌드·검증·배포 절차

- `luvbox/__init__.py`의 `__version__`을 단일 버전 기준으로 삼습니다.
- 릴리스 전 `.\.venv\Scripts\python.exe -m pip check`, `tools\check_build_inputs.py`, `tools\package_release.py --check-guides`, `tools\release_key.py --check`, QML 린트, 전체 `unittest`, `--self-test`, `--download-smoke`, `.\build.ps1`을 모두 통과해야 합니다.
- 원격 저장소 반영 전 `git fetch origin`으로 선행 커밋 여부를 확인하고, 릴리스 업로드 후에는 한글·공백 포함 임시 경로에서 `LuvBox.exe`를 다시 내려받아 SHA-256 및 격리 시작 검사를 통과한 뒤에만 이전 릴리스를 정리합니다(Git 태그는 보존).
