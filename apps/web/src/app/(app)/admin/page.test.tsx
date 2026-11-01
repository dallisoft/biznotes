import {
  focusManager,
  onlineManager,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ADMIN_OVERVIEW_PATHS } from "@/hooks/use-admin-overview";
import type {
  AdminMCPServer,
  AdminModel,
  AdminProvider,
  AdminUser,
  AuditLogRow,
  NotebookIndexStatus,
  ProviderCircuit,
  ReindexRun,
} from "@/lib/admin-types";
import { ApiError } from "@/lib/api-client";
import { APP_QUERY_DEFAULTS } from "@/lib/query-defaults";
import type { SkillReviewRequest } from "@/lib/skill-types";
import AdminOverviewPage from "./page";

const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

// 네트워크 경계만 mock(§3.3-3) — api 객체만 바꾸고 ApiError 등은 실제 모듈.
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));

const P = ADMIN_OVERVIEW_PATHS;
const ALL_PATHS = Object.values(P);

// ------------------------------------------------------------------ fixture

const user = (i: number, is_active = true): AdminUser => ({
  id: `u${i}`,
  email: `u${i}@example.com`,
  name: `U${i}`,
  role: i === 0 ? "admin" : "user",
  is_active,
  must_change_password: false,
  created_at: "2026-10-01T00:00:00Z",
});
const provider = (id: string, enabled = true): AdminProvider => ({
  id,
  name: id,
  type: "mock",
  base_url: null,
  has_api_key: false,
  enabled,
  model_count: 1,
});
const circuit = (id: string, open = false): ProviderCircuit => ({
  id,
  name: id,
  open,
  open_remaining_seconds: 0,
  recent_failures: 0,
});
const model = (
  id: string,
  capabilities: string[],
  opts: { enabled?: boolean; is_default?: boolean } = {},
): AdminModel => ({
  id,
  provider_id: "p",
  provider_name: "p",
  model_name: id,
  display_name: id,
  // 이 빌드가 모르는 능력(새 코어가 보낸 값)도 흉내 낼 수 있게 넓은 입력을 생성 타입으로 단언한다
  capabilities: capabilities as AdminModel["capabilities"],
  is_default: opts.is_default ?? false,
  enabled: opts.enabled ?? true,
  context_window: null,
  cost_per_1k: {},
});
const mcp = (id: string, last_health_ok: boolean | null): AdminMCPServer => ({
  id,
  name: id,
  transport: "stdio",
  endpoint_or_command: "x",
  has_env: false,
  enabled: true,
  last_health_check: null,
  last_health_ok,
});
const nb = (i: number, failed = 0, processing = 0): NotebookIndexStatus => ({
  notebook_id: `n${i}`,
  title: `N${i}`,
  source_count: 2,
  ready: 2,
  failed,
  processing,
  chunk_total: 4,
  reindex_pending: 0,
  stale: 0,
});
const run = (id: string, queued: number): ReindexRun => ({
  id,
  notebook_id: null,
  failed_only: false,
  refetch: false,
  requested: 4,
  queued,
  done: 4 - queued,
  failed: 0,
  created_at: "2026-10-15T00:00:00Z",
  finished_at: null,
  removed: 0,
  retrying: 0,
  max_attempts: 3,
  stale_only: false,
});
const review = (id: string): SkillReviewRequest => ({
  id,
  version_id: `v-${id}`,
  kind: "expose_org",
  target_visibility: "org",
  status: "pending",
  requested_by: null,
  decided_by: null,
  decided_at: null,
  note: null,
  created_at: "2026-10-15T00:00:00Z",
});
const audit = (i: number, action = "llm.provider_update"): AuditLogRow => ({
  id: `a${i}`,
  user_email: i === 1 ? null : "admin@example.com",
  action,
  resource: `res-${i}`,
  detail: {},
  created_at: new Date(Date.UTC(2026, 9, 15, 0, 0, 59 - i)).toISOString(),
});

