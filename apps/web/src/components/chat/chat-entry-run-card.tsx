"use client";

import { useQueryClient } from "@tanstack/react-query";
import { FileText, Square, Zap } from "lucide-react";
import { lazy, Suspense, useEffect, useState } from "react";
import { toast } from "sonner";
import { AgentApprovalCard } from "@/components/agents/agent-approval-card";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import { useAgentRun, useCancelRun } from "@/hooks/use-agents";
import type { ArtifactRef } from "@/lib/agent-types";
import {
  ENTRY_RUN_STATUS_LABEL,
  ENTRY_RUN_STATUS_TONE,
  isActiveRunStatus,
} from "@/lib/chat-entry";
import type { ChatEntryRunRef } from "@/lib/chat-types";

// 산출물 뷰어는 열 때만 불러온다 — 뷰어 묶음(마인드맵·슬라이드 등)을 채팅 화면 첫 로드에 싣지 않는다
const ArtifactContent = lazy(() =>
  import("@/components/agents/artifact-content").then((m) => ({ default: m.ArtifactContent })),
);

/**
 * 채팅 진입 run 카드(#66) — 진입 질문 아래에 붙어 진행 상태·승인·중지·산출물을 보인다.
 *
 * 상태는 run 단건을 폴링해 읽는다(`useAgentRun` — 끝나지 않은 동안 2초). 새로고침 뒤에도 질문 행의
 * `entry_run`으로 같은 run에 다시 붙는다. run이 끝났는데 메시지 목록이 아직 끝나지 않은 상태로 알고 있으면
 * 메시지를 다시 읽는다 — 결과 행이 들어오고 입력창 잠금(D3)이 풀린다.
 */
export function ChatEntryRunCard({
  entryRun,
  sessionId,
  notebookId,
  canWrite,
}: {
  entryRun: ChatEntryRunRef;
  sessionId: string;
  notebookId: string;
  /** 노트북 write 이상 — 산출물 뷰어의 「메모로 저장」을 보인다. */
  canWrite: boolean;
}) {
  const queryClient = useQueryClient();
  const run = useAgentRun(entryRun.run_id, entryRun.status);
  const cancel = useCancelRun(notebookId);
  const [openArtifact, setOpenArtifact] = useState<ArtifactRef | null>(null);

  const status = run.data?.status ?? entryRun.status;
  const active = isActiveRunStatus(status);
  const knownActive = isActiveRunStatus(entryRun.status);

  useEffect(() => {
    if (run.data && !isActiveRunStatus(run.data.status) && knownActive) {
      void queryClient.invalidateQueries({ queryKey: ["chat-messages", sessionId] });
    }
  }, [run.data, knownActive, queryClient, sessionId]);

  function stop() {
    cancel.mutate(entryRun.run_id, {
      onSuccess: () => {
        void queryClient.invalidateQueries({ queryKey: ["agent-run", entryRun.run_id] });
        void queryClient.invalidateQueries({ queryKey: ["chat-messages", sessionId] });
      },
      onError: (err) => toast.error(`중지하지 못했습니다: ${err.message}`),
    });
  }

  const artifacts = (run.data?.artifact_keys ?? []) as ArtifactRef[];

  return (
    <div
      className="ml-auto w-full max-w-[85%] rounded-xl border bg-card px-3 py-2 text-sm"
      data-testid={`entry-run-card-${entryRun.run_id}`}
    >
      <div className="flex items-center gap-2">
        <Zap className="size-3.5 shrink-0 text-primary" aria-hidden />
        <span className="truncate font-medium">{entryRun.display_name}</span>
        {entryRun.slash_name && (
          <code className="shrink-0 font-mono text-xs text-muted-foreground">/{entryRun.slash_name}</code>
        )}
        <StatusBadge
          tone={ENTRY_RUN_STATUS_TONE[status]}
          pulse={status === "running"}
          className="ml-auto"
          data-testid="entry-run-status"
        >
          {ENTRY_RUN_STATUS_LABEL[status]}
        </StatusBadge>
        {active && (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="h-6 gap-1 px-2 text-xs"
            disabled={cancel.isPending}
            onClick={stop}
          >
            <Square className="size-3 fill-current" aria-hidden /> 중지
          </Button>
        )}
      </div>
      {run.isError && !run.data && (
        <p className="mt-1 text-xs text-muted-foreground" role="status" data-testid="entry-run-error">
          {run.error.status >= 400 && run.error.status < 500
            ? "실행 정보를 볼 수 없습니다"
            : "실행 상태를 불러오지 못했습니다 — 잠시 뒤 다시 확인합니다"}
        </p>
      )}
      {run.data?.status === "awaiting_approval" && (
        <AgentApprovalCard run={run.data} notebookId={notebookId} />
      )}
      {artifacts.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1.5" aria-label="산출물">
          {artifacts.map((artifact) => (
            <Button
              key={artifact.key}
              type="button"
              size="sm"
              variant="outline"
              className="h-6 gap-1 px-2 text-xs"
              onClick={() => setOpenArtifact(artifact)}
            >
              <FileText className="size-3" aria-hidden /> {artifact.name}
            </Button>
          ))}
        </div>
      )}
      {run.data && openArtifact && (
        <Dialog open onOpenChange={(open) => !open && setOpenArtifact(null)}>
          <DialogContent className="flex h-[80vh] flex-col sm:max-w-3xl">
            <DialogHeader>
              <DialogTitle>{openArtifact.name}</DialogTitle>
            </DialogHeader>
            <div className="min-h-0 flex-1">
              <Suspense fallback={<Skeleton className="h-40" />}>
                <ArtifactContent
                  run={run.data}
                  artifact={openArtifact}
                  notebookId={notebookId}
                  canWrite={canWrite}
                />
              </Suspense>
            </div>
          </DialogContent>
        </Dialog>
      )}
    </div>
  );
}
