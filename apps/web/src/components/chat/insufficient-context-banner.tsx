"use client";

import { SearchX } from "lucide-react";
import { Button } from "@/components/ui/button";

/** [Phase 18b] 검색 근거 부족 시 노출하는 배너 — 웹 검색/소스 추가를 유도한다. */
export function InsufficientContextBanner({ onAddSource }: { onAddSource?: () => void }) {
  return (
    <div
      className="flex items-center gap-2 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning-text"
      data-testid="insufficient-context-banner"
    >
      <SearchX className="size-3.5 shrink-0" />
      <span className="flex-1">
        등록된 소스에서 충분한 근거를 찾지 못했습니다. 소스를 추가하거나 질문을 구체화해 보세요.
      </span>
      {onAddSource && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-6 shrink-0 px-2 text-xs text-warning-text hover:bg-warning/20"
          onClick={onAddSource}
        >
          소스 추가
        </Button>
      )}
    </div>
  );
}