/** 주의 항목이 하나도 없는 정상 상태(목업 D6과 같은 모양). */
function healthyData(): Record<string, unknown> {
  return {
    [P.users]: [user(0), user(1)],
    [P.providers]: [provider("claude"), provider("gemini"), provider("mock")],
    [P.providerStatus]: [circuit("claude"), circuit("gemini"), circuit("mock")],
    [P.models]: [
      model("chat", ["llm"], { is_default: true }),
      model("emb", ["embedding"], { is_default: true }),
      model("off", ["llm"], { enabled: false }),
    ],
    [P.mcpServers]: [mcp("demo", true), mcp("vacation", true)],
    [P.indexStatus]: [nb(1), nb(2), nb(3)],
    [P.activeReindex]: [],
    [P.pendingReviews]: [],
    [P.auditLogs]: Array.from({ length: 8 }, (_, i) => audit(i + 1)),
  };
}

type Behaviour = "fail" | "hang";

/** 경로별 응답 — `overrides`의 값이 "fail"이면 500, "hang"이면 응답하지 않는다. */
function mockApi(data: Record<string, unknown>, overrides: Record<string, Behaviour> = {}) {
  api.get.mockImplementation((path: string) => {
    const behaviour = overrides[path];
    if (behaviour === "fail") return Promise.reject(new ApiError(500, "서버 오류"));
    if (behaviour === "hang") return new Promise(() => {});
    if (!(path in data)) return Promise.reject(new Error(`예상하지 않은 요청: ${path}`));
    return Promise.resolve(structuredClone(data[path]));
  });
}

function renderPage() {
  // 앱과 같은 질의 기본값(providers.tsx) — 개요 훅의 전용 옵션이 기본값을 덮는지 같이 확인된다.
  const queryClient = new QueryClient({ defaultOptions: { queries: { ...APP_QUERY_DEFAULTS } } });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <AdminOverviewPage />
    </QueryClientProvider>,
  );
  return { queryClient, ...view };
}

const card = (id: string) => screen.getByTestId(`overview-card-${id}`);
const getCalls = () => api.get.mock.calls.map((c) => c[0] as string);

async function waitAllSettled() {
  await waitFor(() => {
    for (const id of CARD_IDS) expect(card(id)).toHaveAttribute("data-state", "success");
  });
}

const CARD_IDS = ["users", "providers", "models", "mcp", "index", "reindex", "reviews"];

/** 실패 응답은 재시도 1회(지연 1초) 뒤에 확정된다 — 가짜 시간을 1초 넘게 진행하고 상태를 기다린다. */
async function waitForCardState(id: string, state: string) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_100);
  });
  await waitFor(() => expect(card(id)).toHaveAttribute("data-state", state));
}

beforeEach(() => {
  // 재시도 지연(1초)·60초 경과를 가짜 시간으로 진행한다. shouldAdvanceTime으로 waitFor도 그대로 동작.
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.resetAllMocks();
  focusManager.setFocused(undefined);
  onlineManager.setOnline(true);
});

