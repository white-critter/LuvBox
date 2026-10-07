# 2.0.2·2.1.0 패치 검토

검토일: 2026-10-07. 기준은 `2ac6c81` 이후의 `c19219c`(테마 캐시)와 `81faa7b`(사라진 파일·선택적 다시 받기)입니다. 제품 코드는 수정하지 않았습니다.

## 보완이 필요한 사항

아래 7건은 모두 오프라인 임시 파일·DB·가짜 추출기로 재현했습니다. 정상적인 기본 저장을 막는 상시 오류는 아니지만, 해당 조건에서는 사용자 선택과 다른 결과가 생깁니다. 우선순위는 모두 P2입니다.

### 1. 라이브러리와 다른 계정의 기록으로 다시 받기

- 위치: [history_gui.py](C:/Users/wclov/workspace/XlikeSaver/xlikesaver/history_gui.py:1952), [gui.py](C:/Users/wclov/workspace/XlikeSaver/xlikesaver/gui.py:1396).
- A 계정의 라이브러리를 연 뒤 메인 아이디를 B로 바꾸고 A의 사라진 파일을 다시 받으면, 콜백은 파일 키만 전달하고 메인은 현재 B 설정으로 작업을 시작합니다.
- 재현 결과: `selected_catalog_account=account_a`, `started_account=account_b`. 선택한 기록과 다른 계정/저장 위치에 새 파일·기록이 생길 수 있습니다.
- 선택의 계정·StoragePaths를 함께 전달하고, 메인 계정/경로가 달라졌으면 새로고침 후 다시 선택하도록 해야 합니다.

### 2. 저장과 숨김 선택의 동시 실행

- 위치: [history_gui.py](C:/Users/wclov/workspace/XlikeSaver/xlikesaver/history_gui.py:1878), [core.py](C:/Users/wclov/workspace/XlikeSaver/xlikesaver/core.py:1262).
- 저장 중에도 ‘다시 받지 않기·숨김 해제’가 가능하지만 worker는 시작할 때 읽은 숨김 목록을 계속 사용합니다. 반대로 숨김 worker가 시작된 직후에도 메인 저장을 시작할 수 있습니다.
- 평면 저장 파일을 삭제하고 작성자별 저장 중 해당 항목을 숨겼는데도 `ignored=True`, `saved=1`로 파일이 다시 생성됐습니다.
- 저장과 선택 기록 작업의 교차를 막고, 실행 상태가 바뀔 때 버튼을 즉시 갱신해야 합니다. 버튼 표시뿐 아니라 실제 실행 진입점에서도 확인이 필요합니다.

### 3. 비공개 트윗 하나가 다시 받기 전체를 중단

- 위치: [core.py](C:/Users/wclov/workspace/XlikeSaver/xlikesaver/core.py:404).
- 사용 중인 gallery-dl은 `TweetResultByRestId`에서 비공개 트윗을 `AuthRequired(message='Protected Tweet')`로 보고합니다. 로그의 예외 이름이 인증 오류 패턴에 걸려, 트윗 하나의 접근 불가가 전체 계정 인증 실패로 처리됩니다.
- 첫 트윗은 Protected, 다음 트윗은 다운로드 가능한 fixture에서 첫 트윗만 조회하고 `status=error`로 끝났습니다. 두 번째 파일은 받지 않았습니다.
- 트윗 자체의 비공개/연령 제한과 실제 세션 만료·계정 인증 오류를 구분해야 합니다. 실제 HTTP 인증 오류와 요청 제한의 전체 중단은 유지해야 합니다.

### 4. 숨김 선택이 저장 폴더 사본에 반영되지 않음

- 위치: [history_gui.py](C:/Users/wclov/workspace/XlikeSaver/xlikesaver/history_gui.py:1919).
- `_choice_worker`는 로컬 DB만 바꾸며 기존 `_publish(location)` 흐름을 호출하지 않습니다.
- 사본을 먼저 만든 뒤 숨기면 로컬 숨김은 1개, 저장 폴더 사본은 0개였습니다. 다음 저장 전에 다른 PC로 옮기거나 로컬 기록을 잃으면 방금 선택한 ‘다시 받지 않기’가 복원되지 않습니다.
- 작업 충돌을 막은 상태에서 선택을 기록하고 DB를 닫은 뒤 사본/백업도 갱신해야 합니다.

