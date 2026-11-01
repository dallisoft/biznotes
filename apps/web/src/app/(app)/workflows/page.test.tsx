/**
 * 워크플로우 목록 페이지 — ui-ux-redesign P4(plan §6.6.2 워크플로우 목록 P-09, AC-4.1~4.4, HCI-3 ②③).
 * 경계(`@/lib/api-client`·라우터·sonner 토스트)만 대체한다. 페이지·카드·다이얼로그·훅(useDeleteWorkflow 등)·
 * RowActions·ConfirmDialog는 실제 코드다(AC-4.3 「훅 실경로, api 모듈만 모킹」).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import WorkflowsPage from "@/app/(app)/workflows/page";
import { filterWorkflows, workflowMeta } from "@/components/workflow/workflow-card";
import type { Workflow } from "@/lib/workflow-types";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }));
vi.mock("@/lib/api-client", () => ({
  api: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));

import { toast } from "sonner";
import { api, ApiError } from "@/lib/api-client";

const m = vi.mocked(api);

function wf(id: string, extra: Partial<Workflow> = {}): Workflow {
  return {
    id,
    name: `워크플로우 ${id}`,
    description: "",
    icon: "⚙️",
    kind: "workflow",
    definition: { nodes: [], edges: [] },
    params_schema: {},
    scope: "user",
    owner_id: "me",
    enabled: false,
    version: 1,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    can_edit: true,
    ...extra,
  } as Workflow;
}

/** GET /workflows 응답을 정한다 — 목록이 바뀌는 시나리오(삭제 뒤 재조회)는 배열을 바꿔 끼운다. */
let current: Workflow[] | Error = [];

function renderPage(list: Workflow[] | Error) {
  current = list;
  m.get.mockImplementation(async (path: string) => {
    if (path !== "/api/v1/workflows") throw new Error(`unexpected GET ${path}`);
    if (current instanceof Error) throw current;
    return current;
  });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <WorkflowsPage />
    </QueryClientProvider>,
  );
}

async function cards() {
  const grid = await screen.findByTestId("workflow-grid");
  return within(grid).getAllByRole("listitem");
}

async function openMenu(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(screen.getByRole("button", { name: `${name} 동작 더보기` }));
  return screen.findByRole("menu");
}

afterEach(() => {
  m.get.mockReset();
  m.post.mockReset();
  m.delete.mockReset();
  push.mockReset();
  vi.mocked(toast.success).mockReset();
  vi.mocked(toast.error).mockReset();
});

