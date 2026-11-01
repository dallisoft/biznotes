import { readFileSync } from "node:fs";
import path from "node:path";
import {
  QueryClient,
  QueryClientProvider,
  focusManager,
  onlineManager,
} from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api-client";
import { APP_QUERY_DEFAULTS } from "@/lib/query-defaults";
import type { NotebookIndexStatus, ReindexRun, RerankerStatus } from "@/lib/admin-types";
import { qk } from "@/lib/query-keys";
import { stubNarrowViewport } from "@/hooks/use-narrow-viewport.test-kit";
import AdminRagPage from "./page";

const { api, toast } = vi.hoisted(() => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));

// API 경계만 mock(§11 실경로 정책) — `api` 객체만 대체하고 ApiError 등은 실제 모듈을 쓴다.
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("sonner", () => ({ toast }));

const SETTINGS = {
  chunk_size: 800,
  chunk_overlap: 100,
  top_k: 6,
  rerank_enabled: false,
  rerank_candidates: 20,
  rerank_model: "BAAI/bge-reranker-base",
  verify_lexical_threshold: 0.25,
  verify_embedding_threshold: 0.55,
  insufficient_context_threshold_base: 0.5,
  insufficient_context_threshold_rerank: 0.5,
};

const INDEX_STATUS: NotebookIndexStatus[] = [
  {
    notebook_id: "nb-1",
    title: "노트북 1",
    source_count: 3,
    ready: 2,
    failed: 1,
    processing: 0,
    chunk_total: 42,
    reindex_pending: 0,
    stale: 0,
  },
];

// core-v0.15.0 #209 — 리랭커 선택지(AC-2.4)
const RERANKER_PATH = "/api/v1/admin/rag/reranker";
const GATEWAY_KEY = "gateway:3f0c9f8e-1111-4222-8333-944445555666:openai/bge-gw";
const MOCK_KEY = "gateway:4a1d0a9f-2222-4333-8444-a55556666777:mock-rr";
const LOCAL_KEY = "fastembed:BAAI/bge-reranker-base";
const RERANKER_STATUS: RerankerStatus = {
  saved_key: "BAAI/bge-reranker-base",
  active_key: LOCAL_KEY,
  active_display_name: "BAAI/bge-reranker-base",
  error: null,
  options: [
    { key: GATEWAY_KEY, display_name: "사내 리랭커", available: true },
    { key: MOCK_KEY, display_name: "Mock 리랭커", available: false },
    { key: LOCAL_KEY, display_name: "BAAI/bge-reranker-base (로컬)", available: true },
  ],
};

const LIST_PATH = "/api/v1/admin/rag/reindex?limit=100";
const ACTIVE_PATH = "/api/v1/admin/rag/reindex?active_only=true&limit=100";
const DETAIL_PREFIX = "/api/v1/admin/rag/reindex/";
const RUNS_KEY = qk.adminRag.reindexRuns(100);

const RUN_A = "aaaaaaaa-0000-4000-8000-000000000001";
const RUN_B = "bbbbbbbb-0000-4000-8000-000000000002";
const RUN_T = "cccccccc-0000-4000-8000-000000000003";

function makeRun(overrides: Partial<ReindexRun> = {}): ReindexRun {
  return {
    id: RUN_A,
    notebook_id: null,
    failed_only: false,
    refetch: false,
    requested: 3,
    queued: 2,
    done: 1,
    failed: 0,
    created_at: "2026-10-05T00:00:00Z",
    finished_at: null,
    removed: 0,
    retrying: 0,
    max_attempts: 3,
    stale_only: false,
    ...overrides,
  };
}

function terminal(overrides: Partial<ReindexRun> = {}): ReindexRun {
  return makeRun({ queued: 0, done: 3, finished_at: "2026-10-05T00:01:00Z", ...overrides });
}

/**
 * 가짜 서버 상태 — `api.get`(API 경계)만 이 상태로 응답한다. 목록과 상세는 같은 run 저장소를
 * 읽고, 테스트별로 목록 오류·미응답, run별 상세 응답을 덮어쓴다.
 */
const server = {
  settings: SETTINGS as Record<string, unknown>,
  reranker: (): Promise<unknown> => Promise.resolve(RERANKER_STATUS),
  runs: new Map<string, ReindexRun>(),
  indexStatus: INDEX_STATUS,
  listError: null as Error | null,
  listPending: false,
  detail: new Map<string, () => Promise<ReindexRun>>(),
};

function route(path: string): Promise<unknown> {
  if (path === "/api/v1/admin/rag/settings") return Promise.resolve(server.settings);
  if (path === RERANKER_PATH) return server.reranker();
  if (path === "/api/v1/admin/rag/index-status") return Promise.resolve(server.indexStatus);
  // 소스 요약 백필 진행률(코어 v0.30.0 #454, #285) — 섹션 동작은 source-digest-backfill-section.test.tsx
  if (path.startsWith("/api/v1/admin/source-digests/backfill")) {
    return Promise.resolve({ scope: "all", progress: null });
  }
  if (path === LIST_PATH || path === ACTIVE_PATH) {
    if (server.listPending) return new Promise(() => {});
    if (server.listError) return Promise.reject(server.listError);
    const runs = [...server.runs.values()].sort((a, b) => b.created_at.localeCompare(a.created_at));
    return Promise.resolve(path === ACTIVE_PATH ? runs.filter((run) => run.queued > 0) : runs);
  }
  if (path.startsWith(DETAIL_PREFIX)) {
    const id = path.slice(DETAIL_PREFIX.length);
    const override = server.detail.get(id);
    if (override) return override();
    // 기존 5건 호환: 서버가 모르는 run은 이미 종결된 것으로 응답한다.
    return Promise.resolve(server.runs.get(id) ?? terminal({ id, requested: 1, done: 1 }));
  }
  return Promise.resolve([]);
}

function detailCalls(id: string) {
  return api.get.mock.calls.filter(([p]) => p === `${DETAIL_PREFIX}${id}`).length;
}

function listCalls() {
  return api.get.mock.calls.filter(([p]) => p === ACTIVE_PATH).length;
}

function renderPage(seed?: (client: QueryClient) => void) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  // 마운트 전 캐시(예: 롤백 전에 받은 응답)를 심는다
  seed?.(queryClient);
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <AdminRagPage />
    </QueryClientProvider>,
  );
  return { ...utils, queryClient };
}

beforeEach(() => {
  vi.clearAllMocks();
  server.settings = SETTINGS;
  server.reranker = () => Promise.resolve(RERANKER_STATUS);
  server.runs = new Map();
  server.indexStatus = INDEX_STATUS;
  server.listError = null;
  server.listPending = false;
  server.detail = new Map();
  api.get.mockImplementation(route);
});


type Clicker = { click: (element: Element) => Promise<void> };

/** 위험 구역 「실패한 작업 재시도…」 → 확인 다이얼로그 「재시도」(§6.5 DangerZone — P5 리뷰 P5-05). */
async function confirmRetryFailed(u: Clicker = userEvent) {
  await u.click(screen.getByRole("button", { name: "실패한 작업 재시도…" }));
  const dialog = await screen.findByRole("alertdialog");
  expect(dialog).toHaveTextContent("실패한 작업을 다시 시도할까요?");
  await u.click(within(dialog).getByRole("button", { name: "재시도" }));
  // 모달이 완전히 닫혀야(바깥 inert·aria-hidden 해제) 페이지 요소를 role로 찾을 수 있다
  await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
}

