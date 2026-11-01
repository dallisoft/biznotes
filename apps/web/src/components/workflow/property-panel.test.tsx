/**
 * 일반 워크플로우 에디터 속성 패널 — 계획 AC-28 (b)(옵션에 없는 기존 값의 표시)와 AC-30 (a)
 * (react_agent 허용 도구의 mcp_call 숨김)를 실제 렌더로 확인한다. 패널은 이 에디터 전용이며
 * `getDescriptor`의 기본 표면("user")을 쓴다.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PropertyPanel } from "@/components/workflow/property-panel";

vi.mock("@/lib/api-client", () => ({
  api: { get: vi.fn().mockResolvedValue([]) },
  ApiError: class ApiError extends Error {},
}));

function renderPanel(node: Record<string, unknown>, onPatch = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <PropertyPanel
        node={node as never}
        variables={[]}
        onPatch={onPatch}
        onDelete={() => {}}
      />
    </QueryClientProvider>,
  );
  return onPatch;
}

describe("llm 노드 도구 — 옵션에 없는 기존 값 (AC-28 (b))", () => {
  it("저장된 mcp_call은 체크박스로 보이지 않지만 값은 보존되고, 다른 옵션 토글 뒤에도 남는다", () => {
    const onPatch = renderPanel({ id: "g", type: "llm", prompt: "x", tools: ["mcp_call", "rag_search"] });

    expect(screen.queryByLabelText("MCP 도구")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("HTTP 요청")).not.toBeInTheDocument();
    expect(screen.getByLabelText("RAG 검색")).toBeChecked();
    expect(screen.getByLabelText("웹 검색")).not.toBeChecked();

    fireEvent.click(screen.getByLabelText("웹 검색"));
    expect(onPatch).toHaveBeenCalledWith({ tools: ["mcp_call", "rag_search", "web_search"] });
  });
});

describe("react_agent 허용 도구 — 일반 에디터 표면 (AC-30 (a))", () => {
  it("mcp_call 옵션을 내지 않고 승인 게이트 도구(call_agent·http_request)는 낸다", () => {
    renderPanel({ id: "r", type: "react_agent", goal: "x" });

    expect(screen.queryByLabelText("MCP 도구(단건 지정)")).not.toBeInTheDocument();
    expect(screen.getByLabelText("서브에이전트 호출")).toBeInTheDocument();
    expect(screen.getByLabelText("HTTP 요청")).toBeInTheDocument();
  });

  it("기존 mcp_tool 노드는 일반 에디터에서도 속성 패널이 렌더된다(숨김이지 삭제가 아님)", () => {
    renderPanel({ id: "m", type: "mcp_tool", server_id: "", tool_name: "" });
    expect(screen.getByTestId("property-panel")).toHaveTextContent("MCP 도구");
    expect(screen.queryByText(/알 수 없는 노드 타입/)).not.toBeInTheDocument();
  });

  it("MCP 서버를 찾는 안내는 관리 메뉴 이름(「관리 → MCP 서버」)을 쓴다 — 「어드민 콘솔」 아님(plan §6.3.1, P6)", () => {
    renderPanel({ id: "m", type: "mcp_tool", server_id: "", tool_name: "" });
    const panel = screen.getByTestId("property-panel");
    expect(panel).toHaveTextContent("관리 → MCP 서버에서 확인하세요");
    expect(panel).not.toHaveTextContent("어드민");
  });
});

describe("save_sources 노드 — 리터럴 읽기 전용, items만 편집 (#260)", () => {
  const node = {
    id: "save",
    type: "save_sources",
    items: "{list.text}",
    namespace: "naver-research",
    allowed_hosts: ["stock.pstatic.net"],
    notebook_rule: { title_template: "종목분석 {month}", timezone: "Asia/Seoul", emoji: "📈" },
    requires_approval: false,
  };

  it("이름공간·허용 호스트·노트북 규칙은 값만 보이고 입력 칸이 없다", () => {
    renderPanel(node);
    const panel = screen.getByTestId("property-panel");
    expect(panel).toHaveTextContent("소스로 저장");
    expect(screen.getByTestId("readonly-namespace")).toHaveTextContent("naver-research");
    expect(screen.getByTestId("readonly-allowed_hosts")).toHaveTextContent("stock.pstatic.net");
    expect(screen.getByTestId("readonly-notebook_rule")).toHaveTextContent("종목분석 {month}");
    // 편집 칸은 items 하나뿐이다 — 리터럴 값은 입력·텍스트 영역에 들어 있지 않다
    const editors = [...panel.querySelectorAll("input, textarea")] as HTMLInputElement[];
    expect(editors.map((e) => e.value)).toEqual(["{list.text}"]);
  });

  it("items를 고치면 items만 패치한다", () => {
    const onPatch = renderPanel(node);
    fireEvent.change(screen.getByDisplayValue("{list.text}"), {
      target: { value: "{other.text}" },
    });
    expect(onPatch).toHaveBeenCalledWith({ items: "{other.text}" });
    expect(onPatch).toHaveBeenCalledTimes(1);
  });

  it("값이 없는 리터럴은 「(없음)」으로 보인다", () => {
    renderPanel({ id: "save", type: "save_sources", items: "{list.text}" });
    expect(screen.getByTestId("readonly-notebook_rule")).toHaveTextContent("(없음)");
  });
});