describe("관리 개요 — 카드와 링크(AC-2.7)", () => {
  it("7개 카드의 순서·라벨·링크 대상이 §6.2.6 표와 같고, 감사 로그 링크는 감사 로그 화면이다(#236)", async () => {
    mockApi(healthyData());
    renderPage();
    await waitAllSettled();

    const expected: [string, string, string][] = [
      ["users", "사용자", "/admin/users"],
      ["providers", "LLM 프로바이더", "/admin/llm"],
      ["models", "활성 모델", "/admin/llm"],
      ["mcp", "MCP 서버", "/admin/mcp"],
      ["index", "인덱스", "/admin/rag"],
      ["reindex", "진행 중 재인덱싱", "/admin/rag"],
      ["reviews", "스킬 검토 대기", "/admin/skills"],
    ];
    const rendered = screen.getAllByTestId(/^overview-card-/).map((el) => el.dataset.testid);
    expect(rendered).toEqual(expected.map(([id]) => `overview-card-${id}`));
    for (const [id, label, href] of expected) {
      const el = card(id);
      expect(within(el).getByText(label)).toBeInTheDocument();
      const links = within(el).getAllByRole("link");
      expect(links).toHaveLength(1);
      expect(links[0]).toHaveAttribute("href", href);
    }
    expect(
      within(screen.getByTestId("overview-audit")).getByRole("link", {
        name: "감사 로그에서 모두 보기",
      }),
    ).toHaveAttribute("href", "/admin/audit");
  });

  it("정상 상태의 카드 값 — 목업 D6과 같은 형식", async () => {
    mockApi(healthyData());
    renderPage();
    await waitAllSettled();

    expect(within(card("users")).getByText("2명")).toBeInTheDocument();
    expect(within(card("users")).getByText("활성 2명")).toBeInTheDocument();
    expect(within(card("providers")).getByText("3개")).toBeInTheDocument();
    expect(within(card("providers")).getByText("정상 3 / 차단 0")).toBeInTheDocument();
    expect(within(card("models")).getByText("2 / 3")).toBeInTheDocument();
    expect(within(card("models")).getByText("기본 모델 미지정 능력 없음")).toBeInTheDocument();
    expect(within(card("mcp")).getByText("정상 2 / 오류 0 / 미확인 0")).toBeInTheDocument();
    expect(within(card("index")).getByText("노트북 3개")).toBeInTheDocument();
    expect(within(card("index")).getByText("실패 0 · 처리 중 0")).toBeInTheDocument();
    expect(within(card("reindex")).getByText("없음")).toBeInTheDocument();
    expect(within(card("reindex")).getByRole("link", { name: /RAG에서 보기/ })).toBeInTheDocument();
    expect(within(card("reviews")).getByText("0건")).toBeInTheDocument();
    // 기준 시각 캡션과 새로고침(보조 버튼 — primary 아님, DP-2)
    expect(screen.getByTestId("overview-snapshot")).toHaveTextContent(/^기준 시각 /);
    const refresh = screen.getByRole("button", { name: "새로고침" });
    expect(refresh).toHaveClass("bg-background");
    expect(refresh).not.toHaveClass("bg-primary");
    expect(screen.getByRole("heading", { level: 1, name: "관리 개요" })).toBeInTheDocument();
  });

  it("진행 중 재인덱싱은 진행률 없이 건수와 「RAG에서 실시간 진행률 보기」 링크만 보인다(구 API 종결 run 제외)", async () => {
    mockApi({ ...healthyData(), [P.activeReindex]: [run("r1", 2), run("r2", 0)] });
    renderPage();
    await waitAllSettled();

    const el = card("reindex");
    expect(within(el).getByText("진행 중 1건")).toBeInTheDocument();
    expect(within(el).getByRole("link", { name: "RAG에서 실시간 진행률 보기 — 진행 중 재인덱싱" })).toHaveAttribute(
      "href",
      "/admin/rag",
    );
    expect(within(el).queryByRole("progressbar")).toBeNull();
    expect(within(el).queryByText(/%/)).toBeNull();
  });
});

