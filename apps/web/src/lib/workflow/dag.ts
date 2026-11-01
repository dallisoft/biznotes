/** 워크플로우 DAG 검증·레이아웃·변수 추론 — 런타임 토폴로지 규칙(runtime._topological_order) 미러 */
import { getDescriptor, validateNodeFields } from "@/lib/workflow/node-catalog";

export interface DagValidation {
  error: string | null;
  warning: string | null;
}

/** 사이클·끊긴 엣지·고아 노드 검증 (런타임 _topological_order와 동일 Kahn 규칙) */
export function validateDag(
  defNodes: Record<string, unknown>[],
  defEdges: string[][],
): DagValidation {
  const ids = new Set(defNodes.map((n) => String(n.id)));
  for (const [from, to] of defEdges) {
    if (!ids.has(from) || !ids.has(to)) {
      return { error: `엣지가 존재하지 않는 노드를 참조합니다: ${from} → ${to}`, warning: null };
    }
  }
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

  // 필드 required 검증 (디스크립터 기반)
  for (const node of defNodes) {
    const fieldErrors = validateNodeFields(node);
    if (fieldErrors.length > 0) {
      return { error: fieldErrors[0], warning: null };
    }
  }

  // terminal 노드(save_artifact)가 출력 엣지를 가지면 경고
  for (const node of defNodes) {
    const desc = getDescriptor(String(node.type));
    if (desc?.terminal && defEdges.some(([from]) => from === String(node.id))) {
      return {
        error: null,
        warning: `'${node.id}'(${desc.label})는 출력이 없어 다음 노드로 연결할 수 없습니다`,
      };
    }
  }

  const connected = new Set(defEdges.flat());
  const orphans = [...ids].filter((id) => !connected.has(id));
  const warning =
    ids.size > 1 && orphans.length > 0
      ? `연결되지 않은 노드: ${orphans.join(", ")} (독립 실행됨)`
      : null;
  return { error: null, warning };
}

/** 토폴로지 레이어 기반 자동 배치 좌표 */
export function layoutPositions(
  defNodes: Record<string, unknown>[],
  defEdges: string[][],
): Record<string, { x: number; y: number }> {
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
  const perLayer = new Map<number, number>();
  const out: Record<string, { x: number; y: number }> = {};
  for (const id of ids) {
    const l = layerOf.get(id) ?? 0;
    const idx = perLayer.get(l) ?? 0;
    perLayer.set(l, idx + 1);
    out[id] = { x: l * 260 + 40, y: idx * 130 + 40 };
  }
  return out;
}

/** 노드 출력별 노출 키 — 프롬프트 변수 자동완성 제안용 */
function outputKeys(type: string): string[] {
  switch (type) {
    case "rag_search":
    case "source_load":
      return ["context"];
    case "llm":
    case "http":
    case "mcp_tool":
    case "agent_call":
    case "save_sources":
      return ["text"];
    default:
      return [];
  }
}

/**
 * 특정 노드에서 참조 가능한 변수 토큰 목록(그래프상 선행 노드 + params).
 * params 키는 paramKeys로 전달(없으면 params.* 일반 토큰만).
 */
export function upstreamVariables(
  targetId: string,
  defNodes: Record<string, unknown>[],
  defEdges: string[][],
  paramKeys: string[] = [],
): string[] {
  // 선행(조상) 노드 수집 — 역방향 BFS
  const incoming = new Map<string, string[]>();
  for (const [from, to] of defEdges) {
    incoming.set(to, [...(incoming.get(to) ?? []), from]);
  }
  const ancestors = new Set<string>();
  const stack = [...(incoming.get(targetId) ?? [])];
  while (stack.length) {
    const cur = stack.pop()!;
    if (ancestors.has(cur)) continue;
    ancestors.add(cur);
    stack.push(...(incoming.get(cur) ?? []));
  }
  const byId = new Map(defNodes.map((n) => [String(n.id), n]));
  const vars: string[] = [];
  for (const pk of paramKeys) vars.push(`{params.${pk}}`);
  if (paramKeys.length === 0) vars.push("{params.topic}");
  for (const aid of ancestors) {
    const node = byId.get(aid);
    if (!node) continue;
    for (const key of outputKeys(String(node.type))) {
      vars.push(`{${aid}.${key}}`);
    }
  }
  return vars;
}
