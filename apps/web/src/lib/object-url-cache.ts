/**
 * 산출물 object URL의 수명(core-v0.23.0-upgrade PR-2 리뷰 M3) — 인증 요청으로 받은 바이너리 산출물(삽화·오디오)은
 * `URL.createObjectURL`로 그린다. URL은 쿼리 캐시가 소유한다: 캐시가 그 항목을 버릴 때(관찰자가 모두 사라지고
 * gcTime이 지난 뒤, 또는 로그아웃 등으로 `clear()`할 때) 해제한다. 같은 키를 여러 화면이 쓰는 동안에는
 * 항목이 남아 있으므로 해제되지 않는다.
 */
import type { QueryClient } from "@tanstack/react-query";

/** object URL을 데이터로 갖는 쿼리 키의 첫 마디 — `useSlideImages`·`ArtifactContent`가 쓴다. */
export const OBJECT_URL_QUERY_ROOTS = new Set(["artifact-image", "artifact-audio"]);

/** 캐시에서 빠지는 object URL 항목을 해제하는 구독을 단다. 돌려주는 함수로 구독을 푼다. */
export function installObjectUrlCleanup(client: QueryClient): () => void {
  return client.getQueryCache().subscribe((event) => {
    if (event.type !== "removed") return;
    const { queryKey, state } = event.query;
    if (!OBJECT_URL_QUERY_ROOTS.has(String(queryKey[0]))) return;
    if (typeof state.data === "string" && state.data.startsWith("blob:")) URL.revokeObjectURL(state.data);
  });
}