describe("AdminRagPage 재인덱싱 응답 표시", () => {
  it("skipped 1건 이상이면 건너뛴 건수와 run_id를 표시한다", async () => {
    api.post.mockResolvedValue({ enqueued: 2, skipped: 3, run_id: "run-abc" });

    renderPage();
    await screen.findByText("노트북 1");
    await userEvent.click(screen.getByRole("button", { name: "재인덱싱" }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/api/v1/admin/rag/reindex", {
        notebook_id: "nb-1",
      }),
    );
    const result = await screen.findByTestId("reindex-result");
    expect(result).toHaveTextContent("2건 등록");
    expect(result).toHaveTextContent("3건 건너뜀 (이미 대기·처리 중)");
    // 원시 run UUID 대신 「요청 번호 앞 8자」 + 복사(§6.6.2 RAG 행)
    expect(result).toHaveTextContent("요청 번호 run-abc");
    expect(within(result).getByRole("button", { name: "요청 번호 복사" })).toBeInTheDocument();
    expect(toast.success).toHaveBeenCalledWith(
      "재인덱싱 작업 2건을 등록했습니다 — 3건은 이미 대기·처리 중이라 건너뛰었습니다",
      expect.anything(),
    );
  });

  it("skipped 0건이면 '건너뛴 소스 없음'을 표시한다", async () => {
    api.post.mockResolvedValue({ enqueued: 5, skipped: 0, run_id: "run-zero" });

    renderPage();
    await screen.findByText("노트북 1");
    await userEvent.click(screen.getByRole("button", { name: "재인덱싱" }));

    const result = await screen.findByTestId("reindex-result");
    expect(result).toHaveTextContent("5건 등록");
    expect(result).toHaveTextContent("건너뛴 소스 없음");
    expect(result).not.toHaveTextContent("건너뜀");
    expect(toast.success).toHaveBeenCalledWith("재인덱싱 작업 5건을 등록했습니다", expect.anything());
  });

  it("run_id가 null이면 '요청 번호 없음'으로 표시한다", async () => {
    api.post.mockResolvedValue({ enqueued: 0, skipped: 0, run_id: null });

    renderPage();
    await screen.findByText("노트북 1");
    await confirmRetryFailed();

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/api/v1/admin/rag/reindex", {
        failed_only: true,
      }),
    );
    const result = await screen.findByTestId("reindex-result");
    expect(result).toHaveTextContent("요청 번호 없음");
    expect(within(result).queryByRole("button", { name: "요청 번호 복사" })).toBeNull();
  });

  it("새 요청이 실패하면 직전 성공 결과 줄이 남지 않는다", async () => {
    api.post.mockResolvedValueOnce({ enqueued: 7, skipped: 0, run_id: "run-ok" });

    renderPage();
    await screen.findByText("노트북 1");
    await userEvent.click(screen.getByRole("button", { name: "재인덱싱" }));
    expect(await screen.findByTestId("reindex-result")).toHaveTextContent("7건 등록");

    api.post.mockRejectedValueOnce(new Error("재인덱싱 실패"));
    await confirmRetryFailed();

    // 실패 토스트는 「{동작}하지 못했습니다」 + 서버 문구는 설명으로만(§6.5 토스트)
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "재인덱싱을 요청하지 못했습니다",
        expect.objectContaining({ description: expect.stringContaining("재인덱싱 실패") }),
      ),
    );
    expect(screen.queryByTestId("reindex-result")).not.toBeInTheDocument();
  });

  it("요청 전에는 결과 줄을 렌더하지 않는다", async () => {
    renderPage();
    await screen.findByText("노트북 1");
    expect(screen.queryByTestId("reindex-result")).not.toBeInTheDocument();
  });
});

