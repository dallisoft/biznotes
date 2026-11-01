import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SourceDigestBackfillSection } from "@/components/admin/source-digest-backfill-section";
import type { SourceDigestBackfillProgress } from "@/lib/admin-types";
import { APP_QUERY_DEFAULTS } from "@/lib/query-defaults";

const { api, toast } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("sonner", () => ({ toast }));

const PATH = "/api/v1/admin/source-digests/backfill";
const NOTEBOOKS = [{ notebook_id: "nb-1", title: "종목분석" }];

function progress(over: Partial<SourceDigestBackfillProgress> = {}): SourceDigestBackfillProgress {
  return {
    scope: "all",
    notebook_id: null,
    status: "done",
    total: 141,
    generated: 130,
    skipped: 10,
    failed: 1,
    stop_reason: null,
    started_at: "2026-10-29T00:00:00Z",
    updated_at: "2026-10-29T00:10:00Z",
    finished_at: "2026-10-29T00:10:00Z",
    ...over,
  };
}

function renderSection(replies: Record<string, unknown>) {
  api.get.mockImplementation((p: string) =>
    p in replies ? Promise.resolve(replies[p]) : Promise.reject(new Error(`unexpected GET ${p}`)),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { ...APP_QUERY_DEFAULTS, retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <SourceDigestBackfillSection notebooks={NOTEBOOKS} />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.resetAllMocks());

describe("SourceDigestBackfillSection — 소스 요약 백필(코어 #454, #285)", () => {
  it("모든 노트북의 마지막 진행률을 보인다", async () => {
    renderSection({ [PATH]: { scope: "all", progress: progress() } });
    const status = await screen.findByTestId("digest-backfill-status");
    await waitFor(() => expect(status).toHaveTextContent("끝남"));
    expect(status).toHaveTextContent("만듦 130 · 건너뜀 10 · 실패 1 / 전체 141");
  });

  it("상한으로 멈추면 이유와 남은 수를 보인다", async () => {
    renderSection({
      [PATH]: { scope: "all", progress: progress({ status: "stopped", stop_reason: "daily_cost_cap", generated: 40, skipped: 0, failed: 0 }) },
    });
    const status = await screen.findByTestId("digest-backfill-status");
    await waitFor(() => expect(status).toHaveTextContent("하루 비용 상한으로 멈췄습니다 — 남은 101개"));
  });

  it("기록이 없으면 안내하고, 확인 뒤 범위(노트북)로 시작 요청을 보낸다", async () => {
    const user = userEvent.setup();
    renderSection({ [PATH]: { scope: "all", progress: null }, [`${PATH}?notebook_id=nb-1`]: { scope: "nb-1", progress: null } });
    expect(await screen.findByText("모든 노트북에서 요약을 만든 기록이 없습니다.")).toBeInTheDocument();

    await user.click(screen.getByRole("combobox", { name: "요약을 만들 범위" }));
    await user.click(await screen.findByRole("option", { name: "종목분석" }));
    expect(await screen.findByText("종목분석에서 요약을 만든 기록이 없습니다.")).toBeInTheDocument();

    api.post.mockResolvedValue({ scope: "nb-1", scheduled: true, progress: progress({ status: "queued", scope: "nb-1", notebook_id: "nb-1" }) });
    await user.click(screen.getByRole("button", { name: "요약 만들기…" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "만들기" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith(PATH, { notebook_id: "nb-1" }));
    await waitFor(() => expect(screen.getByTestId("digest-backfill-status")).toHaveTextContent("대기"));
    // 대기·진행 중이면 다시 시작할 수 없다
    expect(screen.getByRole("button", { name: "요약 만들기…" })).toBeDisabled();
  });

  it("진행 중이면 5초마다 다시 읽고, 끝나면 폴링을 멈추고 버튼을 푼다", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const replies = [
        { scope: "all", progress: progress({ status: "running", finished_at: null, generated: 10 }) },
        { scope: "all", progress: progress({ status: "running", finished_at: null, generated: 60 }) },
        { scope: "all", progress: progress() },
      ];
      api.get.mockImplementation(() => Promise.resolve(replies.shift() ?? { scope: "all", progress: progress() }));
      const client = new QueryClient({
        defaultOptions: { queries: { ...APP_QUERY_DEFAULTS, retry: false }, mutations: { retry: false } },
      });
      render(
        <QueryClientProvider client={client}>
          <SourceDigestBackfillSection notebooks={NOTEBOOKS} />
        </QueryClientProvider>,
      );
      const status = await screen.findByTestId("digest-backfill-status");
      await waitFor(() => expect(status).toHaveTextContent("진행 중"));
      expect(screen.getByRole("button", { name: "요약 만들기…" })).toBeDisabled();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });
      await waitFor(() => expect(status).toHaveTextContent("만듦 60"));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });
      await waitFor(() => expect(status).toHaveTextContent("끝남"));
      expect(screen.getByRole("button", { name: "요약 만들기…" })).toBeEnabled();

      const calls = api.get.mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(20000);
      });
      expect(api.get.mock.calls.length).toBe(calls); // 끝난 뒤에는 다시 읽지 않는다
    } finally {
      vi.useRealTimers();
    }
  });

  it("진행률 조회가 실패하면 안내와 「다시 확인」을 보인다", async () => {
    const user = userEvent.setup();
    api.get.mockRejectedValueOnce(new Error("503")).mockResolvedValue({ scope: "all", progress: progress() });
    const client = new QueryClient({
      defaultOptions: { queries: { ...APP_QUERY_DEFAULTS, retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <SourceDigestBackfillSection notebooks={NOTEBOOKS} />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("진행률을 읽지 못했습니다")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "다시 확인" }));
    await waitFor(() => expect(screen.getByTestId("digest-backfill-status")).toHaveTextContent("끝남"));
  });

  it("시작이 실패하면 오류 토스트를 띄우고 버튼은 다시 쓸 수 있다", async () => {
    const user = userEvent.setup();
    renderSection({ [PATH]: { scope: "all", progress: null } });
    await screen.findByText("모든 노트북에서 요약을 만든 기록이 없습니다.");
    api.post.mockRejectedValue(new Error("503"));
    await user.click(screen.getByRole("button", { name: "요약 만들기…" }));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "만들기" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "요약 만들기…" })).toBeEnabled();
  });
});
