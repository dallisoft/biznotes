"use client";

import { useEffect, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

/**
 * admin 폼 다이얼로그 골격 (R4-2 §4).
 *
 * 제출 중 비활성 · 에러 표면 · **닫힘 시 폼 리셋**을 한 곳에 모은다. 리셋이
 * 핵심이다 — 다이얼로그를 닫았다 다시 열면 직전 입력이 남아 있던 버그가
 * 페이지마다 따로 있었다.
 *
 * 폼 필드는 children으로 그대로 받는다(제네릭화하지 않는다). 필드 스키마를
 * 추상화하는 순간 커스텀 위젯 때문에 새는 추상화가 되기 때문이다.
 */
export function CrudFormDialog({
  open,
  onOpenChange,
  title,
  description,
  submitLabel = "저장",
  pending,
  error,
  onSubmit,
  onReset,
  trigger,
  className,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  submitLabel?: string;
  pending: boolean;
  /** 표면화할 제출 에러. 없으면 렌더하지 않는다. */
  error?: string | null;
  onSubmit: () => void;
  /** 다이얼로그가 닫힐 때 폼 상태를 초기화한다. */
  onReset?: () => void;
  /** 있으면 `<DialogTrigger>`로 감싼다(제어 컴포넌트로만 쓰려면 생략). */
  trigger?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  useEffect(() => {
    if (!open) onReset?.();
    // onReset은 매 렌더 새 함수일 수 있어 deps에 넣으면 열린 채로 리셋된다.
    // 열림/닫힘 전이에만 반응한다.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {trigger && <DialogTrigger render={<Button size="sm" />}>{trigger}</DialogTrigger>}
      <DialogContent className={className}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description && <DialogDescription>{description}</DialogDescription>}
        </DialogHeader>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            onSubmit();
          }}
          className="flex flex-col gap-3"
        >
          {children}
          {error && <p className="text-sm text-destructive">{error}</p>}
          <Button type="submit" disabled={pending}>
            {submitLabel}
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  );
}
