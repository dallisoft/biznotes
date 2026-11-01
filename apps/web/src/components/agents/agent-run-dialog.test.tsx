import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Agent } from "@/lib/agent-types";
import { STUDIO_CARDS, type StudioCard } from "@/lib/studio-registry";

vi.mock("@/hooks/use-agents", () => ({
  useCreateRun: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/components/agents/source-picker", () => ({
  SourcePicker: () => <div data-testid="source-picker" />,
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const { AgentRunDialog } = await import("@/components/agents/agent-run-dialog");

function baseAgent(overrides: Partial<Agent> = {}): Agent {
  return {
    id: "agent1",
    name: "심층 리서치",
    description: "웹과 노트북을 오가며 리서치합니다",
    icon: "🔬",
    kind: "workflow",
    params_schema: { type: "object", properties: {} },
    scope: "system",
    enabled: true,
    version: 1,
    ...overrides,
  };
}

function renderDialog(agent: Agent, studioCard?: StudioCard) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <AgentRunDialog
        agent={agent}
        notebookId="nb1"
        open={true}
        onOpenChange={() => {}}
        studioCard={studioCard}
      />
    </QueryClientProvider>,
  );
}

describe("AgentRunDialog — 실행 전 비용 안내(phase-22-design.md §2.4)", () => {
  it("cost_budget_usd가 설정된 에이전트는 비용 안내 문구를 표시한다", () => {
    renderDialog(baseAgent({ cost_budget_usd: 0.5 }));
    const notice = screen.getByTestId("cost-budget-notice");
    expect(notice.textContent).toContain("0.50");
  });

  it("1센트 미만 비용 상한은 「< $0.01」로 보이고 정확값 「$0.004」를 title로 준다(P1-01, plan §6.5)", () => {
    renderDialog(baseAgent({ cost_budget_usd: 0.004 }));
    const notice = screen.getByTestId("cost-budget-notice");
    expect(notice).toHaveTextContent("이 작업은 최대 < $0.01 USD까지 과금될 수 있습니다.");
    const amount = screen.getByText("< $0.01");
    expect(notice).toContainElement(amount);
    expect(amount).toHaveAttribute("title", "$0.004");
  });

  it("1센트 이상 비용 상한은 정확히 보이므로 title이 없다", () => {
    renderDialog(baseAgent({ cost_budget_usd: 0.5 }));
    expect(screen.getByText("$0.50")).not.toHaveAttribute("title");
  });

  it("cost_budget_usd가 없는 에이전트는 비용 안내를 표시하지 않는다", () => {
    renderDialog(baseAgent({ cost_budget_usd: null }));
    expect(screen.queryByTestId("cost-budget-notice")).not.toBeInTheDocument();
  });
});

describe("AgentRunDialog — 스튜디오 카드로 연 다이얼로그(#182)", () => {
  const mindmap = STUDIO_CARDS[0];
  const skillAgent = baseAgent({
    name: "마인드맵",
    description: "소스 내용을 인터랙티브 마인드맵 트리(JSON)로 구조화합니다",
    icon: "🧩",
    kind: "skill",
    input: { mode: "rag", scope: "selected" },
  });

  it("카드 아이콘·설명을 쓰고 입력 정책 배지·스킬 설명을 보이지 않는다", () => {
    renderDialog(skillAgent, mindmap);
    const title = screen.getByRole("heading", { name: /마인드맵/ });
    expect(title).toHaveTextContent("🧠");
    expect(title).not.toHaveTextContent("🧩");
    expect(screen.getByText(mindmap.description)).toBeInTheDocument();
    expect(screen.queryByText(/\(JSON\)/)).not.toBeInTheDocument();
    expect(screen.queryByTestId("input-policy-badges")).not.toBeInTheDocument();
    expect(screen.getByTestId("source-picker")).toBeInTheDocument();
  });

  it("카드 없이 연 다이얼로그는 에이전트 아이콘·설명·입력 정책 배지를 그대로 보인다", () => {
    renderDialog(skillAgent);
    expect(screen.getByRole("heading", { name: /마인드맵/ })).toHaveTextContent("🧩");
    expect(screen.getByText(skillAgent.description as string)).toBeInTheDocument();
    expect(screen.getByTestId("input-policy-badges")).toBeInTheDocument();
  });

  it("스튜디오 다이얼로그는 input 정책이 selected가 아니어도 소스 선택기를 보인다", () => {
    renderDialog(baseAgent({ input: { mode: "rag", scope: "all" } }), mindmap);
    expect(screen.getByTestId("source-picker")).toBeInTheDocument();
  });
});
