# LuvBox 1.9.4 개발 안내

현재 앱은 Windows용 PySide6 + Qt Quick 단일 UI입니다. 기본 실행은 저장 화면이며, 같은 앱에서 X 연결·저장·갤러리·설정을 사용합니다. 최신 검사 결과는 [검증 기록](verification.md), 유지하는 보안 경계는 [보안 검토](security-review.md), 측정 방법은 [성능 안내](performance.md)를 참고하세요.

## 준비와 실행

Windows 10·11 64비트와 Python 3.12 이상을 사용합니다. 저장소를 새로 받았다면 Git LFS의 글꼴·FFmpeg 실제 파일을 복원합니다.

```powershell
git lfs pull
.\setup.ps1
.\run.bat
```

Python 위치를 지정하려면 `setup.ps1 -PythonPath 'C:\경로\python.exe'`를 사용합니다. 준비 스크립트는 `.venv`에 `requirements-dev.txt`를 설치하되 `requirements-lock.txt`의 고정 버전을 적용하고, ExifTool을 준비해 번들 FFmpeg를 확인합니다. UI·다운로드 실행에 별도 서버나 X 개발자 API 키는 필요하지 않습니다. Node.js는 선택적 브라우저 확장 호환 검사에만 사용합니다.

Python은 현재 각 계열의 3.12.16·3.13.18·3.14.10·3.15.2 이상을 사용합니다. `check_build_inputs.py --python-only`로 자산 없이 확인할 수 있으며 준비·빌드도 같은 기준으로 검사합니다. 1.3.4 Windows 빌드는 Astral python-build-standalone 20261026의 CPython 3.12.16 x64를 프로젝트의 `.python/`에 두고 기존 `.venv`를 `python.exe -m venv --upgrade .venv`로 갱신했습니다. 시스템 전역·Codex Python은 변경하지 않습니다. `.python/`은 개인 가상환경의 기반이므로 사용하는 동안 보존하며 Git·배포 소스에는 넣지 않습니다. 원본 자산 SHA-256은 `590dfcbe3a34c07301e6332e13e2188b98655cd072b08afa45a7998dc4c22bc5`입니다.

대응하는 Python 전체 배포의 빌드 메타데이터·원본 라이선스는 `tools/python/`에 보존하고 릴리스에 동봉합니다. 다른 Python 배포로 빌드할 때는 그 배포의 라이선스와 구성요소 고지도 확인하세요.

콘솔에서 실행하려면 `.\.venv\Scripts\python.exe entry.py`를 사용합니다. 제품 버전의 기준은 `luvbox/__init__.py`입니다. Pillow 13.0.0, certifi 2026.10.19, setuptools 84.1.0의 보안 수정은 고정 의존성과 함께 유지합니다.

## 코드 구조

| 위치 | 역할 |
| --- | --- |
| `entry.py`, `luvbox/qt_app.py`, `assets/qt/` | 실행 진입점, Qt 앱 제어와 QML 화면 |
| `assets/qt/Theme.qml`, `qmldir`, `Flat*.qml`, `Card.qml`, `Glow.qml`, `DialogButtons.qml`, `SettingRow.qml`, `WrapRow.qml` | 테마 값 한 곳, 화면이 함께 쓰는 버튼·입력·메뉴·대화상자 틀·카드·붉은 빛 |
| `qt_accounts.py`, `qt_browser_setup.py`, `browser_discovery.py`, `login.py`, `browser_bridge.py` | 연결 승인·확장 설치 안내·DPAPI 계정 보관 |
| `qt_download.py`, `qt_saved_preview.py`, `download_service.py`, `core.py`, `review.py` | 저장 상태·선택적 첫 장면, 별도 worker와 IPC, 실제 다운로드와 저장 여부 확인 |
| `qt_library.py`, `qt_gallery.py`, `qt_wheel.py`, `duplicates.py`, `clip_classifier.py` | 읽기 전용 갤러리, 썸네일·스크롤·휠 입력, 중복 탐지(완전 일치, 선택 시 픽셀 확인), 로컬 AI CLIP 자동 분류 |
| `unsaved_preview.py` | 저장하지 못한 미디어의 작은 미리보기 한 장을 X 이미지 서버에서 받아 로컬 캐시에 보관 |
| `app_settings.py`, `settings.py`, `file_mutex.py`, `qt_schedule.py`, `scheduled.py`, `activity.py` | 설정·일일 예약·수동/자동 실행 결과와 프로세스 간 변경 보호 |
| `history.py`, `storage.py`, `recovery.py`, `deletion.py`, `recycling.py`, `qt_management.py` | 기록·갤러리 필터의 읽기, 경로 승인·이전·사본·복구와 휴지통 이동 |
| `activity.py`, `run_history.py`, `unsaved.py`, `qt_records.py` | 실행 결과 기록부, 실행 기록 병합 조회, 저장 못 한 미디어 기록부와 설정 › 고급의 목록·다시 저장 |
| `preview.py`, `thumbcache.py`, `media.py` | 로컬 미리보기·캐시와 GIF 변환 |
| `metadata.py`, `process_limits.py`, `protection.py` | 출처·작성자 기록, 외부 도구 제한과 X 요청 보호 |
| `tools/launcher/Launcher.cs`, `tools/build_launcher.py`, `launcher.py`, `tools/draw_splash.py`, `assets/splash/` | `LuvBox.exe` 실행기(한 번 풀기·시작 화면), 한 파일로 묶기와 꼬리표 형식, 시작 화면 그림 |
| `updater.py`, `qt_updater.py`, `assets/qt/UpdateNotice.qml` | 최신 릴리스 확인, 받기·검증·교체, 알림과 설정의 업데이트 항목 |
| `selftest.py`, `qt_smoke.py`, `tools/check_*startup.py`, `tools/package_release.py` | 격리 검사와 배포 파일(단일 EXE·체크섬·소스·라이선스 ZIP) 만들기 |

