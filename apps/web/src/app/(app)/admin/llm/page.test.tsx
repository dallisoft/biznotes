import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { stubNarrowViewport } from "@/hooks/use-narrow-viewport.test-kit";
import type { AdminModel, AdminProvider, ProviderCircuit } from "@/lib/admin-types";
import AdminLLMPage from "./page";

const { api, toast } = vi.hoisted(() => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
  toast: { success: vi.fn(), error: vi.fn() },
}));

// 네트워크 경계(`api`)만 대체한다 — 훅·RowActions·ConfirmDialog·SegmentedControl은 실경로(§3.3-3 R6).
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("sonner", () => ({ toast }));

const PROVIDERS_PATH = "/api/v1/admin/llm/providers";
const STATUS_PATH = "/api/v1/admin/llm/providers/status";
const MODELS_PATH = "/api/v1/admin/llm/models";

function provider(overrides: Partial<AdminProvider> = {}): AdminProvider {
  return {
    id: "p-gemini",
    name: "gemini",
    type: "google",
    base_url: null,
    has_api_key: true,
    enabled: true,
    model_count: 4,
    embedding_max_concurrency: null,
    ...overrides,
  } as AdminProvider;
}

function model(overrides: Partial<AdminModel> = {}): AdminModel {
  return {
    id: "m-flash",
    provider_id: "p-gemini",
    provider_name: "gemini",
    model_name: "gemini/gemini-3.8-flash",
    display_name: "Gemini Flash",
    capabilities: ["llm"],
    is_default: false,
    enabled: true,
    context_window: null,
    fallback_priority: null,
    cost_per_1k: {},
    ...overrides,
  } as AdminModel;
}

const EMBEDDING = model({
  id: "m-emb",
  model_name: "gemini/gemini-embedding-2",
  display_name: "임베딩 A",
  capabilities: ["embedding"],
  is_default: true,
});
const IMAGE = model({
  id: "m-image",
  model_name: "gemini/gemini-3.8-flash-image",
  display_name: "이미지 B",
  capabilities: ["image"],
});

const server = {
  providers: [] as AdminProvider[],
  circuits: (): Promise<ProviderCircuit[]> => Promise.resolve([]),
  models: [] as AdminModel[],
  providersError: null as Error | null,
};

function route(path: string): Promise<unknown> {
  if (path === PROVIDERS_PATH) {
    return server.providersError ? Promise.reject(server.providersError) : Promise.resolve(server.providers);
  }
  if (path === STATUS_PATH) return server.circuits();
  if (path === MODELS_PATH) return Promise.resolve(server.models);
  return Promise.resolve([]);
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AdminLLMPage />
    </QueryClientProvider>,
  );
}

/** 이름으로 행을 찾는다 — 프로바이더 이름은 모델 표의 「프로바이더」 열에도 나오므로 섹션 안에서만 찾는다. */
async function rowIn(section: "프로바이더" | "모델", name: string) {
  const region = await screen.findByRole("region", { name: section });
  return (await within(region).findByText(name)).closest("tr") as HTMLElement;
}
const rowOf = (name: string) => rowIn("프로바이더", name);
const modelRowOf = (name: string) => rowIn("모델", name);

async function openGuide(user: ReturnType<typeof userEvent.setup>) {
  const guide = await screen.findByTestId("embedding-swap-guide");
  await user.click(within(guide).getByText("임베딩 모델 교체 순서"));
  expect(guide).toHaveAttribute("open");
  return guide;
}

beforeEach(() => {
  vi.clearAllMocks();
  server.providers = [];
  server.circuits = () => Promise.resolve([]);
  server.models = [EMBEDDING];
  server.providersError = null;
  api.get.mockImplementation(route);
  api.post.mockResolvedValue(undefined);
  api.patch.mockImplementation((_path: string, body: unknown) => Promise.resolve(body));
  api.delete.mockResolvedValue(undefined);
});

