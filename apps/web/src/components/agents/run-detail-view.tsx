"use client";

import {
  ArrowLeft,
  Check,
  ChevronRight,
  Clock,
  Cpu,
  FileText,
  ShieldAlert,
  Wrench,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { ArtifactContent } from "@/components/agents/artifact-content";
import { RunSourceCoverage } from "@/components/agents/run-source-coverage";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge, type StatusTone } from "@/components/ui/status-badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAgentRunApprovals, useAgentRunLogs } from "@/hooks/use-agents";
import {
  APPROVAL_STATUS_LABEL,
  REACT_TOOL_CALL_NODE_TYPE,
  RUN_STATUS_LABEL,
  type AgentRun,
  type AgentRunApproval,
  type ExecutionLog,
} from "@/lib/agent-types";
import { formatRelative } from "@/lib/format";
import { runStatusLabel } from "@/lib/labels";
import { modelAttemptLabel, nodeErrorLabel } from "@/lib/node-error-labels";
import { visibleArtifacts } from "@/lib/run-coverage";
import type { StudioCard } from "@/lib/studio-registry";

/** node_id 기준으로 react_tool_call 보조행을 부모 react_agent 노드 아래로 그룹핑
 * (ReAct Phase 2 Q5: 보조행 id는 "{부모node_id}:tool:{idx}" 형식). 그 외 노드는
 * 자기 자신만 담은 그룹으로 취급한다. */
export function groupLogs(
  logs: ExecutionLog[],
): { parent: ExecutionLog; children: ExecutionLog[] }[] {
  const toolCalls = logs.filter((l) => l.node_type === REACT_TOOL_CALL_NODE_TYPE);
  const primary = logs.filter((l) => l.node_type !== REACT_TOOL_CALL_NODE_TYPE);
  return primary.map((parent) => ({
    parent,
    children: toolCalls.filter((c) => c.node_id.startsWith(`${parent.node_id}:tool:`)),
  }));
}

/**
 * 실행 로그에서 실제로 응답한 모델 이름(처음 나온 순서, 중복 없음)과 폴백 여부 — 상세 머리의 「사용 모델」
 * 요약(#216). 노드별 값은 「실행 로그」 탭의 행이 보인다.
 */
export function usedModels(logs: ExecutionLog[]): { names: string[]; fallback: boolean } {
  const names: string[] = [];
  let fallback = false;
  for (const log of logs) {
    if (!log.model) continue;
    if (!names.includes(log.model.display_name)) names.push(log.model.display_name);
    if (log.fallback_used) fallback = true;
  }
  return { names, fallback };
}

/** 노드 실행 로그 상태 배지 — 문구는 실행 상태 라벨 사전(lib/labels), 미지 값은 원시값 폴백. */
export function LogStatusBadge({ status }: { status: string }) {
  const tone: StatusTone =
    status === "succeeded" ? "success" : status === "failed" ? "danger" : "neutral";
  return <StatusBadge tone={tone}>{runStatusLabel(status)}</StatusBadge>;
}

/**
 * 노드 로그 행. **실패 노드에만** 코드별 공개 문구(`nodeErrorLabel(error_code)` — 모르는 코드·null은 일반 문구)와
 * `error_message`를 보인다(core-v0.20.0-upgrade §5.5). `error_message`는 코어가 민감 부류(모델·공급자 원문)를
 * 이미 고정 문구로 바꾼 값이고, 외부 문자열이라 React 텍스트로만 그린다. 성공·실행 중 행에는 보이지 않는다.
 *
 * 모델(#216): 응답한 모델이 있는 행(성공한 llm·react_agent)은 모델 이름과, 1차 모델 대신 폴백 모델이 답했으면
 * 「폴백」 배지를 보인다. 모든 후보가 실패한 행은 시도한 모델과 각 실패 문구(`modelAttemptLabel`)를 순서대로 보인다.
 */