Qt 객체와 QML 상태는 UI 스레드에서 다룹니다. 파일·DB·네트워크 작업은 작업 스레드나 별도 worker에서 처리하고, 신호·큐·JSON 메시지로 결과를 전달합니다. 이전 작업의 썸네일 결과는 generation으로 구분하며, 진행 이벤트는 제어 이벤트의 순서를 보존하면서 합칩니다. 종료 시 실행 중인 작업과 파일 핸들을 정리합니다.

휴지통 이동이 끝나면 목록을 다시 읽지 않고 옮긴 행만 모델에서 뺍니다(`GalleryModel.removeKeys`, `LibraryController.removeDeleted`). 다음 페이지는 마지막으로 보인 행 뒤에서 이어지므로 저장소는 커서 앞의 행 수만 줄입니다(`LibraryRepository.forget`). 실패·미처리 파일이 있는 결과, 불러오는 중인 페이지, 중복 사진 보기, 32곳을 넘게 흩어진 선택은 이전처럼 전체를 다시 읽습니다. 폴더 전체 작업(자동분류·중복 검사·가르치기·AI 태그 초기화)이 끝날 때도 결과가 현재 필터를 바꾸는 경우에만 다시 읽고, 그 밖에는 열린 항목만 다시 조회합니다(`_settle`). 미리보기의 불러오는 표시는 열린 항목의 조회(`selectionBusy`)만 따르며 폴더 전체 작업의 `busy`와 구분합니다. 갤러리 썸네일 디코딩 작업 수는 논리 코어의 절반을 2~4개 범위로 씁니다.

중복 묶음은 SHA-256과 크기가 같은 파일입니다(`duplicates.cluster_images`). `near`를 켜면 dHash가 4비트 이내인 후보를 32×32 RGB 축소본으로 비교해 평균 1%·99번째 백분위 6%·최대 15% 이내이고 가로세로 비율이 1% 이내일 때만 묶습니다. 평균만 보면 같은 틀에 글자만 다른 그림이 통과하므로 부분 차이를 함께 봅니다. 묶음을 만든 방식은 `catalog_meta`의 `duplicate_rule`(`exact`·`near`)에 남기며, 이 값이 없는 1.6.x 묶음은 처음 읽을 때 저장된 파일 해시로 다시 계산합니다(`HistoryStore.regroup_exact_duplicates`). 중복 보기는 `duplicate_page`가 묶음 순서로 읽고 남길 사진(즐겨찾기, 큰 파일 순)을 SQL에서 정하므로 필터가 묶음 일부를 가려도 사본이 남길 사진으로 바뀌지 않습니다.

통계는 `statisticsLoaded = Signal("QVariantMap")`으로 보냅니다. `Signal(object)`로 보낸 dict는 QML에서 필드를 읽을 수 없어 1.6.x의 통계 창은 값을 표시하지 못했습니다. QML로 dict를 보내는 신호는 `QVariantMap`·`QVariantList`로 선언하고, 실제 QML에서 값이 보이는지 장면 테스트로 확인합니다.

휠은 갤러리가 `GalleryWheel`(한 칸 ≈ 타일 높이의 0.6배, 최소 120px), 그 밖의 스크롤 영역이 `SmoothWheel`(120px)을 쓰며 둘 다 같은 방향으로 200ms 안에 이어지면 0.5씩 4배까지 빨라집니다. 새 스크롤 영역에는 `SmoothWheel { flickable: … }`을 넣습니다. 갤러리 그리드는 `interactive: false`이고 끌기는 `DragHandler`가 사각형 선택으로 처리합니다.

### 화면 모양과 테마

색·간격·모서리·글자 크기·컨트롤 높이는 `assets/qt/Theme.qml` 한 파일에 있습니다. `qmldir`이 이 파일을 싱글턴으로 선언하므로 같은 폴더의 QML은 `Theme.accent`처럼 바로 읽습니다. `qmldir`이 빠지면 화면이 뜨지 않으므로 소스 ZIP과 빌드 기록의 파일 목록에 넣었습니다(`package_release.project_source_files`). 다른 QML 파일에는 색 글자(`"#…"`)와 숫자로 쓴 글자 크기·모서리를 두지 않으며, 테마에서 읽는 이름이 실제로 있고 테마의 이름이 모두 쓰이는지까지 `tests/test_qt_theme.py`가 검사합니다.

