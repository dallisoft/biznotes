import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useChangePassword, useLogin } from "@/hooks/use-auth";
import { ApiError } from "@/lib/api-client";
import type { User } from "@/lib/types";

// API 경계만 mock — api 객체만 대체하고 ApiError 등은 실제 모듈을 쓴다(AC-3.7).
const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
const nav = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => nav }));

const USER: User = {
  id: "u1",
  email: "u@example.com",
  name: "U",
  role: "user",
  is_active: true,
  must_change_password: false,
  created_at: "2026-10-10T00:00:00Z",
};

function setup() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return { queryClient, wrapper };
}

afterEach(() => {
  vi.resetAllMocks();
});

describe("useLogin — 강제 변경 분기·redirect 헬퍼 (AC-3.2, AC-3.3)", () => {
  it("must_change_password=false면 safeInternalPath를 거친 redirect로 간다", async () => {
    api.post.mockResolvedValue({ user: USER });
    const { wrapper, queryClient } = setup();
    const { result } = renderHook(() => useLogin(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ email: "e", password: "p", redirect: "/notebooks/1?s=2" });
    });

    expect(nav.push).toHaveBeenCalledWith("/notebooks/1?s=2");
    expect(queryClient.getQueryData(["me"])).toEqual(USER);
  });

  it("must_change_password=true면 redirect 대신 변경 화면으로 가고 원래 목적지를 싣는다", async () => {
    api.post.mockResolvedValue({ user: { ...USER, must_change_password: true } });
    const { wrapper } = setup();
    const { result } = renderHook(() => useLogin(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ email: "e", password: "p", redirect: "/workflows" });
    });

    expect(nav.push).toHaveBeenCalledWith(
      `/change-password?redirect=${encodeURIComponent("/workflows")}`,
    );
  });

  it.each([
    ["//attacker.example/p"],
    ["/a/..//attacker.example/p"],
    ["/%2e%2e//attacker.example/p"],
    ["https://attacker.example"],
    ["/\\attacker.example"],
  ])("외부로 새는 redirect(%s)는 /로 바꾼다 — 기존 safeRedirectTarget 교체 회귀", async (redirect) => {
    api.post.mockResolvedValue({ user: USER });
    const { wrapper } = setup();
    const { result } = renderHook(() => useLogin(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ email: "e", password: "p", redirect });
    });

    expect(nav.push).toHaveBeenCalledWith("/");
  });

  it("구 API 응답(키 없음)은 강제 변경으로 읽지 않는다", async () => {
    const legacy: Record<string, unknown> = { ...USER };
    delete legacy.must_change_password;
    api.post.mockResolvedValue({ user: legacy });
    const { wrapper } = setup();
    const { result } = renderHook(() => useLogin(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ email: "e", password: "p", redirect: null });
    });

    expect(nav.push).toHaveBeenCalledWith("/");
  });
});

describe("useChangePassword (AC-3.3)", () => {
  it("POST /api/v1/auth/password로 보내고 성공 응답의 user로 me 캐시를 갱신한다", async () => {
    const changed = { ...USER, must_change_password: false };
    api.post.mockResolvedValue({ access_token: "a", refresh_token: "r", token_type: "bearer", user: changed });
    const { wrapper, queryClient } = setup();
    queryClient.setQueryData(["me"], { ...USER, must_change_password: true });
    const { result } = renderHook(() => useChangePassword(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ current_password: "old-pass1", new_password: "new-pass12" });
    });

    expect(api.post).toHaveBeenCalledWith("/api/v1/auth/password", {
      current_password: "old-pass1",
      new_password: "new-pass12",
    });
    expect(queryClient.getQueryData(["me"])).toEqual(changed);
    expect(nav.push).not.toHaveBeenCalled();
  });

  it("실패하면 캐시를 바꾸지 않고 ApiError를 그대로 낸다", async () => {
    const before = { ...USER, must_change_password: true };
    api.post.mockRejectedValue(new ApiError(400, "x", undefined, "invalid_current_password"));
    const { wrapper, queryClient } = setup();
    queryClient.setQueryData(["me"], before);
    const { result } = renderHook(() => useChangePassword(), { wrapper });

    act(() => {
      result.current.mutate({ current_password: "a", new_password: "b" });
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe("invalid_current_password");
    expect(queryClient.getQueryData(["me"])).toEqual(before);
  });
});
