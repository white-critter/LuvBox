# LuvBox Windows 작업 이어받기

현재 UI는 PySide6 + Qt Quick이며 제품 버전의 기준은 `luvbox/__init__.py`입니다.

1. `git status --short --branch`와 최근 이력으로 미커밋 변경을 확인하고 보존합니다.
2. [Windows 실행 순서](docs/windows-handoff.md)에 따라 의존성·고정 입력을 준비합니다.
3. [개발 안내](docs/development.md)에서 구조와 검사·빌드 진입점을 확인합니다.
4. 수정에 맞는 회귀 검사와 격리된 Qt·다운로드 시작 검사를 수행합니다.
5. 배포 시 `build.ps1`로 새 버전을 만들고 산출물의 버전·해시·실행 결과를 확인합니다.

현재 변경사항은 [CHANGELOG](CHANGELOG.md), 완료된 검사와 생략 사유는 [검증 기록](docs/verification.md)에 기록합니다. 인증·파일 접근 경계는 [보안 검토](docs/security-review.md), 측정 방법은 [성능 안내](docs/performance.md)를 따릅니다. 과거 원문은 Git 태그·커밋 기록에 보존합니다.

`%LOCALAPPDATA%\LuvBox`와 사용자 미디어·예약은 테스트 데이터가 아닙니다. 검사는 임시 앱 데이터·생성 미디어·합성 계정만 사용하고, 기존 설정·로그인·기록과 이전 이름의 호환 코드를 보존합니다. 데이터 충돌은 기존 안내와 명시적 선택 흐름을 유지합니다. 인증 값·사용자 DB·브라우저 프로필은 로그·Git·배포물에 포함하지 않습니다.

GitHub 푸시·릴리스는 해당 작업에 대한 사용자 지시를 따릅니다. Git 기록 재작성이나 복구하기 어려운 원격 기록 삭제는 사전에 확인받습니다.