- **버튼**(`FlatButton`의 `tone`): `primary`는 화면·대화상자마다 하나뿐인 주 동작, 기본 `quiet`는 보통 동작, `ghost`는 물러나 있어야 하는 동작(닫기 옆의 보조 버튼, 제목 줄의 통계·단축키), `danger`는 다른 동작 사이에 놓인 지우는 동작, `chip`은 켜고 끄는 필터, `soft`는 주 동작이 따로 있는 화면에서 눈에 띄어야 하는 동작(업데이트 알림), `nav`는 화면 탭입니다. 목록 줄이나 상태 줄 안의 버튼은 `compact`, 한 줄을 다 쓰는 주 동작은 `large`를 씁니다. 풍선말은 `tip`에 적습니다. 0.5초 머물러야 나오고, 누르면 포인터가 나갔다 올 때까지 나오지 않습니다.
- **대화상자**: `FlatDialog`(제목·덧말·`banner`, 같은 여백, 카드 배경)와 `DialogButtons`(오른쪽 정렬, 주 동작이 먼저, 닫기·취소가 마지막)를 씁니다. 창 전체를 쓰는 미리보기와 저장 전 확인만 자기 머리말을 두고 배경은 같은 `Card`를 씁니다. 메뉴는 `FlatMenu`·`FlatMenuItem`입니다.
- **붉은 빛**: `Glow`는 시작 화면의 빛과 같은 감쇠(`(1 − t)^2.2`)를 작은 캔버스에 한 번 그려 늘려 씁니다. 빛은 항목의 가장자리에서 정확히 0이 되어 직선으로 끊기지 않습니다. 창 배경, 로고 뒤, `Card { glow: … }`(대화상자·선택 막대·알림)에 쓰며, 창의 빛은 저장 화면에서 저장이 진행 중이고 창이 활성일 때만 3.4초 주기로 밝기가 바뀝니다(`lightBreathing`). 그 밖에는 움직이지 않습니다.
- **갤러리 위쪽**: 첫 줄(`gallerySearchRow`)은 검색·정렬·종류·크기, 둘째 줄(`galleryFilterRow`)은 줄바꿈하는 필터 칩(`galleryFilterBar`, `WrapRow`)과 오른쪽의 미리보기·새로고침·여러 개 선택입니다. 날짜 칩(`galleryFilterToggle`)은 범위를 펼치기만 하고, 날짜가 걸려 있을 때만 켜진 색이 됩니다. 상태 줄(`galleryStatusLine`)은 불러오기 오류, 잠깐 뜨는 안내, 그리고 보관함 작업과 파일 관리 가운데 **글이 마지막으로 바뀐 쪽**을 보여 줍니다(`statusSource`). 이전 결과가 지금 진행 중인 일을 가리지 않게 하려는 것입니다. 선택 중 동작은 그리드 아래의 `gallerySelectionBar`에 있습니다.
- **확인 방법**: `tests/test_qt_look_qml.py`가 실제 창에서 화면마다 강조 버튼이 하나인지, 통계·단축키가 한 번에 열리는지, 선택을 끝내는 컨트롤이 하나인지, 풍선말이 상태를 따라가는지, 설정이 고급을 펼친 채 스크롤 없이 들어오는지를 검사합니다. `--shots 폴더`를 주고 `QT_QPA_PLATFORM` 없이 실행하면 실제 Windows 창의 화면을 단계마다 저장합니다.

### 기능을 바꿀 때 함께 볼 것

새 기능이나 변경이 기존 기능의 전제를 바꾸는지 아래 짝을 확인하고, 바뀌면 같은 릴리즈에서 함께 고칩니다.

- 목록에서 항목이 빠지거나 순서가 바뀌는 변경 ↔ 제자리 삭제(`removeDeleted`), 다음 페이지 커서, 미리보기 이전·다음, 여러 개 선택.
- 사진의 분류·태그·중복 여부를 바꾸는 작업 ↔ 통계 창, AI 분류 메뉴의 개수, 중복 배지, 열려 있는 미리보기의 표시.
- 저장 엔진이 미디어를 건너뛰거나 실패하는 경로 ↔ 저장 못 한 미디어 목록과 그 미리보기, 실행 기록의 개수, 전체 확인에서의 재방문.
- 새 화면 요소(목록·팝업·메뉴) ↔ 휠 처리기, 글자 단축키 차단(`modalOpen`·`listEngaged`·`menuOpen`), 앱을 닫을 때 닫는 창 목록, 단축키 안내.
- 새 화면이나 대화상자 ↔ `Theme` 값만 쓰는지, `FlatDialog` 틀과 `DialogButtons`, 강조 버튼이 하나인지, 풍선말은 `tip`인지(`test_qt_theme`, `test_qt_look_qml`).
- QML로 넘기는 값의 자료형과 설정 키 ↔ 이전 버전이 읽어도 깨지지 않는지, 손상된 값의 기본값 복구.

미리보기·저장 전 확인창은 부모 오버레이에서 24px 여백을 남기고 확장합니다. 헤더의 창 최대화·복원은 호스트 앱 창을 제어합니다. 갤러리 본문은 기본으로 접고 필요할 때 펼치며, 이미지 비율과 미디어 해제·저장 승인 순서는 유지합니다.

휠·버튼 확대/축소, 드래그 이동, 화면 맞춤과 지원하는 정지 사진의 원본 배율을 제공합니다. 이전·다음은 현재 검색·정렬 순서를 따르고 끝에서 순환하지 않습니다. 입력란의 방향키를 가로채지 않으며 새 파일·닫기·숨김에서 확대·이동을 초기화합니다. 저장 화면의 자세히 보기는 최근 실제 저장 파일의 첫 프레임·작성자·본문·게시 시각·원문을 표시합니다. 숨겨진 동안 디코딩하지 않고 현재 요청 하나와 가장 최근 대기 하나만 유지하며 추가 X 요청을 보내지 않습니다.