describe("헤더·카드(AC-4.1·4.4)", () => {
  it("PageHeader actions에 「새 워크플로우」 주 버튼 하나(create-workflow testid 유지)", async () => {
    const { container } = renderPage([wf("a")]);
    await cards();
    expect(screen.getByRole("heading", { level: 1, name: "워크플로우" })).toBeInTheDocument();
    const actions = container.querySelector<HTMLElement>("[data-slot=page-header-actions]")!;
    const buttons = within(actions).getAllByRole("button");
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveTextContent("새 워크플로우");
    expect(buttons[0]).toHaveAttribute("data-testid", "create-workflow");
    expect(buttons[0]).toHaveClass("bg-primary");
  });

  it("상태는 「게시됨/초안」 StatusBadge, 원시값 draft 없음, 메타 한 줄(노드·버전·시스템)", async () => {
    const { container } = renderPage([
      wf("pub", { enabled: true, version: 3, definition: { nodes: [{ id: "a", type: "llm" }], edges: [] } }),
      wf("drf"),
      wf("sys", { scope: "system", can_edit: false }),
    ]);
    const [pub, drf, sys] = await cards();
    expect(within(pub).getByText("게시됨")).toHaveAttribute("data-tone", "success");
    expect(within(drf).getByText("초안")).toHaveAttribute("data-tone", "neutral");
    expect(pub).toHaveTextContent("노드 1개 · v3");
    expect(sys).toHaveTextContent("노드 0개 · v1 · 시스템");
    expect(container.textContent).not.toMatch(/draft/i);
  });

  it("편집 가능한 카드 제목은 편집 화면 링크, 편집 불가 카드 제목은 「복제해서 편집」 안내를 연다(제자리 링크 없음)", async () => {
    const user = userEvent.setup();
    m.post.mockResolvedValue(wf("clone-1"));
    renderPage([wf("mine"), wf("theirs", { can_edit: false, name: "남의 워크플로우" })]);
    const [mine, theirs] = await cards();
    expect(within(mine).getByRole("link", { name: "워크플로우 mine" })).toHaveAttribute(
      "href",
      "/workflows/mine/edit",
    );
    expect(within(theirs).queryByRole("link")).not.toBeInTheDocument();

    await user.click(within(theirs).getByRole("button", { name: "남의 워크플로우" }));
    const dialog = await screen.findByRole("dialog", { name: "이 워크플로우는 편집할 수 없습니다" });
    expect(dialog).toHaveTextContent("‘남의 워크플로우’은(는) 다른 사람이 만들었거나");
    await user.click(within(dialog).getByRole("button", { name: "복제해서 편집" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/workflows/clone-1/edit"));
    expect(m.post).toHaveBeenCalledWith("/api/v1/workflows/theirs/duplicate");
  });

  it("편집 불가 안내의 「닫기」는 푸터 버튼 하나뿐이고(X 없음 — 같은 이름 둘 방지, E-P5I-1) 누르면 닫힌다", async () => {
    const user = userEvent.setup();
    renderPage([wf("theirs", { can_edit: false, name: "남의 워크플로우" })]);
    const [theirs] = await cards();
    await user.click(within(theirs).getByRole("button", { name: "남의 워크플로우" }));
    const dialog = await screen.findByRole("dialog", { name: "이 워크플로우는 편집할 수 없습니다" });
    // 이름 「닫기」 버튼은 푸터의 텍스트 버튼 하나다(X 아이콘 버튼 없음)
    expect(within(dialog).getAllByRole("button", { name: "닫기" })).toHaveLength(1);
    expect(within(dialog).getByRole("button", { name: "닫기" })).toHaveTextContent(/^닫기$/);
    await user.click(within(dialog).getByRole("button", { name: "닫기" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "이 워크플로우는 편집할 수 없습니다" })).not.toBeInTheDocument(),
    );
  });

  it("workflowMeta·filterWorkflows 단위 규칙", () => {
    expect(workflowMeta(wf("x", { version: 2, definition: { nodes: Array(1200).fill({ id: "n", type: "llm" }), edges: [] } })))
      .toBe("노드 1,200개 · v2");
    const list = [wf("a", { name: "주간 보고", description: "" }), wf("b", { name: "번역", description: "Weekly 요약" })];
    expect(filterWorkflows(list, "  보고 ").map((w) => w.id)).toEqual(["a"]);
    expect(filterWorkflows(list, "weekly").map((w) => w.id)).toEqual(["b"]);
    expect(filterWorkflows(list, "")).toBe(list);
  });
});

describe("검색·빈 상태·오류(§6.5)", () => {
  it("이름·설명으로 클라이언트 필터, 결과 없으면 「필터 지우기」 — 서버 재요청 없음", async () => {
    const user = userEvent.setup();
    renderPage([wf("a", { name: "주간 보고" }), wf("b", { name: "번역", description: "영문 요약" })]);
    await cards();
    const search = screen.getByRole("searchbox", { name: "워크플로우 검색" });
    await user.type(search, "요약");
    expect((await cards()).map((c) => c.textContent)).toEqual([expect.stringContaining("번역")]);
    await user.clear(search);
    await user.type(search, "없는 이름");
    expect(screen.getByText("조건에 맞는 항목이 없습니다")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "필터 지우기" }));
    expect(await cards()).toHaveLength(2);
    expect(m.get).toHaveBeenCalledTimes(1);
  });

  it("워크플로우가 없으면 빈 상태 — 생성 진입점은 헤더 하나(DP-2)", async () => {
    renderPage([]);
    expect(await screen.findByText("아직 워크플로우가 없습니다")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "새 워크플로우" })).toHaveLength(1);
  });

  it("목록을 못 불러오면 ErrorState + 「다시 시도」", async () => {
    const user = userEvent.setup();
    renderPage(new Error("down"));
    expect(await screen.findByText("워크플로우 목록을 불러오지 못했습니다")).toBeInTheDocument();
    current = [wf("a")];
    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    expect(await cards()).toHaveLength(1);
  });

  it("「다시 시도」 뒤 포커스는 사라지지 않고 래퍼 안 첫 버튼 「새 워크플로우」에 놓인다(HCI-P2-2)", async () => {
    const user = userEvent.setup();
    renderPage(new Error("down"));
    await screen.findByText("워크플로우 목록을 불러오지 못했습니다");
    let release: (value: Workflow[]) => void = () => {};
    m.get.mockImplementation(() => new Promise<Workflow[]>((resolve) => (release = resolve)));
    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    expect(await screen.findByText("워크플로우 목록을 불러오는 중")).toBeInTheDocument();
    const create = screen.getByTestId("create-workflow");
    await waitFor(() => expect(document.activeElement).toBe(create));
    release([wf("a")]);
    expect(await cards()).toHaveLength(1);
    expect(document.activeElement).toBe(create);
  });
});