describe("AdminLLMPage 헤더·구조 (AC-5.1)", () => {
  it("브레드크럼 「관리 / LLM 게이트웨이」, h1 하나, 섹션 제목은 h2 text-lg, 헤더 주 동작은 「프로바이더 추가」 하나", async () => {
    renderPage();
    const nav = screen.getByRole("navigation", { name: "위치" });
    expect(within(nav).getByRole("link", { name: "관리" })).toHaveAttribute("href", "/admin");
    expect(within(nav).getByText("LLM 게이트웨이")).toHaveAttribute("aria-current", "page");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("LLM 게이트웨이");
    for (const name of ["프로바이더", "모델"]) {
      expect(screen.getByRole("heading", { level: 2, name })).toHaveClass("text-lg");
    }
    const actions = document.querySelector("[data-slot=page-header-actions]") as HTMLElement;
    expect(within(actions).getAllByRole("button").map((b) => b.textContent)).toEqual(["프로바이더 추가"]);
    // 「+ 텍스트」 금지(§6.3.3) — 모델 추가는 섹션의 보조(outline) 동작
    expect(screen.queryByRole("button", { name: /^\+/ })).toBeNull();
    expect(screen.getByRole("button", { name: "모델 추가" })).toBeInTheDocument();
  });

  it("모델 머리글은 한국어이고 Modality·Default 원시 영문이 없다 (AC-5.4)", async () => {
    renderPage();
    await screen.findByText("임베딩 A");
    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual(["표시명", "프로바이더", "능력", "기본", "폴백 순서", "상태", "동작"]);
    expect(document.body.textContent).not.toMatch(/Modality|Default/);
  });

  it("프로바이더 목록이 비면 EmptyState, 실패하면 ErrorState(다시 시도)", async () => {
    const user = userEvent.setup();
    const { unmount } = renderPage();
    expect(await screen.findByText("아직 프로바이더가 없습니다")).toBeInTheDocument();
    unmount();

    server.providersError = new Error("boom");
    renderPage();
    expect(await screen.findByText("프로바이더 목록을 불러오지 못했습니다")).toBeInTheDocument();
    server.providersError = null;
    server.providers = [provider()];
    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    expect(await rowOf("gemini")).toBeInTheDocument();
    // 성공하면 포커스는 다시 그려진 프로바이더 표 안(첫 행 「수정」)으로 간다(HCI-P2-2)
    const recovered = await rowOf("gemini");
    await waitFor(() => expect(within(recovered).getByRole("button", { name: "수정" })).toHaveFocus());
  });
});

