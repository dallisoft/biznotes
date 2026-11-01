import { describe, expect, it } from "vitest";
import {
  allExpandableIds,
  LAYOUT_COLUMN_GAP,
  layoutMindmap,
  measureNode,
  visibleNodes,
  type LayoutNode,
} from "@/lib/mindmap/layout";
import type { MindmapNode } from "@/lib/mindmap/parse";

function node(id: string, label: string, children: MindmapNode[] = []): MindmapNode {
  return { id, label, title: label, children };
}

function overlaps(a: LayoutNode, b: LayoutNode): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

function assertNoOverlap(nodes: LayoutNode[]) {
  for (let i = 0; i < nodes.length; i += 1) {
    for (let j = i + 1; j < nodes.length; j += 1) {
      expect(overlaps(nodes[i], nodes[j]), `${nodes[i].id} ↔ ${nodes[j].id}`).toBe(false);
    }
  }
}

/** 200노드 · 깊이 10 · 한 부모 자식 50개 fixture */
function bigTree(): MindmapNode {
  let count = 1;
  const wide: MindmapNode[] = [];
  for (let i = 0; i < 50; i += 1) {
    wide.push(node(`w${i}`, i % 3 === 0 ? "아주 긴 라벨 ".repeat(8) : `잎 ${i}`));
    count += 1;
  }
  // 깊이 10 사슬
  let chain = node("c9", "사슬 끝");
  count += 1;
  for (let d = 8; d >= 1; d -= 1) {
    chain = node(`c${d}`, `사슬 ${d}`, [chain]);
    count += 1;
  }
  const branches: MindmapNode[] = [node("wide", "넓은 부모", wide), chain];
  count += 1;
  let k = 0;
  while (count < 200) {
    const kids: MindmapNode[] = [];
    for (let j = 0; j < 4 && count + 1 + kids.length < 200; j += 1) kids.push(node(`b${k}-${j}`, `손자 ${j}`));
    branches.push(node(`b${k}`, `가지 ${k}`, kids));
    count += 1 + kids.length;
    k += 1;
  }
  return node("root", "루트", branches);
}

describe("layoutMindmap (AC-1.2)", () => {
  const tree = node("root", "루트", [
    node("a", "가", [node("a1", "가1"), node("a2", "가2"), node("a3", "가3")]),
    node("b", "나"),
  ]);

  it("깊이 d의 x = 깊이 d−1 열의 x + 그 열의 최대 폭 + 간격", () => {
    const l = layoutMindmap(tree, new Set(["root", "a"]));
    const byId = new Map(l.nodes.map((n) => [n.id, n]));
    const col0 = Math.max(...l.nodes.filter((n) => n.depth === 0).map((n) => n.width));
    const col1 = Math.max(...l.nodes.filter((n) => n.depth === 1).map((n) => n.width));
    expect(byId.get("root")?.x).toBe(0);
    expect(byId.get("a")?.x).toBe(col0 + LAYOUT_COLUMN_GAP);
    expect(byId.get("a1")?.x).toBe(col0 + LAYOUT_COLUMN_GAP + col1 + LAYOUT_COLUMN_GAP);
  });

  it("자식 있는 노드의 y는 첫·끝 자식 중심의 중앙이고, 잎은 세로로 누적된다", () => {
    const l = layoutMindmap(tree, new Set(["root", "a"]));
    const byId = new Map(l.nodes.map((n) => [n.id, n]));
    const center = (id: string) => {
      const n = byId.get(id) as LayoutNode;
      return n.y + n.height / 2;
    };
    expect(center("a")).toBeCloseTo((center("a1") + center("a3")) / 2);
    expect(center("root")).toBeCloseTo((center("a") + center("b")) / 2);
    const a1 = byId.get("a1") as LayoutNode;
    const a2 = byId.get("a2") as LayoutNode;
    expect(a2.y).toBeGreaterThan(a1.y + a1.height - 0.001);
  });

  it("접힌 노드의 하위는 제외한다", () => {
    const l = layoutMindmap(tree, new Set(["root"]));
    expect(l.nodes.map((n) => n.id)).toEqual(["root", "a", "b"]);
    expect(l.edges.map((e) => e.id)).toEqual(["root->a", "root->b"]);
    expect(l.nodes.find((n) => n.id === "a")?.expanded).toBe(false);
  });

  it("같은 입력 → 같은 좌표", () => {
    const t = bigTree();
    const all = new Set(allExpandableIds(t));
    expect(layoutMindmap(t, all)).toEqual(layoutMindmap(t, all));
  });

  it("200노드 · 깊이 10 · 자식 50개 fixture에서 노드 사각형 겹침 0(전 쌍 검사)", () => {
    const t = bigTree();
    const l = layoutMindmap(t, new Set(allExpandableIds(t)));
    expect(l.nodes).toHaveLength(200);
    expect(Math.max(...l.nodes.map((n) => n.depth))).toBe(9);
    assertNoOverlap(l.nodes);
  });

  it("부모 상자가 자식 묶음보다 높아도 겹치지 않는다(작은 자식 하나씩 가진 형제)", () => {
    const tall = "길다 ".repeat(40);
    const t = node("r", "r", [node("p1", tall, [node("x", "x")]), node("p2", tall, [node("y", "y")])]);
    const l = layoutMindmap(t, new Set(["r", "p1", "p2"]));
    assertNoOverlap(l.nodes);
  });

  it("visibleNodes는 보이는 노드를 전위 순서로 준다", () => {
    expect(visibleNodes(tree, new Set(["root", "a"])).map((v) => v.node.id)).toEqual([
      "root",
      "a",
      "a1",
      "a2",
      "a3",
      "b",
    ]);
  });

  it("measureNode는 폭을 상하한 안에 두고 긴 라벨은 줄을 늘린다(최대 3줄)", () => {
    const short = measureNode(node("s", "짧"));
    const long = measureNode(node("l", "아주 긴 라벨 ".repeat(20)));
    expect(short.width).toBeGreaterThanOrEqual(112);
    expect(long.width).toBeLessThanOrEqual(240);
    expect(long.height).toBeGreaterThan(short.height);
  });
});
