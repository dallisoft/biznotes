import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useGlobalSearch } from "@/hooks/use-global-search";

vi.mock("@/lib/api-client", () => ({
  api: { get: vi.fn() },
}));

import { api } from "@/lib/api-client";

const mockGet = vi.mocked(api.get);

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

afterEach(() => {
  vi.useRealTimers();
  mockGet.mockReset();
});

const EMPTY = { notebooks: [], sources: [], notes: [] };

describe("useGlobalSearch", () => {
  it("입력 후 300ms가 지나야 query가 반영된다 (디바운스)", async () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(({ q }) => useGlobalSearch(q), {
      wrapper,
      initialProps: { q: "녹" },
    });
    expect(result.current.query).toBe("");

    rerender({ q: "녹차" });
    act(() => vi.advanceTimersByTime(299));
    expect(result.current.query).toBe("");

    act(() => vi.advanceTimersByTime(1));
    expect(result.current.query).toBe("녹차");
  });

  it("타이핑이 이어지면 이전 타이머를 취소한다 (마지막 입력만 반영)", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(({ q }) => useGlobalSearch(q), {
      wrapper,
      initialProps: { q: "녹" },
    });
    act(() => vi.advanceTimersByTime(200));
    rerender({ q: "녹차" });
    act(() => vi.advanceTimersByTime(200));
    expect(result.current.query).toBe(""); // "녹"의 타이머는 취소됨
    act(() => vi.advanceTimersByTime(100));
    expect(result.current.query).toBe("녹차");
  });

  it("빈 입력(공백만)은 검색을 실행하지 않는다", async () => {
    vi.useFakeTimers();
    renderHook(() => useGlobalSearch("   "), { wrapper });
    act(() => vi.advanceTimersByTime(300));
    expect(mockGet).not.toHaveBeenCalled();
  });

  it("query 확정 시 인코딩된 검색 API를 호출하고 그룹 결과를 반환한다", async () => {
    mockGet.mockResolvedValue({
      ...EMPTY,
      notes: [{ id: "n1", notebook_id: "nb1", title: "녹차 메모", snippet: "70~80도" }],
    });
    vi.useFakeTimers();
    const { result } = renderHook(() => useGlobalSearch("녹차 온도"), { wrapper });
    act(() => vi.advanceTimersByTime(300));
    vi.useRealTimers();

    await waitFor(() => expect(result.current.results.data).toBeDefined());
    expect(mockGet).toHaveBeenCalledWith(
      `/api/v1/search?q=${encodeURIComponent("녹차 온도")}`,
    );
    expect(result.current.results.data?.notes[0].title).toBe("녹차 메모");
  });
});