describe("AdminRagPage 재인덱싱 진행 패널·잠금", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    focusManager.setFocused(undefined);
    onlineManager.setOnline(true);
    vi.useRealTimers();
  });

  const user = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

  async function tick(ms: number) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  function reindexButtons() {
    return [
      screen.getByRole("button", { name: "전체 재인덱싱…" }),
      screen.getByRole("button", { name: "실패한 작업 재시도…" }),
      screen.getByRole("button", { name: "재인덱싱" }),
    ];
  }

  async function expectLocked(reason: RegExp) {
    await waitFor(() => {
      for (const button of reindexButtons()) {
        expect(button).toBeDisabled();
        expect(button).toHaveAttribute("aria-describedby", "reindex-busy-reason");
        expect(button).toHaveAccessibleDescription(reason);
      }
    });
    const reasonElement = document.getElementById("reindex-busy-reason");
    expect(reasonElement).toHaveAttribute("role", "status");
    expect(reasonElement).toHaveTextContent(reason);
  }

  async function expectUnlocked() {
    await waitFor(() => {
      for (const button of reindexButtons()) expect(button).toBeEnabled();
    });
  }

  async function startFullReindex() {
    const u = user();
    await u.click(screen.getByRole("button", { name: "전체 재인덱싱…" }));
    const dialog = await screen.findByRole("alertdialog");
    await u.click(within(dialog).getByRole("button", { name: "재인덱싱" }));
    // 모달이 완전히 닫혀야(바깥 inert·aria-hidden 해제) 페이지 요소를 role로 찾을 수 있다
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  }

  it("목록 응답 전에는 세 버튼이 disabled이고 aria-describedby가 활성 run 확인 중을 가리킨다", async () => {
    server.listPending = true;
    renderPage();
    await screen.findByText("노트북 1");
    await expectLocked(/활성 run을 확인하는 중/);
  });

  it("목록 500이면 잠금 유지 + 다시 확인 버튼이 refetch를 호출한다", async () => {
    server.listError = new ApiError(500, "boom");
    renderPage();
    await screen.findByText("노트북 1");
    await expectLocked(/활성 run을 확인할 수 없어 요청을 잠갔습니다/);
    const before = listCalls();
    server.listError = null;
    await user().click(screen.getByRole("button", { name: "다시 확인" }));
    await waitFor(() => expect(listCalls()).toBe(before + 1));
    await expectUnlocked();
    expect(screen.queryByRole("button", { name: "다시 확인" })).not.toBeInTheDocument();
  });

  it("run_id 응답 뒤 1초 내 reindex-progress와 progressbar가 보이고 세 버튼이 disabled", async () => {
    let postedAt = Number.NaN;
    api.post.mockImplementation(() => {
      postedAt = Date.now();
      server.runs.set(RUN_A, makeRun({ id: RUN_A, requested: 3, queued: 2, done: 1 }));
      return Promise.resolve({ enqueued: 3, skipped: 0, run_id: RUN_A });
    });
    renderPage();
    await screen.findByText("노트북 1");
    await expectUnlocked();
    await startFullReindex();
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/admin/rag/reindex", {}));
    const panel = await screen.findByTestId("reindex-progress");
    await within(panel).findByRole("progressbar", {}, { timeout: 1_000 });
    expect(Date.now() - postedAt).toBeLessThan(1_000);
    expect(panel).toHaveTextContent("이 run의 등록 대상 3건");
    expect(panel).toHaveTextContent("대기·처리 미완료 2");
    expect(panel).toHaveTextContent("완료 1");
    expect(panel).toHaveTextContent("실패 0");
    // 내가 {}로 시작한 run은 정확히 "전체"(knownScopes 배선) — "전체 또는 삭제된 노트북"이 아니다(P1-06)
    expect(within(panel).getByText("전체", { exact: true })).toBeInTheDocument();
    expect(panel).not.toHaveTextContent("전체 또는 삭제된 노트북");
    expect(within(panel).getByTestId("reindex-status")).toHaveTextContent("진행 중");
    await expectLocked(/재인덱싱 1건이 진행 중이라 새 요청을 받을 수 없습니다/);
  });

  it("queued 2→0 전이에 토스트 1회, index-status·runs invalidate, 버튼 활성화", async () => {
    server.runs.set(RUN_A, makeRun({ id: RUN_A, requested: 3, queued: 2, done: 1 }));
    const { queryClient } = renderPage();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const panel = await screen.findByTestId("reindex-progress");
    await waitFor(() => expect(detailCalls(RUN_A)).toBeGreaterThan(0));
    await expectLocked(/재인덱싱 1건이 진행 중/);

    server.runs.set(RUN_A, terminal({ id: RUN_A, requested: 3, done: 3 }));
    await tick(1_000);
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("재인덱싱 종료 — 완료 3건 · 실패 0건"),
    );
    expect(invalidate).toHaveBeenCalledWith({ queryKey: qk.adminRag.indexStatus });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: RUNS_KEY });
    await expectUnlocked();
    await tick(5_000);
    const doneToasts = toast.success.mock.calls.filter(([m]) =>
      String(m).startsWith("재인덱싱 종료"),
    );
    expect(doneToasts).toHaveLength(1);
    expect(toast.warning).not.toHaveBeenCalled();
    // 타인의 run은 종결되면 포커스 대상에서 빠진다(selectFocusedRun ③은 내가 시작한 run만).
    // P1-11 이후 빈 패널은 testid·내용 없이 role=status 영역만 남는다.
    expect(screen.queryByTestId("reindex-progress")).not.toBeInTheDocument();
    expect(panel).toHaveTextContent(/^$/);
  });

  it("이미 queued 0인 run만 있으면 토스트 없음·버튼 활성", async () => {
    server.runs.set(RUN_T, terminal({ id: RUN_T }));
    renderPage();
    await screen.findByText("노트북 1");
    await expectUnlocked();
    await tick(3_000);
    expect(toast.success).not.toHaveBeenCalled();
    expect(toast.warning).not.toHaveBeenCalled();
    expect(detailCalls(RUN_T)).toBe(0);
    expect(screen.queryByTestId("reindex-progress")).not.toBeInTheDocument();
  });

  it("마운트 시 목록에 queued>0 run(타인)이 있으면 패널 복원·폴링 시작", async () => {
    server.runs.set(RUN_B, makeRun({ id: RUN_B, queued: 5, done: 0, requested: 5 }));
    renderPage();
    const panel = await screen.findByTestId("reindex-progress");
    expect(within(panel).getByTestId("reindex-status")).toHaveTextContent("진행 중");
    expect(panel).toHaveTextContent("전체 또는 삭제된 노트북");
    expect(panel).not.toHaveTextContent("최근 100건");
    await waitFor(() => expect(detailCalls(RUN_B)).toBe(1));
    await tick(3_000);
    expect(detailCalls(RUN_B)).toBeGreaterThanOrEqual(3);
    await expectLocked(/재인덱싱 1건이 진행 중/);
  });

  it("두 run 활성 중 포커스 run 종결 시 잠금 유지·포커스 승계, 둘째 종결 후 해제", async () => {
    server.runs.set(
      RUN_A,
      makeRun({ id: RUN_A, created_at: "2026-10-05T00:00:00Z", notebook_id: null }),
    );
    server.runs.set(
      RUN_B,
      makeRun({ id: RUN_B, created_at: "2026-10-05T00:05:00Z", notebook_id: "nb-1" }),
    );
    renderPage();
    const panel = await screen.findByTestId("reindex-progress");
    // 최신 활성 run(B)이 포커스, A는 요약 줄
    await waitFor(() => expect(panel).toHaveTextContent("노트북 노트북 1"));
    expect(within(panel).getByTestId("reindex-other-active")).toHaveTextContent(
      "다른 진행 중 run 1건 — 범위 전체 또는 삭제된 노트북",
    );
    await expectLocked(/재인덱싱 2건이 진행 중/);

    server.runs.set(RUN_B, terminal({ id: RUN_B, notebook_id: "nb-1" }));
    await tick(1_000);
    await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(1));
    // 목록 갱신 후 포커스가 A로 승계되고 잠금 유지
    await tick(2_000);
    await waitFor(() => expect(detailCalls(RUN_A)).toBeGreaterThan(0));
    await waitFor(() => expect(panel).toHaveTextContent("전체 또는 삭제된 노트북"));
    expect(within(panel).queryByTestId("reindex-other-active")).not.toBeInTheDocument();
    await expectLocked(/재인덱싱 1건이 진행 중/);

    server.runs.set(RUN_A, terminal({ id: RUN_A }));
    await tick(1_000);
    await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(2));
    await expectUnlocked();
  });

  it("목록이 상세보다 먼저 종결을 반영해 포커스가 넘어가도 직전 포커스 run의 종결 토스트는 1회", async () => {
    server.runs.set(RUN_A, makeRun({ id: RUN_A, created_at: "2026-10-05T00:00:00Z" }));
    server.runs.set(RUN_B, makeRun({ id: RUN_B, created_at: "2026-10-05T00:05:00Z" }));
    renderPage();
    await screen.findByTestId("reindex-progress");
    await waitFor(() => expect(detailCalls(RUN_B)).toBeGreaterThan(0));
    // 목록은 B 종결을 먼저 보여 주고, B 상세는 아직 활성(지연)으로 응답한다
    server.runs.set(RUN_B, terminal({ id: RUN_B }));
    server.detail.set(RUN_B, () => Promise.resolve(makeRun({ id: RUN_B })));
    await tick(2_000);
    await waitFor(() => expect(detailCalls(RUN_A)).toBeGreaterThan(0));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("재인덱싱 종료 — 완료 3건 · 실패 0건"),
    );
    await tick(3_000);
    expect(toast.success).toHaveBeenCalledTimes(1);
    await expectLocked(/재인덱싱 1건이 진행 중/);
  });

  it("포커스 run 404 뒤 해당 id GET 호출이 0회로 멈추고 패널 문구·버튼 상태가 맞다", async () => {
    // 목록은 계속 A를 활성으로 돌려주지만(지연 반영) 상세는 404 — 같은 id를 다시 고르면 안 된다.
    server.detail.set(RUN_A, () => Promise.reject(new ApiError(404, "not found")));
    api.post.mockImplementation(() => {
      server.runs.set(RUN_A, makeRun({ id: RUN_A }));
      return Promise.resolve({ enqueued: 2, skipped: 0, run_id: RUN_A });
    });
    const { queryClient } = renderPage();
    const setData = vi.spyOn(queryClient, "setQueryData");
    await screen.findByText("노트북 1");
    await expectUnlocked();
    await startFullReindex();
    const panel = await screen.findByTestId("reindex-progress");
    await waitFor(() =>
      expect(within(panel).getByTestId("reindex-status")).toHaveTextContent(
        "run을 찾을 수 없습니다(삭제됨)",
      ),
    );
    const calls = detailCalls(RUN_A);
    expect(calls).toBe(1);
    expect(setData).toHaveBeenCalledWith(RUNS_KEY, expect.any(Function));
    await tick(10_000);
    expect(detailCalls(RUN_A)).toBe(calls);
    await expectUnlocked();
  });

  it("403이면 폴링 중단·alert·버튼 잠금 유지", async () => {
    server.runs.set(RUN_A, makeRun({ id: RUN_A }));
    server.detail.set(RUN_A, () => Promise.reject(new ApiError(403, "forbidden")));
    renderPage();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("세션 또는 권한 오류 — 다시 로그인하세요");
    await expectLocked(/세션 또는 권한 오류/);
    const calls = detailCalls(RUN_A);
    await tick(10_000);
    expect(detailCalls(RUN_A)).toBe(calls);
    await expectLocked(/세션 또는 권한 오류/);
  });

  it("null 응답은 reindex-result 유지·startedRunId 불변·runs invalidate·타인 run 포커스", async () => {
    api.post.mockImplementation(() => {
      // 그 사이 다른 admin이 시작한 run이 목록에 나타난다
      server.runs.set(RUN_B, makeRun({ id: RUN_B, notebook_id: "nb-1" }));
      return Promise.resolve({ enqueued: 0, skipped: 2, run_id: null });
    });
    renderPage();
    await screen.findByText("노트북 1");
    await expectUnlocked();
    const before = listCalls();
    await confirmRetryFailed(user());
    const result = await screen.findByTestId("reindex-result");
    expect(result).toHaveTextContent("요청 번호 없음");
    expect(toast.success).toHaveBeenCalledWith(
      "재인덱싱 작업 0건을 등록했습니다 — 2건은 이미 대기·처리 중이라 건너뛰었습니다",
      expect.anything(),
    );
    await waitFor(() => expect(listCalls()).toBeGreaterThan(before));
    const panel = await screen.findByTestId("reindex-progress");
    await waitFor(() => expect(panel).toHaveTextContent("노트북 노트북 1"));
    expect(detailCalls(RUN_B)).toBeGreaterThan(0);
    expect(api.get).not.toHaveBeenCalledWith(`${DETAIL_PREFIX}null`);
    expect(panel).not.toHaveTextContent("선택 후보");
    expect(screen.getByTestId("reindex-result")).toHaveTextContent("요청 번호 없음");
  });

  it("{enqueued 2, skipped 3} 응답에 후보 5 줄", async () => {
    api.post.mockImplementation(() => {
      server.runs.set(RUN_A, makeRun({ id: RUN_A, requested: 2, queued: 2, done: 0 }));
      return Promise.resolve({ enqueued: 2, skipped: 3, run_id: RUN_A });
    });
    renderPage();
    await screen.findByText("노트북 1");
    await expectUnlocked();
    await startFullReindex();
    const panel = await screen.findByTestId("reindex-progress");
    await waitFor(() =>
      expect(panel).toHaveTextContent("선택 후보 5 = 등록 2 + 건너뜀 3(이미 대기·처리 중)"),
    );
    expect(panel).toHaveTextContent("이 run의 등록 대상 2건");
  });

  it("노트북별 run은 노트북 {title}, 복원 null run은 전체 또는 삭제된 노트북", async () => {
    server.runs.set(
      RUN_A,
      makeRun({ id: RUN_A, notebook_id: null, created_at: "2026-10-05T00:00:00Z" }),
    );
    server.runs.set(
      RUN_B,
      makeRun({
        id: RUN_B,
        notebook_id: "nb-1",
        failed_only: true,
        created_at: "2026-10-05T00:05:00Z",
      }),
    );
    renderPage();
    const panel = await screen.findByTestId("reindex-progress");
    await waitFor(() => expect(panel).toHaveTextContent("노트북 노트북 1 · 실패만"));
    expect(within(panel).getByTestId("reindex-other-active")).toHaveTextContent(
      "전체 또는 삭제된 노트북",
    );
  });

  it("재인덱싱 대기 컬럼이 값·Badge로 표시", async () => {
    server.indexStatus = [
      { ...INDEX_STATUS[0], reindex_pending: 4 },
      { ...INDEX_STATUS[0], notebook_id: "nb-2", title: "노트북 2", reindex_pending: 0 },
    ];
    renderPage();
    await screen.findByText("노트북 2");
    const header = screen.getByRole("columnheader", { name: "재인덱싱 대기" });
    const headers = within(header.closest("tr")!).getAllByRole("columnheader");
    const column = headers.indexOf(header);
    const rows = screen.getAllByRole("row").slice(1);
    const cell = (row: HTMLElement) => within(row).getAllByRole("cell")[column];
    expect(cell(rows[0])).toHaveTextContent("4");
    expect(cell(rows[0]).querySelector("[data-slot=badge]")).not.toBeNull();
    expect(cell(rows[1])).toHaveTextContent("0");
    expect(cell(rows[1]).querySelector("[data-slot=badge]")).toBeNull();
  });

  it("AdminRagPage 모듈이 @/lib/api-client를 import하지 않는다", () => {
    const source = readFileSync(path.resolve(__dirname, "page.tsx"), "utf8");
    expect(source).not.toMatch(/from "@\/lib\/api-client"/);
    expect(source).toMatch(/from "@\/hooks\/use-reindex"/);
  });

  it('POST resolve 직후 상세 Promise 미해결·목록은 종결 run만이어도 세 버튼 disabled + 사유 "방금 시작한 run의 상태를 확인하는 중"', async () => {
    server.runs.set(RUN_T, terminal({ id: RUN_T }));
    server.detail.set(RUN_A, () => new Promise(() => {}));
    api.post.mockResolvedValue({ enqueued: 2, skipped: 0, run_id: RUN_A });
    renderPage();
    await screen.findByText("노트북 1");
    await expectUnlocked();
    await startFullReindex();
    await waitFor(() => expect(detailCalls(RUN_A)).toBe(1));
    await expectLocked(/방금 시작한 run의 상태를 확인하는 중/);
    const panel = screen.getByTestId("reindex-progress");
    expect(within(panel).getByTestId("reindex-status")).toHaveTextContent("run 상태 확인 중");
    expect(within(panel).queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("초기 성공(종결 run만)→background 500(isRefetchError)→세 버튼 잠금·마지막 데이터 유지→다시 확인 성공→해제", async () => {
    server.runs.set(RUN_T, terminal({ id: RUN_T }));
    const { queryClient } = renderPage();
    await screen.findByText("노트북 1");
    await expectUnlocked();

    server.listError = new ApiError(500, "boom");
    const before = listCalls();
    act(() => focusManager.setFocused(false));
    act(() => focusManager.setFocused(true));
    await waitFor(() => expect(listCalls()).toBe(before + 1));
    await expectLocked(/활성 run을 확인할 수 없어 요청을 잠갔습니다/);
    expect(queryClient.getQueryState(RUNS_KEY)?.status).toBe("error");
    expect(queryClient.getQueryData<ReindexRun[]>(RUNS_KEY)?.map((r) => r.id)).toEqual([RUN_T]);

    server.listError = null;
    await user().click(screen.getByRole("button", { name: "다시 확인" }));
    await expectUnlocked();
    expect(queryClient.getQueryState(RUNS_KEY)?.status).toBe("success");
  });

  it("확인 직후 포커스가 비활성 트리거 대신 사유 영역(reindex-busy-reason)으로 이동한다", async () => {
    api.post.mockImplementation(() => {
      server.runs.set(RUN_A, makeRun({ id: RUN_A }));
      return Promise.resolve({ enqueued: 2, skipped: 0, run_id: RUN_A });
    });
    renderPage();
    await screen.findByText("노트북 1");
    await expectUnlocked();
    await startFullReindex();
    const reason = document.getElementById("reindex-busy-reason");
    await waitFor(() => expect(reason).toHaveFocus());
    expect(document.body).not.toHaveFocus();
    await expectLocked(/재인덱싱 1건이 진행 중/);
    expect(reason).toHaveFocus();
  });

  it("확인 다이얼로그가 열린 동안 타 admin 활성 run이 감지되면 확인 버튼이 비활성·사유 표시되고 POST가 나가지 않는다", async () => {
    renderPage();
    await screen.findByText("노트북 1");
    await expectUnlocked();
    const u = user();
    await u.click(screen.getByRole("button", { name: "전체 재인덱싱…" }));
    const dialog = await screen.findByRole("alertdialog");
    const confirm = within(dialog).getByRole("button", { name: "재인덱싱" });
    expect(confirm).toBeEnabled();

    // 유휴 30초 폴링이 다른 admin의 활성 run을 발견한다
    server.runs.set(RUN_T, makeRun({ id: RUN_T }));
    const before = listCalls();
    await tick(30_000);
    await waitFor(() => expect(listCalls()).toBeGreaterThan(before));
    await waitFor(() => expect(confirm).toBeDisabled());
    expect(confirm).toHaveAccessibleDescription(/재인덱싱 1건이 진행 중/);
    await u.click(confirm);
    expect(api.post).not.toHaveBeenCalled();
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  });

  it("사유 영역(role=status)은 잠금 해제 중에도 마운트돼 있고 잠금 시 같은 노드의 문구만 바뀐다", async () => {
    server.detail.set(RUN_A, () => new Promise(() => {}));
    api.post.mockResolvedValue({ enqueued: 2, skipped: 0, run_id: RUN_A });
    renderPage();
    await screen.findByText("노트북 1");
    await expectUnlocked();
    const reason = document.getElementById("reindex-busy-reason");
    expect(reason).toHaveAttribute("role", "status");
    expect(reason).toHaveTextContent(/^$/);
    await startFullReindex();
    await expectLocked(/방금 시작한 run의 상태를 확인하는 중/);
    expect(document.getElementById("reindex-busy-reason")).toBe(reason);
  });
});

describe("AdminRagPage 설정 저장 (core-v0.12.0-upgrade P1-11 · AC-5.5)", () => {
  const saveBar = () => screen.queryByRole("region", { name: "저장되지 않은 변경 사항" });

  it("설정 응답 전에는 폼 대신 로딩 상태 — 기본값을 보이거나 저장할 수 없다", async () => {
    let resolveSettings: (value: unknown) => void = () => {};
    api.get.mockImplementation((path: string) =>
      path === "/api/v1/admin/rag/settings"
        ? new Promise((resolve) => {
            resolveSettings = resolve;
          })
        : route(path),
    );
    renderPage();
    await screen.findByText("노트북 1");
    expect(screen.getByText("검색 설정을 불러오는 중")).toBeInTheDocument();
    expect(screen.queryByLabelText("청크 크기")).toBeNull();
    expect(saveBar()).toBeNull();
    await act(async () => {
      resolveSettings({ ...SETTINGS, chunk_size: 1200 });
    });
    expect(await screen.findByLabelText("청크 크기")).toHaveValue(1200);
    expect(saveBar()).toBeNull();
  });

  it("설정을 불러오지 못하면 ErrorState(다시 시도)를 보이고, 다시 시도에 성공하면 폼을 연다", async () => {
    const user = userEvent.setup();
    let fail = true;
    api.get.mockImplementation((path: string) =>
      path === "/api/v1/admin/rag/settings"
        ? fail
          ? Promise.reject(new ApiError(500, "boom"))
          : Promise.resolve(SETTINGS)
        : route(path),
    );
    renderPage();
    expect(await screen.findByText("검색 설정을 불러오지 못했습니다")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    expect(await screen.findByLabelText("청크 크기")).toHaveValue(800);
    // 성공하면 포커스는 다시 그려진 설정 폼의 첫 입력으로 간다(HCI-P2-2)
    await waitFor(() => expect(screen.getByLabelText("청크 크기")).toHaveFocus());
  });

  it("저장 막대는 바꿨을 때만 보이고, 되돌리기는 원래 값으로·저장은 PUT 1회 뒤 막대를 닫는다", async () => {
    const user = userEvent.setup();
    api.put.mockImplementation((_path: string, body: unknown) => Promise.resolve(body));
    renderPage();
    const chunk = await screen.findByLabelText("청크 크기");
    expect(saveBar()).toBeNull();

    await user.clear(chunk);
    await user.type(chunk, "900");
    expect(saveBar()).not.toBeNull();
    expect(saveBar()).toHaveTextContent("저장되지 않은 변경 사항이 있습니다");
    await user.click(screen.getByRole("button", { name: "되돌리기" }));
    expect(chunk).toHaveValue(800);
    expect(saveBar()).toBeNull();
    expect(api.put).not.toHaveBeenCalled();

    await user.clear(chunk);
    await user.type(chunk, "900");
    await user.click(screen.getByRole("button", { name: "변경 사항 저장" }));
    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
    expect(api.put).toHaveBeenCalledWith("/api/v1/admin/rag/settings", { ...SETTINGS, chunk_size: 900 });
    await waitFor(() => expect(saveBar()).toBeNull());
    expect(toast.success).toHaveBeenCalledWith("RAG 설정을 저장했습니다", expect.anything());
  });

  it("값을 바꿨다 원래대로 돌려 놓으면 막대가 사라진다", async () => {
    const user = userEvent.setup();
    renderPage();
    const topK = await screen.findByLabelText("검색 결과 수(top-k)");
    await user.clear(topK);
    await user.type(topK, "7");
    expect(saveBar()).not.toBeNull();
    await user.clear(topK);
    await user.type(topK, "6");
    expect(saveBar()).toBeNull();
  });

  it("범위를 벗어난 값은 저장하지 않는다(폼 입력 검증을 거쳐 제출)", async () => {
    const user = userEvent.setup();
    renderPage();
    const chunk = await screen.findByLabelText("청크 크기");
    await user.clear(chunk);
    await user.type(chunk, "50");
    await user.click(screen.getByRole("button", { name: "변경 사항 저장" }));
    expect(api.put).not.toHaveBeenCalled();
    expect(saveBar()).not.toBeNull();
  });

  it("「변경 사항 저장」과 「전체 재인덱싱…」은 같은 폼·같은 줄에 없다 — 운영 작업은 위험 구역에 따로 있다", async () => {
    const user = userEvent.setup();
    renderPage();
    const chunk = await screen.findByLabelText("청크 크기");
    await user.clear(chunk);
    await user.type(chunk, "900");
    const save = screen.getByRole("button", { name: "변경 사항 저장" });
    const full = screen.getByRole("button", { name: "전체 재인덱싱…" });
    const retry = screen.getByRole("button", { name: "실패한 작업 재시도…" });
    // 저장 버튼은 어떤 form 안에도 없고(폼 밖 저장 막대), 재인덱싱 버튼도 설정 폼 밖이다
    expect(save.closest("form")).toBeNull();
    expect(full.closest("form")).toBeNull();
    expect(retry.closest("form")).toBeNull();
    const dangerZone = screen.getByRole("region", { name: "위험 구역" });
    expect(dangerZone).toHaveAttribute("data-slot", "danger-zone");
    expect(dangerZone).toContainElement(full);
    expect(dangerZone).toContainElement(retry);
    expect(dangerZone).not.toContainElement(save);
    const bar = save.closest("[data-slot=sticky-save-bar]") as HTMLElement;
    expect(bar).not.toContainElement(full);
    // 설정 섹션과 위험 구역은 서로를 품지 않는 별개 영역이다
    const settings = screen.getByRole("region", { name: "검색 설정" });
    expect(settings).toContainElement(save);
    expect(settings).not.toContainElement(full);
    expect(dangerZone).not.toContainElement(settings);
  });

  it("「관리 / RAG」 브레드크럼, 「RAG 관리」 h1, 섹션 h2(text-lg) 아래 FormSection 4개는 h3 — 개발 phase 코드 「(18a)」「(18b)」가 없다", async () => {
    renderPage();
    await screen.findByLabelText("청크 크기");
    const nav = screen.getByRole("navigation", { name: "위치" });
    expect(within(nav).getByRole("link", { name: "관리" })).toHaveAttribute("href", "/admin");
    expect(within(nav).getByText("RAG")).toHaveAttribute("aria-current", "page");
    expect(screen.getAllByRole("heading", { level: 1 }).map((h) => h.textContent)).toEqual(["RAG 관리"]);
    expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual([
      "검색 설정",
      "인덱스 운영",
      "소스 요약",
      "위험 구역",
    ]);
    for (const h of screen.getAllByRole("heading", { level: 2 })) expect(h).toHaveClass("text-lg");
    expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual([
      "청킹",
      "검색과 리랭킹",
      "인용 검증 기준",
      "근거 부족 판정 기준",
      "노트북별 인덱스 현황",
    ]);
    expect(document.body.textContent).not.toMatch(/\(18[ab]\)/);
    // 단위·범위는 입력 설명으로 연결된다(FormField)
    expect(screen.getByLabelText("청크 크기")).toHaveAccessibleDescription("범위 100~4000");
    expect(screen.getByLabelText("어휘 겹침 기준")).toHaveAccessibleDescription("범위 0~1");
  });

  it("위험 구역 두 버튼은 모두 확인을 거친다 — 전체 재인덱싱은 비용·소요 시간을, 「실패한 작업 재시도…」는 실패 소스만 다시 처리함을 알리고 취소하면 요청 0회(§6.5, P5 리뷰 P5-05)", async () => {
    const user = userEvent.setup();
    api.post.mockResolvedValue({ enqueued: 0, skipped: 0, run_id: null });
    renderPage();
    await screen.findByText("노트북 1");
    const full = screen.getByRole("button", { name: "전체 재인덱싱…" });
    await waitFor(() => expect(full).toBeEnabled());
    await user.click(full);
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("전체 소스를 재인덱싱할까요?");
    expect(dialog).toHaveTextContent("임베딩 모델에 따라 비용이 들 수 있습니다");
    await user.click(within(dialog).getByRole("button", { name: "취소" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(api.post).not.toHaveBeenCalled();

    const retry = screen.getByRole("button", { name: "실패한 작업 재시도…" });
    await user.click(retry);
    const confirm = await screen.findByRole("alertdialog");
    expect(confirm).toHaveTextContent("실패한 작업을 다시 시도할까요?");
    expect(confirm).toHaveTextContent("인덱싱에 실패한 소스만 다시 처리합니다");
    // 초기 포커스는 「취소」, 확인 버튼은 동작 동사이고 빨강이 아니다(비파괴 작업 — §6.5)
    await waitFor(() => expect(within(confirm).getByRole("button", { name: "취소" })).toHaveFocus());
    expect(within(confirm).getByRole("button", { name: "재시도" }).className).not.toMatch(/bg-destructive/);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(api.post).not.toHaveBeenCalled();
    await waitFor(() => expect(retry).toHaveFocus());

    await confirmRetryFailed(user);
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/api/v1/admin/rag/reindex", { failed_only: true }),
    );
    expect(api.post).toHaveBeenCalledTimes(1);
  });

  it("노트북이 없으면 인덱스 현황 표 대신 EmptyState, 조회 실패면 ErrorState", async () => {
    server.indexStatus = [];
    const { unmount } = renderPage();
    expect(await screen.findByText("아직 노트북이 없습니다")).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "재인덱싱 대기" })).toBeNull();
    unmount();

    api.get.mockImplementation((path: string) =>
      path === "/api/v1/admin/rag/index-status"
        ? Promise.reject(new ApiError(500, "boom"))
        : route(path),
    );
    renderPage();
    expect(await screen.findByText("인덱스 현황을 불러오지 못했습니다")).toBeInTheDocument();
  });

  it("요청 번호 복사 버튼은 전체 UUID를 클립보드에 쓴다", async () => {
    api.post.mockResolvedValue({ enqueued: 1, skipped: 0, run_id: RUN_A });
    // user-event가 설치하는 클립보드(브라우저 경계)로 실제 쓰인 값을 읽는다
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("노트북 1");
    await user.click(screen.getByRole("button", { name: "재인덱싱" }));
    const result = await screen.findByTestId("reindex-result");
    expect(result).toHaveTextContent("요청 번호 aaaaaaaa");
    expect(result).not.toHaveTextContent(RUN_A);
    await user.click(within(result).getByRole("button", { name: "요청 번호 복사" }));
    await waitFor(async () => expect(await navigator.clipboard.readText()).toBe(RUN_A));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("요청 번호를 복사했습니다", expect.anything()),
    );
  });
});

