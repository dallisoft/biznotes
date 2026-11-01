"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

/** 포커스 대상 — ref 또는 호출 시점의 요소를 돌려주는 함수(행이 바뀌어도 최신 요소를 찾을 수 있게). */
export type FocusTarget =
  | React.RefObject<HTMLElement | null>
  | (() => HTMLElement | null | undefined);

/** RowActions의 `⋯` 트리거 표식 — 행이 사라질 때 다음·이전 행의 트리거를 찾는 데 쓴다(계약 ③). */
export const ROW_ACTIONS_TRIGGER_ATTR = "data-row-actions-trigger";
/** 표 섹션 제목 표식 — `tabIndex={-1}`과 함께 붙인다. 행 대체 후보가 없을 때 포커스를 받는다(계약 ③). */
export const FOCUS_FALLBACK_ATTR = "data-focus-fallback";
/** 셸 본문 랜드마크 id(§6.8-3) — 마지막 대체 후보(계약 ③). */
export const MAIN_CONTENT_ID = "main-content";

const NATIVE_FOCUSABLE =
  "a[href],button,input,select,textarea,summary,[tabindex],[contenteditable='true']";

function readTarget(target: FocusTarget | undefined): HTMLElement | null {
  if (!target) return null;
  return (typeof target === "function" ? target() : target.current) ?? null;
}

/** 지금 포커스를 받을 수 있는 「살아 있는」 요소인가 — DOM에 붙어 있고, 비활성·inert가 아니며, 포커스 가능한 요소다. */
export function isFocusable(el: HTMLElement | null | undefined): el is HTMLElement {
  if (!el || !el.isConnected) return false;
  if ((el as HTMLButtonElement).disabled) return false;
  if (el.closest("[inert]")) return false;
  return el.matches(NATIVE_FOCUSABLE);
}

/**
 * 복귀 대상이 사라질 때의 대체 후보(계약 ③): 다음 행의 `⋯` → 이전 행의 `⋯` → 표 섹션 제목 → `#main-content`.
 * 행은 `origin`에서 가장 가까운 `tr`·`[role=row]`·`li`·`[data-row]`다. 행이 사라지기 전에 계산해 둔다.
 */
export function focusFallbacks(origin: HTMLElement): HTMLElement[] {
  const out: HTMLElement[] = [];
  const trigger = `[${ROW_ACTIONS_TRIGGER_ATTR}]`;
  const row = origin.closest<HTMLElement>("tr, [role='row'], li, [data-row]");
  if (row) {
    for (let s = row.nextElementSibling; s; s = s.nextElementSibling) {
      const t = s.querySelector<HTMLElement>(trigger);
      if (t) {
        out.push(t);
        break;
      }
    }
    for (let s = row.previousElementSibling; s; s = s.previousElementSibling) {
      const t = s.querySelector<HTMLElement>(trigger);
      if (t) {
        out.push(t);
        break;
      }
    }
  }
  for (let a = (row ?? origin).parentElement; a; a = a.parentElement) {
    const heading = a.querySelector<HTMLElement>(`[${FOCUS_FALLBACK_ATTR}]`);
    if (heading) {
      out.push(heading);
      break;
    }
  }
  const main = origin.ownerDocument.getElementById(MAIN_CONTENT_ID);
  if (main) out.push(main);
  return out;
}

/** 후보 중 포커스를 실제로 받은 첫 요소에 포커스를 둔다. */
function focusFirst(candidates: HTMLElement[]): HTMLElement | null {
  for (const el of candidates) {
    if (!isFocusable(el)) continue;
    el.focus();
    if (el.ownerDocument.activeElement === el) return el;
  }
  return null;
}

/**
 * 확인 뒤 복귀 대상(행의 `⋯`·트리거)이 DOM에서 사라지거나 비활성이 되어 포커스가 유실되면 대체 후보로 옮긴다
 * (계약 ③, BL-150 P1R-01 ③). React 수명주기와 묶지 않는다 — 다이얼로그가 그 행 안에 있으면 행과 함께
 * 언마운트되므로, 정리는 한 번 처리한 뒤·사용자가 포커스를 옮긴 뒤·제한 시간 뒤에 스스로 한다.
 * `transit`(닫히는 확인 다이얼로그) 안의 포커스는 아직 이동 중으로 본다.
 */
