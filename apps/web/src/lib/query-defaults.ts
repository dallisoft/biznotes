/**
 * 앱 QueryClient의 질의 기본값 — `components/providers.tsx`가 쓰고, 캐시 재사용에 기대는 화면 테스트도 같은 값을 쓴다
 * (테스트가 기본값 `staleTime: 0`으로만 돌면 fresh 캐시로 요청이 생략되는 경로를 놓친다 — 리뷰 P2-10).
 */
export const APP_QUERY_DEFAULTS = {
  retry: 1,
  staleTime: 10_000,
  refetchOnWindowFocus: false,
} as const;
