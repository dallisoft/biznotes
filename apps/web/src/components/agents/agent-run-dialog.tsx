"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";
import { RunModelSelect } from "@/components/agents/run-model-select";
import { defaultValues, SchemaForm } from "@/components/agents/schema-form";
import { SourcePicker } from "@/components/agents/source-picker";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useCreateRun } from "@/hooks/use-agents";
import { INPUT_MODE_LABEL, INPUT_SCOPE_LABEL, type Agent } from "@/lib/agent-types";
import { formatCurrencyUSD, formatCurrencyUSDTitle } from "@/lib/format";
import type { StudioCard } from "@/lib/studio-registry";

export function AgentRunDialog({
  agent,
  notebookId,
  open,
  onOpenChange,
  studioCard = null,
  initialSourceIds,
}: {
  agent: Agent | null;
  notebookId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /**
   * 스튜디오 카드로 연 다이얼로그 — 에이전트의 input 정책과 무관하게 소스 선택기를 보이고(mindmap-agent-skill AC-1.9),
   * 카드의 아이콘·설명을 쓰며 입력 정책 배지를 숨긴다(#182 — 소스 선택기가 정책을 대신 보여준다).
   */
  studioCard?: StudioCard | null;
  /** 소스 선택 기본값(스튜디오: 현재 소스 패널 선택). 빈 배열 = 노트북 전체. */
  initialSourceIds?: string[];
}) {
  const createRun = useCreateRun(notebookId);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [sourceIds, setSourceIds] = useState<string[]>([]);
  // 실행 모델(#216) — null = 「기본 모델」(요청에 model_id를 넣지 않는다)
  const [modelId, setModelId] = useState<string | null>(null);

  useEffect(() => {
    if (agent) {
      setValues(defaultValues(agent.params_schema));
      setSourceIds(initialSourceIds ?? []);
      setModelId(null);
    }
    // 정당: 다이얼로그를 여는 시점(agent 변경)의 기본값만 쓴다 — 소스 패널 선택이 바뀌어도 열린 폼을 덮지 않는다
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent]);

  const input = agent?.input;
  const selectScope = studioCard !== null || input?.scope === "selected";

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!agent) return;
    createRun.mutate(
      {
        agent_id: agent.id,
        params: values,
        // scope:selected이고 사용자가 고른 경우에만 전송. 빈 배열은 노트 전체 폴백.
        ...(selectScope && sourceIds.length > 0 ? { source_ids: sourceIds } : {}),
        ...(modelId ? { model_id: modelId } : {}),
      },
      {
        onSuccess: () => {
          toast.success(`"${agent.name}" 실행을 시작했습니다`);
          onOpenChange(false);
        },
        onError: (err) => toast.error(`실행 실패: ${err.message}`),
      },
    );
  }

  if (!agent) return null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <span>{studioCard?.icon ?? agent.icon}</span> {agent.name}
          </DialogTitle>
          <DialogDescription>{studioCard?.description ?? agent.description}</DialogDescription>
        </DialogHeader>
        {input && !studioCard && (
          <div className="flex items-center gap-1.5" data-testid="input-policy-badges">
            <Badge variant="outline" className="text-xs">
              {INPUT_MODE_LABEL[input.mode]}
            </Badge>
            <Badge variant="outline" className="text-xs">
              {INPUT_SCOPE_LABEL[input.scope]}
            </Badge>
          </div>
        )}
        {typeof agent.cost_budget_usd === "number" && (
          <p className="text-xs text-muted-foreground" data-testid="cost-budget-notice">
            이 작업은 최대{" "}
            <span title={formatCurrencyUSDTitle(agent.cost_budget_usd)}>
              {formatCurrencyUSD(agent.cost_budget_usd)}
            </span>{" "}
            USD까지 과금될 수 있습니다.
          </p>
        )}
        <form onSubmit={handleSubmit} className="flex min-w-0 flex-col gap-4">
          <SchemaForm schema={agent.params_schema} values={values} onChange={setValues} />
          {selectScope && (
            <SourcePicker
              notebookId={notebookId}
              selected={sourceIds}
              onChange={setSourceIds}
            />
          )}
          <RunModelSelect value={modelId} onChange={setModelId} />
          <DialogFooter>
            <Button type="submit" disabled={createRun.isPending} data-testid="run-agent">
              {createRun.isPending ? "시작 중..." : "생성"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
