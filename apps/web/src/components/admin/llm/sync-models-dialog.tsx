"use client";

import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { StatusBadge } from "@/components/ui/status-badge";
import { useProviderModelSync } from "@/hooks/use-admin-llm";
import type { AdminProvider } from "@/lib/admin-types";
import { describeError } from "@/lib/notify";

/**
 * 등록된 모델이 프로바이더에 여전히 있는지 확인한다 — 제어형(트리거 없음). 프로바이더 행의 더보기 메뉴
 * 「모델 동기화 확인」이 연다(§6.6.2 LLM 행). 열렸을 때만 조회한다. 닫힐 때 포커스는 그 행의 `⋯`로 돌아간다.
 */
export function SyncModelsDialog({
  provider,
  open,
  onOpenChange,
  returnFocusTo,
}: {
  provider: AdminProvider;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  returnFocusTo?: React.RefObject<HTMLElement | null>;
}) {
  const sync = useProviderModelSync(provider.id, open);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent finalFocus={returnFocusTo}>
        <DialogHeader>
          <DialogTitle>{provider.name} — 모델 동기화 확인</DialogTitle>
        </DialogHeader>
        <p className="text-xs text-muted-foreground">
          등록된 모델이 프로바이더에 여전히 있는지 확인합니다. 사라진 모델은 실행 기록을 지키려고 자동으로
          삭제하지 않고 표시만 합니다.
        </p>
        {sync.isLoading && (
          <p role="status" className="py-6 text-center text-sm text-muted-foreground">
            확인하는 중…
          </p>
        )}
        {sync.isError && (
          <p role="alert" className="py-6 text-center text-sm text-destructive">
            동기화 상태를 확인하지 못했습니다. {describeError(sync.error).join(" ")}
          </p>
        )}
        {sync.data && (
          <ul className="flex flex-col gap-1">
            {sync.data.map((m) => (
              <li key={m.id} className="flex items-center justify-between gap-2 py-1 text-sm">
                <span className="truncate">{m.display_name}</span>
                {m.still_available ? (
                  <StatusBadge tone="success">사용 가능</StatusBadge>
                ) : (
                  <StatusBadge tone="danger">프로바이더에서 사라짐</StatusBadge>
                )}
              </li>
            ))}
          </ul>
        )}
      </DialogContent>
    </Dialog>
  );
}
