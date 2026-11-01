/**
 * 노트북 변경 훅의 캐시 정책(plan §6.6.2 노트북 작업 화면 행 — r1 PR-09, r2 보조 의견: 태그 응답 void).
 * 네트워크 경계(`api`)만 대체하고 훅·TanStack Query 캐시는 실제다.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  evictNotebookQueries,
  mergeNotebookPatch,
  notebookScopedQueryKeys,
  useDeleteNotebook,
  useToggleFavorite,
  useUpdateNotebook,
} from "@/hooks/use-notebooks";
import { useAttachTag, useDeleteTag, useDetachTag } from "@/hooks/use-tags";
import type { Notebook } from "@/lib/types";

const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));

afterEach(() => vi.resetAllMocks());

function nb(id: string, overrides: Partial<Notebook> = {}): Notebook {
  return {
    id,
    owner_id: "me",
    title: `노트북 ${id}`,
    emoji: "📓",
    description: "",
    visibility: "private",
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    source_count: 4,
    is_owner: true,
    permission: "owner",
    tags: ["연구"],
    is_favorite: true,
    ...overrides,
  };
}

function setup() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const invalidate = vi.spyOn(client, "invalidateQueries");
  return { client, wrapper, invalidate };
}

describe("mergeNotebookPatch", () => {
  it("바뀐 필드(제목·이모지·설명·공개 범위·수정 시각)만 덮고, PATCH 응답이 기본값으로 주는 계산 필드는 기존 값을 둔다", () => {
    const prev = nb("a");
    const patched = nb("a", {
      title: "새 이름",
      emoji: "☕",
      description: "설명",
      updated_at: "2026-10-15T00:00:00Z",
      source_count: 0,
      tags: [],
      is_favorite: false,
    });
    expect(mergeNotebookPatch(prev, patched)).toEqual({
      ...prev,
      title: "새 이름",
      emoji: "☕",
      description: "설명",
      updated_at: "2026-10-15T00:00:00Z",
    });
  });

  it("캐시가 없으면 새로 만들지 않는다(undefined)", () => {
    expect(mergeNotebookPatch(undefined, nb("a"))).toBeUndefined();
  });
});

describe("useUpdateNotebook", () => {
  it("성공 시 상세·목록 캐시의 그 항목을 곧바로 고치고(다른 항목은 그대로), 상세·목록을 무효화한다", async () => {
    const { client, wrapper, invalidate } = setup();
    client.setQueryData(["notebook", "a"], nb("a"));
    client.setQueryData(["notebooks", "all"], [nb("a"), nb("b")]);
    client.setQueryData(["notebooks", "mine"], [nb("a")]);
    api.patch.mockResolvedValue(nb("a", { title: "새 이름", source_count: 0, tags: [], is_favorite: false }));

    const { result } = renderHook(() => useUpdateNotebook(), { wrapper });
    await act(() => result.current.mutateAsync({ id: "a", title: "새 이름" }));

    expect(api.patch).toHaveBeenCalledWith("/api/v1/notebooks/a", { title: "새 이름" });
    expect(client.getQueryData<Notebook>(["notebook", "a"])).toMatchObject({
      title: "새 이름",
      tags: ["연구"],
      source_count: 4,
      is_favorite: true,
    });
    expect(client.getQueryData<Notebook[]>(["notebooks", "all"])?.map((n) => n.title)).toEqual([
      "새 이름",
      "노트북 b",
    ]);
    expect(client.getQueryData<Notebook[]>(["notebooks", "mine"])?.[0].tags).toEqual(["연구"]);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["notebook", "a"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["notebooks"] });
  });

  it("전체 공개 토글(core-v0.20.0 AC-3.2) — 본문은 정확히 {visibility}이고 목록·상세 캐시의 visibility만 바뀐다", async () => {
    const { client, wrapper, invalidate } = setup();
    client.setQueryData(["notebook", "a"], nb("a"));
    client.setQueryData(["notebooks", "all"], [nb("a"), nb("b")]);
    api.patch.mockResolvedValue(nb("a", { visibility: "public", source_count: 0, tags: [], is_favorite: false }));

    const { result } = renderHook(() => useUpdateNotebook(), { wrapper });
    await act(() => result.current.mutateAsync({ id: "a", visibility: "public" }));

    expect(api.patch.mock.calls).toStrictEqual([["/api/v1/notebooks/a", { visibility: "public" }]]);
    expect(client.getQueryData<Notebook>(["notebook", "a"])).toMatchObject({
      visibility: "public",
      tags: ["연구"],
      source_count: 4,
      is_favorite: true,
    });
    expect(client.getQueryData<Notebook[]>(["notebooks", "all"])?.map((n) => n.visibility)).toEqual([
      "public",
      "private",
    ]);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["notebook", "a"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["notebooks"] });
  });

  it("상세 캐시가 없으면 PATCH 응답(계산 필드가 기본값)으로 상세를 새로 만들지 않는다", async () => {
    const { client, wrapper } = setup();
    api.patch.mockResolvedValue(nb("a", { source_count: 0, tags: [] }));
    const { result } = renderHook(() => useUpdateNotebook(), { wrapper });
    await act(() => result.current.mutateAsync({ id: "a", title: "x" }));
    expect(client.getQueryData(["notebook", "a"])).toBeUndefined();
  });

  it("실패하면 캐시를 바꾸지 않는다", async () => {
    const { client, wrapper } = setup();
    client.setQueryData(["notebook", "a"], nb("a"));
    api.patch.mockRejectedValue(new Error("boom"));
    const { result } = renderHook(() => useUpdateNotebook(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ id: "a", title: "x" }).catch(() => {});
    });
    expect(client.getQueryData<Notebook>(["notebook", "a"])?.title).toBe("노트북 a");
  });
});

describe("useDeleteNotebook — 홈 카드 메뉴와 노트북 헤더가 같은 경로(PR-09)", () => {
  it("성공 시 모든 목록 캐시에서 그 항목을 빼고, 상세·하위 쿼리·대화별 메시지를 지운 뒤 목록을 무효화한다(다른 노트북은 그대로)", async () => {
    const { client, wrapper, invalidate } = setup();
    client.setQueryData(["notebooks", "all"], [nb("a"), nb("b")]);
    client.setQueryData(["notebooks", "shared"], [nb("a")]);
    for (const key of notebookScopedQueryKeys("a")) client.setQueryData(key, { cached: true });
    client.setQueryData(["chat-sessions", "a"], [{ id: "s1" }, { id: "s2" }]);
    client.setQueryData(["chat-messages", "s1"], []);
    client.setQueryData(["chat-messages", "s2"], []);
    // 다른 노트북의 캐시는 건드리지 않는다
    client.setQueryData(["notebook", "b"], nb("b"));
    client.setQueryData(["sources", "b"], []);
    client.setQueryData(["chat-messages", "other"], []);
    api.delete.mockResolvedValue(undefined);

    const { result } = renderHook(() => useDeleteNotebook(), { wrapper });
    await act(() => result.current.mutateAsync("a"));

    expect(api.delete).toHaveBeenCalledWith("/api/v1/notebooks/a");
    expect(client.getQueryData<Notebook[]>(["notebooks", "all"])?.map((n) => n.id)).toEqual(["b"]);
    expect(client.getQueryData<Notebook[]>(["notebooks", "shared"])).toEqual([]);
    for (const key of [
      ...notebookScopedQueryKeys("a"),
      ["chat-messages", "s1"],
      ["chat-messages", "s2"],
    ]) {
      expect(client.getQueryCache().find({ queryKey: key, exact: true }), JSON.stringify(key)).toBeUndefined();
    }
    expect(client.getQueryData(["notebook", "b"])).toBeDefined();
    expect(client.getQueryData(["sources", "b"])).toBeDefined();
    expect(client.getQueryData(["chat-messages", "other"])).toBeDefined();
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["notebooks"] });
  });

  it("실패하면(403 등) 캐시를 지우지 않는다", async () => {
    const { client, wrapper } = setup();
    client.setQueryData(["notebook", "a"], nb("a"));
    client.setQueryData(["notebooks", "all"], [nb("a")]);
    api.delete.mockRejectedValue(new Error("forbidden"));
    const { result } = renderHook(() => useDeleteNotebook(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync("a").catch(() => {});
    });
    expect(client.getQueryData(["notebook", "a"])).toBeDefined();
    expect(client.getQueryData<Notebook[]>(["notebooks", "all"])).toHaveLength(1);
  });

  it("evictNotebookQueries는 진행 중 요청을 취소하고 지운다", async () => {
    const { client } = setup();
    let aborted = false;
    void client.fetchQuery({
      queryKey: ["sources", "a"],
      queryFn: ({ signal }) =>
        new Promise((_resolve, reject) => {
          signal.addEventListener("abort", () => {
            aborted = true;
            reject(new Error("aborted"));
          });
        }),
    }).catch(() => {});
    evictNotebookQueries(client, "a");
    await waitFor(() => expect(aborted).toBe(true));
    expect(client.getQueryCache().find({ queryKey: ["sources", "a"] })).toBeUndefined();
  });
});

describe("useToggleFavorite", () => {
  it("성공 시 목록과 그 노트북 상세를 무효화한다", async () => {
    const { wrapper, invalidate } = setup();
    api.post.mockResolvedValue(undefined);
    const { result } = renderHook(() => useToggleFavorite(), { wrapper });
    await act(() => result.current.mutateAsync({ id: "a", favorite: true }));
    expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/a/favorite");
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["notebooks"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["notebook", "a"] });
  });
});

describe("태그 훅 — 응답이 void라 상세·목록을 무효화해 다시 읽는다", () => {
  it.each([
    ["useAttachTag", "post"],
    ["useDetachTag", "delete"],
  ] as const)("%s: 그 노트북 상세와 목록을 무효화한다", async (hook, method) => {
    const { wrapper, invalidate } = setup();
    api[method].mockResolvedValue(undefined);
    const { result } = renderHook(() => (hook === "useAttachTag" ? useAttachTag("a") : useDetachTag("a")), {
      wrapper,
    });
    await act(() => result.current.mutateAsync("t1"));
    expect(api[method]).toHaveBeenCalledWith("/api/v1/notebooks/a/tags/t1");
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["notebooks"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["notebook", "a"] });
  });

  it("useDeleteTag: 태그·목록과 모든 노트북 상세(접두 「notebook」)를 무효화한다", async () => {
    const { client, wrapper, invalidate } = setup();
    client.setQueryData(["notebook", "a"], nb("a"));
    client.setQueryData(["notebook", "b"], nb("b"));
    api.delete.mockResolvedValue(undefined);
    const { result } = renderHook(() => useDeleteTag(), { wrapper });
    await act(() => result.current.mutateAsync("t1"));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["tags"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["notebooks"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["notebook"] });
    expect(client.getQueryState(["notebook", "a"])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["notebook", "b"])?.isInvalidated).toBe(true);
  });
});
