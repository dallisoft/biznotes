/**
 * 포커스 복귀 도우미 — 내용이 바뀌며 포커스를 쥔 요소가 DOM에서 빠졌을 때(오류 → 로딩 → 성공 등) 살아남는 경계 안으로
 * 키보드 포커스를 되돌리는 데 쓴다(P2 통합 admin R-4 · P2 리뷰 P2-01). 판정·이동 기본 동작만 둔다 — 「다시 시도」 경계의
 * 규칙은 `hooks/use-retry-focus-landing`(P6에서 하나로 합침), 오류 상태가 처음 사라질 때의 복귀는 `ErrorState`가 쓴다.
 */

/** 키보드로 포커스할 수 있는 요소(비활성 제외). */
export const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** 포커스가 사라졌는가 — body이거나 DOM에서 빠진 요소. 다른 곳에 새로 생긴 포커스는 빼앗지 않으려고 쓴다. */
export function focusIsLost(doc: Document = document): boolean {
  const active = doc.activeElement;
  return !active || active === doc.body || !active.isConnected;
}

/** `root` 안의 첫 포커스 가능 요소로 포커스를 옮긴다. 옮길 요소가 없으면 false. */
export function focusFirstWithin(root: HTMLElement | null | undefined): boolean {
  const target = root?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR);
  if (!target) return false;
  target.focus();
  return true;
}
