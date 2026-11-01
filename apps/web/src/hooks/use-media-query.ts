"use client";

import { useEffect, useState } from "react";

/**
 * CSS 미디어 쿼리 매칭 여부를 추적한다.
 *
 * SSR/하이드레이션 불일치를 피하려 서버·첫 렌더는 항상 `defaultValue`를 반환하고,
 * 마운트 후 effect에서 실제 매칭값으로 보정한다(데스크톱 기본값 true 권장).
 */
export function useMediaQuery(query: string, defaultValue = false): boolean {
  const [matches, setMatches] = useState(defaultValue);

  useEffect(() => {
    const mql = window.matchMedia(query);
    const handler = () => setMatches(mql.matches);
    handler();
    mql.addEventListener("change", handler);
    return () => mql.removeEventListener("change", handler);
  }, [query]);

  return matches;
}
