import { QueryClient, QueryClientProvider, focusManager, onlineManager } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AdminProvider } from "@/lib/admin-types";
import { EmbeddingGateButton } from "./embedding-gate-button";

const { api, ApiError } = vi.hoisted(() => {
  class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  }
  return {
    api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
    ApiError,
  };
});

vi.mock("@/lib/api-client", () => ({ api, ApiError }));

const PROVIDER: AdminProvider = {
  id: "provider-1",
  name: "emb",
  type: "openai",
  base_url: null,
  has_api_key: true,
  enabled: true,
  model_count: 0,
  embedding_max_concurrency: 3,
};

const GATE_PATH = "/api/v1/admin/llm/providers/provider-1/embedding-gate";

// 프로덕션 기본(providers.tsx `retry: 1`)과 같게 둔다 — 저장소 관례인 `retry: false`
// 테스트 클라이언트 위에서는 훅의 `retry: false`가 빠져도 재시도가 없어 무재시도 검증이 공허하다
function renderButton(provider: AdminProvider = PROVIDER) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: 1 } } });
  return render(
    <QueryClientProvider client={client}>
      <EmbeddingGateButton provider={provider} />
    </QueryClientProvider>,
  );
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("EmbeddingGateButton", () => {
  it("상한이 없거나 필드가 없으면 아무것도 렌더하지 않고 조회하지 않는다", () => {
    const { container, unmount } = renderButton({ ...PROVIDER, embedding_max_concurrency: null });
    expect(container).toBeEmptyDOMElement();
    unmount();
    const legacy = { ...PROVIDER };
    delete legacy.embedding_max_concurrency;
    expect(renderButton(legacy).container).toBeEmptyDOMElement();
    expect(api.get).not.toHaveBeenCalled();
  });

  it("렌더만으로는 조회하지 않고, 클릭 1회에 1회 조회한 뒤 자동 재조회하지 않는다", async () => {
    api.get.mockResolvedValue({
      id: "provider-1",
      name: "emb",
      limit: 3,
      in_use: 0,
      earliest_expiry_in: null,
      last_error: null,
    });
    renderButton();
    expect(screen.getByText("임베딩 상한 3")).toBeInTheDocument();
    await advance(15_000);
    expect(api.get).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "게이트 상태" }));
    expect(await screen.findByText("0/3")).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledTimes(1);
    expect(api.get).toHaveBeenCalledWith(GATE_PATH);

    await advance(60_000);
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
      onlineManager.setOnline(false);
      onlineManager.setOnline(true);
    });
    await advance(1_000);
    expect(api.get).toHaveBeenCalledTimes(1);
  });

  it("포화 상태는 강조하고 last_error는 경고로 보인다", async () => {
    api.get.mockResolvedValue({
      id: "provider-1",
      name: "emb",
      limit: 3,
      in_use: 3,
      earliest_expiry_in: 12.34,
      last_error: "timeout",
    });
    renderButton();
    fireEvent.click(screen.getByRole("button", { name: "게이트 상태" }));
    const usage = await screen.findByText("3/3");
    expect(usage.className).toContain("text-destructive");
    expect(screen.getByText("가장 이른 해제까지 12.3초")).toBeInTheDocument();
    expect(screen.getByText("Redis 오류: timeout")).toBeInTheDocument();
  });

  it("503이면 확인 불가를 보이고 재시도하지 않는다", async () => {
    api.get.mockRejectedValue(new ApiError(503, "redis down"));
    renderButton();
    fireEvent.click(screen.getByRole("button", { name: "게이트 상태" }));
    expect(await screen.findByText("게이트 상태 확인 불가(Redis)")).toBeInTheDocument();
    await advance(2_500);
    expect(api.get).toHaveBeenCalledTimes(1);
  });
});