describe("AdminLLMPage 프로바이더 행 동작 (AC-5.4)", () => {
  it("행에는 「수정」과 더보기만 보이고, 더보기는 연결 테스트·모델 가져오기·모델 동기화 확인 / 삭제 순서다", async () => {
    server.providers = [provider()];
    const user = userEvent.setup();
    renderPage();
    const row = await rowOf("gemini");
    expect(within(row).getAllByRole("button").map((b) => b.getAttribute("aria-label") ?? b.textContent)).toEqual([
      "수정",
      "gemini 동작 더보기",
    ]);
    await user.click(within(row).getByRole("button", { name: "gemini 동작 더보기" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual([
      "연결 테스트",
      "모델 가져오기",
      "모델 동기화 확인",
      "삭제",
    ]);
    expect(within(menu).getByRole("separator")).toBeInTheDocument();
  });

  it("mock 프로바이더는 모델 가져오기·동기화 확인이 없다", async () => {
    server.providers = [provider({ id: "p-mock", name: "mock-local", type: "mock", model_count: 3 })];
    const user = userEvent.setup();
    renderPage();
    const row = await rowOf("mock-local");
    expect(within(row).getByText("mock")).toHaveClass("font-mono"); // 유형은 식별자 — 번역하지 않고 모노(§6.3.2)
    await user.click(within(row).getByRole("button", { name: "mock-local 동작 더보기" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["연결 테스트", "삭제"]);
  });

  it("삭제는 확인 전 요청 0회, 확인 후 1회 — 연결된 모델 수를 알린다", async () => {
    server.providers = [provider()];
    const user = userEvent.setup();
    renderPage();
    const row = await rowOf("gemini");
    await user.click(within(row).getByRole("button", { name: "gemini 동작 더보기" }));
    await user.click(await screen.findByRole("menuitem", { name: "삭제" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("‘gemini’ 프로바이더를 삭제할까요?");
    expect(dialog).toHaveTextContent("연결된 모델 4개도 함께 삭제됩니다");
    expect(api.delete).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "삭제" }));
    await waitFor(() => expect(api.delete).toHaveBeenCalledTimes(1));
    expect(api.delete).toHaveBeenCalledWith(`${PROVIDERS_PATH}/p-gemini`);
  });

  it("「수정」은 현재 값으로 채운 다이얼로그를 열고, 닫으면 「수정」으로 포커스가 돌아온다", async () => {
    server.providers = [provider({ embedding_max_concurrency: 4 })];
    const user = userEvent.setup();
    renderPage();
    const row = await rowOf("gemini");
    const edit = within(row).getByRole("button", { name: "수정" });
    await user.click(edit);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText(/임베딩 동시 요청 상한/)).toHaveValue("4");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(edit).toHaveFocus());
  });

  it("더보기로 연 「모델 동기화 확인」은 닫으면 그 행의 ⋯로 포커스가 돌아온다", async () => {
    server.providers = [provider()];
    api.get.mockImplementation((path: string) =>
      path === `${PROVIDERS_PATH}/p-gemini/models/sync-check`
        ? Promise.resolve([{ id: "m-1", model_name: "x", display_name: "모델 X", still_available: false }])
        : route(path),
    );
    const user = userEvent.setup();
    renderPage();
    const row = await rowOf("gemini");
    const more = within(row).getByRole("button", { name: "gemini 동작 더보기" });
    await user.click(more);
    await user.click(await screen.findByRole("menuitem", { name: "모델 동기화 확인" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("프로바이더에서 사라짐")).toHaveAttribute("data-tone", "danger");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(more).toHaveFocus());
  });

  it("연결 테스트는 메뉴 선택만으로 실행되고 결과를 토스트로 알린다", async () => {
    server.providers = [provider()];
    api.post.mockResolvedValue({ ok: true, detail: "응답 수신" });
    const user = userEvent.setup();
    renderPage();
    const row = await rowOf("gemini");
    await user.click(within(row).getByRole("button", { name: "gemini 동작 더보기" }));
    await user.click(await screen.findByRole("menuitem", { name: "연결 테스트" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith(`${PROVIDERS_PATH}/p-gemini/test`));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        "연결 테스트에 성공했습니다",
        expect.objectContaining({ description: "응답 수신" }),
      ),
    );
  });
});

describe("더보기 메뉴로 여는 모달의 포커스 (P5 리뷰 P5-02, plan §6.5·§6.8-2)", () => {
  const GEMINI_PATH = `${PROVIDERS_PATH}/p-gemini`;
  const DIALOGS = [
    {
      item: "모델 가져오기",
      path: `${GEMINI_PATH}/available-models`,
      body: [
        {
          model_name: "gemini/gemini-new",
          raw_id: "models/gemini-new",
          display_name: "Gemini New",
          capabilities: ["llm"],
          already_registered: false,
        },
      ],
      loaded: "gemini/gemini-new",
    },
    {
      item: "모델 동기화 확인",
      path: `${GEMINI_PATH}/models/sync-check`,
      body: [{ id: "m-1", model_name: "x", display_name: "모델 X", still_available: false }],
      loaded: "프로바이더에서 사라짐",
    },
  ];

  const insideDialog = (dialog: HTMLElement) => dialog.contains(document.activeElement);

  it.each(DIALOGS)(
    "「$item」: 키보드로 열면 포커스가 모달 안(열림 직후·비동기 조회 뒤)이고 Tab·Shift+Tab은 안에서 순환하며, 닫으면 그 행 ⋯로 돌아간다",
    async ({ item, path, body, loaded }) => {
      server.providers = [provider()];
      let resolveLoad: (value: unknown) => void = () => {};
      api.get.mockImplementation((p: string) =>
        p === path ? new Promise((resolve) => (resolveLoad = resolve)) : route(p),
      );
      const user = userEvent.setup();
      renderPage();
      const row = await rowOf("gemini");
      const more = within(row).getByRole("button", { name: "gemini 동작 더보기" });
      act(() => more.focus());
      await user.keyboard("{Enter}");
      const menu = await screen.findByRole("menu");
      const target = within(menu).getByRole("menuitem", { name: item });
      // 메뉴의 첫 포커스는 다음 프레임이다 — 항목에 포커스가 생긴 뒤 이동한다(부하에서 목표를 지나치던 경합, P6)
      await waitFor(() => expect(document.activeElement?.getAttribute("role")).toBe("menuitem"));
      for (let i = 0; i < 6 && document.activeElement !== target; i += 1) await user.keyboard("{ArrowDown}");
      expect(target).toHaveFocus();
      await user.keyboard("{Enter}");

      const dialog = await screen.findByRole("dialog");
      // 열림 직후 — 닫히는 메뉴의 늦은 포커스 복귀가 끝날 시간을 준 뒤에도 모달 안(리뷰 재현: 100ms 뒤 ⋯였다)
      await waitFor(() => expect(insideDialog(dialog)).toBe(true));
      await act(() => new Promise((r) => setTimeout(r, 100)));
      expect(insideDialog(dialog), document.activeElement?.outerHTML).toBe(true);

      // 비동기 조회가 끝나 내용이 바뀐 뒤에도 모달 안
      await act(async () => resolveLoad(body));
      expect(await within(dialog).findByText(loaded)).toBeInTheDocument();
      await act(() => new Promise((r) => setTimeout(r, 50)));
      expect(insideDialog(dialog), document.activeElement?.outerHTML).toBe(true);

      // Tab·Shift+Tab은 모달 안에서만 돈다(§6.5 ⑤). 끝에서 넘어가면 Base UI 포커스 가드가 반대쪽 끝으로
      // 되돌린다(다음 프레임) — 가드 말고 모달 밖의 실제 요소에 닿으면 실패다.
      const cycle = async (shift: boolean, label: string) => {
        const visited = new Set<Element>();
        for (let i = 0; i < 8; i += 1) {
          await user.tab({ shift });
          const active = document.activeElement as HTMLElement | null;
          expect(
            insideDialog(dialog) || active?.hasAttribute("data-base-ui-focus-guard"),
            `${label} ${i + 1}: ${active?.outerHTML}`,
          ).toBe(true);
          await waitFor(() => expect(insideDialog(dialog)).toBe(true));
          visited.add(document.activeElement!);
        }
        return visited;
      };
      const forward = await cycle(false, "Tab");
      const backward = await cycle(true, "Shift+Tab");
      expect(forward.size).toBeGreaterThan(0);
      expect(backward.size).toBeGreaterThan(0);

      // 닫으면 그 행의 ⋯로(메뉴 항목은 이미 사라졌다 — §6.5 RowActions 포커스)
      await user.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
      await waitFor(() => expect(more).toHaveFocus());
    },
  );

  it("모달을 열지 않는 일반 항목(「연결 테스트」)은 메뉴가 닫히면 그 행 ⋯로 포커스가 돌아온다", async () => {
    server.providers = [provider()];
    api.post.mockResolvedValue({ ok: true, detail: "응답 수신" });
    const user = userEvent.setup();
    renderPage();
    const row = await rowOf("gemini");
    const more = within(row).getByRole("button", { name: "gemini 동작 더보기" });
    act(() => more.focus());
    await user.keyboard("{Enter}");
    const menu = await screen.findByRole("menu");
    const target = within(menu).getByRole("menuitem", { name: "연결 테스트" });
    await waitFor(() => expect(document.activeElement?.getAttribute("role")).toBe("menuitem"));
    for (let i = 0; i < 6 && document.activeElement !== target; i += 1) await user.keyboard("{ArrowDown}");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(api.post).toHaveBeenCalledWith(`${GEMINI_PATH}/test`));
    await waitFor(() => expect(more).toHaveFocus());
  });
});

describe("모델 가져오기 능력 필터 (#206, 코어 v0.23.2 #347)", () => {
  it("이미지 생성 모델은 「이미지 생성 N」 태그·배지로 보이고, 태그를 누르면 그 모델만 남는다", async () => {
    server.providers = [provider()];
    const discovered = [
      { model_name: "gemini/gemini-2.5-flash", capabilities: ["llm", "vlm"] },
      { model_name: "gemini/gemini-2.5-flash-image", capabilities: ["image"] },
      { model_name: "gemini/imagen-4.0-generate-001", capabilities: ["image"] },
      { model_name: "gemini/gemini-embedding-001", capabilities: ["embedding"] },
    ].map((m) => ({ ...m, raw_id: m.model_name, display_name: m.model_name, already_registered: false }));
    api.get.mockImplementation((p: string) =>
      p === `${PROVIDERS_PATH}/p-gemini/available-models` ? Promise.resolve(discovered) : route(p),
    );
    const user = userEvent.setup();
    renderPage();
    const row = await rowOf("gemini");
    await user.click(within(row).getByRole("button", { name: "gemini 동작 더보기" }));
    await user.click(within(await screen.findByRole("menu")).getByRole("menuitem", { name: "모델 가져오기" }));
    const dialog = await screen.findByRole("dialog");
    const chips = await within(dialog).findByRole("group", { name: "능력 필터" });
    expect(within(chips).getAllByRole("button").map((b) => b.textContent)).toEqual([
      "전체 4",
      "텍스트 생성(LLM) 1",
      "이미지 이해(VLM) 1",
      "임베딩 1",
      "이미지 생성 2",
    ]);

    await user.click(within(chips).getByRole("button", { name: "이미지 생성 2" }));
    expect(within(dialog).queryByText("gemini/gemini-2.5-flash")).toBeNull();
    expect(within(dialog).queryByText("gemini/gemini-embedding-001")).toBeNull();
    const imageRow = within(dialog)
      .getAllByText("gemini/gemini-2.5-flash-image")[0]
      .closest("div.flex") as HTMLElement;
    expect(within(imageRow).getByText("이미지 생성")).toBeInTheDocument();
    expect(within(dialog).getAllByText("gemini/imagen-4.0-generate-001").length).toBeGreaterThan(0);
  });
});

describe("좁은 화면 표 규칙 (plan §6.7, P5 리뷰 P5-04)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("프로바이더·모델 표는 좁은 화면 규칙 래퍼(첫 열 고정·오른쪽 단서) 안에 있다", async () => {
    server.providers = [provider()];
    renderPage();
    await rowOf("gemini");
    await modelRowOf("임베딩 A");
    const tables = screen.getAllByRole("table");
    expect(tables).toHaveLength(2);
    for (const t of tables) expect(t.closest('[data-slot="admin-table-frame"]')).not.toBeNull();
  });

  it("< 768: 행마다 ⋯ 하나 — 「수정」은 메뉴 첫 항목이고, 연 다이얼로그를 닫으면 그 행 ⋯로 돌아간다(프로바이더·모델)", async () => {
    stubNarrowViewport();
    server.providers = [provider({ embedding_max_concurrency: 4 })];
    const user = userEvent.setup();
    renderPage();
    const row = await rowOf("gemini");
    await waitFor(() => expect(screen.queryByRole("button", { name: "수정" })).toBeNull());
    // 동작 칸에는 ⋯ 하나뿐(상한 게이트 상태 버튼은 「상태」 칸)
    const actionsCell = row.lastElementChild as HTMLElement;
    expect(within(actionsCell).getAllByRole("button").map((b) => b.getAttribute("aria-label"))).toEqual([
      "gemini 동작 더보기",
    ]);
    const more = within(row).getByRole("button", { name: "gemini 동작 더보기" });
    act(() => more.focus());
    await user.keyboard("{Enter}");
    const menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual([
      "수정",
      "연결 테스트",
      "모델 가져오기",
      "모델 동기화 확인",
      "삭제",
    ]);
    await user.keyboard("{Enter}"); // 첫 항목 「수정」
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText(/임베딩 동시 요청 상한/)).toHaveValue("4");
    await act(() => new Promise((r) => setTimeout(r, 50)));
    expect(dialog.contains(document.activeElement), document.activeElement?.outerHTML).toBe(true);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(more).toHaveFocus());

    const modelRow = await modelRowOf("임베딩 A");
    const modelMore = within(modelRow).getByRole("button", { name: "임베딩 A 동작 더보기" });
    await user.click(modelMore);
    const modelMenu = await screen.findByRole("menu");
    expect(within(modelMenu).getAllByRole("menuitem")[0]).toHaveTextContent("수정");
    await user.click(within(modelMenu).getByRole("menuitem", { name: "수정" }));
    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(modelMore).toHaveFocus());
  });
});

