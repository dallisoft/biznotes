# web

biznotes 프론트엔드(Next.js·React·TypeScript·Tailwind). 저장소 전체 안내는 [루트 README](../../README.md)와 [AGENTS.md](../../AGENTS.md)에 있다.

```bash
NEXT_PUBLIC_API_BASE_URL= pnpm --filter web dev   # http://localhost:3000 — /api/v1을 make up의 api(28000)로 프록시
pnpm --filter web lint
pnpm --filter web typecheck
pnpm --filter web test           # Vitest, 커버리지는 test:coverage(하한은 vitest.config.ts)
```

- API 타입은 `@biznotes/shared-types`(api OpenAPI 생성물)를 쓴다. 응답이 바뀌면 루트에서 `pnpm gen:types`.
- 브라우저가 부르는 API 주소는 `NEXT_PUBLIC_API_BASE_URL`(빌드 때 고정), 서버 렌더링은 `API_INTERNAL_URL`이다. 개발 서버에서 빈 값으로 두면 same-origin 프록시(`next.config.ts`, 대상은 `DEV_API_PROXY_TARGET`)를 써서 CORS 설정 없이 붙는다.
- `src/app` 라우트, `src/components` 화면 조각(`ui/`는 공용 컴포넌트), `src/hooks`·`src/stores` 데이터·상태, `src/lib` API 클라이언트·유틸.
- `e2e/`는 Playwright 스펙이다. 실스택(`make up && make seed`)이 필요하고 수동으로 돈다: `pnpm exec playwright test`.
