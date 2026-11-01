"use client";

import { useEffect, useState } from "react";

/** 표 규칙의 좁은 화면(plan §6.7 「표(< 768)」) — Tailwind `max-md`(width < 48rem)와 같은 경계. */
export const NARROW_VIEWPORT_QUERY = "(max-width: 767.98px)";

/**
 * 좁은 화면(< 768px)인지 추적한다. 서버·첫 렌더와 `matchMedia`가 없는 환경(jsdom)에서는 넓은 화면(false)으로 본다 —
 * 데스크톱 마크업이 기본이고, 마운트 뒤 실제 값으로 보정한다.
 */
export function useNarrowViewport(): boolean {
  const [narrow, setNarrow] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const mql = window.matchMedia(NARROW_VIEWPORT_QUERY);
    const update = () => setNarrow(mql.matches);
    update();
    mql.addEventListener("change", update);
    return () => mql.removeEventListener("change", update);
  }, []);

  return narrow;
}
