"use client";

import { useQuery } from "@tanstack/react-query";
import { FileText } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { defaultValues, SchemaForm } from "@/components/agents/schema-form";
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
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useCreateRun } from "@/hooks/use-agents";
import { useNotebooks } from "@/hooks/use-notebooks";
import { api } from "@/lib/api-client";
import { RUN_STATUS_LABEL, type AgentRun } from "@/lib/agent-types";
import type { Workflow } from "@/lib/workflow-types";

const ACTIVE = new Set(["queued", "running"]);

export function TestRunDialog({
  workflow,
  open,
  onOpenChange,
}: {
  workflow: Workflow;
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const notebooks = useNotebooks("all");
  const [notebookId, setNotebookId] = useState("");
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [runId, setRunId] = useState<string | null>(null);
  const createRun = useCreateRun(notebookId);

  useEffect(() => {
    if (open) {
      setValues(defaultValues(workflow.params_schema));
      setRunId(null);
    }
  }, [open, workflow.params_schema]);

  // 생성된 run을 종료 상태까지 폴링 (SSE 채널과 동일 데이터, 단순 폴링)
  const run = useQuery<AgentRun>({
    queryKey: ["test-run", runId],
    queryFn: () => api.get<AgentRun>(`/api/v1/agent-runs/${runId}`),
    enabled: !!runId,
    refetchInterval: (q) => (q.state.data && ACTIVE.has(q.state.data.status) ? 1500 : false),
  });

  function start() {
    if (!notebookId) {
      toast.error("실행할 노트북을 선택하세요");
      return;
    }
    createRun.mutate(
      { agent_id: workflow.id, params: values },
      {
        onSuccess: (r) => setRunId(r.id),
        onError: (err) => toast.error(`실행 실패: ${err.message}`),
      },
    );
  }

  const r = run.data;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>테스트 실행 — {workflow.name}</DialogTitle>
          <DialogDescription>
            노트북과 파라미터를 골라 워크플로우를 1회 실행하고 결과를 확인합니다.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1">
            <Label className="text-xs">노트북</Label>
            <Select value={notebookId} onValueChange={(v) => v && setNotebookId(v)}>
              <SelectTrigger className="w-full" data-testid="test-run-notebook">
                <SelectValue placeholder="노트북 선택" />
              </SelectTrigger>
              <SelectContent>
                {(notebooks.data ?? []).map((nb) => (
                  <SelectItem key={nb.id} value={nb.id}>
                    {nb.emoji} {nb.title}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <SchemaForm schema={workflow.params_schema} values={values} onChange={setValues} />

          {r && (
            <div className="rounded-md border p-3 text-xs" data-testid="test-run-result">
              <div className="flex items-center justify-between">
                <span className="font-medium">실행 상태</span>
                <Badge variant={r.status === "failed" ? "destructive" : "secondary"}>
                  {RUN_STATUS_LABEL[r.status]}
                  {r.status === "running" && ` ${r.progress}%`}
                </Badge>
              </div>
              {r.result_summary && (
                <p className="mt-2 whitespace-pre-wrap text-muted-foreground">
                  {r.result_summary.slice(0, 500)}
                </p>
              )}
              {r.error && <p className="mt-2 text-destructive">{r.error}</p>}
              {r.artifact_keys.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1">
                  {r.artifact_keys.map((a) => (
                    <span
                      key={a.key}
                      className="inline-flex items-center gap-1 rounded bg-primary/10 px-1.5 py-0.5 text-xs text-primary"
                    >
                      <FileText className="size-3" /> {a.name}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button
            onClick={start}
            disabled={createRun.isPending || (!!r && ACTIVE.has(r.status))}
            data-testid="test-run-start"
          >
            {r && ACTIVE.has(r.status) ? "실행 중..." : "실행"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
