## 요약 / Summary

<!-- 무엇을 왜 바꿨나. 관련 이슈가 있으면 링크 / What and why. Link related issues -->

## 테스트 / Testing

<!-- 돌린 것과 결과. 기본은 `make ci`. UI·워커·인제스트를 바꿨다면 `make up` 실스택에서 확인한 흐름 -->
<!-- What you ran and the result. Default: `make ci`. For UI/worker/ingestion changes, the flow you checked on `make up` -->

- [ ] `make ci`

## 마이그레이션·계약 동반 / Migrations and contracts

<!-- 해당하는 것에 표시 / Check what applies -->

- [ ] 모델을 바꿨다 → `apps/api/alembic/versions/`에 리비전을 더했고 `make migrate-check`가 통과한다
- [ ] API 응답을 바꿨다 → `pnpm gen:types` 결과(`packages/shared-types`)를 포함했다
- [ ] 의존성을 바꿨다 → `make lock-docker` 결과를 포함했다
- [ ] 해당 없음 / None
