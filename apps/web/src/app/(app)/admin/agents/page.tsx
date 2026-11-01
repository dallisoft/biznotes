"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, ShieldCheck } from "lucide-react";
import { useEffect, useId, useState } from "react";
import { qk } from "@/lib/query-keys";
import { AdminTableFrame } from "@/components/admin/admin-table-frame";
import { PageHeader } from "@/components/shell/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Label } from "@/components/ui/label";
import { LoadingState } from "@/components/ui/loading-state";
import { RowActions } from "@/components/ui/row-actions";
import { StatusBadge } from "@/components/ui/status-badge";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { WorkflowBuilder, validateDag } from "@/components/admin/workflow-builder";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TablePagination } from "@/components/ui/table-pagination";
import { Textarea } from "@/components/ui/textarea";
import { useCrudResource, type CrudResource } from "@/hooks/use-crud-resource";
import { usePagination, type PaginationState } from "@/hooks/use-pagination";
import { api } from "@/lib/api-client";
import type { AdminAgent } from "@/lib/admin-types";
import type { ToolPolicy } from "@/lib/agent-types";
import { activeLabel, agentScopeLabel, kindLabel } from "@/lib/labels";
import { notifyError, notifySuccess } from "@/lib/notify";

/** JSON 에디터 + (workflow 유형) React Flow 비주얼 빌더 — 양방향 전환 */
function EditAgentDialog({
  agent,
  open,
  onOpenChange,
}: {
  agent: AdminAgent | null;
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [definition, setDefinition] = useState("");
  const [paramsSchema, setParamsSchema] = useState("");
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [tab, setTab] = useState<"json" | "visual">("json");
  const definitionId = useId();
  const paramsId = useId();
  const errorId = useId();

  useEffect(() => {
    if (agent) {
      setDefinition(JSON.stringify(agent.definition, null, 2));
      setParamsSchema(JSON.stringify(agent.params_schema, null, 2));
      setJsonError(null);
      setTab("json");
    }
  }, [agent]);

  // 비주얼 탭용 파싱 — 실패 시 null (JSON 탭에서 수정 유도)
  const parsedDef = (() => {
    if (agent?.kind !== "workflow") return null;
    try {
      const d = JSON.parse(definition);
      return Array.isArray(d.nodes) ? d : null;
    } catch {
      return null;
    }
  })();

  const save = useMutation({
    mutationFn: () => {
      const body = {
        definition: JSON.parse(definition),
        params_schema: JSON.parse(paramsSchema),
      };
      return api.patch(`/api/v1/admin/agents/${agent!.id}`, body);
    },
    onSuccess: () => {
      notifySuccess(`‘${agent?.name ?? ""}’ 에이전트를 저장했습니다`, {
        description: "버전이 하나 올라갔습니다",
      });
      queryClient.invalidateQueries({ queryKey: qk.adminAgents.all });
      queryClient.invalidateQueries({ queryKey: qk.agents.all });
      onOpenChange(false);
    },
    onError: (e) => notifyError("에이전트를 저장", e),
  });

  function handleSave() {
    try {
      const def = JSON.parse(definition);
      JSON.parse(paramsSchema);
      // workflow는 저장 전 DAG 검증 (런타임 토폴로지 정렬과 동일 규칙)
      if (agent?.kind === "workflow" && Array.isArray(def.nodes)) {
        const { error } = validateDag(def.nodes, def.edges ?? []);
        if (error) {
          setJsonError(error);
          return;
        }
      }
      setJsonError(null);
    } catch (e) {
      setJsonError(
        `JSON 형식이 올바르지 않습니다${e instanceof Error && e.message ? ` — ${e.message}` : ""}`,
      );
      return;
    }
    save.mutate();
  }

  if (!agent) return null;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* 다이얼로그는 화면 가운데 고정이라 정의가 길면 뷰포트를 넘어 상단(탭)·하단(저장)이 잘린다(BL-125).
          높이를 뷰포트로 제한하고 편집 본문만 내부 스크롤해 헤더·탭·저장 버튼을 항상 보이게 한다. */}
      <DialogContent
        className="flex max-h-[calc(100dvh-2rem)] flex-col overflow-hidden sm:max-w-3xl"
        data-testid="edit-agent-dialog"
      >
        <DialogHeader className="shrink-0">
          <DialogTitle>
            {agent.icon} {agent.name} <Badge variant="secondary">v{agent.version}</Badge>
          </DialogTitle>
          <DialogDescription>
            정의와 입력 파라미터 스키마를 JSON으로 편집합니다. 저장하면 서버가 구조를 검증하고 버전이
            올라갑니다.
          </DialogDescription>
        </DialogHeader>
        {agent.kind === "workflow" && (
          <Tabs
            value={tab}
            onValueChange={(v) => setTab(v as "json" | "visual")}
            className="shrink-0"
          >
            <TabsList>
              <TabsTrigger value="json">JSON</TabsTrigger>
              <TabsTrigger value="visual" disabled={parsedDef === null} data-testid="tab-visual">
                비주얼 빌더
              </TabsTrigger>
            </TabsList>
          </Tabs>
        )}
        <div className="-mx-1 min-h-0 flex-1 overflow-y-auto px-1" data-testid="edit-agent-dialog-body">
          {tab === "visual" && parsedDef !== null ? (
            <WorkflowBuilder
              key={agent.id}
              definition={parsedDef}
              onChange={(def) => setDefinition(JSON.stringify(def, null, 2))}
            />
          ) : (
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="flex flex-col gap-1">
                <Label htmlFor={definitionId}>정의(JSON)</Label>
                <Textarea
                  id={definitionId}
                  rows={16}
                  className="font-mono text-xs"
                  value={definition}
                  aria-invalid={jsonError ? true : undefined}
                  aria-describedby={jsonError ? errorId : undefined}
                  onChange={(e) => setDefinition(e.target.value)}
                />
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor={paramsId}>입력 파라미터 스키마(JSON)</Label>
                <Textarea
                  id={paramsId}
                  rows={16}
                  className="font-mono text-xs"
                  value={paramsSchema}
                  aria-invalid={jsonError ? true : undefined}
                  aria-describedby={jsonError ? errorId : undefined}
                  onChange={(e) => setParamsSchema(e.target.value)}
                />
              </div>
            </div>
          )}
        </div>
        {jsonError && (
          <p id={errorId} role="alert" className="shrink-0 text-sm text-destructive-text">
            {jsonError}
          </p>
        )}
        <Button className="shrink-0 coarse:min-h-11" onClick={handleSave} disabled={save.isPending}>
          {save.isPending ? "저장 중…" : "저장"}
        </Button>
      </DialogContent>
    </Dialog>
  );
}

