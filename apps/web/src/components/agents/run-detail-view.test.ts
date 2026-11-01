import { describe, expect, it, vi } from "vitest";
import { groupLogs } from "./run-detail-view";
import type { ExecutionLog } from "@/lib/agent-types";

// run-detail-view → artifact-content → mindmap-viewer가 불러오는 @xyflow 스타일시트는 Vitest PostCSS 변환과
// 충돌한다 — 스타일시트만 비운다(agent-panel.test.tsx·mindmap-viewer.test.tsx와 같은 처리, 로직 mock 아님).
vi.mock("@xyflow/react/dist/style.css", () => ({}));

function makeLog(overrides: Partial<ExecutionLog>): ExecutionLog {
  return {
    id: overrides.id ?? "id",
    run_id: "run-1",
    node_id: overrides.node_id ?? "n",
    node_type: overrides.node_type ?? "llm",
    status: "succeeded",
    error_message: null,
    error_code: null,
    token_usage: null,
    cost_usd: null,
    duration_ms: 10,
    started_at: null,
    finished_at: null,
    model: null,
    fallback_used: null,
    model_attempts: null,
    ...overrides,
  };
}

describe("groupLogs", () => {
  it("groups react_tool_call rows under their parent react_agent node by id prefix", () => {
    const logs: ExecutionLog[] = [
      makeLog({ id: "1", node_id: "r", node_type: "react_agent" }),
      makeLog({ id: "2", node_id: "r:tool:0", node_type: "react_tool_call" }),
      makeLog({ id: "3", node_id: "r:tool:1", node_type: "react_tool_call" }),
    ];
    const groups = groupLogs(logs);
    expect(groups).toHaveLength(1);
    expect(groups[0].parent.node_id).toBe("r");
    expect(groups[0].children.map((c) => c.id)).toEqual(["2", "3"]);
  });

  it("does not cross-attribute tool calls between two different react_agent nodes", () => {
    const logs: ExecutionLog[] = [
      makeLog({ id: "1", node_id: "r1", node_type: "react_agent" }),
      makeLog({ id: "2", node_id: "r2", node_type: "react_agent" }),
      makeLog({ id: "3", node_id: "r1:tool:0", node_type: "react_tool_call" }),
      makeLog({ id: "4", node_id: "r2:tool:0", node_type: "react_tool_call" }),
    ];
    const groups = groupLogs(logs);
    expect(groups).toHaveLength(2);
    const g1 = groups.find((g) => g.parent.node_id === "r1")!;
    const g2 = groups.find((g) => g.parent.node_id === "r2")!;
    expect(g1.children.map((c) => c.id)).toEqual(["3"]);
    expect(g2.children.map((c) => c.id)).toEqual(["4"]);
  });

  it("returns non-react nodes as their own group with no children", () => {
    const logs: ExecutionLog[] = [
      makeLog({ id: "1", node_id: "search", node_type: "rag_search" }),
      makeLog({ id: "2", node_id: "gen", node_type: "llm" }),
    ];
    const groups = groupLogs(logs);
    expect(groups).toHaveLength(2);
    expect(groups.every((g) => g.children.length === 0)).toBe(true);
  });

  it("returns an empty array for empty logs", () => {
    expect(groupLogs([])).toEqual([]);
  });

  it("does not attribute a tool call whose node_id merely starts with the parent id without the ':tool:' separator", () => {
    // "rXtool:0"이 "r"로 시작하지만 ":tool:" 구분자가 없으므로 매칭되면 안 된다.
    const logs: ExecutionLog[] = [
      makeLog({ id: "1", node_id: "r", node_type: "react_agent" }),
      makeLog({ id: "2", node_id: "rXtool:0", node_type: "react_tool_call" }),
    ];
    const groups = groupLogs(logs);
    expect(groups).toHaveLength(1);
    expect(groups[0].children).toHaveLength(0);
  });
});