### 5. 부분 복원 후 중단하면 성공한 파일의 숨김이 남음

- 위치: [core.py](C:/Users/wclov/workspace/XlikeSaver/xlikesaver/core.py:1333).
- 숨긴 파일의 다시 받기가 성공하면 숨김을 해제하도록 되어 있지만, 해제는 전체 retry 루프가 정상적으로 빠져나온 뒤에만 실행됩니다. 사용자 중단 예외는 이 처리를 건너뜁니다.
- 두 파일을 숨기고 첫 파일을 복원한 뒤 중단했을 때 `saved=1`, 첫 파일 존재는 참이지만 두 항목 모두 숨김 상태였습니다.
- 이미 성공한 항목의 숨김 해제는 중단/오류로 끝나는 경로에서도 반영해야 합니다. 미처리 항목은 기존 선택을 유지해야 합니다.

### 6. 숨긴 5,000개가 남은 사라진 파일을 가림

- 위치: [history.py](C:/Users/wclov/workspace/XlikeSaver/xlikesaver/history.py:956).
- 숨김 필터보다 먼저 전체 목록을 5,000개로 자릅니다. 최신 5,000개를 숨기고 오래된 미숨김 1개를 남기면 통계에는 1개가 있지만 UI 목록은 ‘사라진 파일이 없습니다’가 됩니다.
- 숨김/미숨김 필터와 총개수를 반영하는 조회·페이지 처리가 필요합니다. 항목을 숨긴 뒤 다음 미숨김 항목이 나타나야 합니다.

### 7. 유효한 ZIP 안의 불완전 이미지 캐시가 앱 시작을 막음

- 위치: [theme.py](C:/Users/wclov/workspace/XlikeSaver/xlikesaver/theme.py:299).
- 캐시 검증은 PNG 접두어만 확인하며 필수 자산 목록과 PNG 디코딩 성공 여부는 확인하지 않습니다.
- 임시 ZIP에 정상 `card` 하나만 넣으면 `KeyError('button')`, 전체 자산 중 `card` PNG를 16바이트로 줄여 넣으면 Tk의 이미지 디코딩 오류로 시작이 실패했습니다. 일반 ZIP 손상 검사는 통과하는 경우입니다.
- 필수 이미지 누락과 디코딩 실패도 캐시 실패로 처리하고, 보조 캐시를 폐기해 한 번 다시 생성해야 합니다.

## 확인한 정상 부분

- 테마 이미지의 메모리 재사용, 버전·모드별 키, 프로세스별 임시 파일과 원자적 교체는 속도 개선에 적합합니다.
- 미디어 ID 기준으로 정리 방식 간 중복을 제거하고, 숨김 선택을 병합할 때 최신 시각을 우선하는 방식은 적절합니다.
- 새 선택 테이블은 DB 복구 대상으로 포함되어 있습니다.
- 다시 받기는 선택한 미디어를 트윗별로 묶고, 재시도 실행을 Like 순서와 빠른 확인 판단에서 제외합니다.
- 기존 요청 간격·요청 한도 보호를 재사용하며, 받지 못한 항목의 중복 방지 기록을 복원하는 일반 경로도 유지합니다.

## 검사와 범위

`tests.test_missing_files`, `tests.test_core`, `tests.test_history`, `tests.test_theme`의 **136개 중 135개 통과, 1개 환경 조건으로 생략**했습니다. 기존 검사는 위 경계 조건을 포함하지 않아 통과했으며, 별도 재현에서 문제를 확인했습니다.

로그: `.test-output/review-2.1.0-tests.log`. GUI 재현: `.test-output/review210/gui_repro.py`. 부분 중단·비공개 트윗 재현 결과: `.test-output/review210/core-repro.json`.

이번 검토는 실제 X 다운로드, 사용자의 인증정보·설정·기록·미디어 변경, 배포본 재빌드를 포함하지 않습니다.

## 다음 방향

Windows판은 저장·정리·기록·통계·자동 저장·누락 파일 복원까지 기능이 충분합니다. 새 기능보다 위 동작을 안정화하는 것이 우선입니다. 안드로이드 앱은 그 이후의 개발 후보로 두며 이번 검토에서는 개발하지 않습니다.