describe("AdminLLMPage 프로바이더 상태 (StatusBadge)", () => {
  it("차단 n초(위험) + 「차단 해제」, 실패 n회(경고), 정상(성공)을 말로 보인다", async () => {
    server.providers = [
      provider({ id: "p-a", name: "alpha" }),
      provider({ id: "p-b", name: "beta" }),
      provider({ id: "p-c", name: "gamma" }),
    ];
    server.circuits = () =>
      Promise.resolve([
        { id: "p-a", name: "alpha", open: true, open_remaining_seconds: 30, recent_failures: 5 },
        { id: "p-b", name: "beta", open: false, open_remaining_seconds: 0, recent_failures: 2 },
      ] as ProviderCircuit[]);
    const user = userEvent.setup();
    renderPage();
    const alpha = await rowOf("alpha");
    expect(await within(alpha).findByText("차단 30초")).toHaveAttribute("data-tone", "danger");
    expect(within(await rowOf("beta")).getByText("실패 2회")).toHaveAttribute("data-tone", "warning");
    expect(within(await rowOf("gamma")).getByText("정상")).toHaveAttribute("data-tone", "success");

    await user.click(within(alpha).getByRole("button", { name: "alpha 차단 해제" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith(`${PROVIDERS_PATH}/p-a/circuit-reset`));
  });

  it("차단 상태를 받지 못하면 「정상」으로 단정하지 않고 「미확인」을 보인다", async () => {
    server.providers = [provider()];
    server.circuits = () => Promise.reject(new Error("status down"));
    renderPage();
    const row = await rowOf("gemini");
    expect(await within(row).findByText("미확인")).toHaveAttribute("data-tone", "neutral");
    expect(within(row).queryByText("정상")).toBeNull();
  });
});

describe("AdminLLMPage 프로바이더 임베딩 동시 요청 상한", () => {
  it("상한이 있는 행에만 배지와 게이트 상태 버튼을 보인다", async () => {
    server.providers = [
      provider({ id: "p-limited", name: "limited", embedding_max_concurrency: 2, model_count: 0 }),
      provider({ id: "p-free", name: "free", embedding_max_concurrency: null, model_count: 0 }),
    ];
    renderPage();

    const limited = await rowOf("limited");
    const free = await rowOf("free");
    expect(within(limited).getByText("임베딩 상한 2")).toBeInTheDocument();
    expect(within(limited).getByRole("button", { name: "게이트 상태" })).toBeInTheDocument();
    expect(within(free).queryByText(/임베딩 상한/)).not.toBeInTheDocument();
    expect(within(free).queryByRole("button", { name: "게이트 상태" })).not.toBeInTheDocument();
    expect(api.get).not.toHaveBeenCalledWith(expect.stringContaining("/embedding-gate"));
  });
});

describe("AdminLLMPage 모델 행 (AC-5.3·5.4)", () => {
  it("표시명 아래 모노 모델명, 능력은 한국어 라벨, 기본은 「기본」 배지만(지정은 더보기)", async () => {
    server.models = [EMBEDDING, model()];
    const user = userEvent.setup();
    renderPage();
    const emb = await modelRowOf("임베딩 A");
    expect(within(emb).getByText("gemini/gemini-embedding-2")).toHaveClass("font-mono");
    expect(within(emb).getByText("임베딩")).toBeInTheDocument();
    expect(within(emb).getByText("기본")).toHaveAttribute("data-tone", "info");
    const flash = await modelRowOf("Gemini Flash");
    expect(within(flash).getByText("텍스트 생성(LLM)")).toBeInTheDocument();
    expect(within(flash).queryByRole("button", { name: /기본/ })).toBeNull();

    await user.click(within(flash).getByRole("button", { name: "Gemini Flash 동작 더보기" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["기본 모델로 지정", "삭제"]);
    await user.click(within(menu).getByRole("menuitem", { name: "기본 모델로 지정" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith(`${MODELS_PATH}/m-flash/set-default`));
  });

  it("행 스위치는 「{표시명} 활성」 이름을 가진다", async () => {
    server.models = [EMBEDDING, model(), IMAGE];
    renderPage();
    await screen.findByText("임베딩 A");
    for (const name of ["임베딩 A 활성", "Gemini Flash 활성", "이미지 B 활성"]) {
      expect(screen.getByRole("switch", { name })).toBeInTheDocument();
    }
  });

  it("활성 임베딩 모델을 끄면 확인을 받는다 — 취소하면 요청 0회·상태 그대로, 스위치로 포커스 복귀", async () => {
    const user = userEvent.setup();
    renderPage();
    const toggle = await screen.findByRole("switch", { name: "임베딩 A 활성" });
    expect(toggle).toBeChecked();
    await user.click(toggle);
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("‘임베딩 A’ 임베딩 모델을 끌까요?");
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "취소" })).toHaveFocus());
    await user.click(within(dialog).getByRole("button", { name: "취소" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(api.patch).not.toHaveBeenCalled();
    expect(toggle).toBeChecked();
    await waitFor(() => expect(toggle).toHaveFocus());
  });

  it("확인하면 비활성화 요청이 1회 나간다", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("switch", { name: "임베딩 A 활성" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "비활성화" }));
    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    expect(api.patch).toHaveBeenCalledWith(`${MODELS_PATH}/m-emb`, { enabled: false });
  });

  it("임베딩이 아닌 모델 끄기·임베딩 모델 켜기는 확인 없이 바로 반영한다", async () => {
    server.models = [model(), { ...EMBEDDING, enabled: false }];
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("switch", { name: "Gemini Flash 활성" }));
    await user.click(screen.getByRole("switch", { name: "임베딩 A 활성" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(2));
    expect(api.patch).toHaveBeenCalledWith(`${MODELS_PATH}/m-flash`, { enabled: false });
    expect(api.patch).toHaveBeenCalledWith(`${MODELS_PATH}/m-emb`, { enabled: true });
  });

  it("폴백 순서 입력은 이름을 가지고, 바꾼 뒤 포커스를 옮기면 저장하고 토스트로 알린다", async () => {
    server.models = [model()];
    const user = userEvent.setup();
    renderPage();
    const input = await screen.findByRole("spinbutton", { name: "Gemini Flash 폴백 순서" });
    await user.type(input, "2");
    await user.tab();
    await waitFor(() =>
      expect(api.patch).toHaveBeenCalledWith(`${MODELS_PATH}/m-flash`, { fallback_priority: 2 }),
    );
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("폴백 순서를 저장했습니다", expect.anything()),
    );
  });

  it("기본 모델 행의 폴백 순서에는 「다른 모델이 1차일 때만 적용」 설명이 붙고, 다른 행에는 없다", async () => {
    server.providers = [provider()];
    server.models = [
      model({ id: "m-sonnet", display_name: "Sonnet", is_default: true, fallback_priority: 3 }),
      model({ id: "m-opus", display_name: "Opus", fallback_priority: 1 }),
    ];
    renderPage();
    const sonnet = await screen.findByRole("spinbutton", { name: "Sonnet 폴백 순서" });
    expect(sonnet).toHaveAccessibleDescription("다른 모델이 1차일 때만 적용");
    expect(screen.getByRole("spinbutton", { name: "Opus 폴백 순서" })).not.toHaveAccessibleDescription();
  });

  it("텍스트 생성 실행 순서 — 기본 모델(폴백 3)이 1차이고 나머지는 폴백 순서대로, 임베딩 필터에서는 숨긴다", async () => {
    server.providers = [provider()];
    server.models = [
      EMBEDDING,
      model({ id: "m-sonnet", display_name: "Sonnet", is_default: true, fallback_priority: 3 }),
      model({ id: "m-flash", display_name: "Flash", fallback_priority: 2 }),
      model({ id: "m-opus", display_name: "Opus", fallback_priority: 1 }),
    ];
    const user = userEvent.setup();
    renderPage();
    const summary = await screen.findByRole("region", { name: "텍스트 생성 실행 순서" });
    expect(within(summary).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Sonnet기본",
      "Opus폴백 1",
      "Flash폴백 2",
    ]);

    const filter = screen.getByRole("radiogroup", { name: "능력" });
    await user.click(within(filter).getByRole("radio", { name: "임베딩" }));
    expect(screen.queryByRole("region", { name: "텍스트 생성 실행 순서" })).toBeNull();
    await user.click(within(filter).getByRole("radio", { name: "텍스트 생성" }));
    expect(screen.getByRole("region", { name: "텍스트 생성 실행 순서" })).toBeInTheDocument();
  });

  it("기본 모델을 바꾸면(set-default 후 재조회) 번호를 고치지 않아도 새 기본 모델이 1차, 옛 기본 모델은 폴백 순서 자리로 간다", async () => {
    server.providers = [provider()];
    server.models = [
      model({ id: "m-sonnet", display_name: "Sonnet", is_default: true, fallback_priority: 3 }),
      model({ id: "m-flash", display_name: "Flash", fallback_priority: 2 }),
      model({ id: "m-opus", display_name: "Opus", fallback_priority: 1 }),
    ];
    api.post.mockImplementation((path: string) => {
      if (path === `${MODELS_PATH}/m-opus/set-default`) {
        server.models = server.models.map((m) => ({ ...m, is_default: m.id === "m-opus" }));
      }
      return Promise.resolve(undefined);
    });
    const user = userEvent.setup();
    renderPage();
    const summary = await screen.findByRole("region", { name: "텍스트 생성 실행 순서" });
    const opusRow = screen.getByRole("spinbutton", { name: "Opus 폴백 순서" }).closest("tr") as HTMLElement;
    await user.click(within(opusRow).getByRole("button", { name: "Opus 동작 더보기" }));
    await user.click(await screen.findByRole("menuitem", { name: "기본 모델로 지정" }));
    await waitFor(() =>
      expect(within(summary).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
        "Opus기본",
        "Flash폴백 2",
        "Sonnet폴백 3",
      ]),
    );
    expect(api.patch).not.toHaveBeenCalled();
  });

  it("능력 필터(SegmentedControl)로 모델을 거르고, 결과가 없으면 「필터 지우기」로 돌아간다", async () => {
    server.models = [EMBEDDING, model()];
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Gemini Flash");
    const filter = screen.getByRole("radiogroup", { name: "능력" });
    expect(within(filter).getAllByRole("radio").map((r) => r.textContent)).toEqual([
      "전체",
      "텍스트 생성",
      "임베딩",
      "이미지 생성",
    ]);

    await user.click(within(filter).getByRole("radio", { name: "임베딩" }));
    expect(screen.getByText("임베딩 A")).toBeInTheDocument();
    expect(screen.queryByText("Gemini Flash")).toBeNull();

    await user.click(within(filter).getByRole("radio", { name: "이미지 생성" }));
    expect(screen.getByText("조건에 맞는 항목이 없습니다")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "필터 지우기" }));
    expect(screen.getByText("Gemini Flash")).toBeInTheDocument();
    expect(within(filter).getByRole("radio", { name: "전체" })).toBeChecked();
  });

  it("모델 삭제는 확인 전 0회·후 1회", async () => {
    const user = userEvent.setup();
    renderPage();
    const row = await modelRowOf("임베딩 A");
    await user.click(within(row).getByRole("button", { name: "임베딩 A 동작 더보기" }));
    await user.click(await screen.findByRole("menuitem", { name: "삭제" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("재인덱싱이 끝날 때까지 검색할 수 없습니다");
    expect(api.delete).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "삭제" }));
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith(`${MODELS_PATH}/m-emb`));
    expect(api.delete).toHaveBeenCalledTimes(1);
  });
});