function LogRow({ log, indented = false }: { log: ExecutionLog; indented?: boolean }) {
  const failed = log.status === "failed";
  const attempts = failed ? (log.model_attempts ?? []) : [];
  return (
    <div
      className={`border-b py-1.5 text-xs last:border-b-0 ${indented ? "pl-6" : ""}`}
      data-testid={`execution-log-${log.id}`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-1.5">
          {indented && <Wrench className="size-3 shrink-0 text-muted-foreground" />}
          <span className="truncate font-mono">{log.node_id}</span>
          <span className="shrink-0 text-muted-foreground">{log.node_type}</span>
        </span>
        <span className="flex shrink-0 items-center gap-2 text-muted-foreground">
          {log.duration_ms !== null && <span>{log.duration_ms}ms</span>}
          <LogStatusBadge status={log.status} />
        </span>
      </div>
      {log.model && (
        <p className="mt-1 flex min-w-0 items-center gap-1.5 text-muted-foreground" data-testid="node-model">
          <Cpu className="size-3 shrink-0" aria-hidden />
          <span className="sr-only">사용 모델</span>
          <span className="truncate">{log.model.display_name}</span>
          {log.fallback_used && (
            <StatusBadge tone="warning" title="1차 모델이 실패해 폴백 모델이 응답했습니다" data-testid="node-fallback">
              폴백
            </StatusBadge>
          )}
        </p>
      )}
      {failed && (
        <div className="mt-1 flex flex-col gap-0.5">
          <p className="font-medium text-destructive" data-testid="node-error-label">
            {nodeErrorLabel(log.error_code)}
          </p>
          {log.error_message && (
            <p
              className="whitespace-pre-wrap break-words text-muted-foreground"
              data-testid="node-error-message"
            >
              {log.error_message}
            </p>
          )}
          {attempts.length > 0 && (
            <div className="mt-0.5" data-testid="node-model-attempts">
              <p className="text-muted-foreground">시도한 모델</p>
              <ol className="flex flex-col gap-0.5">
                {attempts.map((a, i) => (
                  <li key={`${a.id}-${i}`} className="flex min-w-0 items-center gap-1.5">
                    <Cpu className="size-3 shrink-0 text-muted-foreground" aria-hidden />
                    <span className="truncate font-medium">{a.display_name}</span>
                    <span className="shrink-0 text-muted-foreground">— {modelAttemptLabel(a.error_code)}</span>
                  </li>
                ))}
              </ol>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ApprovalHistoryRow({ approval }: { approval: AgentRunApproval }) {
  const icon =
    approval.status === "approved" ? (
      <Check className="size-3 text-success" />
    ) : approval.status === "rejected" ? (
      <X className="size-3 text-destructive" />
    ) : approval.status === "expired" ? (
      <Clock className="size-3 text-muted-foreground" />
    ) : (
      <ShieldAlert className="size-3 text-warning" />
    );
  return (
    <div
      className="flex items-center justify-between gap-2 border-b py-1.5 text-xs last:border-b-0"
      data-testid={`approval-history-${approval.id}`}
    >
      <span className="flex min-w-0 items-center gap-1.5">
        {icon}
        <code className="truncate font-mono">{approval.tool_name}</code>
      </span>
      <span className="flex shrink-0 items-center gap-2 text-muted-foreground">
        <span>{APPROVAL_STATUS_LABEL[approval.status]}</span>
        <span>{formatRelative(approval.requested_at)}</span>
      </span>
    </div>
  );
}

const ACTIVE_RUN_STATUS = new Set<AgentRun["status"]>([
  "queued",
  "running",
  "awaiting_approval",
]);

type DetailTab = "result" | "logs";

/** 「결과」 탭 — 산출물 본문(여럿이면 고르는 줄), 없으면 진행·오류·요약 안내. */
function RunResult({
  run,
  notebookId,
  canWrite,
  selectedKey,
  onSelect,
}: {
  run: AgentRun;
  notebookId: string;
  canWrite: boolean;
  selectedKey: string | undefined;
  onSelect: (key: string) => void;
}) {
  // 반영 현황(`coverage.json`, #217)은 본문이 아니라 결과 위 표시로 보인다 — 산출물 선택 줄에서 뺀다
  const artifacts = visibleArtifacts(run);
  const selected = artifacts.find((a) => a.key === selectedKey) ?? artifacts[0];

  if (selected) {
    return (
      <>
        <RunSourceCoverage run={run} notebookId={notebookId} />
        {artifacts.length > 1 && (
          <div className="flex shrink-0 flex-wrap gap-1 px-3 pt-2" role="group" aria-label="산출물 선택">
            {artifacts.map((a) => (
              <button
                key={a.key}
                type="button"
                aria-pressed={a.key === selected.key}
                onClick={() => onSelect(a.key)}
                className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs text-primary hover:bg-primary/20 aria-pressed:bg-primary/15 aria-pressed:font-medium"
              >
                <FileText className="size-3" /> {a.name}
              </button>
            ))}
          </div>
        )}
        <ArtifactContent
          key={selected.key}
          run={run}
          artifact={selected}
          notebookId={notebookId}
          canWrite={canWrite}
        />
      </>
    );
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-3 text-sm">
      {ACTIVE_RUN_STATUS.has(run.status) ? (
        <p className="text-muted-foreground">실행 중입니다. 완료되면 결과가 여기에 표시됩니다.</p>
      ) : run.error ? (
        <p className="text-destructive">{run.error}</p>
      ) : run.result_summary ? (
        <p className="whitespace-pre-wrap">{run.result_summary}</p>
      ) : (
        <p className="text-muted-foreground">
          표시할 산출물이 없습니다. 진행 기록은 「실행 로그」 탭에서 확인하세요.
        </p>
      )}
    </div>
  );
}

/**
 * run 상세 뷰 — 「결과」 탭(산출물 본문)과 「실행 로그」 탭(노드별 실행 로그·react_agent tool-call 보조행(19b)·
 * 승인/거부/자동승인 이력(19c))으로 나눈다. 실행 결과를 열면 결과(문서)가 먼저 보인다.
 *
 * [리디자인] 모달(Dialog) 대신 에이전트 패널 안에 인라인으로 표시한다.
 * 상단에 "← 목록" 뒤로가기 버튼과 상태 헤더를 두고, 탭 내용은 패널 높이에 맞춰 세로 스크롤된다.
 * `runs`(useAgentRuns 폴링 결과)에서 동일 id의 최신 run을 찾아 반영한다 — `run` prop은 선택 시점
 * 스냅샷이라 패널을 연 채로는 갱신되지 않으므로 폴링 리스트의 최신값을 우선한다(실행 중에 열어도
 * 완료되면 결과 탭에 산출물이 나타난다). 열리면 제목으로 포커스를 옮긴다(돌아갈 때의 포커스 복귀는
 * AgentPanel이 맡는다).
 */
export function RunDetailView({
  run,
  runs,
  notebookId,
  canWrite,
  initialArtifactKey,
  studioCard = null,
  onBack,
}: {
  run: AgentRun;
  runs?: AgentRun[];
  notebookId: string;
  /** 노트북 write 이상 — read 공유자에게는 쓰기 동작(메모로 저장)을 숨긴다(#174). */
  canWrite: boolean;
  /** 산출물 칩(또는 스튜디오 항목의 대표 산출물)으로 열었으면 그 산출물을 먼저 보인다. */
  initialArtifactKey?: string;
  /** 스튜디오 항목이면 그 카드 — 머리를 「스튜디오 › 카드 이름」으로 보인다(판정은 AgentPanel이 에이전트 id로 한다). */
  studioCard?: StudioCard | null;
  onBack: () => void;
}) {
  const liveRun = runs?.find((r) => r.id === run.id) ?? run;
  const isActive = ACTIVE_RUN_STATUS.has(liveRun.status);
  const [tab, setTab] = useState<DetailTab>("result");
  const [selectedKey, setSelectedKey] = useState(initialArtifactKey);
  const titleRef = useRef<HTMLHeadingElement>(null);

  const logs = useAgentRunLogs(run.id, isActive);
  const approvals = useAgentRunApprovals(run.id);

  const groups = useMemo(() => groupLogs(logs.data ?? []), [logs.data]);
  const models = useMemo(() => usedModels(logs.data ?? []), [logs.data]);

  useEffect(() => {
    titleRef.current?.focus();
  }, []);

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="run-detail-view">
      <div className="flex shrink-0 items-center gap-2 border-b px-3 py-2">
        <Button
          variant="ghost"
          size="sm"
          onClick={onBack}
          className="text-muted-foreground"
          data-testid="run-detail-back"
        >
          <ArrowLeft className="size-4" /> 목록
        </Button>
        <h2
          ref={titleRef}
          tabIndex={-1}
          className="ml-1 flex min-w-0 items-center gap-1.5 text-sm font-semibold outline-none"
        >
          {studioCard ? (
            // 스튜디오 결과는 위치를 「스튜디오 › 카드 이름」으로 보인다(mindmap-agent-skill AC-1.10 · #180)
            <span className="truncate" data-testid="run-detail-breadcrumb">
              스튜디오 <span aria-hidden>›</span> {studioCard.label}
            </span>
          ) : (
            <>
              <span aria-hidden>{liveRun.agent_icon}</span>
              <span className="truncate">{liveRun.agent_name}</span>
            </>
          )}
        </h2>
      </div>

      <Tabs
        value={tab}
        onValueChange={(v) => setTab(v as DetailTab)}
        className="min-h-0 flex-1 gap-0"
      >
        <div className="flex shrink-0 items-center justify-between gap-2 border-b px-3 py-2">
          <TabsList>
            <TabsTrigger value="result" className="px-2 text-xs">
              결과
            </TabsTrigger>
            <TabsTrigger value="logs" className="px-2 text-xs">
              실행 로그
            </TabsTrigger>
          </TabsList>
          <span className="truncate text-xs text-muted-foreground">
            {RUN_STATUS_LABEL[liveRun.status]} · {formatRelative(liveRun.created_at)}
            {models.names.length > 0 && (
              // 실제로 응답한 모델(#216) — 결과 탭에서도 보이게 머리에 둔다. 노드별 값은 「실행 로그」 탭
              <span data-testid="run-model-summary">
                {" "}
                · 사용 모델 {models.names.join(", ")}
                {models.fallback ? " (폴백)" : ""}
              </span>
            )}
          </span>
        </div>

        <TabsContent value="result" className="flex min-h-0 flex-1 flex-col">
          <RunResult
            run={liveRun}
            notebookId={notebookId}
            canWrite={canWrite}
            selectedKey={selectedKey}
            onSelect={setSelectedKey}
          />
        </TabsContent>

        <TabsContent value="logs" className="min-h-0 flex-1 overflow-y-auto">
          <div className="flex flex-col gap-3 p-3">
            <div>
              <h3 className="mb-1 flex items-center gap-1 text-xs font-semibold text-muted-foreground">
                <ChevronRight className="size-3" /> 노드 실행 기록
              </h3>
              {logs.isPending ? (
                <Skeleton className="h-24" />
              ) : logs.isError ? (
                <p className="text-xs text-destructive">불러오지 못했습니다</p>
              ) : groups.length === 0 ? (
                <p className="text-xs text-muted-foreground">기록된 로그가 없습니다</p>
              ) : (
                <ScrollArea className="max-h-48 rounded-md border p-2">
                  {groups.map(({ parent, children }) => (
                    <div key={parent.id}>
                      <LogRow log={parent} />
                      {children.map((c) => (
                        <LogRow key={c.id} log={c} indented />
                      ))}
                    </div>
                  ))}
                </ScrollArea>
              )}
            </div>

            <div>
              <h3 className="mb-1 flex items-center gap-1 text-xs font-semibold text-muted-foreground">
                <ShieldAlert className="size-3" /> 승인 이력
              </h3>
              {approvals.isPending ? (
                <Skeleton className="h-16" />
              ) : approvals.isError ? (
                <p className="text-xs text-destructive">불러오지 못했습니다</p>
              ) : (approvals.data ?? []).length === 0 ? (
                <p className="text-xs text-muted-foreground">승인 요청 이력이 없습니다</p>
              ) : (
                <ScrollArea className="max-h-40 rounded-md border p-2">
                  {(approvals.data ?? []).map((a) => (
                    <ApprovalHistoryRow key={a.id} approval={a} />
                  ))}
                </ScrollArea>
              )}
            </div>
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
