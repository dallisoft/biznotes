"use client";

import { useCallback, useMemo, useState } from "react";
import {
  addEdge,
  Background,
  Controls,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ADMIN_BUILDER_NODE_TYPES as NODE_TYPES } from "@/lib/workflow/node-catalog";

type NodeType = (typeof NODE_TYPES)[number];

const NODE_DEFAULTS: Record<NodeType, Record<string, unknown>> = {
  rag_search: { query: "{params.topic}", top_k: 6 },
  llm: { system: "", prompt: "{search.context}" },
  http: { url: "https://", method: "GET" },
  mcp_tool: { server_id: "", tool_name: "", arguments: {} },
  save_artifact: { name: "result.md", content: "{generate.text}", content_type: "text/markdown" },
};

const NODE_COLOR: Record<NodeType, string> = {
  rag_search: "#3b82f6",
  llm: "#8b5cf6",
  http: "#f59e0b",
  mcp_tool: "#10b981",
  save_artifact: "#64748b",
};

interface WorkflowDef {
  nodes: Record<string, unknown>[];
  edges?: string[][];
  [key: string]: unknown;
}

/** 토폴로지 레이어 기반 자동 배치 */
function layout(defNodes: Record<string, unknown>[], defEdges: string[][]): Node[] {
  const ids = defNodes.map((n) => String(n.id));
  const indegree = new Map(ids.map((id) => [id, 0]));
  for (const [, to] of defEdges) indegree.set(to, (indegree.get(to) ?? 0) + 1);
  const layerOf = new Map<string, number>();
  let frontier = ids.filter((id) => (indegree.get(id) ?? 0) === 0);
  let layer = 0;
  const remaining = new Map(indegree);
  while (frontier.length > 0) {
    frontier.forEach((id) => layerOf.set(id, layer));
    const next: string[] = [];
    for (const [from, to] of defEdges) {
      if (!frontier.includes(from)) continue;
      const d = (remaining.get(to) ?? 0) - 1;
      remaining.set(to, d);
      if (d === 0) next.push(to);
    }
    frontier = next;
    layer += 1;
  }
  const perLayerCount = new Map<number, number>();
  return defNodes.map((raw) => {
    const id = String(raw.id);
    const l = layerOf.get(id) ?? 0; // 사이클 노드는 0층에 표시
    const idx = perLayerCount.get(l) ?? 0;
    perLayerCount.set(l, idx + 1);
    const type = String(raw.type) as NodeType;
    return {
      id,
      position: { x: l * 240 + 20, y: idx * 110 + 20 },
      data: { label: `${raw.type} · ${id}`, raw },
      style: {
        border: `2px solid ${NODE_COLOR[type] ?? "#888"}`,
        borderRadius: 8,
        padding: 6,
        fontSize: 12,
        background: "var(--background, #fff)",
      },
    };
  });
}

export function validateDag(
  defNodes: Record<string, unknown>[], defEdges: string[][],
): { error: string | null; warning: string | null } {
  const ids = new Set(defNodes.map((n) => String(n.id)));
  for (const [from, to] of defEdges) {
    if (!ids.has(from) || !ids.has(to)) {
      return { error: `엣지가 존재하지 않는 노드를 참조합니다: ${from} → ${to}`, warning: null };
    }
  }
  // Kahn — 사이클 검출 (런타임 _topological_order와 동일 규칙)
  const indegree = new Map([...ids].map((id) => [id, 0]));
  for (const [, to] of defEdges) indegree.set(to, (indegree.get(to) ?? 0) + 1);
  const queue = [...ids].filter((id) => indegree.get(id) === 0);
  let visited = 0;
  while (queue.length > 0) {
    const cur = queue.shift()!;
    visited += 1;
    for (const [from, to] of defEdges) {
      if (from !== cur) continue;
      const d = (indegree.get(to) ?? 0) - 1;
      indegree.set(to, d);
      if (d === 0) queue.push(to);
    }
  }
  if (visited !== ids.size) {
    return { error: "사이클이 있습니다 — 실행 순서를 정할 수 없습니다", warning: null };
  }
  const connected = new Set(defEdges.flat());
  const orphans = [...ids].filter((id) => !connected.has(id));
  const warning =
    ids.size > 1 && orphans.length > 0
      ? `연결되지 않은 노드: ${orphans.join(", ")} (독립 실행됨)`
      : null;
  return { error: null, warning };
}

