"use client";

import { AlertTriangle, Check, X } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { useAgentRunApprovals, useResolveApproval } from "@/hooks/use-agents";
import type { AgentRun } from "@/lib/agent-types";

/**
 * HITL 승인 카드 — run.status가 awaiting_approval일 때 대기 중인 부작용 도구
 * 호출을 사용자가 승인/거부한다(phase-19-design.md §1.1, §4.1).
 *
 * arguments_summary는 백엔드가 마스킹한 값만 노출하며(SEC-D1·CDX-D7),
 * checkpoint(재개용 임시 상태)는 API 응답에 아예 포함되지 않는다.
 */
export function AgentApprovalCard({
  run,
  notebookId,
}: {
  run: AgentRun;
  notebookId: string;
}) {
  const approvals = useAgentRunApprovals(run.status === "awaiting_approval" ? run.id : null);
  const resolve = useResolveApproval(notebookId, run.id);

  if (run.status !== "awaiting_approval") return null;

  const pending = (approvals.data ?? []).find((a) => a.status === "pending");
  if (!pending) return null;

  function handle(outcome: "approve" | "reject") {
    if (!pending) return;
    resolve.mutate(
      { approvalId: pending.id, outcome },
      {
        onSuccess: () =>
          toast.success(outcome === "approve" ? "도구 실행을 승인했습니다" : "도구 실행을 거부했습니다"),
        onError: (err) => toast.error(`처리 실패: ${err.message}`),
      },
    );
  }

  return (
    <div
      className="mt-1 rounded-md border border-warning/40 bg-warning/10 p-2 text-xs"
      data-testid={`approval-card-${run.id}`}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="flex items-start gap-1.5">
        <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-warning" />
        <div className="min-w-0 flex-1">
          <p className="font-medium text-warning-text">
            승인 필요: <code className="font-mono">{pending.tool_name}</code>
          </p>
          <pre className="mt-1 max-h-24 overflow-auto rounded bg-black/5 p-1 text-xs whitespace-pre-wrap break-all dark:bg-white/5">
            {JSON.stringify(pending.arguments_summary, null, 2)}
          </pre>
          <div className="mt-1.5 flex gap-1.5">
            <Button
              size="sm"
              className="h-6 flex-1 gap-1 px-2 text-xs"
              disabled={resolve.isPending}
              onClick={(e) => {
                e.stopPropagation();
                handle("approve");
              }}
              data-testid={`approve-btn-${run.id}`}
            >
              <Check className="size-3" /> 승인
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-6 flex-1 gap-1 px-2 text-xs"
              disabled={resolve.isPending}
              onClick={(e) => {
                e.stopPropagation();
                handle("reject");
              }}
              data-testid={`reject-btn-${run.id}`}
            >
              <X className="size-3" /> 거부
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
