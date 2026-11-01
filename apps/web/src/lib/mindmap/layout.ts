/**
 * 마인드맵 좌→우 트리 레이아웃(mindmap-agent-skill AC-1.2, D-7) — 순수 함수.
 *
 * - 깊이 d의 x = 깊이 d−1 열의 x + 그 열의 최대 폭 + 간격.
 * - 잎(보이는 자식이 없는 노드)은 세로로 누적하고, 자식 있는 노드의 y는 첫·끝 자식 중심의 중앙이다.
 *   부모 상자가 자식 묶음보다 높으면 그 하위 트리를 아래로 밀어 겹침을 없앤다.
 * - 접힌 노드의 하위는 제외한다. 같은 입력 → 같은 좌표.
 */
import type { MindmapNode } from "@/lib/mindmap/parse";

export const LAYOUT_COLUMN_GAP = 72;
export const LAYOUT_ROW_GAP = 12;

export interface NodeSize {
  width: number;
  height: number;
}

export interface LayoutNode {
  id: string;
  node: MindmapNode;
  depth: number;
  parentId: string | null;
  x: number;
  y: number;
  width: number;
  height: number;
  hasChildren: boolean;
  expanded: boolean;
}

export interface LayoutEdge {
  id: string;
  source: string;
  target: string;
}

export interface MindmapLayout {
  nodes: LayoutNode[];
  edges: LayoutEdge[];
  width: number;
  height: number;
}

// 노드 상자 치수 — DOM 측정 없이 결정적으로 정한다(뷰어 노드 스타일과 같은 값).
export const NODE_MIN_WIDTH = 112;
export const NODE_MAX_WIDTH = 240;
export const NODE_PAD_X = 52; // 좌우 여백 + 토글·소스 아이콘 자리
export const NODE_LINE_HEIGHT = 20;
export const NODE_PAD_Y = 16;
export const NODE_MAX_LINES = 3;

function charWidth(ch: string): number {
  const cp = ch.codePointAt(0) ?? 0;
  // 한글·CJK·전각은 넓게, 그 밖은 좁게 어림한다(text-sm 기준)
  if (
    (cp >= 0x1100 && cp <= 0x11ff) ||
    (cp >= 0x2e80 && cp <= 0xa4cf) ||
    (cp >= 0xac00 && cp <= 0xd7a3) ||
    (cp >= 0xf900 && cp <= 0xfaff) ||
    (cp >= 0xff00 && cp <= 0xffef)
  ) {
    return 14;
  }
  return 7.5;
}

/** 기본 측정 함수 — 라벨 글자 폭 어림으로 상자 크기를 정한다. */
export function measureNode(node: MindmapNode): NodeSize {
  let text = 0;
  for (const ch of node.label) text += charWidth(ch);
  const maxText = NODE_MAX_WIDTH - NODE_PAD_X;
  const width = Math.min(NODE_MAX_WIDTH, Math.max(NODE_MIN_WIDTH, Math.ceil(text) + NODE_PAD_X));
  const lines = Math.min(NODE_MAX_LINES, Math.max(1, Math.ceil(text / maxText)));
  return { width, height: lines * NODE_LINE_HEIGHT + NODE_PAD_Y };
}

/** 보이는 노드를 전위 순서로 — 키보드 탐색의 「보이는 순서」. */
export function visibleNodes(
  root: MindmapNode,
  expanded: ReadonlySet<string>,
): { node: MindmapNode; depth: number; parentId: string | null }[] {
  const out: { node: MindmapNode; depth: number; parentId: string | null }[] = [];
  const stack: { node: MindmapNode; depth: number; parentId: string | null }[] = [
    { node: root, depth: 0, parentId: null },
  ];
  while (stack.length > 0) {
    const item = stack.pop() as (typeof stack)[number];
    out.push(item);
    if (expanded.has(item.node.id)) {
      for (let i = item.node.children.length - 1; i >= 0; i -= 1) {
        stack.push({ node: item.node.children[i], depth: item.depth + 1, parentId: item.node.id });
      }
    }
  }
  return out;
}

export function layoutMindmap(
  root: MindmapNode,
  expanded: ReadonlySet<string>,
  measure: (node: MindmapNode) => NodeSize = measureNode,
): MindmapLayout {
  const visible = visibleNodes(root, expanded);
  const byId = new Map<string, LayoutNode>();
  const columnWidth: number[] = [];
  for (const v of visible) {
    const size = measure(v.node);
    const isExpanded = expanded.has(v.node.id) && v.node.children.length > 0;
    byId.set(v.node.id, {
      id: v.node.id,
      node: v.node,
      depth: v.depth,
      parentId: v.parentId,
      x: 0,
      y: 0,
      width: size.width,
      height: size.height,
      hasChildren: v.node.children.length > 0,
      expanded: isExpanded,
    });
    columnWidth[v.depth] = Math.max(columnWidth[v.depth] ?? 0, size.width);
  }
  const columnX: number[] = [];
  columnWidth.forEach((w, d) => {
    columnX[d] = d === 0 ? 0 : columnX[d - 1] + columnWidth[d - 1] + LAYOUT_COLUMN_GAP;
  });

  const childrenOf = (n: LayoutNode): LayoutNode[] =>
    n.expanded ? n.node.children.map((c) => byId.get(c.id) as LayoutNode) : [];

  // 하위 트리를 top부터 배치하고 하위 트리의 아래 끝(bottom)을 돌려준다.
  // 깊이는 소비 경계(깊이 64)와 노드 상한(200)으로 묶여 있어 재귀로 충분하다.
  function shift(n: LayoutNode, dy: number) {
    n.y += dy;
    for (const c of childrenOf(n)) shift(c, dy);
  }
  function place(n: LayoutNode, top: number): number {
    n.x = columnX[n.depth];
    const kids = childrenOf(n);
    if (kids.length === 0) {
      n.y = top;
      return top + n.height;
    }
    let cursor = top;
    let bottom = top;
    for (const k of kids) {
      bottom = place(k, cursor);
      cursor = bottom + LAYOUT_ROW_GAP;
    }
    const first = kids[0];
    const last = kids[kids.length - 1];
    const center = (first.y + first.height / 2 + last.y + last.height / 2) / 2;
    n.y = center - n.height / 2;
    if (n.y < top) {
      const dy = top - n.y;
      for (const k of kids) shift(k, dy);
      bottom += dy;
      n.y = top;
    }
    return Math.max(bottom, n.y + n.height);
  }

  const rootLayout = byId.get(root.id) as LayoutNode;
  const totalBottom = place(rootLayout, 0);

  const nodes = visible.map((v) => byId.get(v.node.id) as LayoutNode);
  const edges: LayoutEdge[] = nodes
    .filter((n) => n.parentId !== null)
    .map((n) => ({ id: `${n.parentId}->${n.id}`, source: n.parentId as string, target: n.id }));
  const width = nodes.reduce((m, n) => Math.max(m, n.x + n.width), 0);
  return { nodes, edges, width, height: totalBottom };
}

/** 모든 자식 있는 노드 id(전체 펼침용, 반복 순회). */
export function allExpandableIds(root: MindmapNode): string[] {
  const ids: string[] = [];
  const stack = [root];
  while (stack.length > 0) {
    const n = stack.pop() as MindmapNode;
    if (n.children.length > 0) ids.push(n.id);
    for (const c of n.children) stack.push(c);
  }
  return ids;
}