export function watchFocusLoss(
  target: HTMLElement,
  fallbacks: HTMLElement[],
  { transit, timeoutMs = 30_000 }: { transit?: HTMLElement | null; timeoutMs?: number } = {},
): () => void {
  const doc = target.ownerDocument;
  const view = doc.defaultView;
  if (!view || typeof view.MutationObserver === "undefined") return () => {};
  let stopped = false;
  const observer = new view.MutationObserver(() => {
    const active = doc.activeElement;
    const lost =
      !active ||
      active === doc.body ||
      active === target ||
      Boolean(transit?.isConnected && transit.contains(active));
    if (target.isConnected && !(target as HTMLButtonElement).disabled) {
      // 대상이 그대로이고 사용자가 다른 곳으로 포커스를 옮겼으면 감시를 끝낸다
      if (!lost) stop();
      return;
    }
    if (lost) focusFirst(fallbacks);
    stop();
  });
  const timer = view.setTimeout(() => stop(), timeoutMs);
  function stop() {
    if (stopped) return;
    stopped = true;
    observer.disconnect();
    view?.clearTimeout(timer);
  }
  observer.observe(doc.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["disabled"],
  });
  return stop;
}

/**
 * 다이얼로그를 닫을 때의 복귀 규칙(계약 ②·③)을 다른 다이얼로그(예: 확인 뒤 이어지는 결과 다이얼로그 — 계약 ④)도
 * 같게 쓰도록 꺼낸 훅.
 * - `capture()` — 복귀 대상이 아직 있을 때(열 때) 대체 후보를 저장한다.
 * - `resolve(confirmed)` — Base UI `finalFocus` 함수로 넘길 수 있는 복귀 대상 계산.
 * - `watchAfterConfirm(transit)` — 확인 직후(행이 사라지기 전) 호출해, 복귀 대상이 나중에 사라지면 대체 후보로 옮긴다.
 */
export function useReturnFocus(returnFocusTo: FocusTarget, focusAfterConfirm?: FocusTarget) {
  const fallbacksRef = useRef<HTMLElement[]>([]);

  const capture = useCallback(() => {
    const origin = readTarget(returnFocusTo);
    if (origin?.isConnected) fallbacksRef.current = focusFallbacks(origin);
  }, [returnFocusTo]);

  const resolve = useCallback(
    (confirmed = false): HTMLElement | true => {
      const after = confirmed ? readTarget(focusAfterConfirm) : null;
      if (isFocusable(after)) return after;
      const origin = readTarget(returnFocusTo);
      if (origin?.isConnected) fallbacksRef.current = focusFallbacks(origin);
      if (isFocusable(origin)) return origin;
      // 복귀 대상이 사라졌거나 비활성(BL-150 P1R-01 ①) → focusAfterConfirm(있으면) → 행 대체 후보 → #main-content
      const candidates = [readTarget(focusAfterConfirm), ...fallbacksRef.current];
      return candidates.find(isFocusable) ?? true;
    },
    [returnFocusTo, focusAfterConfirm],
  );

  const watchAfterConfirm = useCallback(
    (transit?: HTMLElement | null) => {
      // 확인 뒤 포커스를 호출부가 지정했으면(focusAfterConfirm) 그 흐름을 존중한다.
      if (readTarget(focusAfterConfirm)) return;
      const origin = readTarget(returnFocusTo);
      if (!origin?.isConnected) return;
      fallbacksRef.current = focusFallbacks(origin);
      watchFocusLoss(origin, fallbacksRef.current, { transit });
    },
    [returnFocusTo, focusAfterConfirm],
  );

  return { capture, resolve, watchAfterConfirm };
}

export interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 질문형 제목 — 예: 「‘gemini’ 프로바이더를 삭제할까요?」 */
  title: string;
  /** 결과·되돌릴 수 있는지 */
  description?: React.ReactNode;
  /** 확인 버튼 = 동작 동사(「삭제」「비활성화」「초기화」). 「확인」「예」 금지(§6.3.3). */
  actionLabel: string;
  cancelLabel?: string;
  /** 빨간 확인 버튼은 위험 동작만. 기본 true. */
  destructive?: boolean;
  /** 확인 동작. `awaitConfirm`이면 Promise를 돌려준다 — 이행 = 성공(닫음), 거부 = 실패(열린 채 다시 시도). */
  onConfirm: () => void | Promise<unknown>;
  /**
   * (선택, 기본 false) true면 확인을 누른 뒤 `onConfirm`이 돌려준 Promise가 끝날 때까지 다이얼로그를 연 채 기다린다
   * (plan §6.6.2 「실패 시 다이얼로그 상태 유지」). 기다리는 동안 확인 버튼은 `pendingLabel`로 바뀌고 비활성(포커스는 유지)이며
   * 두 번째 확인·취소·Esc를 받지 않는다(중복 제출 차단). 이행하면 닫고, 거부하면 열린 채로 다시 시도할 수 있게 한다 —
   * 실패 안내(토스트 등)는 호출부가 한다. false면 기존처럼 확인 즉시 닫고 `onConfirm`을 부른다.
   */
  awaitConfirm?: boolean;
  /** `awaitConfirm` 대기 중 확인 버튼 문구(선택). 기본 「{actionLabel}하는 중…」. */
  pendingLabel?: string;
  /**
   * 닫힐 때 포커스를 돌려줄 요소(필수, 계약 ②). 행 동작이면 그 행의 `⋯` 트리거다. 확인 뒤 이 요소가 사라지면
   * 다음 행 `⋯` → 이전 행 `⋯` → 섹션 제목 → `#main-content` 순으로 옮긴다(계약 ③).
   */
  returnFocusTo: FocusTarget;
  /** 확인으로 닫힌 뒤 포커스를 옮길 요소(선택) — 지정하면 확인 경로에서 `returnFocusTo`보다 우선한다. */
  focusAfterConfirm?: FocusTarget;
  /** 다이얼로그가 열린 동안 상태가 바뀌어 확인을 막아야 할 때(선택). 기본 false. */
  confirmDisabled?: boolean;
  /** `confirmDisabled`일 때 다이얼로그 안에 보일 사유(선택). */
  confirmDisabledReason?: string;
  /** Root 안에 렌더할 트리거(`AlertDialogTrigger`) — 트리거 일체형 래퍼(`ConfirmButton`)용. */
  trigger?: React.ReactNode;
  /** 설명 아래 추가 본문(선택). */
  children?: React.ReactNode;
}