export function WorkflowBuilder({
  definition,
  onChange,
}: {
  definition: WorkflowDef;
  onChange: (def: WorkflowDef) => void;
}) {
  const defEdges = useMemo(() => definition.edges ?? [], [definition.edges]);
  const [nodes, setNodes, onNodesChange] = useNodesState(layout(definition.nodes, defEdges));
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(
    defEdges.map(([from, to]) => ({ id: `${from}->${to}`, source: from, target: to })),
  );
  const [selected, setSelected] = useState<string | null>(null);
  const [propsDraft, setPropsDraft] = useState("");
  const [propsError, setPropsError] = useState<string | null>(null);

  const emit = useCallback(
    (nextNodes: Node[], nextEdges: Edge[]) => {
      onChange({
        ...definition,
        nodes: nextNodes.map((n) => n.data.raw as Record<string, unknown>),
        edges: nextEdges.map((e) => [e.source, e.target]),
      });
    },
    [definition, onChange],
  );

  const onConnect = useCallback(
    (conn: Connection) => {
      setEdges((eds) => {
        const next = addEdge(conn, eds);
        emit(nodes, next);
        return next;
      });
    },
    [setEdges, emit, nodes],
  );

  function addNode(type: NodeType) {
    const base = type.replace("_", "-");
    let i = 1;
    while (nodes.some((n) => n.id === `${base}-${i}`)) i += 1;
    const id = `${base}-${i}`;
    const raw = { id, type, ...NODE_DEFAULTS[type] };
    const node: Node = {
      id,
      position: { x: 40 + nodes.length * 30, y: 40 + nodes.length * 30 },
      data: { label: `${type} · ${id}`, raw },
      style: {
        border: `2px solid ${NODE_COLOR[type]}`,
        borderRadius: 8,
        padding: 6,
        fontSize: 12,
        background: "var(--background, #fff)",
      },
    };
    const next = [...nodes, node];
    setNodes(next);
    emit(next, edges);
  }

  function selectNode(id: string) {
    setSelected(id);
    const raw = nodes.find((n) => n.id === id)?.data.raw as Record<string, unknown>;
    if (raw) {
      const props = { ...raw };
      delete props.id;
      delete props.type;
      setPropsDraft(JSON.stringify(props, null, 2));
      setPropsError(null);
    }
  }

  function applyProps() {
    if (!selected) return;
    try {
      const props = JSON.parse(propsDraft);
      const next = nodes.map((n) =>
        n.id === selected
          ? {
              ...n,
              data: {
                ...n.data,
                raw: {
                  id: selected,
                  type: (n.data.raw as Record<string, unknown>).type,
                  ...props,
                },
              },
            }
          : n,
      );
      setNodes(next);
      setPropsError(null);
      emit(next, edges);
    } catch (e) {
      setPropsError(e instanceof Error ? e.message : "JSON 파싱 실패");
    }
  }

  function deleteSelected() {
    if (!selected) return;
    const nextNodes = nodes.filter((n) => n.id !== selected);
    const nextEdges = edges.filter((e) => e.source !== selected && e.target !== selected);
    setNodes(nextNodes);
    setEdges(nextEdges);
    setSelected(null);
    emit(nextNodes, nextEdges);
  }

  const validation = validateDag(
    nodes.map((n) => n.data.raw as Record<string, unknown>),
    edges.map((e) => [e.source, e.target]),
  );

  return (
    <div className="flex flex-col gap-2" data-testid="workflow-builder">
      <div className="flex flex-wrap gap-1">
        {NODE_TYPES.map((t) => (
          <Button key={t} variant="outline" size="sm" className="h-7 text-xs" onClick={() => addNode(t)}>
            + {t}
          </Button>
        ))}
      </div>
      <div className="flex gap-2">
        <div className="h-80 flex-1 rounded-md border">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={(changes) => {
              onEdgesChange(changes);
            }}
            onConnect={onConnect}
            onNodeClick={(_, node) => selectNode(node.id)}
            fitView
            proOptions={{ hideAttribution: true }}
          >
            <Background />
            <Controls />
          </ReactFlow>
        </div>
        {selected && (
          <div className="flex w-72 flex-col gap-2 rounded-md border p-2">
            <p className="text-xs font-semibold">{selected} 속성 (id/type 제외)</p>
            <Textarea
              rows={10}
              className="font-mono text-xs"
              value={propsDraft}
              onChange={(e) => setPropsDraft(e.target.value)}
            />
            {propsError && <p className="text-xs text-destructive">{propsError}</p>}
            <div className="flex gap-2">
              <Button size="sm" className="h-7 text-xs" onClick={applyProps}>
                적용
              </Button>
              <Button
                size="sm"
                variant="ghost"
                className="h-7 text-xs text-destructive"
                onClick={deleteSelected}
              >
                노드 삭제
              </Button>
            </div>
          </div>
        )}
      </div>
      {validation.error && (
        <p className="text-sm text-destructive" data-testid="dag-error">
          ⛔ {validation.error}
        </p>
      )}
      {validation.warning && (
        <p className="text-xs text-warning-text" data-testid="dag-warning">
          ⚠️ {validation.warning}
        </p>
      )}
    </div>
  );
}
