# 현재 날짜·시각 스킬 (`current-datetime`)

지금의 날짜·시각·요일을 돌려주는 biznotes 표준 스킬이다. 모델은 현재 시각을 알 수 없다. 그래서 번들 스크립트 `scripts/now.py`가 서버 시계 값을 JSON으로 내고, 스킬 지침은 그 값만 쓰게 한다. 기본 시간대는 한국 표준시(`Asia/Seoul`)다.

## 패키지 구성

```
current-datetime/
├── SKILL.md          # 표준 frontmatter(name·description·compatibility) + 지침: 반드시 스크립트를 실행하고 짐작하지 않는다
└── scripts/now.py    # 표준 라이브러리만 사용. 인자: IANA 시간대(선택). 출력: JSON 한 줄
```

제품 매니페스트(`biznotes.json`)는 두지 않는다. 매니페스트가 있으면 legacy instruction 프로필이 되어 스크립트를 실행할 수 없다(코어 `runtime_profile_for`).

`now.py` 출력 예:

```json
{"timezone": "Asia/Seoul", "abbreviation": "KST", "display": "2026년 10월 28일 수요일 오전 11:12:33 KST",
 "date": "2026-10-28", "time": "11:12:33", "weekday": "수요일", "iso": "2026-10-28T11:12:33+09:00",
 "utc": "2026-10-28T02:12:33Z", "unix": 1793153553}
```

알 수 없는 시간대나 인자 개수 오류는 `{"error": "..."}`와 종료 코드 2를 돌려준다.

## 실행 전제

스킬이 실제로 시각을 돌려주려면 아래가 모두 필요하다. 하나라도 빠지면 스킬은 「현재 시각을 확인할 수 없다」고 답한다(지침상 추정 금지).

1. **표준 런타임**: `SKILL_STANDARD_RUNTIME_ENABLED=true`
   - `infra/.env.example`에 있다(기본 `false`). compose가 api·worker에 넘긴다.
2. **스크립트 백엔드**: `SKILL_SCRIPT_BACKEND=process`와 샌드박스 워커(`docker compose --profile skill-scripts up -d worker-skill-sandbox`).
3. **버전 고정(freeze)**: 위 1이 켜진 뒤에 한다.
   - 실행 프로필은 freeze 시점 설정으로 정해진다. 꺼진 상태로 freeze하면 legacy 버전이 되어 스크립트가 돌지 않는다.
4. **관리자 scripts 검토 승인**(코어 D8): 버전마다 1회 필요하다.
5. **실행 승인**: 호출마다 실행한 사용자 본인이 승인한다(`run_skill_script`는 항상 승인 대상).

## 검증 기록 (2026-11-01)

- 이 저장소의 샌드박스 이미지(`packages/core/docker/skill-sandbox.Dockerfile`)로 띄운 워커(`--profile skill-scripts`)에
  코어 작업 형식 그대로 `scripts/now.py`를 보냈다 → `completed`, exit 0, 시각 JSON.
  스킬 파일·입력은 api가 발급한 서명 URL로 받았다(`docs/plans/biznotes-extract.md` Phase 1·2b 기록).

## zip 다시 만들기

```bash
cd skills && rm -f current-datetime.zip && zip -q -X -r current-datetime.zip current-datetime -x '*/__pycache__/*' '*.DS_Store'
```