## 단일 EXE와 실행기

릴리스는 `LuvBox.exe` 하나입니다. 구성은 `실행기 프로그램 + 앱 폴더의 ZIP + 84바이트 꼬리표`이고, 꼬리표(`luvbox/launcher.py`의 `FOOTER`)에 ZIP의 위치·길이·SHA-256과 버전이 있습니다. 실행기(`tools/launcher/Launcher.cs`)는 Windows에 들어 있는 C# 컴파일러(`Framework64\v4.0.30319\csc.exe`, C# 5)로 빌드하므로 그 문법만 쓰고, 실행에는 Windows 10·11의 .NET Framework 4.x를 씁니다. PyInstaller의 한 파일 방식은 실행할 때마다 266MB를 임시 폴더에 다시 풀어 시작이 4.4초에서 8.8초로 늘어 쓰지 않습니다.

- **시작**: 꼬리표의 해시와 `%LOCALAPPDATA%\LuvBox\app\<버전>\.luvbox-app`에 적힌 해시가 같고 그 폴더에 `LuvBox.exe`가 있으면 바로 실행합니다. 이때는 ZIP을 읽거나 해시하지 않습니다. 아니면 ZIP의 SHA-256을 확인하고 `app\.new-…` 임시 폴더에 푼 뒤 폴더 이름을 `<버전>`으로 바꿉니다. 확인부터 풀기까지 같은 파일 핸들을 쓰기 금지로 열어 둡니다. 풀기는 최대 4개 스레드로 합니다.
- **동시 실행**: `app\.lock`을 독점으로 열어 한 번만 풉니다. 기다린 쪽은 끝난 결과를 그대로 씁니다.
- **경로와 링크**: ZIP 항목 이름은 상위 폴더(`..`)·절대 경로·드라이브·`:`·역슬래시·예약 이름·끝의 점과 공백을 거부하고, 풀 위치가 임시 폴더 안인지 다시 확인합니다. `LuvBox`, `app`, 버전 폴더가 링크(junction 등)면 중단합니다. `tools/build_launcher.py`도 묶기 전에 같은 규칙과 링크·hardlink를 검사하고, 앱 안의 가장 긴 상대 경로를 140자로 제한합니다(Windows의 260자 한도).
- **정리**: 새 버전을 풀고 나면 지금 버전과 바로 이전 버전만 남깁니다. 실행기가 남긴 표시(`.luvbox-app`)가 있는 버전 폴더만 지우며, 실행 중인 버전은 폴더 이름을 바꿀 수 없어 그대로 남습니다.
- **인자와 종료 코드**: 인자가 없으면 시작 화면을 보이고, 앱 창이 처음 그려졌다는 신호(`LUVBOX_READY_EVENT` 이름의 이벤트)를 받으면 끝납니다. 인자가 있으면(`--run-saved`, `--self-test`, `--download-smoke`, `--qt-smoke` 등) 화면 없이 명령줄을 고치지 않고 넘기고 앱의 종료 코드를 돌려줍니다. 실행기 자신의 실패는 70~77입니다(70 앱 없음, 71 손상, 72 풀기 실패, 73 링크된 폴더, 74 다른 실행기를 기다리다 시간 초과, 75 시작 실패, 76 같은 버전 실행 중, 77 풀기 직전에 `LuvBox.exe`가 다른 버전으로 바뀜). 실행기는 자기 파일을 확인할 때 연 핸들 하나로 끝까지 풀고, 그 뒤로는 `LuvBox.exe`를 이름으로 다시 열지 않습니다. 안내 창은 일반 실행과 예약 실행에서만 띄웁니다.
- **예약 작업이 등록하는 경로**: 앱은 실행기 경로를 `LUVBOX_LAUNCHER` 환경 변수로 받고(`launcher.launcher_path()`), 자동 저장 예약은 이 경로와 `--run-saved`를 등록합니다(`scheduled.launch_parts`). `sys.executable`은 버전마다 바뀌는 풀린 사본이고, 실행기 경로는 업데이트가 같은 자리에서 파일만 바꾸므로 그대로입니다. 저장 worker는 지금처럼 `sys.executable`(풀린 사본)을 직접 실행합니다. 풀린 사본을 실행기 없이 직접 실행하면 이전처럼 `sys.executable`을 씁니다.
- **작업 표시줄**: 창을 고정하면 실행기가 고정되도록 창에 다시 시작 명령을 적어 둡니다(`launcher.mark_window`). 실행기와 앱은 같은 AppUserModelID를 써서 한 단추로 묶입니다.
- **검사할 때**: 실행기는 `LOCALAPPDATA` 환경 변수를 먼저 보므로 임시 폴더로 바꿔 실행합니다. `LUVBOX_LAUNCHER_SILENT=1`은 안내 창 없이 종료 코드만 돌려줍니다. `tests/test_launcher.py`가 실제 실행기를 컴파일해 작은 대역 앱으로 첫 실행 풀기·재실행·손상 거부·인자와 종료 코드·동시 실행·이전 버전 정리·링크 거부를 끝까지 실행합니다.

### 시작 화면과 문구