describe("관리 개요 — 파생 상태 규칙(AC-2.7)", () => {
  it("모든 쿼리가 성공하고 주의 항목이 없을 때만 빈 상태 「지금 처리할 항목이 없습니다」", async () => {
    mockApi(healthyData());
    renderPage();
    await waitAllSettled();
    const attention = screen.getByTestId("overview-attention");
    expect(within(attention).getByText("지금 처리할 항목이 없습니다")).toBeInTheDocument();
    expect(within(attention).queryByText("일부 상태를 확인하지 못했습니다")).toBeNull();
  });

  it("쿼리 하나(MCP)를 실패시키면 그 카드만 ErrorState — 0·「정상」을 그리지 않고, 주의 항목은 빈 상태 대신 「일부 상태를 확인하지 못했습니다」", async () => {
    mockApi(healthyData(), { [P.mcpServers]: "fail" });
    renderPage();
    await waitForCardState("mcp", "error");

    const mcpCard = card("mcp");
    expect(within(mcpCard).getByRole("alert")).toHaveTextContent("불러오지 못했습니다");
    expect(within(mcpCard).getByRole("button", { name: "다시 시도" })).toBeInTheDocument();
    expect(mcpCard).not.toHaveTextContent(/\d/);
    expect(mcpCard).not.toHaveTextContent("정상");
    // 링크는 실패해도 남는다 — 상세 화면으로 갈 수 있게
    expect(within(mcpCard).getByRole("link")).toHaveAttribute("href", "/admin/mcp");
    // 재시도 1회(retry: 1) — 같은 경로 2번
    expect(getCalls().filter((p) => p === P.mcpServers)).toHaveLength(2);

    for (const id of CARD_IDS.filter((c) => c !== "mcp")) {
      expect(card(id)).toHaveAttribute("data-state", "success");
    }
    expect(within(card("users")).getByText("2명")).toBeInTheDocument();

    const attention = screen.getByTestId("overview-attention");
    expect(within(attention).queryByText("지금 처리할 항목이 없습니다")).toBeNull();
    expect(within(attention).getByTestId("attention-unresolved")).toHaveTextContent(
      "일부 상태를 확인하지 못했습니다MCP 서버(불러오지 못했습니다)",
    );
  });

  it("두 쿼리를 쓰는 카드(LLM 프로바이더)는 차단 상태 쿼리만 실패해도 실패다 — 목록 값만으로 「정상 3」을 그리지 않는다", async () => {
    mockApi(healthyData(), { [P.providerStatus]: "fail" });
    renderPage();
    await waitForCardState("providers", "error");
    expect(card("providers")).not.toHaveTextContent("정상");
    expect(card("providers")).not.toHaveTextContent("3개");
    expect(screen.getByTestId("attention-unresolved")).toHaveTextContent(
      "LLM 프로바이더(불러오지 못했습니다)",
    );
  });

  it("응답이 오지 않은(로딩) 카드는 스켈레톤만 — 숫자·「정상」·「없음」을 그리지 않고, 주의 항목은 「일부 상태를 확인하지 못했습니다」", async () => {
    mockApi(healthyData(), { [P.indexStatus]: "hang", [P.activeReindex]: "hang" });
    renderPage();
    await waitFor(() => expect(card("users")).toHaveAttribute("data-state", "success"));
    await waitFor(() => expect(card("models")).toHaveAttribute("data-state", "success"));

    for (const id of ["index", "reindex"]) {
      const el = card(id);
      expect(el).toHaveAttribute("data-state", "loading");
      expect(within(el).getByRole("status")).toHaveTextContent("불러오는 중");
      expect(el).not.toHaveTextContent(/\d/);
      expect(el).not.toHaveTextContent("정상");
      expect(el).not.toHaveTextContent("없음");
    }
    const attention = screen.getByTestId("overview-attention");
    expect(within(attention).queryByText("지금 처리할 항목이 없습니다")).toBeNull();
    expect(within(attention).getByTestId("attention-unresolved")).toHaveTextContent(
      "인덱스(불러오는 중)",
    );
  });

  it("카드 안 「다시 시도」는 그 카드의 쿼리만 다시 읽고, 성공하면 값을 보이며 포커스는 그 카드에 남는다", async () => {
    const data = healthyData();
    mockApi(data, { [P.mcpServers]: "fail" });
    renderPage();
    await waitForCardState("mcp", "error");
    const before = getCalls().length;

    mockApi(data);
    const ue = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await ue.click(within(card("mcp")).getByRole("button", { name: "다시 시도" }));
    await waitFor(() => expect(card("mcp")).toHaveAttribute("data-state", "success"));
    expect(getCalls().slice(before)).toEqual([P.mcpServers]);
    expect(within(card("mcp")).getByText("2개")).toBeInTheDocument();
    // 성공으로 ErrorState가 사라져도 포커스는 body로 빠지지 않고 같은 카드의 링크로 간다(P2 통합 — admin R-4)
    await waitFor(() => expect(within(card("mcp")).getByRole("link")).toHaveFocus());
    expect(within(screen.getByTestId("overview-attention")).getByText("지금 처리할 항목이 없습니다")).toBeInTheDocument();
  });

  describe("느린 「다시 시도」의 포커스 — 오류 → 로딩 → 성공·실패 내내 그 카드 칸 안(P2 리뷰 P2-01)", () => {
    /** MCP 카드를 실패 상태로 만든 뒤, 다음 MCP 요청은 `settle`을 부를 때까지 응답하지 않게 바꾼다. */
    async function failThenDefer() {
      const data = healthyData();
      mockApi(data, { [P.mcpServers]: "fail" });
      renderPage();
      await waitForCardState("mcp", "error");
      const deferred: { settle?: (ok: boolean) => void } = {};
      api.get.mockImplementation((path: string) => {
        if (path === P.mcpServers) {
          return new Promise((resolve, reject) => {
            deferred.settle = (ok) =>
              ok ? resolve(structuredClone(data[P.mcpServers])) : reject(new ApiError(500, "서버 오류"));
          });
        }
        return Promise.resolve(structuredClone(data[path as keyof typeof data]));
      });
      return deferred;
    }

    /** 키보드 사용자처럼 「다시 시도」에 포커스를 두고 Enter. */
    async function retryByKeyboard() {
      const ue = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      const retry = within(card("mcp")).getByRole("button", { name: "다시 시도" });
      retry.focus();
      await ue.keyboard("{Enter}");
      return { ue, retry };
    }

    const focusedInCard = (id: string) => {
      const active = document.activeElement;
      expect(active, "포커스가 body로 빠지지 않는다").not.toBe(document.body);
      expect(card(id)).toContainElement(active as HTMLElement);
    };

    it("로딩 중에는 카드 링크에, 지연된 응답이 성공하면 값 카드의 링크에 포커스가 있다", async () => {
      const deferred = await failThenDefer();
      const { retry } = await retryByKeyboard();

      await waitFor(() => expect(card("mcp")).toHaveAttribute("data-state", "loading"));
      expect(retry).not.toBeInTheDocument();
      focusedInCard("mcp");
      expect(document.activeElement).toBe(within(card("mcp")).getByRole("link"));

      await act(async () => deferred.settle?.(true));
      await waitFor(() => expect(card("mcp")).toHaveAttribute("data-state", "success"));
      expect(within(card("mcp")).getByText("2개")).toBeInTheDocument();
      focusedInCard("mcp");
      expect(document.activeElement).toBe(within(card("mcp")).getByRole("link"));
    });

    it("지연된 응답이 다시 실패해도 포커스는 그 카드 안에 남는다", async () => {
      const deferred = await failThenDefer();
      await retryByKeyboard();
      await waitFor(() => expect(card("mcp")).toHaveAttribute("data-state", "loading"));
      focusedInCard("mcp");

      // 개요 쿼리는 재시도 1회(지연 1초) 뒤에 실패를 확정한다 — 두 번째 요청도 실패시킨다
      await act(async () => deferred.settle?.(false));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_100);
      });
      await act(async () => deferred.settle?.(false));
      await waitFor(() => expect(card("mcp")).toHaveAttribute("data-state", "error"));
      focusedInCard("mcp");
      expect(within(card("mcp")).getByRole("button", { name: "다시 시도" })).toBeInTheDocument();
    });

    it("로딩 중에 사용자가 포커스를 다른 곳으로 옮기면 성공해도 빼앗지 않는다", async () => {
      const deferred = await failThenDefer();
      await retryByKeyboard();
      await waitFor(() => expect(card("mcp")).toHaveAttribute("data-state", "loading"));
      const refresh = screen.getByRole("button", { name: "새로고침" });
      refresh.focus();

      await act(async () => deferred.settle?.(true));
      await waitFor(() => expect(card("mcp")).toHaveAttribute("data-state", "success"));
      expect(refresh).toHaveFocus();
    });

    it("최근 감사 로그도 느린 다시 시도의 로딩·성공 내내 그 영역 안(머리글 링크)에 포커스가 있다", async () => {
      const data = healthyData();
      mockApi(data, { [P.auditLogs]: "fail" });
      renderPage();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_100);
      });
      const audit = screen.getByTestId("overview-audit");
      const retry = await within(audit).findByRole("button", { name: "다시 시도" });
      let settle: (() => void) | undefined;
      api.get.mockImplementation((path: string) =>
        path === P.auditLogs
          ? new Promise((resolve) => {
              settle = () => resolve(structuredClone(data[P.auditLogs]));
            })
          : Promise.resolve(structuredClone(data[path as keyof typeof data])),
      );
      const ue = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      retry.focus();
      await ue.keyboard("{Enter}");

      await waitFor(() => expect(retry).not.toBeInTheDocument());
      await waitFor(() => expect(audit).toContainElement(document.activeElement as HTMLElement));
      expect(document.activeElement).toBe(within(audit).getByRole("link", { name: "감사 로그에서 모두 보기" }));

      await act(async () => settle?.());
      await waitFor(() => expect(within(audit).getAllByRole("row").length).toBeGreaterThan(1));
      expect(document.activeElement).toBe(within(audit).getByRole("link", { name: "감사 로그에서 모두 보기" }));
    });
  });

  it("주의 항목: 차단 프로바이더 · MCP 오류 · 인덱스 실패 · 검토 대기 · 기본 모델 미지정을 각 링크와 함께 보인다", async () => {
    mockApi({
      ...healthyData(),
      [P.providerStatus]: [circuit("claude", true), circuit("gemini"), circuit("mock", true)],
      [P.mcpServers]: [mcp("demo", false), mcp("vacation", null)],
      [P.indexStatus]: [nb(1, 2, 0), nb(2, 0, 1), nb(3, 1, 0)],
      [P.pendingReviews]: [review("r1"), review("r2"), review("r3")],
      [P.models]: [model("chat", ["llm"], { is_default: true }), model("emb", ["embedding"])],
    });
    renderPage();
    await waitAllSettled();

    const attention = screen.getByTestId("overview-attention");
    expect(within(attention).queryByText("지금 처리할 항목이 없습니다")).toBeNull();
    expect(within(attention).queryByTestId("attention-unresolved")).toBeNull();
    const expectItem = (id: string, text: string, badge: string, href: string) => {
      const li = within(attention).getByTestId(`attention-${id}`);
      expect(li).toHaveTextContent(text);
      expect(within(li).getByText(badge)).toHaveAttribute("data-slot", "status-badge");
      expect(within(li).getByRole("link")).toHaveAttribute("href", href);
    };
    expectItem("providers", "차단된 LLM 프로바이더 2개 — claude, mock", "차단", "/admin/llm");
    expectItem("mcp", "상태 확인에 실패한 MCP 서버 1개", "오류", "/admin/mcp");
    expectItem("index", "인덱싱에 실패한 소스 3개(노트북 2개)", "실패", "/admin/rag");
    expectItem("reviews", "검토를 기다리는 스킬 공개 요청 3건", "대기", "/admin/skills");
    expectItem("models", "기본 모델이 지정되지 않은 능력 — 임베딩", "미지정", "/admin/llm");

    expect(within(card("providers")).getByText("정상 1 / 차단 2")).toBeInTheDocument();
    expect(within(card("mcp")).getByText("정상 0 / 오류 1 / 미확인 1")).toBeInTheDocument();
    expect(within(card("models")).getByText("기본 모델 미지정: 임베딩")).toBeInTheDocument();
  });

  it("일부 카드가 실패해도 성공한 카드에서 찾은 주의 항목은 보이고, 실패 카드 목록도 함께 보인다", async () => {
    mockApi(
      { ...healthyData(), [P.pendingReviews]: [review("r1")] },
      { [P.indexStatus]: "fail" },
    );
    renderPage();
    await waitForCardState("index", "error");
    const attention = screen.getByTestId("overview-attention");
    expect(within(attention).getByTestId("attention-reviews")).toHaveTextContent("1건");
    expect(within(attention).getByTestId("attention-unresolved")).toHaveTextContent(
      "인덱스(불러오지 못했습니다)",
    );
    expect(within(attention).queryByText("지금 처리할 항목이 없습니다")).toBeNull();
  });

  it("대량 fixture(사용자 500 · 모델 50 · 노트북 1,000)에서 집계는 배열 전체 기준이다", async () => {
    const users = Array.from({ length: 500 }, (_, i) => user(i, i % 8 !== 0)); // 비활성 63명
    const models = Array.from({ length: 50 }, (_, i) =>
      model(`m${i}`, ["llm"], { enabled: i < 31, is_default: i === 0 }),
    );
    const notebooks = Array.from({ length: 1_000 }, (_, i) => nb(i, i % 100 === 0 ? 2 : 0, i % 250 === 0 ? 1 : 0));
    mockApi({ ...healthyData(), [P.users]: users, [P.models]: models, [P.indexStatus]: notebooks });
    renderPage();
    await waitAllSettled();

    expect(within(card("users")).getByText("500명")).toBeInTheDocument();
    expect(within(card("users")).getByText("활성 437명")).toBeInTheDocument();
    expect(within(card("models")).getByText("31 / 50")).toBeInTheDocument();
    expect(within(card("index")).getByText("노트북 1,000개")).toBeInTheDocument();
    // 실패: 노트북 0·100·…·900의 10개 × 2 = 20, 처리 중: 0·250·500·750의 4개
    expect(within(card("index")).getByText("실패 20 · 처리 중 4")).toBeInTheDocument();
    expect(screen.getByTestId("attention-index")).toHaveTextContent("인덱싱에 실패한 소스 20개(노트북 10개)");
  });
});

