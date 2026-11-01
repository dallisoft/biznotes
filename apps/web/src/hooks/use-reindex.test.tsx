import type { PropsWithChildren } from "react";
import {
  QueryClient,
  QueryClientProvider,
  focusManager,
  onlineManager,
} from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api-client";
import type { ReindexRun } from "@/lib/admin-types";
import { qk } from "@/lib/query-keys";
import {
  useActiveReindexRuns,
  useIndexStatus,
  useRagSettings,
  useReindexCacheActions,
  useReindexRun,
  useReindexRuns,
  useSaveRagSettings,
  useStartReindex,
} from "./use-reindex";

const { api, toast } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));

vi.mock("@/lib/api-client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api-client")>();
  return { ...original, api };
});
vi.mock("sonner", () => ({ toast }));

function run(overrides: Partial<ReindexRun> = {}): ReindexRun {
  return {
    id: "run-1",
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

function setupClient() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity },
      mutations: { retry: false },
    },
  });
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, wrapper };
}

/** 시간 이동 없이 대기 중인 Promise·0ms 타이머(React Query notifyManager)만 비운다. */
async function settle() {
  for (let i = 0; i < 3; i += 1) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
  }
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-05T00:00:00Z"));
  vi.clearAllMocks();
  focusManager.setFocused(true);
  onlineManager.setOnline(true);
});

afterEach(() => {
  focusManager.setFocused(undefined);
  onlineManager.setOnline(true);
  vi.useRealTimers();
});