시작 화면의 그림 세 장은 `tools/draw_splash.py`가 그리고(`assets/splash/`), 문구와 위치는 `tools/launcher/splash.ini` 한 곳에 있습니다. 실행기가 글자·진행 막대·버전을 직접 그립니다. 문구는 지금 일어나는 일을 한 줄로 말하고(“보관함을 여는 중”), 덧붙이는 말은 짧은 해요체로 씁니다(“한 번만 걸려요”). 앱 안의 빈 화면과 불러오는 화면도 같은 말투입니다(“아직 사진이 없어요 · 저장 화면에서 좋아요를 담아 보세요”). 오류·확인·진행 안내는 기존처럼 원인과 할 일을 분명히 적습니다.

## 업데이트

`luvbox/updater.py`가 GitHub의 최신 릴리스 설명을 읽어 버전을 숫자로 비교하고, 사람이 **업데이트**를 누를 때만 `LuvBox.exe`와 `SHA256-<버전>.txt`를 받습니다. 체크섬 파일의 서명을 먼저 확인하고(아래 ‘릴리스 서명’), 받은 파일의 SHA-256이 체크섬 파일의 `LuvBox.exe` 줄과 같고 파일 꼬리표의 버전이 그 릴리스와 같을 때만 실행기 자리에 둡니다. 교체는 실행기와 같은 폴더에 복사 → 기존 파일을 `LuvBox.exe.old`로 이름 변경 → 새 파일을 제자리로 이름 변경이며, 마지막 단계가 실패하면 되돌립니다. 남은 `.old`는 다음 시작 때 실행기가 지웁니다. 실행 중인 앱은 자기 버전 폴더에서 돌고 있어 영향을 받지 않고, 다시 시작하면 새 실행기가 새 버전을 풀어 실행합니다.

- 자동 확인은 패키지된 앱에서만, 시작할 때 하루에 한 번입니다. 소스 실행과 시작 검사, 테스트는 스스로 GitHub에 접속하지 않습니다.
- 요청에는 고정 User-Agent(`LuvBox-Updater`)와 Accept만 넣습니다. 받는 주소는 버전에서 직접 만들고, 리다이렉트는 HTTPS의 `github.com`·`*.githubusercontent.com`만 따릅니다.
- 릴리스에 `LuvBox.exe`나 체크섬 파일이 없으면 받기 대신 릴리스 페이지를 열게 안내합니다. 실행기 없이 실행한 경우도 같습니다.
- 알림의 변경 사항은 릴리스 설명에서 가져옵니다. 제목에 ‘변경’이 들어간 항목(‘주요 변경 사항’) 아래의 맨 위 목록을 최대 8줄까지 보여 주므로 릴리스 설명은 이 형식을 유지합니다.
- **릴리스 첨부 이름과 체크섬 형식은 설치된 버전과의 약속입니다.** `LuvBox.exe`, `SHA256-<버전>.txt`(`<소문자 64자리 해시><공백 두 칸><파일 이름>` 줄), `LuvBox-<버전>-source-licenses.zip`을 `updater.release_assets`·`format_checksums`가 정하고 패키징이 그대로 쓰며, `tests/test_release_contract.py`가 글자 그대로 고정합니다. 바꾸면 이미 설치된 버전이 다음 릴리스를 받지 못합니다.
- **릴리스 서명**(1.9.2부터): 체크섬 파일의 셋째 줄 `# luvbox-signature v1 <키 이름 16자> <base64>`는 `LuvBox release <버전>` 한 줄과 체크섬 줄들(앱이 읽는 그대로)에 대한 RSA PKCS#1 v1.5 SHA-256 서명입니다. `luvbox/release_signature.py`가 형식과 믿는 공개 키(`RELEASE_KEYS`)를 갖고, 확인은 정수 연산뿐이라 추가 패키지가 없습니다. 앱은 서명이 믿는 키의 것이고 이 버전·이 줄들에 대한 것일 때만 프로그램을 받습니다. 1.8.0~1.9.1은 이 줄을 행으로 보지 않아 그대로 동작합니다.
- **서명 키**: 개인 키는 관리자 Windows 계정의 키 저장소(CNG)에 `LuvBoxReleaseSigning`이라는 이름으로만 있고, `tools/release_key.ps1`은 만들기·공개 부분 읽기·서명만 하며 개인 키를 파일이나 화면으로 내보내지 않습니다. 저장소·로그·배포 파일에 넣지 않습니다. `build.ps1`은 시작할 때와 패키징할 때 이 키가 있고 `RELEASE_KEYS`에 든 키인지, 실제로 서명되는지 확인하며(`tools/release_key.py --check`), 아니면 멈춥니다. 서명 없는 릴리스는 설치된 버전이 받지 않기 때문입니다.
- **키를 잃거나 바꿀 때**: 설치된 버전은 자기에게 들어 있는 공개 키만 믿습니다. 새 키는 `RELEASE_KEYS`에 더해 **이전 키로 서명한 릴리스로 먼저 배포**하고, 그다음 릴리스부터 새 키로 서명합니다. 이전 키를 이미 잃었다면 그 뒤 버전은 앱 안 업데이트가 멈추고 릴리스 페이지에서 한 번 직접 받아야 합니다. `RELEASE_KEYS`에서 키를 빼거나 바꾸지 않습니다(`tests/test_release_contract.py`가 고정).
- **키 백업과 복원**: `tools/release_key_backup.ps1 -Export <파일>`이 개인 키를 암호로 잠근 PKCS#8 파일로 내보내고, 곧바로 되읽어 파일과 암호가 맞는지 확인합니다. 릴리스 키의 암호는 콘솔에서만 받으므로(12자 이상, 두 번) 사람 없이 실행할 수 없고, 프로젝트 폴더 안이나 이미 있는 파일에는 쓰지 않습니다. `-Verify <파일>`은 백업이 그 암호로 열리는지와 어느 키인지 알려 주고, `-Import <파일>`은 새 PC나 Windows 재설치 뒤에 키 저장소로 되돌립니다(같은 이름의 키가 있으면 거부). 복원한 뒤 `python tools\release_key.py --check`로 앱이 믿는 키인지 확인합니다. 백업 파일과 암호를 함께 가진 사람은 릴리스에 서명할 수 있으니 둘을 서로 다른 곳에 두고, 저장소·릴리스·로그에 넣지 않습니다. 암호화는 Windows가 저장된 키에 제공하는 PKCS#12 방식(3키 3DES, 20만 회 반복)이라 암호가 약하면 파일도 약합니다.

