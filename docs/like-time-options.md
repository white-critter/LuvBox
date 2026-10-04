# Like 시각 기록 방법 비교

검토일: 2026-10-04. 이 문서는 선택을 위한 설명이며 아래 기록 기능은 아직 구현하지 않았습니다.

현재 선택: **프로그램이 사진별 저장 완료를 기억하고 다음 실행에서 건너뛰는 방식**입니다. Like 시각의 기록이나 날짜 범위 선택은 사용하지 않습니다. 아래 내용은 이전에 검토한 대안의 참고 자료입니다.

현재 Like 목록은 게시물 ID와 게시 시각 등을 제공하지만, 사용자가 좋아요를 누른 실제 시각을 제공하지 않습니다. 게시물 ID에서 계산한 시각 역시 게시 시각입니다. 목록 순서만으로 `10월 2일 22:00 ~ 10월 4일 22:10` 같은 범위를 복원할 수 없습니다. gallery-dl도 신뢰할 수 없던 `date_liked` 정보를 제거했습니다. [공식 Like 목록 API](https://docs.x.com/x-api/users/get-liked-posts), [gallery-dl 변경 기록](https://github.com/mikf/gallery-dl/releases/tag/v1.26.5)

## 1. Chrome 확장으로 앞으로의 행동 기록

Chrome의 X 화면에서 사용자가 직접 하트를 누르면, 게시물 ID·PC에서 관측한 시각·Like 또는 취소를 로컬에 기록합니다. 클릭만 기록하면 요청 실패도 성공으로 오인하므로 상태 변화나 성공 여부를 확인해야 합니다. 범위 검색은 확장을 설치한 이후 기록된 항목에 적용합니다.

별도 서버 없이 사용할 수 있습니다. 처음에는 기록 파일을 내보내 Windows 앱으로 가져오는 방법이 단순하고, 이후 Chrome Native Messaging으로 자동 연결할 수 있습니다. 기록 자체는 X에 주기적으로 목록 조회를 할 필요가 없습니다. 사진을 내려받는 요청은 별도로 발생합니다.

이 시각은 **PC에서 관측한 행동 시각**입니다. PC 시계 오차와 통신 지연이 있어 X 서버의 정확한 처리 시각과 같다고 보장하지 않습니다. 확장이 설치된 Chrome 프로필에서 한 행동만 기록하므로 모바일 앱·다른 PC·다른 브라우저·확장이 꺼진 기간의 Like는 놓칩니다. 과거 시각도 복원하지 못합니다. X 화면이 바뀌면 수정이 필요합니다.

자동 Like나 자동 스크롤을 하지 않는 로컬 기록으로 설계할 수 있지만, 이 방법을 X가 승인한 공식 통합이라고 설명할 수는 없습니다. 기존 비공식 사진 수집기의 정책 문제도 별도로 남습니다. [X 개발자 지침](https://docs.x.com/developer-guidelines), [자동화 규칙](https://help.x.com/en/rules-and-policies/x-automation)

## 2. 공식 X Activity API(XAA)의 이벤트 구독

개발자 계정과 App을 준비하고 본인 계정이 OAuth 2.0으로 `like.read` 접근에 동의합니다. `like.create`의 **outbound** 방향으로 구독하면 내가 다른 게시물에 누른 Like를 받습니다. inbound는 다른 사람이 내 게시물에 누른 Like이므로 이 목적에 맞지 않습니다. 현재 문서는 self-serve 구독도 안내하므로 무조건 Enterprise 전용이라고 가정하지 않습니다. [XAA 소개](https://docs.x.com/x-api/activity/introduction), [구독과 권한](https://docs.x.com/x-api/activity/create-x-activity-subscription)

이벤트를 지속 연결(HTTP stream) 또는 HTTPS webhook으로 받아 기록합니다. 계정 단위 이벤트이므로 모바일에서 누른 Like도 받을 수 있는 설계입니다. 실제 개발자 계정에서 접근 권한과 전달 내용을 확인해야 합니다. PC 프로그램에서 stream을 받을 수 있지만 PC나 앱이 꺼진 동안 공백이 생깁니다. 계속 기록하려면 상시 실행되는 PC 또는 webhook 서버가 필요합니다.

이벤트의 `timestamp_ms`가 행동 시각에 해당하고, 게시물의 `created_at`은 게시 시각입니다. 공식 문서에서 `timestamp_ms`는 **제공되는 경우에만** 있다고 안내합니다. 누락되면 수신 시각을 별도로 보관하고 ‘수신 시각’ 또는 ‘추정’으로 표시해야 하며, 정확한 Like 시각으로 취급하면 안 됩니다. [Like 이벤트 필드](https://docs.x.com/x-api/activity/event-payloads#likecreate)

과거 Like 전체를 복원하는 서비스는 아닙니다. stream의 짧은 backfill과 webhook 재전송 기능이 있지만, webhook replay는 최근 24시간 중 해당 webhook으로 전달했거나 전달을 시도했던 이벤트만 복구합니다. 구독 전의 과거 전체 기록을 가져오는 기능과 구분해야 합니다. [stream 복구 옵션](https://docs.x.com/x-api/activity/activity-stream), [webhook replay](https://docs.x.com/x-api/webhooks/create-replay-job-for-webhook)

현재 X API는 선불 크레딧 종량제입니다. 공개 webhook 가격표에 `like.create`의 단가가 명시되어 있지 않으므로 무료라고 단정하지 않습니다. 결정 전에 개발자 콘솔에서 실제 권한과 비용을 확인해야 합니다. 게시물·미디어 정보 조회나 서버 운영 비용도 별도입니다. 이전 Account Activity API는 deprecated 상태이므로 새 구현 후보는 XAA입니다. [요금](https://docs.x.com/x-api/getting-started/pricing), [이전 API 안내](https://docs.x.com/x-api/account-activity/introduction)

## 다른 방법

| 방법 | 날짜 정보의 의미 | 한계 |
| --- | --- | --- |
| 주기적인 Like 목록 비교 | 이전 조회와 새 조회 사이에 추가되었다는 시간 구간 | 실제 Like 시각은 불명. 조회 사이 Like 후 취소하면 놓침. 잦은 조회는 요청과 비용 증가 |
| 공식 API를 연결한 앱에서 사용자가 직접 Like 버튼 누르기 | 앱의 요청 및 성공 확인 시각 | 해당 앱에서 한 행동만 기록. 서버의 실제 행동 시각은 별도 이벤트가 필요 |
| 게시물 URL을 앱으로 보내 보관하기 | 앱에서 보관한 시각 | Like 시각이 아니며 수동 보관 행동이 추가됨 |
| X 데이터 아카이브 가져오기 | 과거 Like 목록을 확보할 후보 | Like 시각과 해당 사진까지 포함된다고 보장할 수 없음. 실제 아카이브의 필드를 확인해야 함 |

주기적 비교 예: 같은 항목을 22:00에 확인했을 때는 없고 22:10에 처음 보이면, 목록이 정상적으로 완전히 제공된다는 가정 아래 Like 시각을 `(22:00, 22:10]` 구간으로 추정할 수 있습니다. 목록 누락이나 노출 범위의 변경으로 오래된 Like가 새로 보인 경우도 구분해야 합니다. 이를 ‘22:10에 Like했다’고 기록하면 부정확합니다. 사용자가 요청한 범위의 경계와 이 구간이 겹치는 항목은 확정 포함 여부를 알 수 없습니다.

공식 API에서 사용자가 명시적으로 누르는 Like 버튼은 자동·대량 Like와 구분됩니다. 이 방법은 별도 X 클라이언트 기능을 개발해야 하므로 현재 저장기에 기록만 추가하는 것보다 범위가 큽니다. [사용자 주도 Like 지침](https://docs.x.com/developer-guidelines)

공식 데이터 아카이브는 과거 목록을 로컬에서 읽어 조회 부담을 줄일 수 있는 후보입니다. X 도움말은 아카이브를 다운로드하는 방법을 설명하지만 Like 행동 시각의 필드를 보장하지 않습니다. 사용자 실제 파일을 확인하기 전에는 과거 시각 복원이 가능하다고 판단하지 않습니다. [X 아카이브 다운로드 안내](https://help.x.com/en/managing-your-account/accessing-your-x-data)

## 선택 기준

- PC Chrome에서 앞으로 누르는 Like만 기록해도 되고 비용과 개발 범위를 줄이고 싶다면 확장 프로그램 방식.
- 모바일까지 포함하고 공식 통합을 우선한다면 XAA의 실제 접근 권한과 비용을 확인한 뒤 이벤트 방식.
- 정확한 시각 대신 ‘두 조회 사이 새로 발견한 항목’이면 충분하면 현재 증분 저장 또는 낮은 빈도의 목록 비교.
- 과거 Like 시각까지 정확히 필요하다면 위 방법들은 해결하지 못합니다. 이미 존재하는 별도 기록이나 실제 아카이브에 시각 필드가 있는지 먼저 확인해야 합니다.
