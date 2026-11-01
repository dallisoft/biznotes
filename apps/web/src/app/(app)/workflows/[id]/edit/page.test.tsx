/**
 * 워크플로우 편집 페이지 — ui-ux-redesign P4(plan §6.6.2 워크플로우 편집 P-10, AC-4.5·AC-4.11, HCI-14).
 * 경계(`@/lib/api-client`·라우터·sonner 토스트·`matchMedia`·react-flow 스타일시트)만 대체한다.
 * 페이지·편집기(WorkflowEditor)·훅·ErrorState는 실제 코드다.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import WorkflowEditPage from "@/app/(app)/workflows/[id]/edit/page";
import type { Workflow } from "@/lib/workflow-types";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useParams: () => ({ id: "w1" }), useRouter: () => ({ push }) }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }));
// 정적 자산 경계: react-flow 스타일시트는 Vitest의 PostCSS 변환 대상이 아니다(기존 테스트와 같은 처리).
vi.mock("@xyflow/react/dist/style.css", () => ({}));
vi.mock("@/lib/api-client", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn() },
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

const WF: Workflow = {
  id: "w1",
  name: "요약 워크플로우",
  description: "",
  icon: "⚙️",
  kind: "workflow",
  definition: { nodes: [], edges: [] },
  params_schema: { type: "object", properties: {} },
  scope: "user",
  owner_id: "me",
  enabled: false,
  version: 2,
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
  can_edit: true,
} as Workflow;

/** 노드 2개·엣지 1개 그래프(P4-01 폭 왕복 회귀) — 서버 원래 값과 미저장 편집을 구분할 수 있게 한다. */
const GRAPH: Workflow = {
  ...WF,
  definition: {
    nodes: [
      { id: "rag-1", type: "rag_search", query: "{params.topic}", top_k: 6 },
      { id: "llm-1", type: "llm", system: "", prompt: "{rag-1.context}" },
    ],
    edges: [["rag-1", "llm-1"]],
  },
} as Workflow;
const EDITED_SCHEMA = { type: "object", properties: { topic: { type: "string" } } };

/** 창 폭 — `(min-width: 768px)` 질의의 결과를 정한다. */
let wide = true;

beforeEach(() => {
  wide = true;
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: query === "(min-width: 768px)" ? wide : false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  for (const fn of Object.values(m)) fn.mockReset();
  push.mockReset();
  vi.mocked(toast.success).mockReset();
  vi.mocked(toast.error).mockReset();
});

function renderPage(workflow: Workflow | Error) {
  m.get.mockImplementation(async (path: string) => {
    if (path === "/api/v1/workflows/w1") {
      if (workflow instanceof Error) throw workflow;
      return workflow;
    }
    if (path.startsWith("/api/v1/notebooks")) return []; // 테스트 실행 다이얼로그의 노트북 선택(닫힌 상태)
    throw new Error(`unexpected GET ${path}`);
  });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <WorkflowEditPage />
    </QueryClientProvider>,
  );
}

