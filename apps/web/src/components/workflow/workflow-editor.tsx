"use client";

import {
  addEdge,
  Background,
  Controls,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { ArrowLeft, Check, OctagonAlert, Play, Save, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useCallback, useMemo, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/ui/status-badge";
import { Textarea } from "@/components/ui/textarea";
import { WORKFLOW_NODE_TYPES, type WorkflowNodeData } from "@/components/workflow/custom-node";
import { PropertyPanel } from "@/components/workflow/property-panel";
import { TestRunDialog } from "@/components/workflow/test-run-dialog";
import { usePublishWorkflow, useUpdateWorkflow } from "@/hooks/use-workflows";
import { workflowStatusLabel } from "@/lib/labels";
import { failureTitle, notifyError, notifySuccess, successMessage } from "@/lib/notify";
import { validateDag, layoutPositions, upstreamVariables } from "@/lib/workflow/dag";
import {
  CATEGORY_LABEL,
  CATEGORY_ORDER,
  NODE_CATALOG,
  type NodeType,
  paletteNodeTypes,
} from "@/lib/workflow/node-catalog";
import type { Workflow, WorkflowNode } from "@/lib/workflow-types";

function toRfNodes(def: Workflow["definition"]): Node<WorkflowNodeData>[] {
  const edges = def.edges ?? [];
  const nodes = def.nodes ?? [];
  const pos = layoutPositions(nodes, edges);
  return nodes.map((raw) => ({
    id: String(raw.id),
    type: "workflow",
    position: pos[String(raw.id)] ?? { x: 40, y: 40 },
    data: { raw: raw as WorkflowNode },
  }));
}

/** 툴바 버튼의 터치 타깃 — `pointer: coarse`면 해상도와 무관하게 44px(plan §6.7·§6.8-8, P4 리뷰 P4-03). */
const TOOLBAR_BUTTON_COARSE = "coarse:h-11 coarse:min-w-11";

// 일반 워크플로우 에디터 = "user" 편집 표면. 로그인 사용자의 role로 바꾸지 않는다 — admin도
// 이 에디터로는 scope "user" 정의만 저장한다(계획 AC-30).
const PALETTE_NODE_TYPES = paletteNodeTypes("user");

function PaletteGroup({ onAdd }: { onAdd: (t: NodeType) => void }) {
  return (
    <div className="flex flex-col gap-3 p-3">
      {CATEGORY_ORDER.map((cat) => {
        const types = PALETTE_NODE_TYPES.filter((t) => NODE_CATALOG[t].category === cat);
        if (types.length === 0) return null;
        return (
          <div key={cat}>
            <p className="mb-1 px-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {CATEGORY_LABEL[cat]}
            </p>
            <div className="flex flex-col gap-1">
              {types.map((t) => {
                const d = NODE_CATALOG[t];
                const Icon = d.icon;
                return (
                  <button
                    key={t}
                    type="button"
                    onClick={() => onAdd(t)}
                    title={d.description}
                    className="flex items-center gap-2 rounded-md border px-2 py-1.5 text-left text-xs transition-colors hover:bg-accent"
                    data-testid={`palette-${t}`}
                  >
                    <Icon className="size-3.5 shrink-0" style={{ color: d.color }} />
                    <span className="truncate">{d.label}</span>
                  </button>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function EditorInner({ workflow }: { workflow: Workflow }) {
  const update = useUpdateWorkflow(workflow.id);
  const publish = usePublishWorkflow(workflow.id);

  const [name, setName] = useState(workflow.name);
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<WorkflowNodeData>>(
    toRfNodes(workflow.definition),
  );
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(
    (workflow.definition.edges ?? []).map(([from, to]) => ({
      id: `${from}->${to}`,
      source: from,
      target: to,
    })),
  );
  const [selected, setSelected] = useState<string | null>(null);
  const [testOpen, setTestOpen] = useState(false);
  const [schemaOpen, setSchemaOpen] = useState(false);
  const [schemaText, setSchemaText] = useState(() =>
    JSON.stringify(workflow.params_schema ?? {}, null, 2),
  );

  const defNodes = useMemo(() => nodes.map((n) => n.data.raw), [nodes]);
  const defEdges = useMemo(
    () => edges.map((e) => [e.source, e.target] as [string, string]),
    [edges],
  );
  const validation = useMemo(() => validateDag(defNodes, defEdges), [defNodes, defEdges]);
  const published = workflow.enabled;
  // 저장·게시가 진행 중이면 두 버튼 모두 막는다(게시는 저장 → 게시 2단계라 중복 클릭이 두 번 게시할 수 있다)
  const busy = update.isPending || publish.isPending;

  const paramKeys = useMemo(() => {
    try {
      return Object.keys(JSON.parse(schemaText).properties ?? {});
    } catch {
      return [];
    }
  }, [schemaText]);

  const selectedNode = nodes.find((n) => n.id === selected)?.data.raw ?? null;
  const variables = useMemo(
    () => (selected ? upstreamVariables(selected, defNodes, defEdges, paramKeys) : []),
    [selected, defNodes, defEdges, paramKeys],
  );

  const onConnect = useCallback(
    (conn: Connection) => setEdges((eds) => addEdge(conn, eds)),
    [setEdges],
  );

  function addNode(type: NodeType) {
    let i = 1;
    while (nodes.some((n) => n.id === `${type}-${i}`)) i += 1;
    const id = `${type}-${i}`;
    const raw: WorkflowNode = { id, type, ...NODE_CATALOG[type].defaults };
    setNodes((ns) => [
      ...ns,
      {
        id,
        type: "workflow",
        position: { x: 60 + ns.length * 24, y: 60 + ns.length * 24 },
        data: { raw },
      },
    ]);
    setSelected(id);
  }

  function patchNode(patch: Record<string, unknown>) {
    if (!selected) return;
    setNodes((ns) =>
      ns.map((n) =>
        n.id === selected ? { ...n, data: { raw: { ...n.data.raw, ...patch } } } : n,
      ),
    );
  }

  function deleteNode() {
    if (!selected) return;
    setNodes((ns) => ns.filter((n) => n.id !== selected));
    setEdges((es) => es.filter((e) => e.source !== selected && e.target !== selected));
    setSelected(null);
  }

  function buildPayload() {
    let params_schema = workflow.params_schema;
    try {
      params_schema = JSON.parse(schemaText);
    } catch {
      // 스키마 편집 다이얼로그에서 오류 표시 — 저장 시 기존 값 유지
    }
    return {
      name: name.trim() || "제목 없는 워크플로우",
      definition: { nodes: defNodes, edges: defEdges },
      params_schema,
    };
  }

  function save() {
    if (validation.error) {
      // 요청 전 검증 실패 — 서버 오류가 아니므로 검증 문구를 그대로 설명으로 보인다
      toast.error(failureTitle("워크플로우를 저장"), { description: validation.error });
      return;
    }
    update.mutate(buildPayload(), {
      onSuccess: (w) => notifySuccess(successMessage("워크플로우", "저장"), { description: `v${w.version}` }),
      onError: (err) => notifyError("워크플로우를 저장", err),
    });
  }

  function togglePublish() {
    if (!published && validation.error) {
      toast.error(failureTitle("워크플로우를 게시"), { description: validation.error });
      return;
    }
    // 게시 전 현재 캔버스를 먼저 저장해 정의 동기화
    update.mutate(buildPayload(), {
      onSuccess: () =>
        publish.mutate(!published, {
          onSuccess: (w) =>
            notifySuccess(w.enabled ? successMessage("워크플로우", "게시") : "워크플로우 게시를 취소했습니다"),
          onError: (err) => notifyError(published ? "게시를 취소" : "워크플로우를 게시", err),
        }),
      onError: (err) => notifyError("워크플로우를 저장", err),
    });
  }

  return (
    <div className="flex h-full flex-col">
      {/* 상단 바(T2 편집기 툴바 — 뒤로 · 이름 · 상태 · 오른쪽 보조 버튼들 → 주 버튼 1개, plan §6.6.1) */}
      {/* 좁은 폭(768~1023, 사이드바 포함)에서는 오른쪽 버튼 묶음이 다음 줄로 내려간다 — 이름 입력이 0폭으로 눌리거나
          주 버튼이 화면 밖으로 잘리지 않게(P-10 ⑤). */}
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b px-4 py-2">
        <div className="flex min-w-64 flex-1 items-center gap-3" data-testid="workflow-editor-identity">
          <Link
            href="/workflows"
            aria-label="워크플로우 목록으로"
            title="워크플로우 목록으로"
            className="inline-flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 coarse:size-11"
          >
            <ArrowLeft aria-hidden className="size-4" />
          </Link>
          <Input
            value={name}
            maxLength={100}
            aria-label="워크플로우 이름"
            onChange={(e) => setName(e.target.value)}
            className="h-8 min-w-24 max-w-xs flex-1 border-transparent bg-transparent px-1 text-base font-semibold shadow-none hover:border-input focus-visible:ring-1 coarse:h-11"
          />
          <StatusBadge tone={published ? "success" : "neutral"} data-testid="workflow-status">
            {`${workflowStatusLabel(published)} · v${workflow.version}`}
          </StatusBadge>
          {validation.error ? (
            <span
              className="flex min-w-0 items-center gap-1 text-xs text-destructive"
              data-testid="dag-error"
              title={validation.error}
            >
              <OctagonAlert aria-hidden className="size-3.5 shrink-0" />
              <span className="truncate">{validation.error}</span>
            </span>
          ) : validation.warning ? (
            <span
              className="flex min-w-0 items-center gap-1 text-xs text-warning-text"
              data-testid="dag-warning"
              title={validation.warning}
            >
              <TriangleAlert aria-hidden className="size-3.5 shrink-0" />
              <span className="truncate">{validation.warning}</span>
            </span>
          ) : (
            <StatusBadge tone="success" icon={<Check />} data-testid="dag-valid">
              유효
            </StatusBadge>
          )}
        </div>
        <div className="ml-auto flex shrink-0 gap-2" data-testid="workflow-editor-actions">
          <Button variant="outline" size="sm" className={TOOLBAR_BUTTON_COARSE} onClick={() => setSchemaOpen(true)}>
            파라미터
          </Button>
          <Button variant="outline" size="sm" className={TOOLBAR_BUTTON_COARSE} onClick={() => setTestOpen(true)}>
            <Play aria-hidden /> 테스트 실행
          </Button>
          {/* 주 버튼 1개(P-10): 미게시면 「게시」, 게시됨이면 「저장」이 주 동작이다. 주 버튼은 맨 오른쪽. */}
          {published ? (
            <>
              <Button variant="outline" size="sm" className={TOOLBAR_BUTTON_COARSE} onClick={togglePublish} disabled={busy}>
                게시 취소
              </Button>
              <Button size="sm" className={TOOLBAR_BUTTON_COARSE} onClick={save} disabled={busy}>
                <Save aria-hidden /> 저장
              </Button>
            </>
          ) : (
            <>
              <Button variant="outline" size="sm" className={TOOLBAR_BUTTON_COARSE} onClick={save} disabled={busy}>
                <Save aria-hidden /> 저장
              </Button>
              <Button size="sm" className={TOOLBAR_BUTTON_COARSE} onClick={togglePublish} disabled={busy}>
                게시
              </Button>
            </>
          )}
        </div>
      </header>

      {/* 3분할 */}
      <div className="flex min-h-0 flex-1">
        <aside className="w-44 shrink-0 overflow-y-auto border-r" data-testid="palette">
          <PaletteGroup onAdd={addNode} />
        </aside>

        <div className="min-w-0 flex-1">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={WORKFLOW_NODE_TYPES}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodeClick={(_, node) => setSelected(node.id)}
            onPaneClick={() => setSelected(null)}
            fitView
            proOptions={{ hideAttribution: true }}
          >
            <Background />
            <Controls />
          </ReactFlow>
        </div>

        <aside className="w-72 shrink-0 border-l">
          {selectedNode ? (
            <PropertyPanel
              node={selectedNode}
              variables={variables}
              onPatch={patchNode}
              onDelete={deleteNode}
            />
          ) : (
            <div className="p-4 text-xs text-muted-foreground">
              노드를 선택하면 속성을 편집할 수 있습니다. 왼쪽 팔레트에서 노드를 추가하세요.
            </div>
          )}
        </aside>
      </div>

      <TestRunDialog workflow={workflow} open={testOpen} onOpenChange={setTestOpen} />

      <Dialog open={schemaOpen} onOpenChange={setSchemaOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>파라미터 스키마 (JSON Schema)</DialogTitle>
            <DialogDescription>
              실행 시 입력받을 파라미터를 정의합니다. 프롬프트에서 {"{params.키}"}로 참조합니다.
            </DialogDescription>
          </DialogHeader>
          <Textarea
            rows={12}
            value={schemaText}
            spellCheck={false}
            className="font-mono text-xs"
            onChange={(e) => setSchemaText(e.target.value)}
          />
          <DialogFooter>
            <Button onClick={() => setSchemaOpen(false)}>확인</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export function WorkflowEditor({ workflow }: { workflow: Workflow }) {
  return (
    <ReactFlowProvider>
      <EditorInner workflow={workflow} />
    </ReactFlowProvider>
  );
}