describe("AdminLLMPage 임베딩 모델 교체 안내", () => {
  it("접이식 info 콜아웃 — 처음엔 닫혀 있고 제목을 누르면 열린다", async () => {
    const user = userEvent.setup();
    renderPage();
    const guide = await screen.findByTestId("embedding-swap-guide");
    expect(guide.tagName).toBe("DETAILS");
    expect(guide).not.toHaveAttribute("open");
    await openGuide(user);
  });

  it("코어 v0.9.1의 5단계를 순서대로 렌더하고, HTTP 상태 코드(409)를 보이지 않는다", async () => {
    const user = userEvent.setup();
    renderPage();

    const guide = await openGuide(user);
    const steps = within(guide)
      .getAllByRole("listitem")
      .map((li) => li.textContent ?? "");

    expect(steps).toHaveLength(5);
    expect(steps[0]).toContain("비활성화");
    expect(steps[1]).toContain("등록");
    expect(steps[1]).toContain("거부됩니다");
    expect(guide.textContent).not.toContain("409");
    expect(steps[2]).toContain("기본 모델로 지정");
    expect(steps[3]).toContain("재인덱싱");
    expect(steps[4]).toContain("삭제");
  });

  it("4단계는 현행 UI에서 실행 가능한 절차(전체 재인덱싱 + '처리 중' 관측)를 지시하고 RAG 관리로 링크한다", async () => {
    const user = userEvent.setup();
    renderPage();

    const guide = await openGuide(user);
    const step4El = within(guide).getAllByRole("listitem")[3];
    const step4 = step4El.textContent ?? "";

    // 제품 UI에 없는 `stale_only` 버튼·`stale_sources` 표시를 전제하는 지시가 아니어야 한다.
    expect(step4).toContain("전체 재인덱싱");
    expect(step4).toContain("처리 중");
    expect(step4).toContain("실패한 작업 재시도");
    expect(step4).not.toMatch(/stale/);
    expect(step4).toContain("준비 중");
    expect(within(step4El).getByRole("link", { name: "RAG 관리" })).toHaveAttribute("href", "/admin/rag");
  });

  it('4단계에 "진행률 표시는 준비 중"이 없다', async () => {
    const user = userEvent.setup();
    renderPage();

    const guide = await openGuide(user);
    const step4 = within(guide).getAllByRole("listitem")[3].textContent ?? "";

    expect(step4).not.toContain("진행률 표시는 준비 중");
    expect(step4).toContain("진행 패널");
  });

  it("기존 모델을 먼저 삭제하면 안 된다는 경고를 렌더한다", async () => {
    renderPage();

    const warning = await screen.findByText(/기존 모델을 먼저 삭제하면/);
    expect(warning).toHaveTextContent("재인덱싱이 끝날 때까지");
  });

  it("전역 1개 고정이라는 구 안내문은 더 이상 렌더하지 않는다", async () => {
    renderPage();

    await screen.findByText("임베딩 모델 교체 순서");
    expect(screen.queryByText(/임베딩 모델은 전역 1개로 고정됩니다/)).not.toBeInTheDocument();
  });
});
