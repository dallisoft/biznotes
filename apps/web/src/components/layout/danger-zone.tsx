"use client";

import { useId } from "react";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { cn } from "@/lib/utils";

/**
 * 위험 구역(§6.5) — 페이지 맨 아래 카드, 테두리 `destructive/40`, 제목 「위험 구역」. 항목은 `DangerZoneItem`으로 넣는다.
 * 위험 동작을 주 동작과 같은 줄에 두지 않는다(DP-5).
 */
export function DangerZone({
  title = "위험 구역",
  description,
  children,
  className,
}: {
  title?: string;
  description?: string;
  children: React.ReactNode;
  className?: string;
}) {
  const titleId = useId();
  return (
    <section
      data-slot="danger-zone"
      aria-labelledby={titleId}
      className={cn("mt-8 rounded-xl border border-destructive/40 bg-card p-4", className)}
    >
      <h2 id={titleId} className="text-lg font-semibold text-destructive-text">
        {title}
      </h2>
      {description && <p className="mt-1 text-sm text-muted-foreground">{description}</p>}
      <div className="mt-3 divide-y">{children}</div>
    </section>
  );
}

/**
 * 위험 구역의 한 항목: 이름·설명(왼쪽) + 버튼(오른쪽, outline + 위험 글자). 버튼은 ConfirmDialog를 거친다
 * (ConfirmButton — 트리거 복귀·BL-150 포커스 보정 포함).
 */
export function DangerZoneItem({
  title,
  description,
  buttonLabel,
  confirm,
  onConfirm,
  disabled,
  focusAfterConfirm,
  confirmDisabled,
  confirmDisabledReason,
}: {
  title: string;
  description?: string;
  /** 오른쪽 버튼 문구(동사구) — 예: 「전체 재인덱싱」 */
  buttonLabel: string;
  /** 확인 다이얼로그 — 질문형 제목, 결과 설명, 동사 확인 버튼 */
  confirm: { title: string; description?: string; actionLabel: string };
  onConfirm: () => void;
  disabled?: boolean;
  focusAfterConfirm?: React.RefObject<HTMLElement | null>;
  confirmDisabled?: boolean;
  confirmDisabledReason?: string;
}) {
  return (
    <div className="flex flex-col gap-3 py-3 first:pt-0 last:pb-0 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <p className="text-sm font-medium">{title}</p>
        {description && <p className="text-sm text-muted-foreground">{description}</p>}
      </div>
      <ConfirmButton
        variant="outline"
        className="shrink-0 text-destructive-text hover:text-destructive-text coarse:min-h-11 coarse:min-w-11"
        disabled={disabled}
        title={confirm.title}
        description={confirm.description}
        actionLabel={confirm.actionLabel}
        onConfirm={onConfirm}
        focusAfterConfirm={focusAfterConfirm}
        confirmDisabled={confirmDisabled}
        confirmDisabledReason={confirmDisabledReason}
      >
        {buttonLabel}
      </ConfirmButton>
    </div>
  );
}