describe("AdminRagPage 리랭커 드롭다운 (core-v0.15.0 AC-2.4)", () => {
  // v0.15.0 API의 GET 응답(설정 행 없음 = 기본값) + 리랭킹 켜짐
  const SETTINGS_V15 = {
    ...SETTINGS,
    rerank_enabled: true,
    embedding_batch_size: 64,
    embedding_timeout_seconds: 60,
    rerank_timeout_seconds: 30,
  };
  const trigger = () => screen.getByRole("combobox", { name: "리랭커" });

  function rerankerCalls() {
    return api.get.mock.calls.filter(([p]) => p === RERANKER_PATH).length;
  }

  /** 저장 막대는 폼이 바뀌었을 때만 보인다(AC-5.5) — 리랭커를 건드리지 않는 저장은 다른 칸을 바꿔 연다. */
  async function editTopK(user: ReturnType<typeof userEvent.setup>, value = "7") {
    const topK = await screen.findByLabelText("검색 결과 수(top-k)");
    await user.clear(topK);
    await user.type(topK, value);
  }

  async function saveAndPayload(user: ReturnType<typeof userEvent.setup>) {
    const save = await screen.findByRole("button", { name: "변경 사항 저장" });
    await waitFor(() => expect(save).toBeEnabled());
    await user.click(save);
    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
    const [path, payload] = api.put.mock.calls[0];
    expect(path).toBe("/api/v1/admin/rag/settings");
    return payload as Record<string, unknown>;
  }

  async function choose(user: ReturnType<typeof userEvent.setup>, name: string) {
    await user.click(trigger());
    await user.click(await screen.findByRole("option", { name }));
  }

  beforeEach(() => {
    server.settings = SETTINGS_V15;
    api.put.mockImplementation((_path: string, body: unknown) => Promise.resolve(body));
  });

  it("옛 무접두 값은 로컬 항목으로 표시되고, 리랭커를 바꾸지 않고 저장하면 원값 그대로 PUT한다", async () => {
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => expect(trigger()).toHaveTextContent("BAAI/bge-reranker-base (로컬)"));
    expect(screen.getByText("사용 중: BAAI/bge-reranker-base")).toBeInTheDocument();
    await editTopK(user);
    const payload = await saveAndPayload(user);
    expect(payload.rerank_model).toBe("BAAI/bge-reranker-base"); // fastembed: 접두사를 붙이지 않는다
    expect(payload).toEqual({ ...SETTINGS_V15, top_k: 7 });
  });

  it("같은 항목을 다시 골라도 원값을 유지한다", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(trigger()).toHaveTextContent("BAAI/bge-reranker-base (로컬)"));

    await choose(user, "BAAI/bge-reranker-base (로컬)");
    // 같은 항목은 폼을 바꾸지 않는다 — 저장 막대가 열리지 않는다
    expect(screen.queryByRole("button", { name: "변경 사항 저장" })).toBeNull();

    await editTopK(user);
    expect((await saveAndPayload(user)).rerank_model).toBe("BAAI/bge-reranker-base");
  });

  it("다른 리랭커를 고르면 gateway: 키로 저장한다", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(trigger()).toHaveTextContent("BAAI/bge-reranker-base (로컬)"));

    await choose(user, "사내 리랭커");

    expect(trigger()).toHaveTextContent("사내 리랭커");
    expect((await saveAndPayload(user)).rerank_model).toBe(GATEWAY_KEY);
  });

  it("자동을 고르면 빈 문자열로 저장한다", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(trigger()).toHaveTextContent("BAAI/bge-reranker-base (로컬)"));

    await choose(user, "자동(기본 리랭커, 없으면 로컬)");

    expect((await saveAndPayload(user)).rerank_model).toBe("");
  });

  it("사용할 수 없는 리랭커는 비활성이고 「사용 불가」로 표시한다", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(trigger()).toHaveTextContent("BAAI/bge-reranker-base (로컬)"));

    await user.click(trigger());
    const unavailable = await screen.findByRole("option", { name: "Mock 리랭커 — 사용 불가" });
    expect(unavailable).toHaveAttribute("aria-disabled", "true");
    await user.click(unavailable);
    expect(trigger()).toHaveTextContent("BAAI/bge-reranker-base (로컬)");
  });

  it("저장된 리랭커를 해석하지 못하면 오류를 보여 준다", async () => {
    const gone = "gateway:5b2e1b0a-3333-4444-8555-b66667777888:gone";
    server.settings = { ...SETTINGS_V15, rerank_model: gone };
    server.reranker = () =>
      Promise.resolve({
        ...RERANKER_STATUS,
        saved_key: gone,
        active_key: null,
        active_display_name: null,
        error: "설정된 리랭커가 없습니다(삭제·이름 변경): gone",
        options: [...RERANKER_STATUS.options, { key: gone, display_name: gone, available: false }],
      });
    renderPage();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "현재 설정으로는 리랭크할 수 없습니다: 설정된 리랭커가 없습니다(삭제·이름 변경): gone",
    );
    expect(screen.queryByText(/^사용 중:/)).not.toBeInTheDocument();
  });

  it("리랭킹이 꺼져 있으면 리랭커 목록을 조회하지 않고, 켜면 조회한다", async () => {
    server.settings = { ...SETTINGS_V15, rerank_enabled: false };
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("노트북 1");

    expect(rerankerCalls()).toBe(0);
    expect(screen.queryByRole("combobox", { name: "리랭커" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("switch", { name: /리랭킹/ }));
    expect(screen.getByRole("switch", { name: /리랭킹/ })).toBeChecked();
    await waitFor(() => expect(rerankerCalls()).toBe(1));
    await waitFor(() => expect(trigger()).toHaveTextContent("BAAI/bge-reranker-base (로컬)"));
  });

  it("리랭크 타임아웃은 5~180 숫자 입력이고 바꾸면 PUT에 반영된다", async () => {
    const user = userEvent.setup();
    renderPage();
    const timeout = await screen.findByLabelText(/리랭크 타임아웃/);
    await waitFor(() => expect(timeout).toHaveValue(30));
    expect(timeout).toHaveAttribute("min", "5");
    expect(timeout).toHaveAttribute("max", "180");

    await user.clear(timeout);
    await user.type(timeout, "45");

    expect((await saveAndPayload(user)).rerank_timeout_seconds).toBe(45);
  });

  it("저장에 성공하면 리랭커 상태를 다시 조회한다", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(rerankerCalls()).toBe(1));

    await editTopK(user);
    await saveAndPayload(user);

    await waitFor(() => expect(rerankerCalls()).toBe(2));
  });

  it("신 web × 구 API: 목록 404·타임아웃 키 없음이어도 렌더하고 원값을 그대로 저장한다", async () => {
    // v0.13.0 GET 응답 — rerank_timeout_seconds가 없고, 구 API만 아는 키가 있을 수 있다
    const legacy = { ...SETTINGS, rerank_enabled: true, legacy_only_key: 7 };
    server.settings = legacy;
    server.reranker = () => Promise.reject(new ApiError(404, "Not Found"));
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText(/리랭커 목록을 불러올 수 없음/)).toHaveTextContent(
      "현재 값(BAAI/bge-reranker-base)을 그대로 저장합니다",
    );
    expect(trigger()).toHaveTextContent("BAAI/bge-reranker-base");
    expect(trigger()).toHaveAttribute("data-disabled");
    expect(screen.queryByLabelText(/리랭크 타임아웃/)).not.toBeInTheDocument();
    expect(rerankerCalls()).toBe(1); // 404는 재시도하지 않는다

    await editTopK(user);
    const payload = await saveAndPayload(user);
    expect(payload).toEqual({ ...legacy, top_k: 7 });
    expect(payload).not.toHaveProperty("rerank_timeout_seconds");
  });
  it("목록 응답 전에는 선택을 잠그고, 그 사이 구 API 404가 오면 원값을 그대로 저장한다(리뷰 P2-01)", async () => {
    const legacy = { ...SETTINGS, rerank_enabled: true };
    server.settings = legacy;
    let rejectStatus: (error: unknown) => void = () => {};
    server.reranker = () =>
      new Promise((_resolve, reject) => {
        rejectStatus = reject;
      });
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => expect(rerankerCalls()).toBe(1));
    expect(trigger()).toHaveAttribute("data-disabled");
    expect(screen.getByText(/리랭커 목록을 불러오는 중/)).toBeInTheDocument();
    await user.click(trigger());
    expect(screen.queryByRole("option", { name: "자동(기본 리랭커, 없으면 로컬)" })).toBeNull();

    await act(async () => {
      rejectStatus(new ApiError(404, "Not Found"));
    });
    await screen.findByText(/리랭커 목록을 불러올 수 없음/);
    expect(trigger()).toHaveAttribute("data-disabled");

    await editTopK(user);
    const payload = await saveAndPayload(user);
    expect(payload).toEqual({ ...legacy, top_k: 7 });
    expect(payload.rerank_model).toBe("BAAI/bge-reranker-base");
  });

  it("목록 응답이 늦게 와도 성공한 뒤에는 고를 수 있다(리뷰 P2-01)", async () => {
    let resolveStatus: (value: unknown) => void = () => {};
    server.reranker = () =>
      new Promise((resolve) => {
        resolveStatus = resolve;
      });
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => expect(rerankerCalls()).toBe(1));
    expect(trigger()).toHaveAttribute("data-disabled");

    await act(async () => {
      resolveStatus(RERANKER_STATUS);
    });
    await waitFor(() => expect(trigger()).not.toHaveAttribute("data-disabled"));
    expect(screen.queryByText(/리랭커 목록을 불러오는 중/)).toBeNull();
    await choose(user, "사내 리랭커");

    expect((await saveAndPayload(user)).rerank_model).toBe(GATEWAY_KEY);
  });
  it("코어가 strip하는 공백(U+0085)이 섞인 원값도 로컬 항목으로 표시하고, 같은 항목을 다시 골라도 원값을 저장한다(리뷰 P2-07)", async () => {
    const raw = "\u0085BAAI/bge-reranker-base\u0085";
    server.settings = { ...SETTINGS_V15, rerank_model: raw };
    server.reranker = () => Promise.resolve({ ...RERANKER_STATUS, saved_key: raw });
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => expect(trigger()).not.toHaveAttribute("data-disabled"));
    expect(trigger()).toHaveTextContent("BAAI/bge-reranker-base (로컬)");
    await choose(user, "BAAI/bge-reranker-base (로컬)");
    expect(screen.queryByRole("button", { name: "변경 사항 저장" })).toBeNull();

    await editTopK(user);
    expect((await saveAndPayload(user)).rerank_model).toBe(raw);
  });
  it("롤백 전 성공 목록이 캐시에 있어도, 구 API 설정 응답이면 잠그고 404 뒤 원값을 저장한다(리뷰 P2-09)", async () => {
    const legacy = { ...SETTINGS, rerank_enabled: true };
    server.settings = legacy;
    let rejectStatus: (error: unknown) => void = () => {};
    server.reranker = () =>
      new Promise((_resolve, reject) => {
        rejectStatus = reject;
      });
    const user = userEvent.setup();
    renderPage((client) => client.setQueryData(qk.adminRag.reranker, RERANKER_STATUS));

    await waitFor(() => expect(rerankerCalls()).toBe(1));
    await screen.findByLabelText("청크 크기"); // 설정 응답 뒤에만 폼이 열린다
    expect(trigger()).toHaveAttribute("data-disabled");
    await user.click(trigger());
    expect(screen.queryByRole("option", { name: "사내 리랭커" })).toBeNull();

    await act(async () => {
      rejectStatus(new ApiError(404, "Not Found"));
    });
    await screen.findByText(/리랭커 목록을 불러올 수 없음/);
    expect(trigger()).toHaveAttribute("data-disabled");

    await editTopK(user);
    const payload = await saveAndPayload(user);
    expect(payload).toEqual({ ...legacy, top_k: 7 });
  });

  it("마운트 전 캐시는 근거가 아니다 — v0.15 서버도 이번 응답이 올 때까지 잠근다(리뷰 P2-09)", async () => {
    let resolveStatus: (value: unknown) => void = () => {};
    server.reranker = () =>
      new Promise((resolve) => {
        resolveStatus = resolve;
      });
    const user = userEvent.setup();
    renderPage((client) => client.setQueryData(qk.adminRag.reranker, RERANKER_STATUS));

    await waitFor(() => expect(rerankerCalls()).toBe(1));
    await waitFor(() => expect(screen.getByLabelText(/리랭크 타임아웃/)).toHaveValue(30));
    expect(trigger()).toHaveAttribute("data-disabled");

    await act(async () => {
      resolveStatus(RERANKER_STATUS);
    });
    await waitFor(() => expect(trigger()).not.toHaveAttribute("data-disabled"));
    await choose(user, "사내 리랭커");
    expect((await saveAndPayload(user)).rerank_model).toBe(GATEWAY_KEY);
  });

  it("대조군: 마운트 뒤 한 번 받은 다음의 배경 재조회 중에는 계속 고를 수 있다(리뷰 P2-09)", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderPage();
    await waitFor(() => expect(trigger()).not.toHaveAttribute("data-disabled"));

    let resolveRefetch: (value: unknown) => void = () => {};
    server.reranker = () =>
      new Promise((resolve) => {
        resolveRefetch = resolve;
      });
    await act(async () => {
      void queryClient.invalidateQueries({ queryKey: qk.adminRag.reranker });
    });
    await waitFor(() => expect(rerankerCalls()).toBe(2));
    expect(trigger()).not.toHaveAttribute("data-disabled");
    await choose(user, "사내 리랭커");
    expect(trigger()).toHaveTextContent("사내 리랭커");

    await act(async () => {
      resolveRefetch(RERANKER_STATUS);
    });
    expect((await saveAndPayload(user)).rerank_model).toBe(GATEWAY_KEY);
  });
  // 실제 앱 캐시 설정(staleTime 10초)으로 같은 QueryClient를 재사용한다 — fresh 캐시로 요청이 생략되는 경로(리뷰 P2-10)
  function appClient() {
    return new QueryClient({
      defaultOptions: {
        queries: { ...APP_QUERY_DEFAULTS, retry: false },
        mutations: { retry: false },
      },
    });
  }

  function mountWith(client: QueryClient) {
    return render(
      <QueryClientProvider client={client}>
        <AdminRagPage />
      </QueryClientProvider>,
    );
  }

  const settingsCalls = () =>
    api.get.mock.calls.filter(([p]) => p === "/api/v1/admin/rag/settings").length;

  it("앱 캐시 설정에서 10초 안에 다시 들어와도 현재 서버에 다시 묻고 선택을 연다(리뷰 P2-10)", async () => {
    const client = appClient();
    const first = mountWith(client);
    await waitFor(() => expect(trigger()).not.toHaveAttribute("data-disabled"));
    expect([settingsCalls(), rerankerCalls()]).toEqual([1, 1]);
    first.unmount();

    const user = userEvent.setup();
    mountWith(client);
    await waitFor(() => expect([settingsCalls(), rerankerCalls()]).toEqual([2, 2]));
    await waitFor(() => expect(trigger()).not.toHaveAttribute("data-disabled"));
    await choose(user, "사내 리랭커");
    expect((await saveAndPayload(user)).rerank_model).toBe(GATEWAY_KEY);
  });

  it("앱 캐시 설정에서 마운트 전 fresh 설정·목록 캐시가 있어도 새 응답 뒤에 연다(리뷰 P2-10)", async () => {
    const client = appClient();
    client.setQueryData(qk.adminRag.settings, SETTINGS_V15);
    client.setQueryData(qk.adminRag.reranker, RERANKER_STATUS);
    mountWith(client);

    await waitFor(() => expect([settingsCalls(), rerankerCalls()]).toEqual([1, 1]));
    await waitFor(() => expect(trigger()).not.toHaveAttribute("data-disabled"));
  });

  it("앱 캐시 설정에서 fresh 목록 캐시만 있고 리랭킹이 꺼져 있으면, 켤 때 다시 묻고 연다(리뷰 P2-10)", async () => {
    server.settings = { ...SETTINGS_V15, rerank_enabled: false };
    const client = appClient();
    client.setQueryData(qk.adminRag.reranker, RERANKER_STATUS);
    const user = userEvent.setup();
    mountWith(client);
    await screen.findByText("노트북 1");
    expect(rerankerCalls()).toBe(0);

    await user.click(screen.getByRole("switch", { name: /리랭킹/ }));
    await waitFor(() => expect(rerankerCalls()).toBe(1));
    await waitFor(() => expect(trigger()).not.toHaveAttribute("data-disabled"));
  });

  it("앱 캐시 설정에서도 롤백 전 fresh 목록 캐시 + 구 API면 잠그고 원값을 저장한다(리뷰 P2-09·P2-10)", async () => {
    const legacy = { ...SETTINGS, rerank_enabled: true };
    server.settings = legacy;
    server.reranker = () => Promise.reject(new ApiError(404, "Not Found"));
    const client = appClient();
    client.setQueryData(qk.adminRag.reranker, RERANKER_STATUS);
    const user = userEvent.setup();
    mountWith(client);

    await screen.findByText(/리랭커 목록을 불러올 수 없음/);
    expect(trigger()).toHaveAttribute("data-disabled");
    await editTopK(user);
    expect(await saveAndPayload(user)).toEqual({ ...legacy, top_k: 7 });
  });
});