/**
 * 제어형 확인 다이얼로그(§6.5). 포커스 계약(D-25 · r1 PR-12):
 * ① 초기 포커스 = 「취소」 ② 취소·Esc = `returnFocusTo`(살아 있으면) ③ 확인 뒤 복귀 대상이 사라지면 대체 후보
 * ④ 확인 뒤 다른 다이얼로그가 이미 포커스를 가져갔으면 뺏지 않는다 ⑤ Tab·Shift+Tab은 다이얼로그 안에서 순환(Base UI 모달).
 * AlertDialog라 바깥 클릭으로는 닫히지 않는다(Base UI가 포인터 닫기를 막는다).
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  actionLabel,
  cancelLabel = "취소",
  destructive = true,
  onConfirm,
  awaitConfirm = false,
  pendingLabel,
  returnFocusTo,
  focusAfterConfirm,
  confirmDisabled = false,
  confirmDisabledReason,
  trigger,
  children,
}: ConfirmDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  const confirmedRef = useRef(false);
  // awaitConfirm 대기 — ref는 같은 틱의 중복 클릭·Enter를 막고, state는 버튼 표시를 바꾼다
  const busyRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const reasonId = useId();
  const { capture, resolve, watchAfterConfirm } = useReturnFocus(returnFocusTo, focusAfterConfirm);
  // BL-150 P1R-02: 확인 직후 닫히는(퇴장 애니메이션 중인) 다이얼로그에는 사유를 넣지 않는다 — 이중 낭독 방지.
  const showReason = open && confirmDisabled && Boolean(confirmDisabledReason);

  // 열릴 때마다(트리거 클릭이든 제어형 `open`이든) 확인 기록을 지우고 대체 후보를 저장한다 — 이전 확인이
  // 다음 취소의 포커스 경로에 새지 않게 한다.
  useEffect(() => {
    if (!open) return;
    confirmedRef.current = false;
    busyRef.current = false;
    setBusy(false);
    capture();
  }, [open, capture]);

  async function confirm() {
    if (confirmDisabled || busyRef.current) return;
    confirmedRef.current = true;
    if (!awaitConfirm) {
      // 행이 사라지기 전에(삭제 요청이 동기로 목록을 바꿔도) 대체 후보를 잡고 감시를 시작한다(계약 ③).
      watchAfterConfirm(popupRef.current);
      onOpenChange(false);
      onConfirm();
      return;
    }
    busyRef.current = true;
    setBusy(true);
    try {
      await onConfirm();
    } catch {
      // 실패 — 열린 채로 두고 다시 시도할 수 있게 대기만 푼다(실패 안내는 호출부 몫)
      confirmedRef.current = false;
      busyRef.current = false;
      setBusy(false);
      return;
    }
    busyRef.current = false;
    setBusy(false);
    watchAfterConfirm(popupRef.current);
    onOpenChange(false);
  }

  const finalFocus = () => {
    const doc = popupRef.current?.ownerDocument ?? document;
    const active = doc.activeElement as HTMLElement | null;
    // 계약 ④: 확인 뒤 이어 열린 결과 다이얼로그가 이미 포커스를 가져갔으면 그대로 둔다.
    if (
      active &&
      active !== doc.body &&
      !popupRef.current?.contains(active) &&
      active.closest("[role='dialog'],[role='alertdialog']")
    ) {
      return false;
    }
    return resolve(confirmedRef.current);
  };

  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        // awaitConfirm 대기 중에는 Esc·취소로 닫지 않는다(요청 결과를 이 다이얼로그가 받아야 한다)
        if (!next && busyRef.current) return;
        onOpenChange(next);
      }}
    >
      {trigger}
      <AlertDialogContent ref={popupRef} initialFocus={cancelRef} finalFocus={finalFocus}>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          {description && <AlertDialogDescription>{description}</AlertDialogDescription>}
        </AlertDialogHeader>
        {children}
        {confirmDisabledReason !== undefined && (
          // 사유를 넘기는 사용처만 — 라이브 영역은 열린 동안 계속 마운트하고 문구만 바꾼다.
          <p
            id={reasonId}
            role="status"
            className={showReason ? "text-sm text-muted-foreground" : "sr-only"}
          >
            {showReason ? confirmDisabledReason : ""}
          </p>
        )}
        <AlertDialogFooter>
          {/* 터치 기기(pointer: coarse)에서 두 버튼 모두 44px 이상(plan §6.7·§6.8-8) */}
          <AlertDialogCancel ref={cancelRef} className="coarse:min-h-11 coarse:min-w-11" disabled={busy}>
            {cancelLabel}
          </AlertDialogCancel>
          <AlertDialogAction
            className={
              destructive
                ? "coarse:min-h-11 coarse:min-w-11 bg-destructive text-destructive-foreground hover:bg-destructive-hover"
                : "coarse:min-h-11 coarse:min-w-11"
            }
            disabled={confirmDisabled || busy}
            // 대기 중에도 누른 버튼이 포커스를 잃지 않는다(aria-disabled) — 실패 뒤 같은 자리에서 다시 시도
            focusableWhenDisabled={busy || undefined}
            aria-busy={busy || undefined}
            aria-describedby={showReason ? reasonId : undefined}
            onClick={() => void confirm()}
          >
            {busy ? (pendingLabel ?? `${actionLabel}하는 중…`) : actionLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
