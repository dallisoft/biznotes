"use client";

import { ChevronDown, ChevronRight, EllipsisVertical } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { StatusBadge } from "@/components/ui/status-badge";
import { useHiddenAgentRuns, useHideFailedRuns, useHideRun, useUnhideRuns } from "@/hooks/use-agents";
import { RUN_STATUS_LABEL, type AgentRun } from "@/lib/agent-types";
import { formatDateTime } from "@/lib/format";
import { notifyError, notifySuccess } from "@/lib/notify";

/** 「실패한 결과 모두 숨기기」를 보이는 최소 개수 — 하나뿐이면 항목 메뉴로 충분하다(#220). */
export const BULK_HIDE_MIN = 2;

/** 일괄 숨기기 대상 — 호출자가 숨길 수 있는(`can_hide`) 실패 run. 취소한 run은 항목 메뉴로만 숨긴다. */
export function bulkHideableRuns(runs: AgentRun[]): AgentRun[] {
  return runs.filter((r) => r.status === "failed" && r.can_hide);
}

/**
 * 숨긴 뒤 「되돌리기」 토스트 — 되돌릴 수 있으므로 확인 대화상자를 두지 않는다(#220 결정 2).
 *
 * 숨긴 항목·일괄 버튼은 목록 재조회로 곧 언마운트된다. 언마운트된 관찰자에게는 `mutate(vars, { onSuccess })`의
 * 호출별 콜백이 오지 않으므로, 숨기기·되돌리기 모두 `mutateAsync` 약속에 잇는다(목록 무효화는 훅 옵션이라 그대로 돈다).
 */
function useUndoToast(notebookId: string) {
  const unhide = useUnhideRuns(notebookId);
  return (message: string, runIds: string[]) =>
    notifySuccess(message, {
      action: {
        label: "되돌리기",
        onClick: () => {
          // 목록은 모든 요청이 끝난 뒤 한 번 다시 읽고, 되돌리지 못한 것만 알린다(일괄이면 그 수)
          unhide.mutateAsync(runIds).then((errors) => {
            if (errors.length === 0) return;
            notifyError(
              runIds.length > 1 ? `결과 ${errors.length}개의 숨김을 해제` : "숨김을 해제",
              errors[0],
            );
          });
        },
      },
    });
}

/**
 * 실패·취소 run 항목의 케밥(#220) — 「숨기기」 하나. 서버가 호출자 기준으로 판정한 `can_hide`일 때만 그린다
 * (실행 소유자 ∨ 노트북 write 이상, 상태는 실패·취소). 스튜디오 성공 항목의 케밥과 같은 모양이다.
 */
export function RunHideMenu({
  notebookId,
  run,
  label,
}: {
  notebookId: string;
  run: AgentRun;
  /** 메뉴 버튼의 접근 이름 앞부분(「{label} 메뉴」) */
  label: string;
}) {
  const hide = useHideRun(notebookId);
  const undoToast = useUndoToast(notebookId);
  if (!run.can_hide) return null;
  return (
    <span onClick={(e) => e.stopPropagation()}>
      <DropdownMenu>
        <DropdownMenuTrigger
          render={
            <button
              type="button"
              aria-label={`${label} 메뉴`}
              className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
              data-testid="run-item-menu"
            >
              <EllipsisVertical className="size-3.5" />
            </button>
          }
        />
        <DropdownMenuContent align="end">
          <DropdownMenuItem
            disabled={hide.isPending}
            onClick={() => {
              hide.mutateAsync(run.id).then(
                () => undoToast("결과를 숨겼습니다", [run.id]),
                (err: unknown) => notifyError("결과를 숨김 처리", err),
              );
            }}
            data-testid="run-item-hide"
          >
            숨기기
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </span>
  );
}

/** 「최근 결과」 머리의 「실패한 결과 모두 숨기기」(#220) — 숨길 수 있는 실패 run이 {@link BULK_HIDE_MIN}개 이상일 때. */
export function HideFailedRunsButton({ notebookId, runs }: { notebookId: string; runs: AgentRun[] }) {
  const hideFailed = useHideFailedRuns(notebookId);
  const undoToast = useUndoToast(notebookId);
  if (bulkHideableRuns(runs).length < BULK_HIDE_MIN) return null;
  return (
    <Button
      variant="ghost"
      size="sm"
      className="h-6 px-1.5 text-xs text-muted-foreground"
      disabled={hideFailed.isPending}
      onClick={() => {
        hideFailed.mutateAsync().then(
          (res) => undoToast(`실패한 결과 ${res.hidden}개를 숨겼습니다`, res.run_ids),
          (err: unknown) => notifyError("실패한 결과를 숨김 처리", err),
        );
      }}
      data-testid="run-list-hide-failed"
    >
      실패한 결과 모두 숨기기
    </Button>
  );
}

/**
 * 「최근 결과」 아래 「숨긴 결과 N개 보기」(#221) — 숨긴 run이 있을 때만 그리고, 기본은 접혀 있다. 펼치면 숨긴 항목을
 * 흐린 글자·점선 테두리로 보이고, 되돌릴 수 있는 항목에 「다시 보이기」를 둔다. 숨긴 run의 `can_hide`는 서버가
 * 호출자 기준으로 판정한 되돌리기 인가다(취소 규칙 — 실행 소유자 ∨ 노트북 write 이상).
 */
export function HiddenRunsSection({ notebookId }: { notebookId: string }) {
  const hidden = useHiddenAgentRuns(notebookId);
  const unhide = useUnhideRuns(notebookId);
  const [open, setOpen] = useState(false);
  const runs = hidden.data ?? [];
  if (runs.length === 0) return null;
  return (
    <div className="flex flex-col gap-1 px-2 pb-2">
      <Button
        variant="ghost"
        size="sm"
        className="h-6 justify-start gap-1 px-1.5 text-xs text-muted-foreground"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        data-testid="hidden-run-toggle"
      >
        {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
        {open ? "숨긴 결과 접기" : `숨긴 결과 ${runs.length}개 보기`}
      </Button>
      {open && (
        <ul className="flex flex-col gap-1" aria-label="숨긴 결과" data-testid="hidden-run-list">
          {runs.map((run) => (
            <li
              key={run.id}
              className="flex items-center justify-between gap-2 rounded-md border border-dashed p-2 text-xs text-muted-foreground"
              data-testid={`hidden-run-item-${run.id}`}
            >
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="flex min-w-0 items-center gap-1">
                  <span aria-hidden>{run.agent_icon}</span>
                  <span className="truncate">{run.agent_name}</span>
                </span>
                <span>{formatDateTime(run.created_at)}</span>
              </span>
              <span className="flex shrink-0 items-center gap-1">
                <StatusBadge tone={run.status === "failed" ? "danger" : "neutral"}>
                  {RUN_STATUS_LABEL[run.status] ?? run.status}
                </StatusBadge>
                {run.can_hide && (
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-6 px-1.5 text-xs"
                    disabled={unhide.isPending}
                    aria-label={`${run.agent_name ?? "실행 결과"} 다시 보이기`}
                    onClick={() => {
                      // 목록 둘(최근·숨긴)은 훅이 요청이 끝난 뒤 다시 읽는다 — 항목이 「최근 결과」로 옮겨 간다
                      unhide.mutateAsync([run.id]).then((errors) => {
                        if (errors.length > 0) notifyError("숨김을 해제", errors[0]);
                      });
                    }}
                    data-testid="hidden-run-unhide"
                  >
                    다시 보이기
                  </Button>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