describe("use-reindex hooks", () => {
  it("queued>0 동안 3초 진행 시 GET /reindex/{id} 호출이 정확히 3회 늘어난다", async () => {
    api.get.mockResolvedValue(run());
    const { wrapper } = setupClient();
    renderHook(() => useReindexRun("run-1"), { wrapper });
    await settle();
    expect(api.get).toHaveBeenCalledTimes(1);
    await advance(3_000);
    expect(api.get).toHaveBeenCalledTimes(4);
  });

  it("queued==0 응답 뒤 10초 진행 시 추가 호출 0회", async () => {
    api.get.mockResolvedValue(run({ queued: 0, done: 3 }));
    const { wrapper } = setupClient();
    renderHook(() => useReindexRun("run-1"), { wrapper });
    await settle();
    const baseline = api.get.mock.calls.length;
    await advance(10_000);
    expect(api.get).toHaveBeenCalledTimes(baseline);
  });

  it("404면 interval=false, 이후 호출 0회, error.status 노출", async () => {
    api.get.mockRejectedValue(new ApiError(404, "missing"));
    const { wrapper } = setupClient();
    const { result } = renderHook(() => useReindexRun("run-1"), { wrapper });
    await settle();
    expect((result.current.error as ApiError).status).toBe(404);
    const baseline = api.get.mock.calls.length;
    await advance(10_000);
    expect(api.get).toHaveBeenCalledTimes(baseline);
  });

  it("401·403이면 폴링 중단(호출 0회)", async () => {
    for (const status of [401, 403]) {
      api.get.mockReset().mockRejectedValue(new ApiError(status, "auth"));
      const { wrapper } = setupClient();
      const { unmount } = renderHook(() => useReindexRun("run-1"), { wrapper });
      await settle();
      const baseline = api.get.mock.calls.length;
      await advance(10_000);
      expect(api.get).toHaveBeenCalledTimes(baseline);
      unmount();
    }
  });

  it("429면 30초 뒤 1회 재시도", async () => {
    api.get.mockRejectedValue(new ApiError(429, "limited"));
    const { wrapper } = setupClient();
    renderHook(() => useReindexRun("run-1"), { wrapper });
    await settle();
    await advance(29_999);
    expect(api.get).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it("500 연속 시 5·10·20·40·60·60초 간격으로 재시도하고 성공 후 1초로 복귀, 마지막 데이터 유지", async () => {
    const calls: number[] = [];
    api.get.mockImplementation(() => {
      calls.push(Date.now());
      if (calls.length <= 6) return Promise.reject(new ApiError(500, "server"));
      return Promise.resolve(run());
    });
    const { client, wrapper } = setupClient();
    client.setQueryData(qk.adminRag.reindexRun("run-1"), run({ done: 0 }));
    const { result } = renderHook(() => useReindexRun("run-1"), { wrapper });
    await settle();
    expect(result.current.data?.id).toBe("run-1");
    for (const interval of [5_000, 10_000, 20_000, 40_000, 60_000]) {
      await advance(interval);
      // 백오프 중에도 마지막 성공 데이터를 유지하고 오류를 함께 노출한다
      expect(result.current.data).toEqual(run({ done: 0 }));
      expect((result.current.error as ApiError | null)?.status).toBe(500);
    }
    await advance(60_000);
    expect(calls.slice(1).map((at, index) => at - calls[index])).toEqual([
      5_000,
      10_000,
      20_000,
      40_000,
      60_000,
      60_000,
    ]);
    await advance(1_000);
    expect(calls.at(-1)! - calls.at(-2)!).toBe(1_000);
  });

  it("이력 목록은 limit=100 경로, 활성 run이 있어도 30초 간격(활성 탐색은 active_only 질의)", async () => {
    api.get.mockResolvedValue([run()]);
    const { wrapper } = setupClient();
    renderHook(() => useReindexRuns(), { wrapper });
    await settle();
    expect(api.get).toHaveBeenLastCalledWith("/api/v1/admin/rag/reindex?limit=100");
    await advance(29_999);
    expect(api.get).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it("활성 질의는 active_only 경로, queued>0만 남기고(구 API 호환) 활성 2초·없으면 30초 간격", async () => {
    api.get
      .mockResolvedValueOnce([run({ id: "a" }), run({ id: "done", queued: 0 })])
      .mockResolvedValueOnce([run({ id: "done", queued: 0 })]);
    const { client, wrapper } = setupClient();
    const activeKey = qk.adminRag.activeReindexRuns(100);
    renderHook(() => useActiveReindexRuns(), { wrapper });
    await settle();
    expect(api.get).toHaveBeenLastCalledWith(
      "/api/v1/admin/rag/reindex?active_only=true&limit=100",
    );
    expect(client.getQueryData<ReindexRun[]>(activeKey)?.map((item) => item.id)).toEqual(["a"]);
    await advance(2_000);
    await settle();
    expect(api.get).toHaveBeenCalledTimes(2);
    expect(client.getQueryData<ReindexRun[]>(activeKey)).toEqual([]);
    await advance(29_999);
    expect(api.get).toHaveBeenCalledTimes(2);
    await advance(1);
    expect(api.get).toHaveBeenCalledTimes(3);
  });

  it("활성 폴링 중 focusManager.setFocused(false) 10초 진행 시 호출 0회, setFocused(true) 직후 1회 refetch", async () => {
    api.get.mockResolvedValue(run());
    const { wrapper } = setupClient();
    renderHook(() => useReindexRun("run-1"), { wrapper });
    await settle();
    act(() => focusManager.setFocused(false));
    const baseline = api.get.mock.calls.length;
    await advance(10_000);
    expect(api.get).toHaveBeenCalledTimes(baseline);
    act(() => focusManager.setFocused(true));
    await settle();
    expect(api.get).toHaveBeenCalledTimes(baseline + 1);
    // AC-18: 활성 상태의 온라인 복귀(false→true)도 즉시 1회 refetch
    const beforeOnline = api.get.mock.calls.length;
    act(() => onlineManager.setOnline(false));
    act(() => onlineManager.setOnline(true));
    await settle();
    expect(api.get).toHaveBeenCalledTimes(beforeOnline + 1);
  });

  it("unmount 뒤 10초 진행 시 호출 0회", async () => {
    api.get.mockResolvedValue(run());
    const { wrapper } = setupClient();
    const { unmount } = renderHook(() => useReindexRun("run-1"), { wrapper });
    await settle();
    unmount();
    const baseline = api.get.mock.calls.length;
    await advance(10_000);
    expect(api.get).toHaveBeenCalledTimes(baseline);
  });

  it("active=true면 2초 간격, false면 추가 호출 없음", async () => {
    api.get.mockResolvedValue([]);
    const { wrapper } = setupClient();
    const { rerender } = renderHook(({ active }) => useIndexStatus(active), {
      wrapper,
      initialProps: { active: true },
    });
    await settle();
    await advance(2_000);
    expect(api.get).toHaveBeenCalledTimes(2);
    rerender({ active: false });
    await advance(10_000);
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it("run_id 있으면 onStarted(run_id, {enqueued, skipped}) 호출 + runs·index-status invalidate", async () => {
    api.post.mockResolvedValue({ run_id: "run-1", enqueued: 2, skipped: 3 });
    const onStarted = vi.fn();
    const { client, wrapper } = setupClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useStartReindex({ onStarted }), { wrapper });
    await act(async () => result.current.mutateAsync({}));
    expect(onStarted).toHaveBeenCalledWith("run-1", { enqueued: 2, skipped: 3 });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: qk.adminRag.reindexRuns(100) });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: qk.adminRag.indexStatus });
  });

  it("run_id null이면 onStarted 미호출, runs 목록만 invalidate, 토스트는 「작업」 용어(§6.3.3)로 건너뛴 수를 알린다", async () => {
    api.post.mockResolvedValue({ run_id: null, enqueued: 0, skipped: 4 });
    const onStarted = vi.fn();
    const { client, wrapper } = setupClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useStartReindex({ onStarted }), { wrapper });
    await act(async () => result.current.mutateAsync({ failed_only: true }));
    expect(onStarted).not.toHaveBeenCalled();
    expect(invalidate).toHaveBeenCalledTimes(1);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: qk.adminRag.reindexRuns(100) });
    expect(toast.success).toHaveBeenCalledWith(
      "재인덱싱 작업 0건을 등록했습니다 — 4건은 이미 대기·처리 중이라 건너뛰었습니다",
      expect.objectContaining({ duration: 4000 }),
    );
  });

  it("GET/PUT settings 경로와 qk.adminRag.settings 키 사용", async () => {
    const settings = { chunk_size: 800 };
    api.get.mockResolvedValue(settings);
    api.put.mockResolvedValue(settings);
    const { client, wrapper } = setupClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(
      () => ({ get: useRagSettings(), save: useSaveRagSettings() }),
      { wrapper },
    );
    await settle();
    expect(api.get).toHaveBeenCalledWith("/api/v1/admin/rag/settings");
    await act(async () => result.current.save.mutateAsync(settings as never));
    expect(api.put).toHaveBeenCalledWith("/api/v1/admin/rag/settings", settings);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: qk.adminRag.settings });
  });

  it("종결·404·401·403·429 대기·5xx 백오프 각 상태에서 setFocused(false)→setFocused(true)와 setOnline(false)→setOnline(true) 실제 전이 시 상세 GET 0회, 백오프 예약 시각 유지", async () => {
    const cases: Array<{ response: ReindexRun | ApiError; wait?: number }> = [
      { response: run({ queued: 0, done: 3 }) },
      { response: new ApiError(404, "missing") },
      { response: new ApiError(401, "auth") },
      { response: new ApiError(403, "auth") },
      { response: new ApiError(429, "limited"), wait: 30_000 },
      { response: new ApiError(500, "server"), wait: 5_000 },
    ];
    for (const item of cases) {
      api.get.mockReset();
      if (item.response instanceof Error) api.get.mockRejectedValue(item.response);
      else api.get.mockResolvedValue(item.response);
      const { wrapper } = setupClient();
      const { unmount } = renderHook(() => useReindexRun("run-1"), { wrapper });
      await settle();
      const baseline = api.get.mock.calls.length;
      act(() => focusManager.setFocused(false));
      act(() => focusManager.setFocused(true));
      act(() => onlineManager.setOnline(false));
      act(() => onlineManager.setOnline(true));
      await settle();
      expect(api.get).toHaveBeenCalledTimes(baseline);
      if (item.wait) {
        await advance(item.wait - 1);
        expect(api.get).toHaveBeenCalledTimes(baseline);
        await advance(1);
        expect(api.get).toHaveBeenCalledTimes(baseline + 1);
      }
      unmount();
    }
  });

  it("목록 500 백오프 중 setFocused(false→true)·setOnline(false→true) 전이 시 호출 0회, 성공 상태에서는 각 1회", async () => {
    api.get.mockRejectedValueOnce(new ApiError(500, "server")).mockResolvedValue([]);
    const { wrapper } = setupClient();
    renderHook(() => useReindexRuns(), { wrapper });
    await settle();
    let baseline = api.get.mock.calls.length;
    act(() => focusManager.setFocused(false));
    act(() => focusManager.setFocused(true));
    act(() => onlineManager.setOnline(false));
    act(() => onlineManager.setOnline(true));
    await settle();
    expect(api.get).toHaveBeenCalledTimes(baseline);
    // 전이가 재시도 시각(실패 후 5초)을 앞당기지도 미루지도 않는다(P1-07, AC-4)
    await advance(4_999);
    expect(api.get).toHaveBeenCalledTimes(baseline);
    await advance(1);
    expect(api.get).toHaveBeenCalledTimes(baseline + 1);
    await settle();
    baseline = api.get.mock.calls.length;
    act(() => focusManager.setFocused(false));
    act(() => focusManager.setFocused(true));
    await settle();
    expect(api.get).toHaveBeenCalledTimes(baseline + 1);
    baseline = api.get.mock.calls.length;
    act(() => onlineManager.setOnline(false));
    act(() => onlineManager.setOnline(true));
    await settle();
    expect(api.get).toHaveBeenCalledTimes(baseline + 1);
  });

  it("useReindexCacheActions.forgetRun: 성공 캐시면 run을 목록 캐시에서 제거하고 invalidate, 오류 캐시면 setQueryData 없이 invalidate만", async () => {
    const runsKey = qk.adminRag.reindexRuns(100);
    const { client, wrapper } = setupClient();
    client.setQueryData<ReindexRun[]>(runsKey, [run({ id: "gone" }), run({ id: "keep" })]);
    const setData = vi.spyOn(client, "setQueryData");
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useReindexCacheActions(), { wrapper });

    const activeKey = qk.adminRag.activeReindexRuns(100);
    client.setQueryData<ReindexRun[]>(activeKey, [run({ id: "gone" })]);
    act(() => result.current.forgetRun("gone"));
    expect(client.getQueryData<ReindexRun[]>(runsKey)?.map((item) => item.id)).toEqual(["keep"]);
    expect(client.getQueryData<ReindexRun[]>(activeKey)).toEqual([]);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: runsKey });

    // 목록이 오류 상태면 캐시를 건드리지 않는다 — status가 error로 남아 degraded 잠금이 유지된다
    api.get.mockRejectedValue(new ApiError(500, "server"));
    await act(async () => {
      await client.fetchQuery({ queryKey: runsKey, queryFn: () => api.get("x") }).catch(() => {});
    });
    expect(client.getQueryState(runsKey)?.status).toBe("error");
    setData.mockClear();
    invalidate.mockClear();
    act(() => result.current.forgetRun("keep"));
    expect(setData).not.toHaveBeenCalledWith(runsKey, expect.anything());
    expect(client.getQueryState(runsKey)?.status).toBe("error");
    expect(client.getQueryData<ReindexRun[]>(runsKey)?.map((item) => item.id)).toEqual(["keep"]);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: runsKey });
  });

  it("useReindexCacheActions.refreshAfterRunFinished: index-status·runs 목록 invalidate, 반환 객체는 리렌더 간 동일", () => {
    const { client, wrapper } = setupClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result, rerender } = renderHook(() => useReindexCacheActions(), { wrapper });
    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
    act(() => result.current.refreshAfterRunFinished());
    expect(invalidate).toHaveBeenCalledTimes(2);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: qk.adminRag.indexStatus });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: qk.adminRag.reindexRuns(100) });
  });
});