describe("관리 개요 — 스냅샷·요청량(AC-2.7 · §6.2.6)", () => {
  it("첫 진입은 GET 정확히 9건(경로 집합 = 표 8개 + 감사 로그)이고, 60초가 지나도·포커스·온라인 복귀에도 추가 요청 0", async () => {
    mockApi(healthyData());
    renderPage();
    await waitAllSettled();
    await waitFor(() => expect(screen.getByTestId("overview-audit").querySelector("table")).not.toBeNull());

    expect(getCalls()).toHaveLength(9);
    expect(new Set(getCalls())).toEqual(new Set(ALL_PATHS));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(getCalls()).toHaveLength(9);

    // staleTime(60초)이 지난 뒤라도 포커스·재연결로 다시 읽지 않는다(스냅샷)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
      focusManager.setFocused(false);
      focusManager.setFocused(true);
      onlineManager.setOnline(false);
      onlineManager.setOnline(true);
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(getCalls()).toHaveLength(9);
  });

  it("「새로고침」은 모든 개요 쿼리를 다시 읽는다 — 다시 정확히 9건, 기준 시각 갱신", async () => {
    mockApi(healthyData());
    renderPage();
    await waitAllSettled();
    await waitFor(() => expect(getCalls()).toHaveLength(9));
    const firstSnapshot = screen.getByTestId("overview-snapshot").querySelector("time")!.dateTime;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    // 다시 읽는 9건은 응답을 잠시 붙잡아 「읽는 중」 상태를 관찰한다
    const release: (() => void)[] = [];
    const data = healthyData();
    api.get.mockImplementation(
      (path: string) =>
        new Promise((resolve) => release.push(() => resolve(structuredClone(data[path])))),
    );
    const ue = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const refresh = screen.getByRole("button", { name: "새로고침" });
    refresh.focus();
    await ue.keyboard("{Enter}");
    await waitFor(() => expect(getCalls()).toHaveLength(18));
    // 읽는 동안: 다시 누를 수 없지만(중복 요청 방지) 키보드 포커스는 그 버튼에 남는다
    expect(refresh).toHaveAttribute("aria-disabled", "true");
    expect(refresh).toHaveFocus();
    await ue.keyboard("{Enter}");
    expect(getCalls()).toHaveLength(18);
    await act(async () => {
      for (const r of release) r();
    });
    await waitFor(() => expect(refresh).not.toHaveAttribute("aria-disabled", "true"));
    expect(refresh).toHaveFocus();
    expect(new Set(getCalls().slice(9))).toEqual(new Set(ALL_PATHS));
    const secondSnapshot = screen.getByTestId("overview-snapshot").querySelector("time")!.dateTime;
    expect(Date.parse(secondSnapshot)).toBeGreaterThan(Date.parse(firstSnapshot));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(getCalls()).toHaveLength(18);
  });

  it("새로고침이 실패한 카드는 이전 값을 남기지 않고 ErrorState로 바뀐다", async () => {
    const data = healthyData();
    mockApi(data);
    renderPage();
    await waitAllSettled();
    expect(within(card("reviews")).getByText("0건")).toBeInTheDocument();

    mockApi(data, { [P.pendingReviews]: "fail" });
    const ue = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await ue.click(screen.getByRole("button", { name: "새로고침" }));
    await waitForCardState("reviews", "error");
    expect(card("reviews")).not.toHaveTextContent("0건");
  });
});