## 데이터와 인증 호환성

설정·암호화 세션·기록·백업·미리보기는 `%LOCALAPPDATA%\LuvBox`에 있습니다. 1.9.4는 설정 파일·기록 DB·릴리스 형식을 바꾸지 않습니다(한 파일의 기록이 여러 개여도 지우거나 합치지 않고, 읽을 때 하나로 다룹니다). 1.9.3은 설정 파일·기록 DB·릴리스 형식을 바꾸지 않습니다. 1.9.2는 설정 파일·기록 DB를 바꾸지 않고 체크섬 파일에 서명 한 줄을 더합니다(이전 버전은 건너뜁니다). 1.9.1은 설정 파일·기록 DB·릴리스 첨부 형식을 바꾸지 않으며, 시작할 때 `updates`에 10분 넘게 남아 있던 받다 만 파일을 지웁니다. 1.9.0은 화면만 바꾸며 설정 파일·기록 DB·릴리스 첨부 형식을 바꾸지 않습니다. 1.8.0은 설정 파일에 업데이트 확인 값(`update_check`, `update_checked_at`, `update_found`)을 더하고, 같은 폴더 아래에 `app`(실행기가 풀어 둔 앱)과 `updates`(받는 중인 파일)를 씁니다. 기록 DB는 바꾸지 않으며 이전 버전은 이 값을 무시합니다. 이전 이름(XlikeSaver)의 데이터 폴더가 남아 있고 LuvBox 폴더가 없거나 비어 있으면 실행기가 `app`을 만들기 전에 앱과 같은 방식으로 폴더 이름을 바꿉니다. 그러지 않으면 앱이 두 폴더가 모두 쓰이는 것으로 보고 멈춥니다. 1.7.0은 설정 파일에 창 크기·위치(`window`)를, 기록 DB의 `catalog_meta`에 중복 묶음 방식(`duplicate_rule`) 한 줄을 더합니다. 표 구조는 바꾸지 않으며 이전 버전은 두 값을 무시합니다. 1.6.9·1.6.10은 설정 파일에 갤러리 썸네일 크기(`gallery_density`, 0·1·2)만 더하며 기록 DB 구조는 바꾸지 않습니다. 이전 버전은 이 값을 무시하고 보존합니다. 미디어 저장 폴더의 `.luvbox` 사본과 이전 파일명·archive·암호화 형식·예약 이름을 계속 인식합니다. UI 변경을 이유로 기존 파일·DB·계정을 초기화하지 않습니다. 폴더 이전의 명시적 승인 경로를 보존하고, 다른 PC의 기록 사본이 주장하는 외부 경로에 관리 권한을 부여하지 않습니다.

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

모든 테스트 파일은 `luvbox`를 불러오기 전에 `from tests import app_data_isolation`을 먼저 불러옵니다(`tests/test_app_data_isolation.py`가 파일마다 확인합니다). 전체 실행에서는 한 파일만 불러와도 프로세스 전체가 격리되지만, 파일 하나만 따로 실행하면 그 파일이 직접 불러와야 합니다. 1.9.3까지는 그 줄이 없는 파일을 따로 돌리면 실제 `%LOCALAPPDATA%\LuvBox`에 테스트용 기록 폴더와 기록 줄이 생겼습니다. 격리 모듈은 그 뒤 앱 데이터 폴더가 실제 폴더로 돌아가면(환경 변수를 되돌리거나 지워도) 예외를 내고, 삼켜진 경우에도 프로세스를 오류로 끝냅니다. 테스트가 띄운 작업 프로세스는 부모가 정한 임시 폴더를 그대로 씁니다. 함수 안에서만 `luvbox`를 불러오고 스크립트로도 실행되는 장면 테스트는 `try: from tests import … except ModuleNotFoundError: import app_data_isolation` 꼴을 씁니다.

