"use client";

import { Badge } from "@/components/ui/badge";
import type { BulkItemResult, BulkItemStatus } from "@/lib/notebook-bulk";

const STATUS_LABELS: Record<BulkItemStatus, string> = {
  succeeded: "완료",
  skipped: "건너뜀",
  failed: "실패",
  not_run: "미실행",
};

const STATUS_VARIANTS: Record<BulkItemStatus, "secondary" | "outline" | "destructive"> = {
  succeeded: "secondary",
  skipped: "secondary",
  failed: "destructive",
  not_run: "outline",
};

/**
 * 일괄 작업의 항목별 결과 목록(plan §5.4). 삭제(Phase 2)와 공유(Phase 3)가 같이 쓴다 —
 * 상태와 사유 문구는 호출자가 결과에 담아 넘긴다. 응답 손실 항목은 「확인 불가」로 따로 표시한다(PR-05).
 */
export function BulkResultList({
  results,
  summary,
  notice,
}: {
  results: readonly BulkItemResult[];
  /** 요약 한 줄(예: 「삭제 a · 실패 b · 확인 불가 u · 미실행 c」). */
  summary: string;
  /** 중지·전역 중단 안내. */
  notice?: string | null;
}) {
  return (
    <div className="grid gap-2" data-testid="bulk-result">
      <p className="text-sm font-medium" data-testid="bulk-result-summary">
        {summary}
      </p>
      {notice && (
        <p className="text-sm text-destructive" role="alert" data-testid="bulk-result-notice">
          {notice}
        </p>
      )}
      <ul
        aria-label="항목별 결과"
        className="max-h-64 divide-y overflow-y-auto rounded-lg border text-sm"
      >
        {results.map((r) => {
          const label = r.status === "failed" && r.unconfirmed ? "확인 불가" : STATUS_LABELS[r.status];
          return (
            <li
              key={r.id}
              className="flex items-start gap-2 px-3 py-2"
              data-testid="bulk-result-item"
              data-status={r.status}
              data-unconfirmed={r.unconfirmed || undefined}
            >
              <Badge variant={STATUS_VARIANTS[r.status]} className="shrink-0">
                {label}
              </Badge>
              <div className="min-w-0">
                <p className="truncate font-medium">{r.title}</p>
                <p className="text-xs text-muted-foreground">{r.message}</p>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