describe("768px 미만 — 편집기 대신 안내(AC-4.11, HCI-14)", () => {
  it("안내 문구와 「워크플로우 목록」 링크를 보이고 편집기(캔버스·이름 입력)는 그리지 않는다", async () => {
    wide = false;
    const { container } = renderPage(WF);
    const notice = await screen.findByTestId("workflow-edit-narrow-notice");
    expect(notice).toHaveTextContent("워크플로우 편집은 화면이 넓은 기기에서 할 수 있습니다");
    expect(within(notice).getByRole("link", { name: "워크플로우 목록" })).toHaveAttribute("href", "/workflows");
    expect(screen.getByRole("heading", { level: 1, name: "요약 워크플로우" })).toBeInTheDocument();
    expect(within(screen.getByRole("navigation", { name: "위치" })).getByRole("link", { name: "워크플로우" }))
      .toHaveAttribute("href", "/workflows");
    expect(screen.queryByRole("textbox", { name: "워크플로우 이름" })).not.toBeInTheDocument();
    expect(container.querySelector(".react-flow")).toBeNull();
    expect(container.querySelector("[data-slot=page-container]")).toHaveAttribute("data-width", "narrow");
  });

  it("편집 중 폭이 768 경계를 오가도(넓음 → 좁음 → 넓음) 미저장 이름·노드·엣지·파라미터가 남고, 그 사이 PATCH는 0회다(P4-01)", async () => {
    // 실제 matchMedia처럼 change 구독자에게 알리는 대역 — 페이지·편집기·훅·react-flow는 실제 코드다
    const listeners = new Set<() => void>();
    const media = {
      matches: true,
      media: "(min-width: 768px)",
      onchange: null,
      addEventListener: (_: string, f: () => void) => listeners.add(f),
      removeEventListener: (_: string, f: () => void) => listeners.delete(f),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    };
    vi.stubGlobal("matchMedia", vi.fn(() => media));
    const resize = (matches: boolean) =>
      act(() => {
        media.matches = matches;
        listeners.forEach((f) => f());
      });
    const user = userEvent.setup();
    m.patch.mockImplementation(async (_path: string, body: unknown) => ({
      ...GRAPH,
      ...(body as Partial<Workflow>),
      version: 3,
    }));
    const { container } = renderPage(GRAPH);

    // ① 이름
    const name = await screen.findByRole("textbox", { name: "워크플로우 이름" });
    await user.clear(name);
    await user.type(name, "미저장 이름");
    // ② 노드 추가(팔레트 「상수」) ③ 노드·엣지 삭제(llm-1 선택 → 「노드 삭제」 — rag-1 → llm-1 엣지도 함께 빠진다)
    await user.click(screen.getByTestId("palette-constant"));
    fireEvent.click(container.querySelector('.react-flow__node[data-id="llm-1"]')!);
    await user.click(await screen.findByRole("button", { name: /노드 삭제/ }));
    // ④ 파라미터 스키마
    await user.click(screen.getByRole("button", { name: "파라미터" }));
    const schema = await screen.findByRole("textbox");
    fireEvent.change(schema, { target: { value: JSON.stringify(EDITED_SCHEMA) } });
    await user.click(screen.getByRole("button", { name: "확인" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    resize(false);
    expect(await screen.findByTestId("workflow-edit-narrow-notice")).toBeInTheDocument();
    // 좁은 동안 편집기는 숨겨진다 — 접근성 트리·탭 순서에서 빠지지만 언마운트되지 않는다
    expect(screen.queryByRole("textbox", { name: "워크플로우 이름" })).not.toBeInTheDocument();
    expect(screen.getByTestId("workflow-editor-container")).toHaveAttribute("hidden");
    expect(container.querySelector(".react-flow")).not.toBeNull();

    resize(true);
    expect(screen.queryByTestId("workflow-edit-narrow-notice")).not.toBeInTheDocument();
    expect(screen.getByTestId("workflow-editor-container")).not.toHaveAttribute("hidden");
    expect(screen.getByRole("textbox", { name: "워크플로우 이름" })).toHaveValue("미저장 이름");
    expect(m.patch).not.toHaveBeenCalled();

    // 저장하면 왕복 전의 편집이 모두 실린다 — 서버 원래 값(llm-1·엣지·빈 스키마)으로 돌아가지 않았다
    await user.click(within(screen.getByTestId("workflow-editor-actions")).getByRole("button", { name: "저장" }));
    await waitFor(() => expect(m.patch).toHaveBeenCalledTimes(1));
    const [path, body] = m.patch.mock.calls[0] as [string, Partial<Workflow>];
    expect(path).toBe("/api/v1/workflows/w1");
    expect(body.name).toBe("미저장 이름");
    expect(body.definition?.nodes?.map((n) => n.id)).toEqual(["rag-1", "constant-1"]);
    expect(body.definition?.edges).toEqual([]);
    expect(body.params_schema).toEqual(EDITED_SCHEMA);
  });

  /** 실제 matchMedia처럼 change 구독자에게 알리는 대역(위 P4-01 테스트와 같은 방식) */
  function stubResizableMedia() {
    const listeners = new Set<() => void>();
    const media = {
      matches: true,
      media: "(min-width: 768px)",
      onchange: null,
      addEventListener: (_: string, f: () => void) => listeners.add(f),
      removeEventListener: (_: string, f: () => void) => listeners.delete(f),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    };
    vi.stubGlobal("matchMedia", vi.fn(() => media));
    return (matches: boolean) =>
      act(() => {
        media.matches = matches;
        listeners.forEach((f) => f());
      });
  }

  it("편집기 안에 포커스가 있는 채로 좁아지면 포커스가 안내 제목으로 가고, 다시 넓어지면 편집기의 원래 자리로 돌아온다(HCI-P4-6 ①)", async () => {
    const resize = stubResizableMedia();
    renderPage(GRAPH);
    const name = await screen.findByRole("textbox", { name: "워크플로우 이름" });
    act(() => name.focus());

    resize(false);
    const notice = await screen.findByTestId("workflow-edit-narrow-notice");
    const title = within(notice).getByText("워크플로우 편집은 화면이 넓은 기기에서 할 수 있습니다");
    expect(title).toHaveFocus();

    resize(true);
    expect(screen.queryByTestId("workflow-edit-narrow-notice")).not.toBeInTheDocument();
    expect(name).toHaveFocus();
  });

  it("편집기 밖에 있던 포커스는 좁아져도 옮기지 않는다", async () => {
    const resize = stubResizableMedia();
    m.get.mockImplementation(async (path: string) => {
      if (path === "/api/v1/workflows/w1") return GRAPH;
      if (path.startsWith("/api/v1/notebooks")) return [];
      throw new Error(`unexpected GET ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <button type="button">셸 메뉴</button>
        <WorkflowEditPage />
      </QueryClientProvider>,
    );
    await screen.findByRole("textbox", { name: "워크플로우 이름" });
    const outside = screen.getByRole("button", { name: "셸 메뉴" });
    act(() => outside.focus());
    resize(false);
    await screen.findByTestId("workflow-edit-narrow-notice");
    expect(outside).toHaveFocus();
    resize(true);
    expect(outside).toHaveFocus();
  });

  it("권한이 없으면 폭과 무관하게 권한 없음 화면이 먼저다", async () => {
    wide = false;
    renderPage({ ...WF, can_edit: false });
    expect(await screen.findByText("이 워크플로우를 편집할 권한이 없습니다")).toBeInTheDocument();
    expect(screen.queryByTestId("workflow-edit-narrow-notice")).not.toBeInTheDocument();
  });
});

describe("편집기 머리(AC-4.5, P-10)", () => {
  it("이름 입력·뒤로 링크에 접근 가능한 이름이 있고, 상태는 「초안 · v2」·「유효」 StatusBadge", async () => {
    const { container } = renderPage(WF);
    const name = await screen.findByRole("textbox", { name: "워크플로우 이름" });
    expect(name).toHaveValue("요약 워크플로우");
    expect(screen.getByRole("link", { name: "워크플로우 목록으로" })).toHaveAttribute("href", "/workflows");
    expect(screen.getByTestId("workflow-status")).toHaveTextContent("초안 · v2");
    expect(screen.getByTestId("dag-valid")).toHaveTextContent("유효");
    expect(screen.getByTestId("dag-valid")).toHaveAttribute("data-tone", "success");
    expect(container.textContent).not.toMatch(/draft|✓|⛔/);
  });

  it("툴바는 줄바꿈 가능 — 왼쪽(뒤로·이름·상태) 묶음은 최소 폭을 갖고, 버튼 묶음은 별도 형제라 좁으면 다음 줄로 내려간다", async () => {
    renderPage(WF);
    const identity = await screen.findByTestId("workflow-editor-identity");
    const actions = screen.getByTestId("workflow-editor-actions");
    const header = identity.parentElement!;
    expect(header.tagName).toBe("HEADER");
    expect(header).toHaveClass("flex-wrap");
    expect(actions.parentElement).toBe(header);
    expect(identity).toHaveClass("min-w-64", "flex-1");
    expect(within(identity).getByRole("textbox", { name: "워크플로우 이름" })).toHaveClass("min-w-24", "flex-1");
    expect(within(identity).getByTestId("workflow-status")).toBeInTheDocument();
    expect(within(identity).getByRole("link", { name: "워크플로우 목록으로" })).toBeInTheDocument();
  });

  it.each([[false], [true]])(
    "터치 기기(pointer: coarse)에서 툴바의 뒤로 링크·이름 입력·버튼은 모두 44px 타깃이다(게시됨=%s, P4-03)",
    async (enabled) => {
      renderPage({ ...WF, enabled });
      const identity = await screen.findByTestId("workflow-editor-identity");
      expect(within(identity).getByRole("link", { name: "워크플로우 목록으로" })).toHaveClass("coarse:size-11");
      expect(within(identity).getByRole("textbox", { name: "워크플로우 이름" })).toHaveClass("coarse:h-11");
      const buttons = within(screen.getByTestId("workflow-editor-actions")).getAllByRole("button");
      expect(buttons).toHaveLength(4);
      for (const b of buttons) expect(b).toHaveClass("coarse:h-11", "coarse:min-w-11");
    },
  );

  it("미게시면 「게시」가 주 버튼(맨 오른쪽)·「저장」은 outline — 채움 버튼은 하나", async () => {
    renderPage(WF);
    const actions = await screen.findByTestId("workflow-editor-actions");
    const buttons = within(actions).getAllByRole("button");
    expect(buttons.map((b) => b.textContent?.trim())).toEqual(["파라미터", "테스트 실행", "저장", "게시"]);
    expect(buttons.filter((b) => b.classList.contains("bg-primary")).map((b) => b.textContent?.trim())).toEqual([
      "게시",
    ]);
  });

  it("게시됨이면 「저장」이 주 버튼·「게시 취소」는 outline", async () => {
    renderPage({ ...WF, enabled: true });
    const actions = await screen.findByTestId("workflow-editor-actions");
    const buttons = within(actions).getAllByRole("button");
    expect(buttons.map((b) => b.textContent?.trim())).toEqual(["파라미터", "테스트 실행", "게시 취소", "저장"]);
    expect(buttons.filter((b) => b.classList.contains("bg-primary")).map((b) => b.textContent?.trim())).toEqual([
      "저장",
    ]);
    expect(screen.getByTestId("workflow-status")).toHaveTextContent("게시됨 · v2");
  });

  it("「게시」는 저장(PATCH) 뒤 publish 1회, 진행 중에는 저장·게시 버튼이 막힌다", async () => {
    const user = userEvent.setup();
    let finishPatch: (v: unknown) => void = () => {};
    m.patch.mockImplementation(() => new Promise((resolve) => (finishPatch = resolve)));
    m.post.mockResolvedValue({ ...WF, enabled: true });
    renderPage(WF);
    const actions = await screen.findByTestId("workflow-editor-actions");
    await user.click(within(actions).getByRole("button", { name: "게시" }));
    await waitFor(() => expect(within(actions).getByRole("button", { name: "게시" })).toBeDisabled());
    expect(within(actions).getByRole("button", { name: "저장" })).toBeDisabled();
    finishPatch({ ...WF, version: 3 });
    await waitFor(() => expect(m.post).toHaveBeenCalledWith("/api/v1/workflows/w1/publish"));
    expect(m.post).toHaveBeenCalledTimes(1);
    expect(m.patch).toHaveBeenCalledWith(
      "/api/v1/workflows/w1",
      expect.objectContaining({ name: "요약 워크플로우" }),
    );
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("워크플로우를 게시했습니다", expect.anything()));
  });

  it("검증 오류가 있으면 저장·게시 요청 없이 검증 문구를 실패 토스트 설명으로 보이고, 머리에 오류 문구(아이콘+텍스트)", async () => {
    const user = userEvent.setup();
    renderPage({ ...WF, definition: { nodes: [], edges: [["a", "b"]] } });
    const actions = await screen.findByTestId("workflow-editor-actions");
    const message = "엣지가 존재하지 않는 노드를 참조합니다: a → b";
    expect(screen.getByTestId("dag-error")).toHaveTextContent(message);
    expect(screen.getByTestId("dag-error")).toHaveAttribute("title", message);
    await user.click(within(actions).getByRole("button", { name: "저장" }));
    expect(toast.error).toHaveBeenCalledWith("워크플로우를 저장하지 못했습니다", { description: message });
    await user.click(within(actions).getByRole("button", { name: "게시" }));
    expect(toast.error).toHaveBeenCalledWith("워크플로우를 게시하지 못했습니다", { description: message });
    expect(m.patch).not.toHaveBeenCalled();
    expect(m.post).not.toHaveBeenCalled();
  });

  it("연결되지 않은 노드는 경고 문구(아이콘+텍스트)로 보인다", async () => {
    const llm = (id: string) => ({ id, type: "llm", system: "", prompt: "안녕" });
    renderPage({ ...WF, definition: { nodes: [llm("a"), llm("b")], edges: [] } });
    expect(await screen.findByTestId("dag-warning")).toHaveTextContent("연결되지 않은 노드: a, b (독립 실행됨)");
    expect(screen.queryByTestId("dag-valid")).not.toBeInTheDocument();
  });

  it("게시됨에서 「게시 취소」는 저장 뒤 unpublish 1회, 성공 토스트 「워크플로우 게시를 취소했습니다」", async () => {
    const user = userEvent.setup();
    m.patch.mockResolvedValue({ ...WF, enabled: true });
    m.post.mockResolvedValue({ ...WF, enabled: false });
    renderPage({ ...WF, enabled: true });
    const actions = await screen.findByTestId("workflow-editor-actions");
    await user.click(within(actions).getByRole("button", { name: "게시 취소" }));
    await waitFor(() => expect(m.post).toHaveBeenCalledWith("/api/v1/workflows/w1/unpublish"));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("워크플로우 게시를 취소했습니다", expect.anything()),
    );
  });

  it("저장 성공은 「워크플로우를 저장했습니다」 + 새 버전 설명", async () => {
    const user = userEvent.setup();
    m.patch.mockResolvedValue({ ...WF, version: 7 });
    renderPage(WF);
    const actions = await screen.findByTestId("workflow-editor-actions");
    await user.click(within(actions).getByRole("button", { name: "저장" }));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        "워크플로우를 저장했습니다",
        expect.objectContaining({ description: "v7" }),
      ),
    );
  });

  it("저장 실패는 「워크플로우를 저장하지 못했습니다」 실패 토스트", async () => {
    const user = userEvent.setup();
    m.patch.mockRejectedValue(new ApiError(403, "no"));
    renderPage(WF);
    const actions = await screen.findByTestId("workflow-editor-actions");
    await user.click(within(actions).getByRole("button", { name: "저장" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "워크플로우를 저장하지 못했습니다",
        expect.objectContaining({ description: "이 작업을 할 권한이 없습니다." }),
      ),
    );
  });
});

describe("권한 없음·오류 화면(§8.3 판정 무변경 — ErrorState)", () => {
  it("권한 없음: 「복제해서 내 워크플로우로 편집」(duplicate 1회 → 복제본 편집)과 「워크플로우 목록으로」, 편집기 없음", async () => {
    const user = userEvent.setup();
    m.post.mockResolvedValue({ ...WF, id: "copy-2" });
    renderPage({ ...WF, can_edit: false });
    const title = await screen.findByText("이 워크플로우를 편집할 권한이 없습니다");
    const box = title.closest<HTMLElement>("[data-slot=error-state]")!;
    expect(box).toHaveAttribute("data-variant", "forbidden");
    expect(within(box).getByRole("link", { name: "워크플로우 목록으로" })).toHaveAttribute("href", "/workflows");
    expect(screen.queryByRole("textbox", { name: "워크플로우 이름" })).not.toBeInTheDocument();
    expect(m.patch).not.toHaveBeenCalled();
    await user.click(within(box).getByRole("button", { name: "복제해서 내 워크플로우로 편집" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/workflows/copy-2/edit"));
    expect(m.post).toHaveBeenCalledTimes(1);
    expect(m.post).toHaveBeenCalledWith("/api/v1/workflows/w1/duplicate");
  });

  it("없는 워크플로우(404)는 「워크플로우를 찾을 수 없습니다」 + 목록 링크, 다시 시도 없음", async () => {
    renderPage(new ApiError(404, "not found"));
    expect(await screen.findByText("워크플로우를 찾을 수 없습니다")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "워크플로우 목록으로" })).toHaveAttribute("href", "/workflows");
    expect(screen.queryByRole("button", { name: "다시 시도" })).not.toBeInTheDocument();
  });

  it("그 밖의 오류는 「워크플로우를 불러오지 못했습니다」 + 「다시 시도」", async () => {
    const user = userEvent.setup();
    renderPage(new ApiError(500, "boom"));
    expect(await screen.findByText("워크플로우를 불러오지 못했습니다")).toBeInTheDocument();
    const before = m.get.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    await waitFor(() => expect(m.get.mock.calls.length).toBeGreaterThan(before));
  });
});

describe("「다시 시도」 포커스 착지점(P2 회고 HCI-P2-2)", () => {
  it("오류 → 스켈레톤 → 편집기로 바뀌는 동안 포커스는 셸 본문(main)에, 결과가 오면 뒤로 링크 「워크플로우 목록으로」에 있다", async () => {
    const user = userEvent.setup();
    let wfReply: () => Promise<unknown> = () => Promise.reject(new ApiError(500, "boom"));
    m.get.mockImplementation((path: string) => {
      if (path === "/api/v1/workflows/w1") return wfReply();
      if (path.startsWith("/api/v1/notebooks")) return Promise.resolve([]);
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <main id="main-content" tabIndex={-1}>
          <WorkflowEditPage />
        </main>
      </QueryClientProvider>,
    );
    await screen.findByText("워크플로우를 불러오지 못했습니다");
    let release: (value: unknown) => void = () => {};
    wfReply = () => new Promise((resolve) => (release = resolve));
    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    await waitFor(() =>
      expect(screen.queryByText("워크플로우를 불러오지 못했습니다")).not.toBeInTheDocument(),
    );
    expect(document.activeElement).toBe(screen.getByRole("main"));
    release(WF);
    const back = await screen.findByRole("link", { name: "워크플로우 목록으로" });
    expect(document.activeElement).toBe(back);
  });
});
