# Chrome Web Store 제출 준비

2026-10-24 공식 문서 확인 기준입니다. 이 문서는 Chrome용 패키지와 제출 문안의 준비 자료입니다. 스토어 등록·업로드·심사·게시는 수행하지 않았습니다. 실제 등록된 항목 ID와 스토어 주소가 없으므로 앱에 설치 링크를 추가하지 않습니다. 공개되기 전에는 앱의 기존 수동 확장 설치 안내를 사용합니다.

## 로컬 제출 파일 만들기

프로젝트의 설치된 빌드 환경에서 다음 명령으로 로컬 파일만 생성합니다.

```powershell
.\.venv\Scripts\python.exe tools\package_browser_extension.py
```

출력 위치는 `release/browser-extension/`이며 다음 파일을 생성합니다.

| 파일 | 용도 |
| --- | --- |
| `LuvBox-browser-extension-<manifest version>.zip` | 개발자 대시보드에 직접 업로드할 확장 패키지 |
| `store-icon-128.png` | 실제 앱 아이콘에서 생성한 128×128 PNG |
| `SHA256SUMS.txt` | ZIP과 스토어 아이콘의 SHA-256 |

버전은 `assets/browser-extension/chromium/manifest.json`의 숫자형 `version`을 그대로 사용합니다. 앱 버전과 별도이며, 이미 업로드한 확장을 갱신할 때는 기존보다 큰 숫자를 설정해야 합니다. Chrome의 버전은 점으로 나눈 1~4개 정수이며 각 값은 0~65535, 불필요한 앞자리 0 없이 전체가 0이 아니어야 합니다. [공식 버전 규칙](https://developer.chrome.com/docs/extensions/reference/manifest/version)

빌더는 아래 7개 파일만 읽고, 기존 `assets/icons/luvbox.png`에서 PNG 아이콘 16·32·48·128px를 메모리로 생성합니다. 폴더 전체 검색, 브라우저 프로필·사용자 데이터 접근, 네트워크 요청은 하지 않습니다.

```text
assets/browser-extension/chromium/manifest.json
assets/browser-extension/chromium/background.js
assets/browser-extension/chromium/profile.js
assets/browser-extension/chromium/connect.js
assets/browser-extension/chromium/connect.html
assets/browser-extension/chromium/connect.css
assets/browser-extension/chromium/extension-page.js
```

ZIP의 최상위에 `manifest.json`과 확장 파일을 두며, `icons/icon-16.png`, `icons/icon-32.png`, `icons/icon-48.png`, `icons/icon-128.png`를 추가합니다. 패키지에 들어가는 manifest에만 `icons` 항목을 넣고 원본 파일은 바꾸지 않습니다. 개인정보 안내 초안과 제출 가이드는 확장 실행 파일에 포함하지 않습니다. 정렬 순서·파일 시각·권한·ZIP 저장 방식을 고정하므로 동일한 입력 파일과 고정된 Python/Pillow 환경에서 같은 ZIP 바이트를 생성합니다. 앱 아이콘의 실제 도형을 96×96 안에 배치하고 128×128 PNG에 투명 여백을 둡니다. [패키지 준비](https://developer.chrome.com/docs/webstore/prepare), [아이콘 요구사항](https://developer.chrome.com/docs/webstore/images)

## 등록 문안 초안

| 항목 | 문안 |
| --- | --- |
| 이름 | LuvBox 브라우저 연결 |
| 짧은 설명 | 사용자가 승인한 X 계정만 이 컴퓨터의 LuvBox에 연결합니다. |
| 단일 목적 | 현재 브라우저 프로필에서 사용자가 확인하고 승인한 X 로그인을 같은 컴퓨터의 Windows용 LuvBox 앱에 전달합니다. |
| 대상 환경 | Chrome 일반 프로필과 Windows용 LuvBox 앱. 확장 설치만으로 앱이 설치되거나 계정이 연결되지는 않습니다. |

상세 설명 초안:

> LuvBox 브라우저 연결은 Windows용 LuvBox 앱의 계정 연결을 돕는 확장입니다. 앱에서 연결을 시작한 뒤, 확장 화면에서 연결 코드와 현재 X 계정을 확인하고 전송을 승인합니다. 앱에서도 계정을 확인하고 최종 승인하면 연결을 저장합니다.
>
> 계정을 확인할 때 X 탭이 잠시 열렸다 닫힙니다. 승인한 X 로그인 쿠키와 계정 정보는 암호화된 형태로 같은 컴퓨터의 LuvBox에 전달됩니다. 확장에는 개발자 원격 수집 서버나 사용 분석 기능이 없습니다. X 비밀번호를 확장에 입력할 필요가 없습니다.
>
> Windows용 LuvBox 앱과 X에 로그인한 Chrome 일반 프로필이 필요합니다. 연결은 앱에서 해제할 수 있으며 확장은 Chrome의 확장 관리에서 제거할 수 있습니다. 문의나 후기에 비밀번호, 쿠키, 토큰 또는 연결 주소를 보내지 마세요.

앱의 실제 공개 다운로드 주소, 지원 연락처, 공개 개인정보처리방침 주소는 발행자가 확정해야 합니다. 존재하지 않는 주소를 문안이나 manifest에 넣지 않습니다.

## 개인정보 신고와 권한 사유

로컬 처리만 하더라도 개인정보 신고가 필요합니다. 아래 사유는 현재 코드에 근거한 초안이며 발행자는 제출 시 실제 대시보드의 데이터 범주와 일치시켜야 합니다. [공식 개인정보 신고 안내](https://developer.chrome.com/docs/webstore/cws-dashboard-privacy), [로컬 처리도 신고해야 한다는 FAQ](https://developer.chrome.com/docs/webstore/program-policies/user-data-faq)

| 권한·접근 | 제출 사유 |
| --- | --- |
| `cookies` | 사용자가 코드 확인 후 현재 계정 확인을 요청했을 때 x.com의 `auth_token`과 `twid`만 읽어 로그인과 계정 ID를 확인합니다. 전송은 별도 승인 후 수행합니다. |
| `https://x.com/*` | 본인 계정 확인용 X 탭에서 사용자 이름·표시 이름을 확인하고 해당 도메인의 로그인 쿠키에 접근하기 위해 필요합니다. |
| `http://127.0.0.1/*` | 사용자가 앱에서 시작한 연결 페이지를 확장 소유의 승인 페이지로 넘기고 암호화한 세션을 같은 컴퓨터의 앱에 전송합니다. 임시 포트는 연결마다 달라집니다. |
| 원격 코드 | 사용하지 않습니다. 실행 JavaScript는 ZIP 내부 파일이며 외부 스크립트를 불러오지 않습니다. |

신고할 데이터에는 인증 정보(`auth_token`), 개인 식별 정보(계정 ID·사용자 이름·표시 이름), 계정 확인을 위해 읽는 X 페이지의 계정 표시 정보가 포함됩니다. 일반 방문 기록을 수집하는 기능은 없습니다. 단순히 “데이터를 수집하지 않음”으로 신고하면 실제 로컬 처리와 맞지 않습니다. 세부 범주와 확인 문항은 발행자가 대시보드에서 검토해야 합니다.

공개 정책 초안은 `assets/browser-extension/PRIVACY.md`입니다. 실제 발행자와 지원 연락처를 확정한 뒤 누구나 열 수 있는 웹 주소에 게시하고 그 실제 주소를 대시보드에 입력해야 합니다.

## 아직 완료되지 않은 항목

| 항목 | 현재 상태와 필요한 다음 작업 |
| --- | --- |
| 개발자 계정 | 등록·공개 연락처·해당 등록 수수료 처리를 발행자가 직접 진행해야 합니다. 이번 준비 과정에서는 계정 생성이나 결제를 하지 않습니다. [공식 등록 안내](https://developer.chrome.com/docs/webstore/register) |
| 실제 스크린샷 | 아직 없습니다. 사용자가 실제 확장 화면에서 최소 1장, 1280×800 또는 640×400으로 촬영해야 합니다. 연결 코드·쿠키·연결 주소가 노출되지 않은 장면을 사용하세요. 가짜 로그인·승인 화면으로 대체하지 않습니다. [공식 이미지 안내](https://developer.chrome.com/docs/webstore/images) |
| 공개 정책·앱 주소 | 실제 개인정보처리방침 URL, 지원 연락처, 동작 확인용 LuvBox 다운로드 URL이 필요합니다. 이 저장소의 문안만으로 공개 URL이 생기지 않습니다. |
| 실제 동작 확인 | 개발 중 브라우저·사용자 계정을 실행하지 않았습니다. 제출 전 발행자가 본인의 일반 프로필에서 설치, 코드 확인, 계정 확인, 두 단계 승인, 취소·만료·해제를 확인해야 합니다. [공식 준비 안내](https://developer.chrome.com/docs/webstore/prepare) |
| 루프백 전송에 대한 정책 적용 | 현재 전송은 `http://127.0.0.1:<임시 포트>/session`을 사용하며 본문을 AES-GCM으로 암호화하고 HMAC-SHA-256으로 인증합니다. 공식 FAQ는 안전한 연결(예: HTTPS/WSS)을 요구합니다. 이 문서만으로 해당 HTTP 루프백 설계의 심사 수용 여부를 확정할 수 없으며, 발행자는 정확한 구조를 설명하고 정책 적용을 검토해야 합니다. 루프백 HTTP 자체가 금지된다고 단정하는 근거로 이 문서를 사용하지 않습니다. [공식 보안 처리 FAQ 8~9](https://developer.chrome.com/docs/webstore/program-policies/user-data-faq) |
| 심사·스토어 주소 | ZIP 생성은 제출 완료를 뜻하지 않습니다. 발행자가 업로드·신고·심사 요청을 직접 완료해야 하며 승인 여부와 일정은 미정입니다. 실제 등록 주소가 확정된 뒤에만 앱 설치 링크를 추가할 수 있습니다. [공식 게시 절차](https://developer.chrome.com/docs/webstore/publish) |

## 심사자용 동작 안내 초안

발행자는 먼저 실제 공개 LuvBox 다운로드 주소와 설치 방법을 이 안내에 보완해야 합니다. 개인 X 비밀번호나 로그인 쿠키를 제출 자료에 포함하지 마세요. 전용 테스트 계정이나 추가 접근 안내가 필요하면 공식 대시보드의 비공개 심사 안내를 확인하고 발행자가 별도로 결정해야 합니다.

1. Windows에서 LuvBox를 열고 Chrome의 일반 프로필을 기본 브라우저로 사용합니다. 발행자가 확인한 앱 배포본과 제출 확장을 설치합니다.
2. X 로그인은 X 사이트에서 진행합니다. 앱의 **브라우저 연결 → 브라우저에서 연결**을 누릅니다.
3. 확장 소유의 연결 화면에 표시되는 8자리 코드가 앱의 코드와 같은지 확인하고 체크합니다.
4. **현재 X 계정 확인**을 누르면 X 탭이 잠시 열렸다 닫히고 현재 계정이 표시됩니다.
5. 해당 계정과 전송 안내를 확인하고 확장의 **@아이디 연결**을 누릅니다. 앱으로 돌아가 계정을 확인하고 **이 계정 연결 승인**을 누릅니다.
6. 앱에서 **현재 계정 연결 해제**를 실행해 저장한 로그인을 제거할 수 있습니다. 확장 제거는 Chrome 확장 관리에서 진행합니다.

검토가 완료된 뒤에도 공개 여부는 발행자가 선택해야 합니다. 대시보드에는 심사 후 직접 게시하는 선택지가 있습니다. 이번 작업은 업로드나 심사 요청도 수행하지 않습니다. [공식 제출·게시 안내](https://developer.chrome.com/docs/webstore/publish)