Qt는 `LOCALAPPDATA`를 읽지 않습니다. 컴파일한 QML(`.qmlc`)을 두는 캐시 폴더를 Windows에 직접 물어보므로, `LOCALAPPDATA`만 바꾸면 그 캐시는 실제 폴더에 남습니다. 1.9.4까지는 테스트가 실제 `%LOCALAPPDATA%\Python\cache\qmlcache`에, 묶은 `LuvBox.exe`의 시작 점검이 실제 `%LOCALAPPDATA%\LuvBox\LuvBox\cache\qmlcache`에 캐시 파일을 썼습니다(기록·설정이 아닌 캐시입니다). 격리 모듈은 `QML_DISK_CACHE_PATH`를 임시 앱 데이터 폴더 안의 `qml-cache`로 정하고(앱 폴더 `LuvBox` 옆이며, 테스트가 띄운 자식 프로세스가 물려받아 함께 씁니다), `tools/check_qt_startup.py`는 점검 폴더 안의 `qml-cache`로 정합니다. 소스에서 `--qt-smoke`·`--self-test`를 직접 실행해 이 변수가 없을 때는 `qt_smoke.py`가 `QML_DISABLE_DISK_CACHE=1`로 캐시를 아예 쓰지 않습니다(`tests/test_qt_smoke_isolation.py`). Qt는 이 값을 프로세스가 처음 QML을 읽을 때 한 번만 읽으므로 그 전에 정해야 합니다. 격리 폴더로 앱이나 QML 창을 띄우는 스크립트는 `LOCALAPPDATA`와 함께 이 변수도 정합니다. 실제 폴더를 건드리지 않았는지 전후 목록으로 확인할 때는 `%LOCALAPPDATA%\LuvBox`뿐 아니라 `%LOCALAPPDATA%\Python\cache`도 함께 봅니다.

기록을 읽어 파일을 지우거나 묶거나 세는 기능(중복, 삭제, 통계, 복구, 폴더 구성)은 생성한 시험 데이터만으로 끝내지 않습니다. 실제 기록의 **사본**으로 모양을 먼저 보고(원본은 읽기만, `?mode=ro&immutable=1`로 열면 옆에 `-wal`/`-shm`을 남기지 않습니다), 경로만 바꾼 복제본에서 기능을 돌립니다. 기록 한 줄은 파일 하나가 아닙니다. 한 파일은 폴더 구성 방식마다 기록을 가질 수 있어(작성자별로 저장한 뒤 한 폴더로 옮기면 두 기록이 같은 경로를 가리킵니다) 사람이 보는 목록과 수는 `history.py`의 `_listed`·`_shadowed`로 파일 하나를 한 번만 셉니다. 파일을 "사본"으로 지우는 동작은 그 순간 내용이 같은 다른 실제 파일이 있는지 확인합니다(`deletion.kept_copy`).

장면 테스트에서 `QTest.keyClick`·`QTest.mouseClick`은 Qt가 그 입력을 처리하는 동안 파이썬 전역 잠금(GIL)을 놓지 않습니다(`processEvents`와 실제 이벤트 루프는 놓습니다). 그 사이 Qt의 이미지 스레드가 파이썬 썸네일 공급자를 부르려고 기다리면, 메인 스레드와 서로 기다리며 멈출 수 있습니다. 썸네일 크기를 바꾸는 키에서 새로 시작한 200회 중 9회 멈췄고, 키 이벤트를 `QCoreApplication.postEvent`로 넣고 `processEvents`로 전달하면 200회 중 0회였습니다. 썸네일이 한꺼번에 다시 요청되는 입력(크기 변경 등)은 `tests/test_qt_gallery_recycle_qml.py`의 `press_key`처럼 보냅니다. 앱은 실제 이벤트 루프로 입력을 받으므로 해당하지 않습니다.

갤러리의 `DragHandler`가 알려 주는 위치는 스크롤되는 내용 좌표입니다(스크롤 양이 이미 들어 있고, 포인터가 멈춘 채 스크롤되면 낡습니다). 보이는 영역 기준 위치는 `scenePosition`을 `gallery.mapFromItem(null, …)`으로 옮겨 구합니다. 끌어서 선택은 맨 위뿐 아니라 스크롤한 상태에서도 검사합니다(`tests/test_qt_gallery_usability_qml.py`).

미리보기의 사진·GIF·영상 전환은 실제 창에서도 확인합니다. 아래 명령은 `QT_QPA_PLATFORM`을 지정하지 않아 실제 Windows 창을 열고 전환마다 스크린샷을 저장합니다. 테스트가 쓰는 오프스크린 백엔드는 영상 픽셀을 그리지 않으므로 영상이 화면에 나오는지는 이 방법으로만 눈으로 확인할 수 있습니다.

```powershell
.\.venv\Scripts\python.exe tests\test_qt_preview_media_switch.py --scene --screenshots '.test-output\preview-shots'
```

## 빌드와 배포 자료

```powershell
.\build.ps1
```

`build.ps1`은 고정 입력·ExifTool·FFmpeg와 Qt 원본 소스·라이선스를 준비하고 PyInstaller 폴더형 앱(`build/app/LuvBox/`)을 만든 뒤, `tools/build_launcher.py`로 실행기를 컴파일해 그 폴더를 `build/single/LuvBox.exe` 하나로 묶습니다. Windows System32를 우선해 잘못된 ICU DLL 수집을 막습니다. 빌드 Python이 요구하는 선택적 SSL 보조 파일은 해당 정책을 보존한 채 동봉합니다.

Windows OpenSSL DLL은 빌드 Python의 `DLLs`에서 명시적으로 수집합니다. 패키징도 COLLECT의 원본 경로와 실제 번들 바이트를 대조하므로 PATH에 있는 다른 도구의 같은 이름 DLL이나 교체된 파일을 거부합니다.

