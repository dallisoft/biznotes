"use client";

import { useLayoutEffect, useRef } from "react";
import { FOCUS_FALLBACK_ATTR, MAIN_CONTENT_ID } from "@/components/ui/confirm-dialog";
import { focusFirstWithin, focusIsLost } from "@/lib/focus";

/** 다시 시도 흐름의 상태 — 처음 조회가 실패한 쿼리를 다시 부르면 React Query는 `pending`(데이터 없음)을 거친다. */
export type RetryFocusStatus = "pending" | "error" | "success";

/**
 * 「다시 시도」의 포커스 착지점 — 상태 전환형 영역의 호출부 경계. 앱의 **단일 규칙**이다(P6 정리, HCI-P4-4): P2 관리 개요
 * 카드 칸(`useRetryFocusBoundary`, P2-01), P3 빈 대화의 연결 카드(인라인 경계), P4 화면들이 따로 갖던 같은 계약을 이 훅
 * 하나로 합쳤다. 판정·이동 도우미는 `lib/focus.ts`, 오류 상태가 처음 사라지는 순간의 복귀는 `ErrorState`가 맡는다.
 *
 * 처음 조회가 실패한 쿼리를 다시 부르면 오류 → 로딩(스켈레톤) → 결과로 두 번 바뀐다. `ErrorState`는 첫 교체 때 살아남은
 * 조상 안 첫 포커스 가능 요소로 포커스를 옮기지만, 스켈레톤에는 그런 요소가 없어 포커스가 body로 떨어지고 결과가 와도
 * 돌아오지 않는다. 이 경계는 다시 시도가 진행되는 동안 상태가 바뀔 때마다, 포커스를 잃었거나 이 경계가 맡겨 둔
 * 자리(영역·대체 제목)에 있으면 영역 안에서 순서대로 옮긴다:
 * ① 첫 포커스 가능 요소 → ② 대체 제목(`data-focus-fallback` + `tabIndex={-1}`) → ③ 영역 자체(`tabIndex={-1}`).
 * 로딩 동안은 ②·③에 맡겨 두고, 결과(성공 또는 다시 실패 — 새 「다시 시도」 버튼)가 오면 ①부터 다시 고른다.
 * 영역이 포커스를 받지 않고 대체 제목도 없으면(카드 칸·카드 묶음처럼 로딩 중에도 링크·버튼이 남는 영역) 로딩 동안에도
 * ①을 쓴다. 그 사이 사용자가 다른 곳으로 옮긴 포커스는 빼앗지 않는다.
 *
 * - `scope="region"`: 영역 요소에 `regionRef`를 준다(로딩 동안 맡겨 둘 자리가 필요하면 대체 제목 또는 영역에 `tabIndex={-1}`).
 * - `scope="page"`: 화면 전체가 오류·로딩·결과로 바뀌는 상세·편집 화면 — 셸 본문 `main#main-content`(`tabIndex={-1}`)를
 *   영역으로 쓴다(결과의 첫 포커스 가능 요소 = 브레드크럼·뒤로 링크).
 *
 * 사용: 다시 시도를 시작할 때(`ErrorState`의 `onRetry` 등) `markRetry()`를 먼저 부른다.
 */
export function useRetryFocusLanding<T extends HTMLElement>(
  status: RetryFocusStatus,
  scope: "region" | "page" = "region",
) {
  const regionRef = useRef<T>(null);
  const retryInFlight = useRef(false);
  useLayoutEffect(() => {
    if (!retryInFlight.current) return;
    if (status !== "pending") retryInFlight.current = false;
    const region = scope === "page" ? document.getElementById(MAIN_CONTENT_ID) : regionRef.current;
    if (!region) return;
    const fallback = region.querySelector<HTMLElement>(`[${FOCUS_FALLBACK_ATTR}]`);
    const active = document.activeElement;
    const parked = active === region || (fallback !== null && active === fallback);
    if (!focusIsLost() && !parked) return;
    if (status !== "pending" && focusFirstWithin(region)) return;
    // 맡겨 둘 자리 ②·③ — 둘 다 포커스를 받지 않으면 영역 안 첫 포커스 가능 요소
    const spot = fallback ?? region;
    spot.focus();
    if (document.activeElement !== spot) focusFirstWithin(region);
  }, [status, scope]);
  return {
    regionRef,
    markRetry: () => {
      retryInFlight.current = true;
    },
  };
}

/** React Query 결과 → 다시 시도 흐름의 상태. 데이터 없이 실패만 있으면 오류다. */
export function retryFocusStatus(query: {
  isPending: boolean;
  isError: boolean;
}): RetryFocusStatus {
  if (query.isPending) return "pending";
  return query.isError ? "error" : "success";
}
