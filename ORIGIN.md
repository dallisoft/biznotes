# 출처 (ORIGIN)

biznotes는 아래 두 비공개 저장소의 특정 커밋에서 떠낸 **스냅샷**으로 시작했다. 원본의 git 이력은 가져오지 않았다. 원본 저장소는 계속 따로 개발되며, biznotes와 자동으로 동기화하지 않는다.

| 원본 | 기준 | 커밋 | biznotes 위치 |
|---|---|---|---|
| agents-core | 태그 `v0.31.0` | `5671bfa7d5f66f9aa07d68ed0c643b4110617a02` | `packages/core/` |
| rapid-notes | `dev` (v0.8.0) | `415824f42df8b3bff77e021b929c7e579d4ba242` | 루트 (`apps/`, `packages/shared-types/`, `infra/`, `skills/`, `Makefile` 등) |

스냅샷 날짜: 2026-11-01.

## 스냅샷에서 뺀 것

- agents-core: `docs/`(설계 노트·스파이크 로그), `.github/`(원본 저장소 전용 CI·이미지 발행·미러 워크플로)
- rapid-notes: `docs/`, `.github/`, `.gstack/`, 루트 `scripts/`(거버넌스 게이트·미러), `README.md`, `AGENTS.md`, `examples/`

## 원본에서 수정을 옮겨 올 때

경로·패키지 이름·저장소 계층이 원본과 달라 `git cherry-pick`은 동작하지 않는다. diff를 손으로 옮기고 아래 표에 기록한다.

| 날짜 | 원본 | 원본 PR·커밋 | biznotes 커밋 | 비고 |
|---|---|---|---|---|
|  |  |  |  |  |