describe("새 워크플로우 — 이름 입력 다이얼로그(AC-4.2)", () => {
  it("열기만으로는 만들지 않고, 이름이 비면 「만들기」 비활성 → 이름 입력 후 POST 1회 → 편집 화면", async () => {
    const user = userEvent.setup();
    m.post.mockResolvedValue(wf("new-wf"));
    renderPage([]);
    await screen.findByText("아직 워크플로우가 없습니다");

    await user.click(screen.getByTestId("create-workflow"));
    const dialog = await screen.findByRole("dialog", { name: "새 워크플로우" });
    const submit = within(dialog).getByRole("button", { name: "만들기" });
    expect(submit).toBeDisabled();
    await user.type(within(dialog).getByLabelText("이름"), "  ");
    expect(submit).toBeDisabled();
    expect(m.post).not.toHaveBeenCalled();

    await user.type(within(dialog).getByLabelText("이름"), "주간 보고서 초안 ");
    await user.click(submit);
    await waitFor(() => expect(push).toHaveBeenCalledWith("/workflows/new-wf/edit"));
    expect(m.post).toHaveBeenCalledTimes(1);
    expect(m.post).toHaveBeenCalledWith("/api/v1/workflows", { name: "주간 보고서 초안" });
    expect(toast.success).toHaveBeenCalledWith("워크플로우를 만들었습니다", expect.anything());
  });

  it("이름 입력란은 서버 제약(100자)을 넘지 않는다", async () => {
    const user = userEvent.setup();
    renderPage([]);
    await screen.findByText("아직 워크플로우가 없습니다");
    await user.click(screen.getByTestId("create-workflow"));
    const dialog = await screen.findByRole("dialog", { name: "새 워크플로우" });
    expect(within(dialog).getByLabelText("이름")).toHaveAttribute("maxLength", "100");
  });
});

