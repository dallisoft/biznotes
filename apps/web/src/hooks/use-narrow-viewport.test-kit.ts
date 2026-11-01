import { vi } from "vitest";
import { NARROW_VIEWPORT_QUERY } from "./use-narrow-viewport";

/**
 * 테스트 전용 — jsdom에는 `matchMedia`가 없다. 좁은 화면(< 768px, plan §6.7) 판정만 `narrow`로 돌려주는 가짜를 심는다.
 * 호출한 테스트는 `vi.unstubAllGlobals()`로 되돌린다.
 */
export function stubNarrowViewport(narrow = true): void {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: narrow && query === NARROW_VIEWPORT_QUERY,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }));
}