묶은 `LuvBox.exe`를 실행기부터 시작하는 Qt 창·갤러리·미리보기·설정·연결창·종료 검사와 오프라인 저장 시작 검사를 모두 통과해야 배포 파일을 만듭니다. 두 검사는 `build/startup-check-<버전>` 폴더를 `LOCALAPPDATA`로 주어 실행하므로 개발자의 실제 LuvBox 데이터에는 풀지 않고, 둘째 검사는 첫째가 풀어 둔 사본을 다시 씁니다. 직접 패키징도 현재 버전과 그 `LuvBox.exe`의 SHA-256에 일치하고 실행기를 거쳤다고 적힌 두 검사 보고서를 요구하며, `LuvBox.exe` 안의 ZIP이 봉인한 앱 폴더와 파일 하나하나 같은지 대조합니다. 소스와 라이선스는 EXE에 넣지 않고 같은 릴리스의 ZIP으로 제공합니다. Qt DLL은 풀린 폴더에 따로 남아 교체할 수 있습니다. Qt와 미디어 구성요소의 버전별 원본 자료는 [Qt 소스 안내](../tools/qt/README.md), [FFmpeg 안내](../tools/ffmpeg/README.md)를 따릅니다. 검사 실패를 피하려고 입력 체크섬·TLS 검증·패키징 허용 목록을 완화하지 않습니다.

앱이 읽지 않는 구성요소는 의도적으로 싣지 않습니다. Qt 번역 파일은 `tools\pyinstaller\hook-PySide6.py`가, Pillow의 AVIF 디코더는 `--exclude-module PIL._avif`가, ExifTool의 HTML 설명서 폴더는 `tools\exiftool\download.py`가 뺍니다(앱은 번역기를 설치하지 않고 모든 이미지 열기가 형식을 직접 지정합니다). 패키징은 번역 파일이 다시 수집되면 거부합니다. ExifTool의 `t` 폴더는 이 프로젝트의 시험이 실제 MP4/MOV 샘플(`t/images/QuickTime.mov`)로 읽으므로 남깁니다. 구성을 바꾼 뒤에는 전체 시험과 실제 EXE의 `--self-test`로 메타데이터·미디어 기능을 다시 확인합니다.

산출물은 `release/LuvBox.exe`, `release/SHA256-1.9.4.txt`, `release/LuvBox-1.9.4-source-licenses.zip`입니다. 이 세 파일을 그 이름 그대로 GitHub 릴리스에 올립니다. 빌드는 시작할 때, 직접 패키징은 실행할 때 README·사용 안내·이 문서의 제목과 산출물 이름·변경 사항 최신 항목이 제품 버전과 같은지 확인하고, 다르면 멈춥니다(`tools/package_release.py --check-guides`의 `require_current_guides`, `tests/test_guide_versions.py`). 빌드 단계의 격리 검사는 실제 X 인증·다운로드나 사용자 예약 변경을 대신하지 않습니다.

릴리스 커밋과 태그를 만들기 전에 `git fetch` 뒤 `origin/main`과 비교합니다. 원격에 새 커밋이 있으면 먼저 병합해 다시 검사·빌드하고, 브랜치 푸시가 성공한 뒤에만 태그를 올립니다(1.6.9는 태그를 먼저 올린 뒤 원격의 병합 커밋을 발견해 배포하지 않았습니다). 올린 뒤에는 `LuvBox.exe`를 다시 받아 해시를 대조하고 격리된 `LOCALAPPDATA`로 시작 검사를 한 번 더 합니다. 릴리스의 이전 버전은 앱 안의 업데이트로 이 릴리스를 받으므로, 첨부 이름을 바꾸거나 빼지 않습니다.

`tools/build_snapshot.py`는 PyInstaller 직전 소스·QML·실행기 소스·배포 입력과 의존성 버전을 기록하고 빌드 후 EXE·실행 폴더 전체 파일의 상대 경로와 해시에 결합합니다. 패키징도 같은 기록을 검사하므로 소스·문서·의존성·번들 런타임이 바뀌면 다시 빌드합니다. 입력 기록과 시작 보고서는 `build/`, 생성 spec은 `build/spec/`에 두며 소스 최상위에 버전별 spec을 쌓지 않습니다. 프로젝트의 허용 소스 파일과 상위 폴더는 링크·junction·hardlink가 없어야 합니다.

설정 › 고급은 단일 일일 예약 생성·시간 변경·해제, 모든 계정의 실행 기록, 저장 못 한 미디어 목록을 제공하고, 갤러리는 작성자 드롭다운·달력 날짜 필터와 휴지통 이동을 제공합니다. 작성자·날짜 필터는 바꾸는 즉시 적용됩니다.

`unsaved.py`의 기록부(`%LOCALAPPDATA%\LuvBox\unsaved.sqlite3`)는 저장 엔진이 용량 제한·내려받기 실패·‘저장 안 함’ 선택을 만난 미디어를 계정·저장 폴더·미디어 식별자 단위로 한 줄씩 보관합니다. 계정별 저장 기록(카탈로그)의 스키마와 병합·복구 경로는 바꾸지 않으며, 같은 미디어를 다시 만나면 한 줄만 갱신하고 저장에 성공하면 지웁니다. 보관은 최대 500건입니다. 다시 저장은 기존 `JobOptions.targets` 경로(트윗마다 요청 1번)를 사용합니다. Windows 휴지통에서 원본을 직접 복원하는 UI와 확장 스토어 등록은 별도 기능 범위입니다. 확장 없는 WebEngine 로그인은 합성 실험 단계이며 제품 런타임에 포함하지 않습니다. 날짜별 검토 보고서는 현재 안내에 통합하고 원문은 Git 태그·기록에 보존합니다.
