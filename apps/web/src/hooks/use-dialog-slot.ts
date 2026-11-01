"use client";

import { useCallback, useLayoutEffect, useRef, useState } from "react";

/**
 * 한 자리에 하나씩 여는 다이얼로그 상태(P3 리뷰 P3-02). 다이얼로그를 열 때마다 세대 번호를 올리고, 닫기 콜백은
 * **그 콜백이 만들어진 렌더에서 열려 있던 창**에 묶는다. 그래서 사용자가 이미 닫은 창의 요청이 늦게 끝나
 * `onOpenChange(false)`를 불러도(다이얼로그는 제출 시점의 콜백을 쥐고 있다) 그사이 연 다른 창 — 또는 같은 종류를 다시
 * 연 새 창 — 을 닫지 않는다. 닫기는 함수형 갱신으로 「아직 그 창이 현재 창일 때만」 적용한다.
 */
export interface DialogSlot<T> {
  /** 지금 열린 창의 값(닫혔으면 null) — 예: 헤더 다이얼로그 종류, 홈의 대상 노트북 */
  value: T | null;
  /** 창을 연다(새 세대). 같은 값을 다시 열어도 새 창이다. */
  open: (value: T) => void;
  /** 이 렌더의 창에 묶인 닫기 콜백 — 늦게 불려도 그 창이 아직 현재 창일 때만 닫는다. `true`는 무시한다(제어형). */
  onOpenChange: (open: boolean) => void;
  /** `onOpenChange`와 같되, 지금 창의 값이 `value`일 때만 닫는다(한 자리를 여러 종류가 나눠 쓸 때). */
  onOpenChangeFor: (value: T) => (open: boolean) => void;
}

export function useDialogSlot<T>(): DialogSlot<T> {
  const [slot, setSlot] = useState<{ value: T; gen: number } | null>(null);
  const genRef = useRef(0);

  const open = useCallback((value: T) => {
    genRef.current += 1;
    setSlot({ value, gen: genRef.current });
  }, []);

  // 이 렌더에서 열려 있는 창의 세대 — 아래 콜백이 붙잡는다
  const gen = slot?.gen ?? null;

  const closeIfCurrent = useCallback(
    (match: (value: T) => boolean) => {
      if (gen === null) return;
      setSlot((cur) => (cur && cur.gen === gen && match(cur.value) ? null : cur));
    },
    [gen],
  );

  const onOpenChange = useCallback(
    (next: boolean) => {
      if (!next) closeIfCurrent(() => true);
    },
    [closeIfCurrent],
  );

  const onOpenChangeFor = useCallback(
    (value: T) => (next: boolean) => {
      if (!next) closeIfCurrent((cur) => Object.is(cur, value));
    },
    [closeIfCurrent],
  );

  return { value: slot?.value ?? null, open, onOpenChange, onOpenChangeFor };
}

/**
 * 다이얼로그 안쪽의 「열린 회차」(P3 재리뷰 P3-R01). 닫혀 있다가 열릴 때, 그리고 열린 채 대상이 바뀔 때 회차가 하나
 * 오른다(닫기만으로는 오르지 않는다 — 닫히는 애니메이션 동안 폼이 그대로 보이게). 다이얼로그는 이 회차를 폼의 `key`로
 * 써서 회차마다 새 입력으로 시작하고, 제출 시점의 회차를 붙잡아 두었다가 요청이 끝났을 때 `isCurrent(회차)`일 때만
 * 창 닫기·인라인 오류 같은 「그 창」의 후처리를 한다 — 사용자가 닫고 다시 연 창의 초안을 이전 요청의 완료가 지우거나
 * 덮지 않는다. 서버 성공에 따른 캐시 갱신(훅)과 토스트는 회차와 무관하게 그대로 한다.
 */
export function useOpenSession(open: boolean, target: string | null | undefined): {
  session: number;
  isCurrent: (session: number) => boolean;
} {
  const key = open ? (target ?? null) : null;
  const [state, setState] = useState<{ key: string | null; session: number }>({ key: null, session: 0 });
  let session = state.session;
  if (key !== state.key) {
    // 이전 렌더의 값을 바탕으로 한 파생 상태 갱신(렌더 중 setState — React가 이 렌더를 버리고 곧바로 다시 그린다)
    if (key !== null) session = state.session + 1;
    setState({ key, session });
  }
  const latest = useRef(session);
  useLayoutEffect(() => {
    latest.current = session;
  }, [session]);
  const isCurrent = useCallback((s: number) => latest.current === s, []);
  return { session, isCurrent };
}