/** 도구별 side_effect 분류 + 이를 자동 승인하는 워크플로우 현황(19c) */
function ToolPoliciesTab() {
  const policies = useQuery({
    queryKey: qk.adminToolPolicies.all,
    queryFn: () => api.get<ToolPolicy[]>("/api/v1/admin/agents/tool-policies"),
  });

  if (policies.isPending) return <LoadingState variant="table" count={3} />;
  if (policies.isError) {
    return (
      <ErrorState
        title="도구 정책을 불러오지 못했습니다"
        onRetry={() => policies.refetch()}
        retrying={policies.isFetching}
      />
    );
  }
  if (policies.data.length === 0) {
    return (
      <EmptyState
        icon={<ShieldCheck />}
        title="도구 정책이 없습니다"
        description="에이전트가 쓰는 도구가 등록되면 여기에 부작용 분류가 보입니다."
      />
    );
  }

  return (
    <AdminTableFrame>
      <Table data-testid="tool-policies-table">
        <TableHeader>
          <TableRow>
            <TableHead>도구</TableHead>
            <TableHead>부작용</TableHead>
            <TableHead>자동 승인 정책이 있는 워크플로우</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {policies.data.map((p) => (
            <TableRow key={p.tool_name}>
              <TableCell>
                <code className="font-mono text-xs">{p.tool_name}</code>
              </TableCell>
              <TableCell>
                {p.side_effect ? (
                  <StatusBadge tone="warning">부작용(승인 필요)</StatusBadge>
                ) : (
                  <StatusBadge tone="neutral">읽기 전용</StatusBadge>
                )}
              </TableCell>
              <TableCell>
                {p.auto_approved_by.length === 0 ? (
                  <span className="text-xs text-muted-foreground">없음</span>
                ) : (
                  <ul className="flex flex-col gap-1">
                    {p.auto_approved_by.map((ref) => (
                      <li key={`${ref.agent_id}-${ref.node_id}`} className="text-xs">
                        <span className="font-medium">{ref.agent_name}</span>
                        <code className="ml-1 text-muted-foreground">{ref.node_id}</code>
                        <pre className="mt-0.5 max-w-md overflow-auto rounded bg-muted p-1 text-xs">
                          {JSON.stringify(ref.policy, null, 2)}
                        </pre>
                      </li>
                    ))}
                  </ul>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </AdminTableFrame>
  );
}

type AgentsResource = CrudResource<AdminAgent, never, { enabled?: boolean }>;

function AgentsTab({
  agents,
  pg,
  onEdit,
}: {
  agents: AgentsResource;
  pg: PaginationState<AdminAgent>;
  onEdit: (agent: AdminAgent) => void;
}) {
  if (agents.list.isPending) return <LoadingState variant="table" />;
  if (agents.list.isError) {
    return (
      <ErrorState
        title="에이전트 목록을 불러오지 못했습니다"
        onRetry={() => agents.list.refetch()}
        retrying={agents.list.isFetching}
      />
    );
  }
  if (pg.total === 0) {
    return (
      <EmptyState
        icon={<Bot />}
        title="아직 에이전트가 없습니다"
        description="스킬·워크플로우를 만들거나 시드를 실행하면 여기에 보입니다."
      />
    );
  }

  return (
    <>
      <AdminTableFrame>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>이름</TableHead>
              <TableHead>유형</TableHead>
              <TableHead>범위</TableHead>
              <TableHead className="text-right">버전</TableHead>
              <TableHead>상태</TableHead>
              <TableHead className="w-24 text-right">동작</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {pg.pageItems.map((a) => (
              <TableRow key={a.id}>
                <TableCell>
                  <span className="flex items-center gap-1.5">
                    {a.icon && <span aria-hidden>{a.icon}</span>}
                    <span className="font-medium">{a.name}</span>
                  </span>
                </TableCell>
                <TableCell>
                  <Badge variant="secondary">{kindLabel(a.kind)}</Badge>
                </TableCell>
                <TableCell>{agentScopeLabel(a.scope)}</TableCell>
                <TableCell className="text-right tabular-nums">v{a.version}</TableCell>
                <TableCell>
                  <div className="flex items-center gap-2">
                    <Switch
                      checked={a.enabled}
                      aria-label={`${a.name} 활성`}
                      onCheckedChange={(c) =>
                        agents.update.mutate(
                          { id: a.id, enabled: c },
                          {
                            onError: (e) =>
                              notifyError(`‘${a.name}’ 에이전트를 ${c ? "활성화" : "비활성화"}`, e),
                          },
                        )
                      }
                    />
                    {/* 상태를 글자로도 보인다(DP-4) — 스위치가 이미 이름·상태를 읽히므로 보조 기술에는 숨긴다 */}
                    <span aria-hidden className="text-xs text-muted-foreground">
                      {activeLabel(a.enabled)}
                    </span>
                  </div>
                </TableCell>
                <TableCell>
                  <RowActions
                    foldPrimaryOnNarrow
                    rowLabel={a.name}
                    primary={{ label: "편집", onClick: () => onEdit(a) }}
                    items={[]}
                  />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </AdminTableFrame>
      <TablePagination pagination={pg} />
    </>
  );
}

export default function AdminAgentsPage() {
  // 에이전트는 생성/삭제가 없다(시딩으로만 추가) — list + 활성 토글(update)만 쓴다.
  // 활성 상태는 스위치가 그대로 보여주므로 성공 토스트를 띄우지 않는다. 실패는 호출부가 알린다(toastErrors: false).
  // 목록·페이지 상태는 페이지에 두어 탭을 오가도 유지된다.
  const agents = useCrudResource<AdminAgent, never, { enabled?: boolean }>({
    queryKey: qk.adminAgents.all,
    basePath: "/api/v1/admin/agents",
    alsoInvalidate: [qk.agents.all],
    toastErrors: false,
  });
  const pg = usePagination(agents.list.data ?? []);
  const [editTarget, setEditTarget] = useState<AdminAgent | null>(null);
  const [mainTab, setMainTab] = useState<"agents" | "tool-policies">("agents");

  return (
    <div>
      <PageHeader
        breadcrumb={[{ label: "관리", href: "/admin" }, { label: "에이전트" }]}
        title="에이전트"
        description="스킬·워크플로우 등 에이전트 정의와 활성 상태를 관리하고, 도구의 부작용 분류를 확인합니다"
      />
      <Tabs
        value={mainTab}
        onValueChange={(v) => setMainTab(v as "agents" | "tool-policies")}
        className="mb-4"
      >
        <TabsList>
          <TabsTrigger value="agents">에이전트</TabsTrigger>
          <TabsTrigger value="tool-policies" data-testid="tab-tool-policies">
            도구 정책
          </TabsTrigger>
        </TabsList>
      </Tabs>
      {/* 탭 내용 경계 — 「다시 시도」가 성공하면 ErrorState가 이 안의 첫 포커스 대상(표)으로 포커스를 옮긴다(HCI-P2-2) */}
      <div data-slot="agents-tab-content">
        {mainTab === "agents" ? (
          <AgentsTab agents={agents} pg={pg} onEdit={setEditTarget} />
        ) : (
          <ToolPoliciesTab />
        )}
      </div>
      <EditAgentDialog
        agent={editTarget}
        open={editTarget !== null}
        onOpenChange={(o) => !o && setEditTarget(null)}
      />
    </div>
  );
}
