/**
 * 공유 관리 훅(core-v0.20.0 P3 — AC-3.1·AC-3.4). 네트워크 경계(`api`)만 대체하고 훅·TanStack Query 캐시는 실제다.
 * 대화상자를 거친 흐름은 `components/dashboard/share-notebook-dialog.test.tsx`가 본다 — 여기서는 캐시 갱신 규칙과
 * 조회 조건(열려 있을 때만)을 직접 고정한다.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useMyGroups } from "@/hooks/use-my-groups";
import {
  upsertShareRow,
  useNotebookShares,
  useRevokeNotebookShare,
  useShareWithGroup,
} from "@/hooks/use-notebook-shares";
import { ApiError } from "@/lib/api-client";
import type { NotebookShare } from "@/lib/types";

const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));

afterEach(() => vi.resetAllMocks());

const row = (id: string, permission: "read" | "write", group = "g1"): NotebookShare => ({
  id,
  notebook_id: "nb1",
  group_id: group,
  permission,
});

function setup() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const invalidate = vi.spyOn(client, "invalidateQueries");
  return { client, wrapper, invalidate };
}

describe("upsertShareRow", () => {
  it("같은 id면 그 행을 바꾸고(같은 그룹 재공유는 처음 행 id — 코어 #222) 순서를 유지한다", () => {
    const rows = [row("s1", "read"), row("s2", "read", "g2")];
    expect(upsertShareRow(rows, row("s1", "write"))).toEqual([row("s1", "write"), row("s2", "read", "g2")]);
  });

  it("새 id면 뒤에 붙이고, 캐시가 없으면 만들지 않는다", () => {
    expect(upsertShareRow([row("s1", "read")], row("s9", "read", "g9"))).toEqual([
      row("s1", "read"),
      row("s9", "read", "g9"),
    ]);
    expect(upsertShareRow(undefined, row("s1", "read"))).toBeUndefined();
  });
});

describe("조회 조건 — 열려 있을 때만 읽는다", () => {
  it("useMyGroups(false)·useNotebookShares(null)는 요청하지 않고, 켜면 /me/groups·/shares를 읽는다", async () => {
    const { wrapper } = setup();
    api.get.mockImplementation((path: string) =>
      Promise.resolve(path === "/api/v1/me/groups" ? [{ id: "g1", name: "연구팀" }] : [row("s1", "read")]),
    );
    const off = renderHook(() => [useMyGroups(false), useNotebookShares(null)] as const, { wrapper });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(api.get).not.toHaveBeenCalled();
    expect(off.result.current[0].fetchStatus).toBe("idle");

    const on = renderHook(() => [useMyGroups(true), useNotebookShares("nb1")] as const, { wrapper });
    await waitFor(() => expect(on.result.current[1].data).toEqual([row("s1", "read")]));
    expect(on.result.current[0].data).toEqual([{ id: "g1", name: "연구팀" }]);
    expect(api.get.mock.calls.map(([p]) => p).sort()).toEqual(["/api/v1/me/groups", "/api/v1/notebooks/nb1/shares"]);
  });
});

describe("useShareWithGroup — 본문·캐시·무효화(AC-3.1·AC-3.4)", () => {
  it("본문은 정확히 {group_id, permission}이고, 응답 행으로 그 노트북의 공유 목록 캐시를 고친 뒤 그 목록만 무효화한다", async () => {
    const { client, wrapper, invalidate } = setup();
    client.setQueryData(["notebook-shares", "nb1"], [row("s1", "read")]);
    client.setQueryData(["notebook-shares", "nb2"], [row("x", "read")]);
    api.post.mockResolvedValue(row("s1", "write"));
    const { result } = renderHook(() => useShareWithGroup(), { wrapper });
    await act(() => result.current.mutateAsync({ notebookId: "nb1", groupId: "g1", permission: "write" }));

    expect(api.post.mock.calls).toStrictEqual([["/api/v1/notebooks/nb1/share", { group_id: "g1", permission: "write" }]]);
    expect(client.getQueryData(["notebook-shares", "nb1"])).toEqual([row("s1", "write")]);
    expect(client.getQueryData(["notebook-shares", "nb2"])).toEqual([row("x", "read")]);
    expect(invalidate.mock.calls).toEqual([[{ queryKey: ["notebook-shares", "nb1"] }]]);
  });

  it("실패하면 그 목록을 다시 읽고, 그룹 404면 그룹 목록도 다시 읽는다 — 그 밖의 실패는 그룹 목록을 건드리지 않는다", async () => {
    const { wrapper, invalidate } = setup();
    const { result } = renderHook(() => useShareWithGroup(), { wrapper });
    api.post.mockRejectedValueOnce(new ApiError(404, "Target group not found"));
    await act(async () => {
      await result.current.mutateAsync({ notebookId: "nb1", groupId: "g1", permission: "read" }).catch(() => {});
    });
    expect(invalidate.mock.calls).toEqual([
      [{ queryKey: ["notebook-shares", "nb1"] }],
      [{ queryKey: ["my-groups"] }],
    ]);

    invalidate.mockClear();
    api.post.mockRejectedValueOnce(new ApiError(500, "boom"));
    await act(async () => {
      await result.current.mutateAsync({ notebookId: "nb1", groupId: "g1", permission: "read" }).catch(() => {});
    });
    expect(invalidate.mock.calls).toEqual([[{ queryKey: ["notebook-shares", "nb1"] }]]);
  });
});

describe("useRevokeNotebookShare — 그 행 id로만 DELETE(AC-3.1)", () => {
  it("성공하면 그 행만 캐시에서 빼고 그 목록만 무효화한다", async () => {
    const { client, wrapper, invalidate } = setup();
    client.setQueryData(["notebook-shares", "nb1"], [row("s1", "read"), row("s2", "write", "g2")]);
    api.delete.mockResolvedValue(undefined);
    const { result } = renderHook(() => useRevokeNotebookShare(), { wrapper });
    await act(() => result.current.mutateAsync({ notebookId: "nb1", shareId: "s2" }));
    expect(api.delete.mock.calls).toEqual([["/api/v1/notebooks/nb1/shares/s2"]]);
    expect(client.getQueryData(["notebook-shares", "nb1"])).toEqual([row("s1", "read")]);
    expect(invalidate.mock.calls).toEqual([[{ queryKey: ["notebook-shares", "nb1"] }]]);
  });

  it("실패하면 캐시는 그대로 두고 그 목록을 다시 읽는다", async () => {
    const { client, wrapper, invalidate } = setup();
    client.setQueryData(["notebook-shares", "nb1"], [row("s1", "read")]);
    api.delete.mockRejectedValue(new ApiError(404, "Share not found"));
    const { result } = renderHook(() => useRevokeNotebookShare(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ notebookId: "nb1", shareId: "s1" }).catch(() => {});
    });
    expect(client.getQueryData(["notebook-shares", "nb1"])).toEqual([row("s1", "read")]);
    expect(invalidate.mock.calls).toEqual([[{ queryKey: ["notebook-shares", "nb1"] }]]);
  });
});
