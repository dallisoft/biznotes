# 마인드맵 스킬 (`skills/mindmap/`)

선택한 소스의 주제와 하위 개념을 **마인드맵 트리 JSON**(`mindmap.json`)으로 만드는 biznotes 스킬이다.
산출물은 에이전트 탭의 마인드맵 뷰어(접기·펼치기·PNG/JSON 내보내기·노드 클릭 → 채팅 질문)로 연다.

계획: [`docs/plans/mindmap-agent-skill/plan.md`](../docs/plans/mindmap-agent-skill/plan.md) P3(AC-3.1~3.6)·P4(§10 r5).
P4부터 이 패키지는 `make seed`가 시스템 스킬 `mindmap`으로 등록하는 **스튜디오 「마인드맵」 카드의 산출 주체**다.

## 패키지 구성

| 파일 | 내용 |
| --- | --- |
| `mindmap/SKILL.md` | frontmatter(`name: mindmap`, 한국어 `description`) + 출력 규칙·문서 형태·예시 |
| `mindmap/biznotes.json` | `mode: instruction`, `input: {mode: rag, scope: selected, max_sources: 12, coverage: per_source, per_source_limit: 1, digest: true}`, `rag_query: "{params.topic}"`(top_k 생략 → 어드민 RAG 설정), `permissions`(mcp·http·scripts=false), `artifact: {name: mindmap.json, content_type: application/json, schema: mindmap}` |
| `mindmap.zip` | 위 두 파일의 가져오기용 묶음 — references 없음(시드 경로가 두 파일만 받는다) |

문서 형태: `{"version": 1, "root": {"id", "label", "children": [...]}}`. 노드마다 선택 필드 `cite`(근거 발췌 번호 `[n]` 배열)를 쓸 수 있다.

## 시드 · 가져오기 (관리자)

기본 경로는 시드다. `make seed`가 `apps/api/scripts/seed_skills/mindmap/`(이 디렉터리의 바이트 동일 사본 — api 이미지에는 저장소 루트가 없다)을 system 스킬 `mindmap`으로 등록·공개하고, 옛 시드 워크플로우 「마인드맵」을 비활성화한다. 아래 ZIP 가져오기는 시험용 private 사본이 필요할 때만 쓴다.


1. `/admin/skills` → 「ZIP 가져오기」로 `skills/mindmap.zip`을 올린다(API: `POST /api/v1/skills/import-zip`).
2. 검증 보고서에 오류가 없는지 확인한다.
3. **freeze → publish**(`POST /api/v1/skills/{id}/freeze` → `POST /api/v1/skills/{id}/versions/{version_id}/publish`). 시험 단계에서는 visibility `private`로 둔다.
4. 노트북 에이전트 탭 「스킬」 목록의 「마인드맵」으로 실행한다(다이얼로그에서 소스 선택).

## ZIP 재생성

```bash
cd skills/mindmap && rm -f ../mindmap.zip && zip -r -X ../mindmap.zip SKILL.md biznotes.json -x '.*'
```

`apps/api/tests/test_mindmap_skill_package.py`가 zip 안 파일 목록·바이트를 디렉터리와 대조한다 — 디렉터리를 고치면 zip도 다시 만든다. 시드 사본도 복사한다:

```bash
cp skills/mindmap/SKILL.md skills/mindmap/biznotes.json apps/api/scripts/seed_skills/mindmap/
```

`apps/api/tests/test_mindmap_contract.py::test_seed_copy_matches_repo_package`가 사본과 정본의 바이트 동일을 막는다.

## 동작과 알려진 제약

매니페스트의 `artifact.schema: mindmap`으로 코어 빌더가 `llm`과 `save_artifact` 사이에 `json_validate`를 끼운다.

- **모든 소스 반영**: 검색 앞에 `source_digest`가 고른 소스 모두의 요약을 넣고(`<source_digests>`, 인용 번호 없음), 검색은 소스마다 관련도 상위 발췌 1개를 보장한다(`<source_excerpts>`, `[n]`). 요약이 캐시에 없는 소스는 run 중에 만든다(생성 상한 `SOURCE_DIGEST_RUN_MAX_GENERATE`). 스킬 run에는 반영 현황(`coverage.json`)이 없어 결과 위 「N개 중 M개 반영」 표시는 나오지 않는다.
- **스키마 보장·재시도·노드 상한**: LLM 응답이 JSON으로 파싱되고 마인드맵 형태를 통과해야 저장된다. 코드 펜스로 감싼 응답도 실패로 보고 사유를 덧붙여 **한 번** 다시 생성하며, 다시 실패하면 run이 실패하고 산출물은 남지 않는다. 노드가 200개를 넘으면 BFS로 잘리고 최상위에 `truncated: true`가 붙는다. 채팅 본문·run 요약도 검증본이다.
- **근거 해석**: 노드의 `cite: [n]`은 run 근거표(이 run의 `rag_search` 인용)로 해석돼 첫 번호의 `source_ref: {source_id, chunk_id}`가 되고, `cite`는 저장되지 않는다. 표에 없는 번호와 직접 쓴 쌍 중 표와 다른 것은 버린다. 건수는 최상위 `refs_resolved`·`refs_dropped`. 뷰어의 「소스로 이동」은 이 `source_ref`로 동작한다. 매니페스트 `system`에 `{search.citations}`를 넣어 LLM이 소스 ID를 복사하게 하는 방법은 쓰지 않는다.
- **채팅 카탈로그 밖**: 제품 매니페스트가 있는 legacy 스킬이라 채팅 에이전트의 자율 스킬 목록(표준 런타임만)에 들어가지 않는다. 실행은 스튜디오 카드나 에이전트 탭 「스킬」 목록(같은 다이얼로그)에서만 한다.
- **롤백**: 시드 변경을 되돌려도 system 스킬은 지워지지 않는다 — 관리자 「스킬」 화면에서 비활성화한다. 워크플로우 정의가 시드에 돌아오면 `make seed`가 다시 켠다.
- **재적용**: 스킬이 꺼졌거나 강등·미공개인 채로 다시 시드하면, 시드는 관리자 차단을 풀지 않고 `WARNING: system skill mindmap is not runnable`을 남긴 뒤 워크플로우를 끄지 않는다. 관리자 화면에서 스킬을 복원하고 다시 시드해야 은퇴가 이뤄진다.
- 가져온(import) private 사본의 publish는 검토 요청(`202 review_pending`)이 된다 — 관리자 「스킬 공개」에서 승인해야 공개된다. 그전에는 소유자만 draft로 실행한다.
