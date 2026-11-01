"use client";

import { useRef, useState } from "react";
import { AlertDialogTrigger } from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";

// browser confirm() 대체 — 트리거 버튼 + 확인 다이얼로그 일체형. 다이얼로그·포커스 계약은 ConfirmDialog가 맡고,
// 이 래퍼는 자기 트리거를 복귀 대상(returnFocusTo)으로 넘긴다(§6.5).
export function ConfirmButton({
  title,
  description,
  actionLabel = "삭제",
  destructive = true,
  onConfirm,
  focusAfterConfirm,
  confirmDisabled = false,
  confirmDisabledReason,
  children,
  ...buttonProps
}: {
  title: string;
  description?: string;
  actionLabel?: string;
  destructive?: boolean;
  onConfirm: () => void;
  /**
   * 확인으로 닫힌 뒤 포커스를 옮길 요소(선택). 확인 직후 트리거가 비활성화되는 화면에서
   * 포커스가 body로 유실되지 않게 한다. 지정하지 않으면 기본 동작(트리거로 복귀). 취소·Esc는 트리거로
   * 돌아가되, 트리거가 그사이 비활성이 됐으면 이 요소로 보낸다(BL-150 P1R-01).
   */
  focusAfterConfirm?: React.RefObject<HTMLElement | null>;
  /** 다이얼로그가 열린 동안 상태가 바뀌어 확인을 막아야 할 때(선택). 기본 false. */
  confirmDisabled?: boolean;
  /** `confirmDisabled`일 때 다이얼로그 안에 보일 사유(선택). */
  confirmDisabledReason?: string;
} & React.ComponentProps<typeof Button>) {
  // 확인 즉시 닫아 뒤의 화면(예: admin/rag 재인덱싱 진행 패널)이 모달·inert에 가려지지 않게 한다
  // (reindex-progress Phase 1) — ConfirmDialog가 확인 클릭에서 onOpenChange(false)를 부른다.
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={setOpen}
      title={title}
      description={description}
      actionLabel={actionLabel}
      destructive={destructive}
      onConfirm={onConfirm}
      returnFocusTo={triggerRef}
      focusAfterConfirm={focusAfterConfirm}
      confirmDisabled={confirmDisabled}
      confirmDisabledReason={confirmDisabledReason}
      trigger={
        <AlertDialogTrigger render={<Button {...buttonProps} ref={triggerRef} />}>
          {children}
        </AlertDialogTrigger>
      }
    />
  );
}