describe("카드 더보기 — 복제·편집·삭제(AC-4.3, Tier 1 비인가 관점)", () => {
  it("can_edit면 「복제 · 편집 · 삭제」, 아니면 「복제」만 — 편집·삭제 메뉴가 없다", async () => {
    const user = userEvent.setup();
    renderPage([wf("mine"), wf("theirs", { can_edit: false })]);
    await cards();

    let menu = await openMenu(user, "워크플로우 mine");
    expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["복제", "편집", "삭제"]);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());

    menu = await openMenu(user, "워크플로우 theirs");
    expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["복제"]);
  });

  it("「삭제」 선택만으로는 DELETE 0회, 취소해도 0회, 확인하면 정확히 1회 → 목록 재조회·성공 토스트", async () => {
    const user = userEvent.setup();
    m.delete.mockImplementation(async () => {
      current = [wf("keep")];
    });
    renderPage([wf("gone", { name: "지울 워크플로우" }), wf("keep")]);
    await cards();

    let menu = await openMenu(user, "지울 워크플로우");
    await user.click(within(menu).getByRole("menuitem", { name: "삭제" }));
    let confirm = await screen.findByRole("alertdialog", { name: "‘지울 워크플로우’ 워크플로우를 삭제할까요?" });
    expect(m.delete).not.toHaveBeenCalled();
    await user.click(within(confirm).getByRole("button", { name: "취소" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(m.delete).not.toHaveBeenCalled();

    menu = await openMenu(user, "지울 워크플로우");
    await user.click(within(menu).getByRole("menuitem", { name: "삭제" }));
    confirm = await screen.findByRole("alertdialog");
    await user.click(within(confirm).getByRole("button", { name: "삭제" }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("워크플로우를 삭제했습니다", expect.anything()));
    expect(m.delete).toHaveBeenCalledTimes(1);
    expect(m.delete).toHaveBeenCalledWith("/api/v1/workflows/gone");
    await waitFor(async () => expect(await cards()).toHaveLength(1));
  });

  it.each([
    [403, "이 작업을 할 권한이 없습니다."],
    [404, "대상을 찾을 수 없습니다. 이미 삭제되었을 수 있습니다."],
  ])("서버가 %i로 거부하면 실패 토스트(DELETE 1회), 카드는 남는다", async (status, description) => {
    const user = userEvent.setup();
    m.delete.mockRejectedValue(new ApiError(status, "denied"));
    renderPage([wf("x", { name: "거부될 워크플로우" })]);
    await cards();
    const menu = await openMenu(user, "거부될 워크플로우");
    await user.click(within(menu).getByRole("menuitem", { name: "삭제" }));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "삭제" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "워크플로우를 삭제하지 못했습니다",
        expect.objectContaining({ description }),
      ),
    );
    expect(m.delete).toHaveBeenCalledTimes(1);
    expect(toast.success).not.toHaveBeenCalled();
    expect(await cards()).toHaveLength(1);
  });

  it("「복제」가 실패하면 실패 토스트, 이동 없음", async () => {
    const user = userEvent.setup();
    m.post.mockRejectedValue(new ApiError(403, "no"));
    renderPage([wf("theirs", { can_edit: false })]);
    await cards();
    const menu = await openMenu(user, "워크플로우 theirs");
    await user.click(within(menu).getByRole("menuitem", { name: "복제" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("워크플로우를 복제하지 못했습니다", expect.anything()),
    );
    expect(push).not.toHaveBeenCalled();
  });

  it("「복제」는 duplicate 1회 뒤 복제본 편집 화면, 「편집」은 편집 화면으로 간다", async () => {
    const user = userEvent.setup();
    m.post.mockResolvedValue(wf("dup"));
    renderPage([wf("mine")]);
    await cards();
    let menu = await openMenu(user, "워크플로우 mine");
    await user.click(within(menu).getByRole("menuitem", { name: "편집" }));
    expect(push).toHaveBeenCalledWith("/workflows/mine/edit");
    menu = await openMenu(user, "워크플로우 mine");
    await user.click(within(menu).getByRole("menuitem", { name: "복제" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/workflows/dup/edit"));
    expect(m.post).toHaveBeenCalledTimes(1);
    expect(m.post).toHaveBeenCalledWith("/api/v1/workflows/mine/duplicate");
    expect(toast.success).toHaveBeenCalledWith("워크플로우를 복제했습니다", expect.anything());
  });
});
