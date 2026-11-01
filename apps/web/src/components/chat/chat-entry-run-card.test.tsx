import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatEntryRunCard } from "@/components/chat/chat-entry-run-card";
import type { AgentRun } from "@/lib/agent-types";
import type { ChatEntryRunRef } from "@/lib/chat-types";

// 실제 카드 → 실제 훅(react-query) → 실제 api-client. 대역은 네트워크(fetch)와 알림뿐이다.
vi.mock("sonner", () => ({ toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() }) }));
// 산출물 뷰어 묶음의 마인드맵 CSS — 다른 산출물 테스트와 같은 처리
vi.mock("@xyflow/react/dist/style.css", () => ({}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

const REF: ChatEntryRunRef = {
  run_id: "r1",
  agent_id: "agent-brief",
  display_name: "브리핑",
  slash_name: "brief",
  status: "running",
};

function runOf(over: Partial<AgentRun>): AgentRun {
  return {
    id: "r1",
    agent_id: "agent-brief",
    notebook_id: "nb1",
    params: { topic: "q" },
    input_source_ids: null,
    trigger: "chat",
    status: "running",
    progress: 0,
    result_summary: null,
    artifact_keys: [],
    token_usage: {},
    error: null,
    created_at: "2026-10-28T00:00:00Z",
    started_at: null,
    finished_at: null,
    skill_version_id: null,
    model_id: null,
    agent_name: "brief",
    agent_icon: null,
    can_cancel: true,
    can_hide: false,
    used_models: [],
    fallback_used: false,
    ...over,
  } as AgentRun;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

let currentRun: AgentRun = runOf({});
const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
  const url = String(input);
  if (url.endsWith("/agent-runs/r1/cancel") && init?.method === "POST") {
    currentRun = runOf({ status: "cancelled" });
    return json(currentRun);
  }
  if (url.endsWith("/agent-runs/r1/approvals")) {
    return json([
      {
        id: "ap1",
        run_id: "r1",
        tool_name: "http_request",
        arguments_summary: { url: "https://example.com" },
        status: "pending",
        resolved_by: null,
        requested_at: "2026-10-28T00:00:00Z",
        resolved_at: null,
        expires_at: "2026-10-28T00:10:00Z",
      },
    ]);
  }
  if (url.includes("/agent-runs/r1/artifacts/")) {
    return new Response("# 결과 본문", { status: 200, headers: { "Content-Type": "text/markdown" } });
  }
  if (url.endsWith("/agent-runs/r1")) return json(currentRun);
  return json([]);
});

function renderCard(entryRun: ChatEntryRunRef = REF) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  render(
    <QueryClientProvider client={client}>
      <ChatEntryRunCard entryRun={entryRun} sessionId="s1" notebookId="nb1" canWrite />
    </QueryClientProvider>,
  );
  return { invalidate };
}

type InvalidateSpy = ReturnType<typeof renderCard>["invalidate"];

const messagesInvalidated = (spy: InvalidateSpy) =>
  spy.mock.calls.some(
    ([filters]) => JSON.stringify((filters as { queryKey?: unknown } | undefined)?.queryKey) === '["chat-messages","s1"]',
  );

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  currentRun = runOf({});
});

afterEach(() => {
  fetchMock.mockClear();
  vi.unstubAllGlobals();
});

describe("ChatEntryRunCard — run 카드 상태(AC-10)", () => {
  it.each([
    ["queued", "대기"],
    ["running", "실행 중"],
    ["awaiting_approval", "승인 대기"],
    ["succeeded", "완료"],
    ["failed", "실패"],
    ["cancelled", "중지"],
  ] as const)("%s → 「%s」", async (status, label) => {
    currentRun = runOf({ status });
    renderCard({ ...REF, status });
    await waitFor(() => expect(screen.getByTestId("entry-run-status")).toHaveTextContent(label));
    expect(screen.getByText("브리핑")).toBeInTheDocument();
    expect(screen.getByText("/brief")).toBeInTheDocument();
    // 「중지」 버튼은 끝나지 않은 run에만
    const stop = screen.queryByRole("button", { name: "중지" });
    if (["queued", "running", "awaiting_approval"].includes(status)) expect(stop).toBeInTheDocument();
    else expect(stop).not.toBeInTheDocument();
  });

  it("승인 대기면 기존 승인 카드를 그대로 보인다", async () => {
    currentRun = runOf({ status: "awaiting_approval" });
    renderCard({ ...REF, status: "awaiting_approval" });
    expect(await screen.findByTestId("approval-card-r1")).toHaveTextContent("http_request");
    expect(screen.getByTestId("approve-btn-r1")).toBeInTheDocument();
  });

  it("「중지」는 run을 취소하고 run·메시지를 다시 읽는다", async () => {
    const user = userEvent.setup();
    const { invalidate } = renderCard();
    await user.click(await screen.findByRole("button", { name: "중지" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([u, i]) => String(u).endsWith("/agent-runs/r1/cancel") && i?.method === "POST"),
      ).toBe(true),
    );
    await waitFor(() => expect(messagesInvalidated(invalidate)).toBe(true));
  });

  it("목록이 끝나지 않은 run으로 알고 있는데 run이 끝났으면 메시지를 다시 읽는다(결과 행·잠금 해제)", async () => {
    currentRun = runOf({ status: "succeeded" });
    const { invalidate } = renderCard({ ...REF, status: "running" });
    await waitFor(() => expect(messagesInvalidated(invalidate)).toBe(true));
  });

  it("이미 끝난 것으로 알고 있으면 메시지를 다시 읽지 않는다", async () => {
    currentRun = runOf({ status: "succeeded" });
    const { invalidate } = renderCard({ ...REF, status: "succeeded" });
    await waitFor(() => expect(screen.getByTestId("entry-run-status")).toHaveTextContent("완료"));
    expect(messagesInvalidated(invalidate)).toBe(false);
  });

  it("run을 읽지 못하면 상태를 알리고, 4xx면 볼 수 없다고 알린다(리뷰 Medium)", async () => {
    const original = fetchMock.getMockImplementation()!;
    try {
      fetchMock.mockImplementation(async (input: string, init?: RequestInit) =>
        String(input).endsWith("/agent-runs/r1") ? json({ detail: "boom" }, 500) : original(input, init),
      );
      renderCard();
      expect(await screen.findByTestId("entry-run-error")).toHaveTextContent(
        "실행 상태를 불러오지 못했습니다 — 잠시 뒤 다시 확인합니다",
      );
      expect(screen.getByTestId("entry-run-status")).toHaveTextContent("실행 중"); // 목록이 아는 상태

      fetchMock.mockImplementation(async (input: string, init?: RequestInit) =>
        String(input).endsWith("/agent-runs/r1") ? json({ detail: "Run not found" }, 404) : original(input, init),
      );
      cleanup();
      renderCard();
      expect(await screen.findByTestId("entry-run-error")).toHaveTextContent("실행 정보를 볼 수 없습니다");
    } finally {
      fetchMock.mockImplementation(original);
    }
  });

  it("산출물 링크를 누르면 기존 뷰어로 본문을 연다", async () => {
    const user = userEvent.setup();
    currentRun = runOf({
      status: "succeeded",
      artifact_keys: [{ key: "r1/result.md", name: "result.md", content_type: "text/markdown" }],
    });
    renderCard({ ...REF, status: "succeeded" });
    await user.click(await screen.findByRole("button", { name: "result.md" }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(await screen.findByText("결과 본문")).toBeInTheDocument();
  });
});
