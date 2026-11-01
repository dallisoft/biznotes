/**
 * 업로드 최대 크기 조회 훅(코어 v0.26.0 #388, #228 AC-13) — `use-sources`의 `useUploadLimits`. 네트워크 경계(`api`)만
 * 대체하고 훅·캐시는 실제로 돌린다.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useResolveUploadLimit, useUploadLimits } from "@/hooks/use-sources";
import { qk } from "@/lib/query-keys";

const { api } = vi.hoisted(() => ({ api: { get: vi.fn() } }));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));

afterEach(() => api.get.mockReset());

function wrapper(client: QueryClient) {
  function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  }
  return Wrapper;
}

describe("useUploadLimits", () => {
  it("공개 조회 경로를 부르고 관리자 저장이 무효화하는 키(qk.uploadLimits)에 둔다", async () => {
    api.get.mockResolvedValue({ max_upload_bytes: 52_428_800 });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useUploadLimits(), { wrapper: wrapper(client) });
    await waitFor(() => expect(result.current.data).toEqual({ max_upload_bytes: 52_428_800 }));
    expect(api.get).toHaveBeenCalledWith("/api/v1/sources/upload-limits");
    expect(client.getQueryData(qk.uploadLimits.all)).toEqual({ max_upload_bytes: 52_428_800 });
  });

  it("꺼 두면(닫힌 다이얼로그) 부르지 않는다", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useUploadLimits(false), { wrapper: wrapper(client) });
    await new Promise((r) => setTimeout(r, 0));
    expect(result.current.data).toBeUndefined();
    expect(api.get).not.toHaveBeenCalled();
  });
});

describe("useResolveUploadLimit — 업로드 직전 한도 확정", () => {
  it("처음 조회가 진행 중이면 새로 부르지 않고 그 요청을 기다린다", async () => {
    let respond!: (body: { max_upload_bytes: number }) => void;
    api.get.mockReturnValue(new Promise((r) => (respond = r)));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => ({ limits: useUploadLimits(), resolve: useResolveUploadLimit() }), {
      wrapper: wrapper(client),
    });
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));
    expect(result.current.limits.isPending).toBe(true);

    const pending = result.current.resolve();
    respond({ max_upload_bytes: 10 * 1024 * 1024 });
    await expect(pending).resolves.toBe(10 * 1024 * 1024);
    expect(api.get).toHaveBeenCalledTimes(1);
  });

  it("캐시가 있으면 부르지 않고 그 값을 쓴다", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(qk.uploadLimits.all, { max_upload_bytes: 123 });
    const { result } = renderHook(() => useResolveUploadLimit(), { wrapper: wrapper(client) });
    await expect(result.current()).resolves.toBe(123);
    expect(api.get).not.toHaveBeenCalled();
  });

  it("조회에 실패하면 undefined — 호출부는 사전 검사 없이 보낸다", async () => {
    api.get.mockRejectedValue(new Error("down"));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useResolveUploadLimit(), { wrapper: wrapper(client) });
    await expect(result.current()).resolves.toBeUndefined();
  });
});