describe("AdminRagPage 좁은 화면 표 규칙 (plan §6.7, P5 리뷰 P5-04·재리뷰 P5-R01)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("< 768: 행 동작 「재인덱싱」은 ⋯ 메뉴 하나로 접히고, 고르면 지금처럼 확인 없이 그 노트북을 재인덱싱 요청한다", async () => {
    stubNarrowViewport();
    const user = userEvent.setup();
    api.post.mockResolvedValue({ enqueued: 3, skipped: 0, run_id: null });
    renderPage();
    const row = (await screen.findByText("노트북 1")).closest("tr")!;
    const actions = row.lastElementChild as HTMLElement;
    await waitFor(() => expect(within(actions).queryByRole("button", { name: "재인덱싱" })).toBeNull());
    expect(within(actions).getAllByRole("button").map((b) => b.getAttribute("aria-label"))).toEqual([
      "노트북 1 동작 더보기",
    ]);
    const more = within(actions).getByRole("button", { name: "노트북 1 동작 더보기" });
    await waitFor(() => expect(screen.getByRole("button", { name: "전체 재인덱싱…" })).toBeEnabled());
    await user.click(more);
    const menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["재인덱싱"]);
    await user.click(within(menu).getByRole("menuitem", { name: "재인덱싱" }));
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/api/v1/admin/rag/reindex", { notebook_id: "nb-1" }),
    );
    expect(screen.queryByRole("alertdialog")).toBeNull(); // 확인 동작은 넓은 화면과 같다(없음)
    expect(await screen.findByTestId("reindex-result")).toHaveTextContent("3건 등록");
  });

  it("< 768: 잠긴 동안(활성 run 확인 중) 메뉴 「재인덱싱」은 비활성이고 잠긴 이유를 보이며 고르면 요청 0회", async () => {
    stubNarrowViewport();
    server.listPending = true;
    const user = userEvent.setup();
    renderPage();
    const row = (await screen.findByText("노트북 1")).closest("tr")!;
    await waitFor(() => expect(within(row).queryByRole("button", { name: "재인덱싱" })).toBeNull());
    await user.click(within(row).getByRole("button", { name: "노트북 1 동작 더보기" }));
    const item = within(await screen.findByRole("menu")).getByRole("menuitem", { name: /^재인덱싱/ });
    expect(item).toHaveAttribute("aria-disabled", "true");
    expect(item).toHaveTextContent("활성 run을 확인하는 중입니다");
    await user.click(item);
    expect(api.post).not.toHaveBeenCalled();
  });

  it("노트북별 인덱스 현황 표는 좁은 화면 규칙 래퍼(첫 열 고정·오른쪽 단서) 안에 있다", async () => {
    renderPage();
    const table = (await screen.findByText("노트북 1")).closest("table")!;
    expect(table.closest('[data-slot="admin-table-frame"]')).not.toBeNull();
  });
});
