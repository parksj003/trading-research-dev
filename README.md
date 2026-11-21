# Trading Research — 공개 개발본

워치리스트 엔진·매매일지와 Streamlit 앱의 개발 및 합성 데이터 테스트를 한 저장소에서 관리합니다. 실제 계좌, 체결 원장, 운영 게시본, 개인 관심종목 목록, 인증정보, 운영 캐시 및 기존 Git 이력은 포함하지 않습니다.

## 구조와 실행

| 경로 | 역할 |
|---|---|
| `engine/` | 수집·계산·일지 생성 코드와 합성 단위 테스트 |
| `app/` | 화면·차트 코드와 합성 UI 테스트 |
| `dev.py` | 공개 파일 검사와 자격증명 없는 테스트 실행 |
| `.github/workflows/ci.yml` | 두 프로젝트의 Python 3.11/3.12 검증 |
| `public-files.json` | 검토한 파일 경로와 최초 분리 시 SHA256 |

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python dev.py check
python dev.py test engine
python dev.py test app
```

엔진 테스트에는 Node.js도 필요합니다. 설치 단계는 패키지 저장소에 연결합니다. 테스트 단계는 운영 자격증명 환경변수를 제거하고 pytest-socket으로 Python 소켓 통신을 차단합니다. 이것은 OS 수준의 네트워크 샌드박스는 아니므로 Public CI에 운영 Secrets를 등록하거나 실제 데이터 저장소를 연결하지 마세요. 외부 기여자의 PR은 기본 `pull_request`로 검증하며, `pull_request_target` 등 권한 상승 트리거를 사용하지 않습니다.

## 더미 데이터와 검증 경계

- `app/watchlists/manual_watchlist.csv`는 두 행의 합성 예시입니다. 개인 목록을 복사하지 않았습니다. NBI는 지수 분류 회귀 테스트용 심볼이며 보유·관심 정보가 아닙니다.
- 425개 목록의 규모·순서·중복 테스트는 코드에서 가상 종목을 생성합니다.
- OHLCV·체결·계좌·청산 경로는 기존 합성 fixture와 테스트 내 생성기를 사용합니다. 압축 fixture도 합성 연구용입니다.
- 엔진의 연구 등록부 테스트는 새 합성 등록부를 생성합니다. Private의 불변 등록부 및 실제 계보 일치는 Private에서 별도 검증해야 합니다.
- `engine/research/base_count/plan.json`은 알고리즘 프로필을 보존하되 운영 종목 목록을 비웠습니다.
- `engine/tests/fixtures/workflows/`는 운영 계약의 **실행되지 않는 테스트 자료**입니다. 루트 `.github/workflows/ci.yml`만 Public Actions에서 실행됩니다. fixture에 있는 `secrets.NAME`은 이름 참조일 뿐 값이 아니며, 운영 워크플로로 복사하지 않습니다.
- Public의 코드 테스트 성공은 실제 인증·시세 수집·게시·텔레그램 연동 성공을 뜻하지 않습니다.

## 간소화 원칙

기존의 날짜별 감사 문서, 증거 JSON, 중복 사용자 설명서, 브라우저 증거, 운영 산출물 및 독립 운영 검증 스크립트는 공개본에서 제외했습니다. 안내는 이 README, 테스트 명령은 dev.py, CI는 한 파일로 통합했습니다. 동작과 독립 배포를 보존하기 위해 내부 Python 모듈을 무리하게 합치지 않았습니다. 두 프로젝트에 필요한 세 계약 모듈은 유지하되 `dev.py check`에서 바이트 일치를 검사합니다.

새 파일은 검토 후 public-files.json에 추가하세요. 최초 SHA256은 출발점 기록이며 이후 정상 코드 수정을 금지하지 않습니다. 파일 경로 검사와 토큰 형식 검사는 전체 개인정보/비밀 검사를 대체하지 않습니다.

## Private 운영 반영

1. 이 저장소에서 수정하고 Public CI가 성공한 정확한 커밋을 선정합니다.
2. Private 운영 저장소의 최신 main에서 배포용 브랜치를 만듭니다.
3. `engine/`의 코드 변경은 엔진 저장소, `app/`의 코드 변경은 앱 저장소에 **파일 단위 diff로** 반영합니다. 전체 디렉터리 동기화나 삭제 동기화를 사용하지 않습니다.
4. 공개본의 `example-owner/`는 예시 소유자입니다. 운영 반영 시 운영 저장소 식별자를 유지하세요. 운영 설정·워크플로·Secrets·관심종목 CSV·연구 plan·게시 이력·더미 fixture는 덮어쓰지 않습니다.
5. 운영 계약 테스트와 실제 연동 검증을 Private PR에서 확인한 후 배포합니다. 배포 PR에 원본 Public 커밋을 기록합니다.
6. Public CI가 정상 실행된 것을 확인하기 전에는 Private CI를 끄지 않습니다. 이후 반복 개발 검증은 Public에서 수행하고 Private 검증은 운영 반영 시 유지합니다.

처음 공개할 때는 이 디렉터리의 검토한 파일만 **새 Git 저장소의 첫 커밋**으로 올리세요. 기존 Private 저장소의 branch/history/mirror를 push하지 마세요. 기존 Private 저장소의 공개 범위를 바꾸지 않습니다.
