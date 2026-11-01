"use client";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * 폼이 바뀌었을 때만 화면 아래에 고정되는 저장 막대(§6.5): 안내 문구 + 「되돌리기」 + 「변경 사항 저장」(primary).
 * 저장 중에는 두 버튼을 비활성화한다. 성공 토스트는 호출부가 띄운다.
 */
export function StickySaveBar({
  dirty,
  saving = false,
  onSave,
  onReset,
  message = "저장되지 않은 변경 사항이 있습니다",
  className,
}: {
  dirty: boolean;
  saving?: boolean;
  onSave: () => void;
  onReset: () => void;
  message?: string;
  className?: string;
}) {
  if (!dirty) return null;
  return (
    <div
      data-slot="sticky-save-bar"
      role="region"
      aria-label="저장되지 않은 변경 사항"
      className={cn(
        "sticky bottom-0 z-10 mt-6 flex flex-col gap-3 border-t bg-background px-4 py-3 shadow-sm sm:flex-row sm:items-center sm:justify-between",
        className,
      )}
    >
      <p role="status" className="text-sm">
        {message}
      </p>
      <div className="flex gap-2 max-sm:*:flex-1">
        <Button variant="outline" className="coarse:min-h-11 coarse:min-w-11" onClick={onReset} disabled={saving}>
          되돌리기
        </Button>
        <Button className="coarse:min-h-11 coarse:min-w-11" onClick={onSave} disabled={saving}>
          {saving ? "저장 중…" : "변경 사항 저장"}
        </Button>
      </div>
    </div>
  );
}
