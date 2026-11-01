import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useCrudResource } from "@/hooks/use-crud-resource";

// 네트워크 경계(`api`)만 바꾸고 `ApiError`는 실제 클래스 — 실패 토스트 규칙(`lib/notify`)이 실경로로 돈다.
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

import { api, ApiError } from "@/lib/api-client";
import { toast } from "sonner";

const mockGet = vi.mocked(api.get);
const mockPost = vi.mocked(api.post);
const mockPatch = vi.mocked(api.patch);
const mockDelete = vi.mocked(api.delete);

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

interface TestRow {
  id: string;
  enabled?: boolean;
}
type TestCreate = Partial<TestRow>;
type TestUpdate = { enabled?: boolean };

const OPTS = { queryKey: ["test-res"], basePath: "/api/v1/test" };

/** 제네릭을 고정한 헬퍼 — 인자 없는 `useCrudResource(OPTS)`는 `unknown`으로 추론된다. */
const renderCrud = (extra: Partial<Parameters<typeof useCrudResource>[0]> = {}) =>
  renderHook(
    () => useCrudResource<TestRow, TestCreate, TestUpdate>({ ...OPTS, ...extra }),
    { wrapper },
  );

afterEach(() => {
  vi.clearAllMocks();
});

describe("useCrudResource", () => {
  it("list는 basePath를 GET한다", async () => {
    mockGet.mockResolvedValue([{ id: "a" }]);
    const { result } = renderCrud();

    await waitFor(() => expect(result.current.list.isSuccess).toBe(true));
    expect(mockGet).toHaveBeenCalledWith("/api/v1/test");
  });

  it("update는 id를 경로로 빼고 나머지를 PATCH 본문으로 보낸다", async () => {
    mockGet.mockResolvedValue([]);
    mockPatch.mockResolvedValue({});
    const { result } = renderCrud();

    result.current.update.mutate({ id: "abc", enabled: false });

    await waitFor(() => expect(mockPatch).toHaveBeenCalled());
    expect(mockPatch).toHaveBeenCalledWith("/api/v1/test/abc", { enabled: false });
  });

  it("remove는 id를 경로에 붙여 DELETE한다", async () => {
    mockGet.mockResolvedValue([]);
    mockDelete.mockResolvedValue(undefined);
    const { result } = renderCrud();

    result.current.remove.mutate("xyz");

    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("/api/v1/test/xyz"));
  });

  it("messages를 준 뮤테이션만 성공 토스트를 띄운다", async () => {
    mockGet.mockResolvedValue([]);
    mockPost.mockResolvedValue({});
    mockDelete.mockResolvedValue(undefined);
    const { result } = renderCrud({ messages: { create: "만들었습니다" } });

    result.current.create.mutate({});
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("만들었습니다"));

    // remove에는 문구가 없으므로 추가 토스트가 없어야 한다.
    result.current.remove.mutate("x");
    await waitFor(() => expect(mockDelete).toHaveBeenCalled());
    expect(toast.success).toHaveBeenCalledTimes(1);
  });

  it("기본값은 에러를 lib/notify 규칙으로 토스트한다 — 제목은 「추가하지 못했습니다」, 서버 원문은 설명 둘째 줄", async () => {
    mockGet.mockResolvedValue([]);
    mockPost.mockRejectedValue(new ApiError(500, "upstream exploded"));
    const { result } = renderCrud();

    result.current.create.mutate({});
    await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(1));
    const [title, opts] = vi.mocked(toast.error).mock.calls[0] as [string, { description?: string }];
    expect(title).toBe("추가하지 못했습니다");
    expect(title).not.toContain("upstream exploded");
    expect(opts.description).toBe("잠시 후 다시 시도해 주세요\nupstream exploded");
  });

  it("기본 실패 토스트의 제목은 동작별(변경·삭제)이고, 알려진 상태는 서버 원문 대신 한국어 문구다", async () => {
    mockGet.mockResolvedValue([]);
    mockPatch.mockRejectedValue(new ApiError(403, "Forbidden: admin only"));
    mockDelete.mockRejectedValue(new ApiError(404, "MCPServer not found"));
    const { result } = renderCrud();

    result.current.update.mutate({ id: "a", enabled: false });
    await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(1));
    result.current.remove.mutate("b");
    await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(2));

    const calls = vi.mocked(toast.error).mock.calls as [string, { description?: string }][];
    expect(calls[0][0]).toBe("변경하지 못했습니다");
    expect(calls[0][1].description).toBe("이 작업을 할 권한이 없습니다.");
    expect(calls[1][0]).toBe("삭제하지 못했습니다");
    expect(calls[1][1].description).toBe("대상을 찾을 수 없습니다. 이미 삭제되었을 수 있습니다.");
    expect(JSON.stringify(calls)).not.toMatch(/Forbidden|not found/);
  });

  it("toastErrors:false면 훅 레벨 에러 토스트를 끈다 — 호출부의 onError와 중복 방지", async () => {
    // react-query v5는 훅 레벨과 mutate 레벨 onError를 **둘 다** 부른다(대체가 아님).
    // 이 플래그가 없으면 아래 호출부는 에러 토스트를 두 번 띄운다.
    mockGet.mockResolvedValue([]);
    mockPost.mockRejectedValue(new Error("터졌다"));
    const { result } = renderCrud({ toastErrors: false });

    const onError = vi.fn();
    result.current.create.mutate({}, { onError });

    await waitFor(() => expect(onError).toHaveBeenCalled());
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("alsoInvalidate에 준 키까지 함께 무효화한다", async () => {
    mockGet.mockResolvedValue([]);
    mockPatch.mockResolvedValue({});
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const spy = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(
      () => useCrudResource({ ...OPTS, alsoInvalidate: [["other"]] }),
      {
        wrapper: ({ children }) => (
          <QueryClientProvider client={client}>{children}</QueryClientProvider>
        ),
      },
    );

    result.current.update.mutate({ id: "a" });

    await waitFor(() => expect(spy).toHaveBeenCalledWith({ queryKey: ["other"] }));
    expect(spy).toHaveBeenCalledWith({ queryKey: ["test-res"] });
  });
});
