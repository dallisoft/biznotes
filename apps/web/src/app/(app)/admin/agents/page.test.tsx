/**
 * 관리자 에이전트 페이지 — BL-125(편집 다이얼로그 상단 잘림) + ui-ux-redesign P5(AC-5.1·5.3·5.6).
 * 다이얼로그는 화면 가운데 고정이라 정의가 길면 뷰포트를 넘어 탭 헤더가 잘렸다. 다이얼로그 높이를
 * 뷰포트로 제한하고 편집 본문만 내부 스크롤하는 구조를 고정한다(jsdom은 레이아웃을 계산하지 않으므로
 * 클래스·구조로 검증하고, 실제 뷰포트 동작은 e2e phase13 "비주얼 워크플로우 빌더"가 1080 높이로 검증).
 * 네트워크 경계(`api` 객체)와 토스트(sonner)만 대체하고 페이지·훅·다이얼로그·라벨 사전은 실제로 실행한다(§3.3-3).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import AdminAgentsPage from "@/app/(app)/admin/agents/page";
import { APP_QUERY_DEFAULTS } from "@/lib/query-defaults";
import { stubNarrowViewport } from "@/hooks/use-narrow-viewport.test-kit";

// @xyflow/react의 CSS import는 이 프로젝트의 Vite/Vitest 조합에서 PostCSS 로드에 실패한다
// (artifact-content.test.tsx와 동일 우회). 이 테스트는 JSON 탭만 렌더한다.
vi.mock("@xyflow/react/dist/style.css", () => ({}));
const { toast } = vi.hoisted(() => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("sonner", () => ({ toast }));
const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
// 네트워크 경계만 mock — ApiError 등은 실제 모듈
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));

// 긴 정의(시드 "마인드맵"처럼) — field-sizing-content 텍스트영역이 내용만큼 늘어나는 조건
const LONG_DEFINITION = {
  nodes: Array.from({ length: 40 }, (_, i) => ({
    id: `n${i}`,
    type: "llm",
    config: { prompt: `step ${i} `.repeat(10) },
  })),
  edges: [],
};

const AGENT = {
  id: "a1",
  slug: "mindmap",
  name: "마인드맵",
  icon: "🧠",
  kind: "workflow",
  scope: "system",
  version: 3,
  enabled: true,
  definition: LONG_DEFINITION,
  params_schema: { type: "object", properties: {} },
};
const AGENT_MCP = {
  ...AGENT,
  id: "a2",
  slug: "web-search",
  name: "웹 검색",
  icon: "🔎",
  kind: "mcp",
  scope: "user",
  version: 1,
  enabled: false,
  definition: {},
};

const POLICIES = [
  { tool_name: "rag_search", side_effect: false, auto_approved_by: [] },
  {
    tool_name: "send_email",
    side_effect: true,
    auto_approved_by: [{ agent_id: "a1", agent_name: "마인드맵", node_id: "mail", policy: { allow: true } }],
  },
];

function renderPage({
  agents = [AGENT, AGENT_MCP] as unknown[],
  agentsError = false,
  policies = POLICIES as unknown[],
}: { agents?: unknown[]; agentsError?: boolean; policies?: unknown[] } = {}) {
  api.get.mockImplementation(async (path: string) => {
    if (path === "/api/v1/admin/agents") {
      if (agentsError) throw new Error("boom");
      return agents;
    }
    if (path === "/api/v1/admin/agents/tool-policies") return policies;
    throw new Error(`unexpected GET ${path}`);
  });
  const client = new QueryClient({ defaultOptions: { queries: { ...APP_QUERY_DEFAULTS, retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AdminAgentsPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
  toast.success.mockReset();
  toast.error.mockReset();
});

describe("AdminAgentsPage 편집 다이얼로그 (BL-125)", () => {
  it("다이얼로그는 뷰포트 높이로 제한되고 편집 본문만 내부 스크롤한다", async () => {
    renderPage();
    const row = (await screen.findByText("마인드맵")).closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: "편집" }));

    const dialog = await screen.findByTestId("edit-agent-dialog");
    // 높이 상한 + 세로 flex(공유 DialogContent의 grid를 덮어씀) + 바깥 스크롤 차단
    expect(dialog).toHaveClass("max-h-[calc(100dvh-2rem)]", "flex", "flex-col", "overflow-hidden");
    expect(dialog).not.toHaveClass("grid");

    // 편집 본문(텍스트영역)만 남은 높이를 채우며 스크롤한다
    const body = within(dialog).getByTestId("edit-agent-dialog-body");
    expect(body).toHaveClass("min-h-0", "flex-1", "overflow-y-auto");
    expect(within(body).getByText("정의(JSON)")).toBeInTheDocument();
    expect(within(body).getByDisplayValue(/"n39"/)).toBeInTheDocument();

    // 탭 헤더와 저장 버튼은 스크롤 영역 밖(항상 보임)에 있고 줄어들지 않는다
    const visualTab = within(dialog).getByTestId("tab-visual");
    expect(body).not.toContainElement(visualTab);
    const save = within(dialog).getByRole("button", { name: "저장" });
    expect(body).not.toContainElement(save);
    expect(save).toHaveClass("shrink-0");
  });
});

describe("AdminAgentsPage 편집 다이얼로그 라벨·검증 (P-20 ②)", () => {
  it("입력 이름은 「정의(JSON)」「입력 파라미터 스키마(JSON)」이고, JSON이 틀리면 저장 요청 없이 오류를 알린다", async () => {
    const user = userEvent.setup();
    renderPage();
    const row = (await screen.findByText("마인드맵")).closest("tr")!;
    await user.click(within(row).getByRole("button", { name: "편집" }));
    const dialog = await screen.findByTestId("edit-agent-dialog");

    const definition = within(dialog).getByLabelText("정의(JSON)");
    expect(within(dialog).getByLabelText("입력 파라미터 스키마(JSON)")).toBeInTheDocument();
    // 원시 필드명 라벨은 없다
    expect(within(dialog).queryByText("definition")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("params_schema")).not.toBeInTheDocument();

    fireEvent.change(definition, { target: { value: "{ not json" } });
    await user.click(within(dialog).getByRole("button", { name: "저장" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("JSON 형식이 올바르지 않습니다");
    expect(definition).toHaveAttribute("aria-invalid", "true");
    expect(api.patch).not.toHaveBeenCalled();
  });

  it("저장은 정의·스키마를 PATCH하고 성공 토스트 뒤 다이얼로그를 닫는다", async () => {
    const user = userEvent.setup();
    api.patch.mockResolvedValue({});
    renderPage({ agents: [AGENT_MCP] });
    await user.click(await screen.findByRole("button", { name: "편집" }));
    const dialog = await screen.findByTestId("edit-agent-dialog");
    await user.click(within(dialog).getByRole("button", { name: "저장" }));

    await waitFor(() =>
      expect(api.patch).toHaveBeenCalledWith("/api/v1/admin/agents/a2", {
        definition: {},
        params_schema: AGENT_MCP.params_schema,
      }),
    );
    await waitFor(() => expect(screen.queryByTestId("edit-agent-dialog")).not.toBeInTheDocument());
    expect(toast.success).toHaveBeenCalledWith("‘웹 검색’ 에이전트를 저장했습니다", expect.anything());
  });
});

describe("AdminAgentsPage 표 (AC-5.1 · AC-5.3 · AC-5.6)", () => {
  it("PageHeader: 브레드크럼 「관리 / 에이전트」와 h1 하나", async () => {
    renderPage();
    const crumb = screen.getByRole("navigation", { name: "위치" });
    expect(within(crumb).getByRole("link", { name: "관리" })).toHaveAttribute("href", "/admin");
    expect(within(crumb).getByText("에이전트")).toHaveAttribute("aria-current", "page");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1, name: "에이전트" })).toBeInTheDocument();
    await screen.findByText("마인드맵");
  });

  it("머리글은 한국어(영문 kind·scope 없음)이고 유형·범위 값은 라벨 사전을 쓴다", async () => {
    renderPage();
    await screen.findByText("마인드맵");
    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual(["이름", "유형", "범위", "버전", "상태", "동작"]);
    expect(headers).not.toContain("kind");
    expect(headers).not.toContain("scope");

    const row = screen.getByText("마인드맵").closest("tr")!;
    expect(within(row).getByText("워크플로우")).toBeInTheDocument(); // workflow → KIND_LABEL
    expect(within(row).getByText("시스템")).toBeInTheDocument(); // system → AGENT_SCOPE_LABEL
    expect(within(row).queryByText("workflow")).not.toBeInTheDocument();
    const mcpRow = screen.getByText("웹 검색").closest("tr")!;
    expect(within(mcpRow).getByText("MCP")).toBeInTheDocument();
    // 코어 enum `user`도 라벨로 보인다(ops E-1 — 원시 「user」 0)
    expect(within(mcpRow).getByText("사용자")).toBeInTheDocument();
    expect(within(mcpRow).queryByText("user")).not.toBeInTheDocument();
  });

  it("행 스위치는 「{이름} 활성」 이름을 갖고, 끄면 enabled=false를 PATCH한다(성공 토스트 없음)", async () => {
    const user = userEvent.setup();
    api.patch.mockResolvedValue({});
    renderPage();
    const sw = await screen.findByRole("switch", { name: "마인드맵 활성" });
    expect(sw).toBeChecked();
    expect(screen.getByRole("switch", { name: "웹 검색 활성" })).not.toBeChecked();

    await user.click(sw);
    await waitFor(() =>
      expect(api.patch).toHaveBeenCalledWith("/api/v1/admin/agents/a1", { enabled: false }),
    );
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("활성 전환이 실패하면 「…하지 못했습니다」 토스트를 한 번만 띄운다", async () => {
    const user = userEvent.setup();
    api.patch.mockRejectedValue(new Error("서버 오류"));
    renderPage();
    await user.click(await screen.findByRole("switch", { name: "마인드맵 활성" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(1));
    expect(toast.error.mock.calls[0][0]).toBe("‘마인드맵’ 에이전트를 비활성화하지 못했습니다");
  });

  it("목록이 비면 표·페이지네이션 대신 EmptyState", async () => {
    renderPage({ agents: [] });
    expect(await screen.findByText("아직 에이전트가 없습니다")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("페이지당 개수")).not.toBeInTheDocument();
  });

  it("목록을 못 불러오면 ErrorState, 「다시 시도」는 목록을 다시 읽고 성공하면 포커스가 표 안으로 간다", async () => {
    const user = userEvent.setup();
    renderPage({ agentsError: true });
    expect(await screen.findByText("에이전트 목록을 불러오지 못했습니다")).toBeInTheDocument();
    const before = api.get.mock.calls.length;
    api.get.mockImplementation(async (path: string) => (path === "/api/v1/admin/agents" ? [AGENT] : POLICIES));
    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    await waitFor(() => expect(api.get.mock.calls.length).toBeGreaterThan(before));
    await screen.findByText("마인드맵");
    // 성공하면 포커스는 페이지 맨 위(브레드크럼)가 아니라 다시 그려진 표 안으로 간다(HCI-P2-2)
    await waitFor(() => expect(screen.getByRole("switch", { name: "마인드맵 활성" })).toHaveFocus());
  });

  it("11개 이상이면 페이지네이션이 보이고(e2e phase13의 「페이지당 개수」), 10개 이하면 숨긴다", async () => {
    const many = Array.from({ length: 11 }, (_, i) => ({ ...AGENT_MCP, id: `m${i}`, name: `에이전트 ${i}` }));
    const { unmount } = renderPage({ agents: many });
    expect(await screen.findByLabelText("페이지당 개수")).toBeInTheDocument();
    unmount();
    renderPage({ agents: many.slice(0, 10) });
    await screen.findByText("에이전트 0");
    expect(screen.queryByLabelText("페이지당 개수")).not.toBeInTheDocument();
  });

  it("탭을 오가도 목록 페이지 위치가 유지된다", async () => {
    const user = userEvent.setup();
    const many = Array.from({ length: 12 }, (_, i) => ({ ...AGENT_MCP, id: `m${i}`, name: `에이전트 ${i}` }));
    renderPage({ agents: many });
    await user.click(await screen.findByRole("button", { name: "다음 페이지" }));
    expect(await screen.findByText("에이전트 11")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "도구 정책" }));
    await screen.findByTestId("tool-policies-table");
    await user.click(screen.getByRole("tab", { name: "에이전트" }));
    expect(await screen.findByText("에이전트 11")).toBeInTheDocument();
  });
});

describe("AdminAgentsPage 도구 정책 탭 (DP-4)", () => {
  it("부작용 분류를 글자 있는 StatusBadge로 보인다", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("tab", { name: "도구 정책" }));
    const table = await screen.findByTestId("tool-policies-table");
    const side = within(table).getByText("부작용(승인 필요)");
    expect(side).toHaveAttribute("data-slot", "status-badge");
    expect(side).toHaveAttribute("data-tone", "warning");
    expect(within(table).getByText("읽기 전용")).toHaveAttribute("data-tone", "neutral");
  });

  it("정책이 없으면 EmptyState", async () => {
    const user = userEvent.setup();
    renderPage({ policies: [] });
    await user.click(await screen.findByRole("tab", { name: "도구 정책" }));
    expect(await screen.findByText("도구 정책이 없습니다")).toBeInTheDocument();
    expect(screen.queryByTestId("tool-policies-table")).not.toBeInTheDocument();
  });
});

describe("AdminAgentsPage 좁은 화면 표 규칙 (plan §6.7, P5 리뷰 P5-04)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("에이전트·도구 정책 표는 좁은 화면 규칙 래퍼(첫 열 고정·오른쪽 단서) 안에 있다", async () => {
    const user = userEvent.setup();
    renderPage();
    const agents = (await screen.findByText("마인드맵")).closest("table")!;
    expect(agents.closest('[data-slot="admin-table-frame"]')).not.toBeNull();
    await user.click(screen.getByRole("tab", { name: "도구 정책" }));
    const policies = await screen.findByTestId("tool-policies-table");
    expect(policies.closest('[data-slot="admin-table-frame"]')).not.toBeNull();
  });

  it("< 768: 「편집」은 ⋯ 메뉴의 항목이 되고, 고르면 편집 다이얼로그가 열린다", async () => {
    stubNarrowViewport();
    const user = userEvent.setup();
    renderPage();
    const row = (await screen.findByText("마인드맵")).closest("tr")!;
    await waitFor(() => expect(within(row).queryByRole("button", { name: "편집" })).toBeNull());
    await user.click(within(row).getByRole("button", { name: "마인드맵 동작 더보기" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["편집"]);
    await user.click(within(menu).getByRole("menuitem", { name: "편집" }));
    expect(await screen.findByTestId("edit-agent-dialog")).toBeInTheDocument();
  });
});