describe("관리 개요 — 최근 감사 로그", () => {
  it("응답 앞 5건만 순서대로 — 시각·사용자·작업(라벨 + 원시 코드)·대상, 미지 코드는 원시값 그대로", async () => {
    const logs = [
      audit(1, "llm.provider_delete"),
      audit(2, "rag.reindex"),
      audit(3, "custom.unknown_action"),
      audit(4),
      audit(5),
      audit(6, "never.shown"),
    ];
    mockApi({ ...healthyData(), [P.auditLogs]: logs });
    renderPage();
    const section = screen.getByTestId("overview-audit");
    await waitFor(() => expect(section.querySelector("table")).not.toBeNull());

    const rows = within(section).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(5);
    expect(within(rows[0]).getByText("LLM 프로바이더 삭제")).toBeInTheDocument();
    expect(within(rows[0]).getByText("llm.provider_delete")).toBeInTheDocument();
    expect(rows[0]).toHaveTextContent("—"); // user_email null
    expect(rows[0]).toHaveTextContent("res-1");
    expect(within(rows[1]).getByText("재인덱싱 요청")).toBeInTheDocument();
    expect(within(rows[2]).getAllByText("custom.unknown_action")).toHaveLength(1);
    expect(section).not.toHaveTextContent("never.shown");
  });

  it("감사 로그가 비면 빈 상태, 실패하면 ErrorState(다른 카드는 그대로)", async () => {
    mockApi({ ...healthyData(), [P.auditLogs]: [] });
    const first = renderPage();
    await waitFor(() =>
      expect(within(screen.getByTestId("overview-audit")).getByText("아직 감사 로그가 없습니다")).toBeInTheDocument(),
    );
    first.unmount();

    mockApi(healthyData(), { [P.auditLogs]: "fail" });
    renderPage();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_100);
    });
    await waitFor(() =>
      expect(within(screen.getByTestId("overview-audit")).getByRole("alert")).toHaveTextContent(
        "감사 로그를 불러오지 못했습니다",
      ),
    );
    await waitAllSettled();
  });
});
