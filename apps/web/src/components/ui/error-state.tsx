"use client";

import { AlertTriangle, Lock, RotateCw } from "lucide-react";
import Link from "next/link";
import { useLayoutEffect, useRef } from "react";
import { Button } from "@/components/ui/button";
import { focusFirstWithin, focusIsLost } from "@/lib/focus";
import { cn } from "@/lib/utils";

/**
 * 「다시 시도」가 성공하면 오류 상태가 사라지며 포커스를 쥔 버튼도 DOM에서 빠져 포커스가 body로 떨어진다.
 * 버튼을 눌러 다시 시도했고 사라지는 순간에도 버튼이 포커스를 쥐고 있었다면, 살아남은 가장 가까운 조상(같은
 * 카드·영역) 안의 첫 포커스 가능 요소로 포커스를 옮긴다. 그 사이 포커스가 다른 곳에 생겼으면 건드리지 않는다.
 * 오류 상태가 사라진 뒤 그 자리의 내용이 한 번 더 바뀌는 흐름(오류 → 로딩 → 성공)은 이 컴포넌트가 볼 수 없으므로,
 * 호출부가 살아남는 경계에서 이어서 관리한다(예: 관리 개요 카드 칸 — P2 리뷰 P2-01).
 */
function useRestoreFocusAfterRecovery() {
  const retryRef = useRef<HTMLButtonElement>(null);
  const retried = useRef(false);
  useLayoutEffect(
    () => () => {
      // 레이아웃 정리는 DOM 제거 전에 실행된다 — 지금 포커스와 조상 사슬을 읽어 둔다
      const button = retryRef.current;
      if (!retried.current || !button || document.activeElement !== button) return;
      const ancestors: HTMLElement[] = [];
      for (let el = button.parentElement; el && el !== document.body; el = el.parentElement) ancestors.push(el);
      queueMicrotask(() => {
        if (!focusIsLost()) return;
        focusFirstWithin(ancestors.find((el) => el.isConnected));
      });
    },
    [],
  );
  return { retryRef, markRetried: () => (retried.current = true) };
}

/**
 * 오류 상태(§6.5): 제목 + 사용자 문구 + 「다시 시도」(refetch) + 선택 「돌아가기」 링크.
 * `variant="forbidden"`은 권한 없음 화면(다시 시도 없음, 호출부가 `actions`로 대안 동작을 넘긴다 —
 * 예: 「복제해서 내 스킬로 편집」).
 */
export function ErrorState({
  variant = "error",
  title,
  description,
  onRetry,
  retrying = false,
  back,
  actions,
  className,
}: {
  variant?: "error" | "forbidden";
  title?: string;
  description?: string;
  onRetry?: () => void;
  retrying?: boolean;
  back?: { href: string; label: string };
  actions?: React.ReactNode;
  className?: string;
}) {
  const { retryRef, markRetried } = useRestoreFocusAfterRecovery();
  const forbidden = variant === "forbidden";
  const heading = title ?? (forbidden ? "이 화면을 볼 권한이 없습니다" : "목록을 불러오지 못했습니다");
  const body = description ?? (forbidden ? undefined : "잠시 후 다시 시도해 주세요.");
  return (
    <div
      data-slot="error-state"
      data-variant={variant}
      role={forbidden ? undefined : "alert"}
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-xl border px-6 py-12 text-center",
        className,
      )}
    >
      <div
        aria-hidden
        className={cn(
          "flex size-12 items-center justify-center rounded-xl [&_svg]:size-6",
          forbidden ? "bg-muted text-muted-foreground" : "bg-destructive/10 text-destructive-text",
        )}
      >
        {forbidden ? <Lock /> : <AlertTriangle />}
      </div>
      <div className="flex flex-col gap-1">
        <p className="text-base font-semibold">{heading}</p>
        {body && <p className="text-sm text-muted-foreground">{body}</p>}
      </div>
      {(onRetry || back || actions) && (
        <div className="flex flex-wrap items-center justify-center gap-2">
          {actions}
          {onRetry && (
            <Button
              ref={retryRef}
              variant="outline"
              onClick={() => {
                markRetried();
                onRetry();
              }}
              disabled={retrying}
              // 다시 읽는 동안에도 키보드 포커스를 잃지 않는다(aria-disabled — 중복 클릭은 막는다)
              focusableWhenDisabled
              className="coarse:h-11 coarse:min-w-11"
            >
              <RotateCw className={retrying ? "motion-safe:animate-spin" : undefined} />
              다시 시도
            </Button>
          )}
          {back && (
            <Button variant="ghost" className="coarse:h-11 coarse:min-w-11" render={<Link href={back.href} />}>
              {back.label}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
