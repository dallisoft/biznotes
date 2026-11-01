import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { API_BASE_URL } from "@/lib/api";
import { onPasswordChangeRequired, PASSWORD_CHANGE_REQUIRED } from "@/lib/api-client";
import { BULK_MESSAGES, type BulkTarget } from "@/lib/notebook-bulk";
import { BULK_CONCURRENCY, runBulkDelete, useBulkDelete } from "./use-notebook-bulk";

// AC-2.4·AC-2.5·AC-2.7 — runBulkDelete·useBulkDelete·runBounded·classifyError·api-client 본문을 그대로
// 실행하고 네트워크 경계(global fetch)만 대체한다. 응답은 요청마다 손으로 돌려준다.

type Call = { method: string; path: string; respond: (res: Response) => void; fail: (e: unknown) => void };

let calls: Call[];

beforeEach(() => {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(
      (url: string, init: RequestInit = {}) =>
        new Promise<Response>((resolve, reject) => {
          calls.push({
            method: init.method ?? "GET",
            path: url.replace(API_BASE_URL, ""),
            respond: resolve,
            fail: reject,
          });
        }),
    ),
  );
});

afterEach(async () => {
  await new Promise((r) => setTimeout(r, 0)); // api-client refreshPromise 해제 틱
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const flush = () => act(() => new Promise<void>((r) => setTimeout(r, 0)));
const noContent = () => new Response(null, { status: 204 });
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const deletes = () => calls.filter((c) => c.method === "DELETE");
const deleteOf = (id: string) => {
  const c = deletes().find((d) => d.path === `/api/v1/notebooks/${id}`);
  if (!c) throw new Error(`DELETE ${id} 요청 없음`);
  return c;
};
const targets = (...ids: string[]): BulkTarget[] => ids.map((id) => ({ id, title: `노트북 ${id}` }));

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const hook = renderHook(() => useBulkDelete(), { wrapper });
  return { client, invalidate, hook };
}

describe("useBulkDelete — 결과·캐시(AC-2.5, D-13)", () => {
  it("204만 성공으로 세고 403/404/5xx/응답 손실은 실패로 남기며, 목록 무효화는 정확히 1회다", async () => {
    const { client, invalidate, hook } = setup();
    for (const id of ["a", "b", "c", "d", "e"]) client.setQueryData(["notebook", id], { id });

    let finished: Promise<unknown> = Promise.resolve();
    act(() => {
      finished = hook.result.current.start(targets("a", "b", "c", "d", "e"));
    });
    await flush();
    expect(hook.result.current.phase).toBe("running");
    expect(deletes().map((d) => d.path)).toEqual([
      "/api/v1/notebooks/a",
      "/api/v1/notebooks/b",
      "/api/v1/notebooks/c",
    ]);

    deleteOf("a").respond(noContent());
    await flush();
    deleteOf("b").respond(json(403, { detail: "Only the owner can delete" }));
    await flush();
    deleteOf("c").respond(json(404, { detail: "Notebook not found" }));
    await flush();
    deleteOf("d").respond(json(500, { detail: "boom" }));
    await flush();
    deleteOf("e").fail(new TypeError("Failed to fetch")); // 요청은 보냈으나 응답 없음
    await act(() => finished);

    const { report, progress } = hook.result.current;
    expect(hook.result.current.phase).toBe("done");
    expect(progress).toEqual({ done: 5, total: 5 });
    expect(report?.stopReason).toBeNull();
    expect(report?.results.map((r) => [r.id, r.status, r.message, r.unconfirmed])).toEqual([
      ["a", "succeeded", BULK_MESSAGES.deleted, false],
      ["b", "failed", BULK_MESSAGES.forbidden, false],
      ["c", "failed", BULK_MESSAGES.notFound, false],
      ["d", "failed", BULK_MESSAGES.server, false],
      ["e", "failed", BULK_MESSAGES.unconfirmed, true],
    ]);

    expect(invalidate).toHaveBeenCalledTimes(1);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["notebooks"] });
    // 성공 id의 상세 캐시만 지운다
    expect(client.getQueryData(["notebook", "a"])).toBeUndefined();
    for (const id of ["b", "c", "d", "e"]) expect(client.getQueryData(["notebook", id])).toEqual({ id });
  });

  it("실행 중 start를 다시 불러도 두 번 실행하지 않는다", async () => {
    const { hook } = setup();
    let first: Promise<unknown> = Promise.resolve();
    let second: Promise<unknown> = Promise.resolve();
    act(() => {
      first = hook.result.current.start(targets("a"));
      second = hook.result.current.start(targets("a"));
    });
    await flush();
    expect(deletes()).toHaveLength(1);
    expect(await second).toBeNull();
    deleteOf("a").respond(noContent());
    await act(() => first);
    expect(deletes()).toHaveLength(1);
  });
});

describe("useBulkDelete — 진행·중지(AC-2.4, D-06)", () => {
  it("진행 k/N을 갱신하고, 중지 뒤 새 항목은 시작하지 않으며 이미 보낸 DELETE는 응답대로 기록한다", async () => {
    const { invalidate, hook } = setup();
    let finished: Promise<unknown> = Promise.resolve();
    act(() => {
      finished = hook.result.current.start(targets("1", "2", "3", "4", "5", "6", "7"));
    });
    await flush();
    expect(hook.result.current.progress).toEqual({ done: 0, total: 7 });
    expect(deletes()).toHaveLength(BULK_CONCURRENCY);

    deleteOf("1").respond(noContent());
    await flush();
    expect(hook.result.current.progress).toEqual({ done: 1, total: 7 });
    expect(deletes()).toHaveLength(4); // 1이 끝나 4가 시작됐다

    act(() => hook.result.current.stop());
    expect(hook.result.current.stopping).toBe(true);
    deleteOf("2").respond(noContent());
    deleteOf("3").respond(json(500, { detail: "x" }));
    await flush();
    expect(deletes()).toHaveLength(4); // 중지 뒤 새 DELETE 0건
    expect(hook.result.current.phase).toBe("running"); // 4의 응답을 기다린다
    deleteOf("4").respond(noContent());
    await act(() => finished);

    const report = hook.result.current.report;
    expect(report?.stopReason).toBe("user");
    expect(report?.results.map((r) => r.status)).toEqual([
      "succeeded",
      "succeeded",
      "failed",
      "succeeded",
      "not_run",
      "not_run",
      "not_run",
    ]);
    expect(report?.results[4].message).toBe(BULK_MESSAGES.notRun);
    expect(invalidate).toHaveBeenCalledTimes(1);

    act(() => hook.result.current.reset());
    expect(hook.result.current.phase).toBe("idle");
    expect(hook.result.current.report).toBeNull();
  });
});

describe("runBulkDelete — 전역 인증 중단(AC-2.7, PR-04)", () => {
  it("401에 refresh도 실패하면 새로 시작하는 항목이 0개이고 나머지는 not_run이다", async () => {
    const done = runBulkDelete(targets("1", "2", "3", "4", "5", "6", "7"), { shouldStop: () => false });
    await flush();
    expect(deletes()).toHaveLength(3);

    deleteOf("1").respond(json(401, { detail: "Not authenticated" }));
    await flush();
    const refresh = calls.filter((c) => c.path === "/api/v1/auth/refresh");
    expect(refresh).toHaveLength(1); // api-client 내부 refresh(세지 않는다)
    refresh[0].respond(json(401, { detail: "expired" }));
    await flush();
    expect(deletes()).toHaveLength(3); // 중단 이후 새 DELETE 0건

    deleteOf("2").respond(noContent()); // 이미 진행 중이던 요청은 결과대로
    deleteOf("3").respond(json(403, { detail: "Only the owner can delete" }));
    const report = await done;

    expect(deletes()).toHaveLength(3);
    expect(report.stopReason).toBe("auth_expired");
    expect(report.results.map((r) => [r.status, r.message])).toEqual([
      ["failed", BULK_MESSAGES.authExpired],
      ["succeeded", BULK_MESSAGES.deleted],
      ["failed", BULK_MESSAGES.forbidden],
      ["not_run", BULK_MESSAGES.notRun],
      ["not_run", BULK_MESSAGES.notRun],
      ["not_run", BULK_MESSAGES.notRun],
      ["not_run", BULK_MESSAGES.notRun],
    ]);
  });

  it("403 password_change_required면 통지하고 새로 시작하는 항목이 0개다", async () => {
    const listener = vi.fn();
    const unsubscribe = onPasswordChangeRequired(listener);
    try {
      const done = runBulkDelete(targets("1", "2", "3", "4", "5"), { shouldStop: () => false });
      await flush();
      deleteOf("2").respond(
        json(403, { detail: { code: PASSWORD_CHANGE_REQUIRED, message: "change password" } }),
      );
      await flush();
      expect(listener).toHaveBeenCalledTimes(1);
      deleteOf("1").respond(noContent());
      deleteOf("3").respond(noContent());
      const report = await done;

      expect(deletes()).toHaveLength(3);
      expect(report.stopReason).toBe("password_change_required");
      expect(report.results.map((r) => r.status)).toEqual([
        "succeeded",
        "failed",
        "succeeded",
        "not_run",
        "not_run",
      ]);
      expect(report.results[1].message).toBe(BULK_MESSAGES.passwordChangeRequired);
    } finally {
      unsubscribe();
    }
  });

  it("일반 403·404는 전역 중단이 아니다 — 다른 항목을 계속 처리한다(AC-2.6)", async () => {
    const done = runBulkDelete(targets("1", "2", "3", "4"), { shouldStop: () => false });
    await flush();
    deleteOf("1").respond(json(403, { detail: "Only the owner can delete" }));
    await flush();
    expect(deletes()).toHaveLength(4);
    deleteOf("2").respond(json(404, { detail: "Notebook not found" }));
    deleteOf("3").respond(noContent());
    deleteOf("4").respond(noContent());
    const report = await done;
    expect(report.stopReason).toBeNull();
    expect(report.results.map((r) => r.status)).toEqual(["failed", "failed", "succeeded", "succeeded"]);
  });

  it("끝까지 실행한 뒤 중지를 눌렀으면 중지 사유를 남기지 않는다", async () => {
    let stop = false;
    const done = runBulkDelete(targets("1"), { shouldStop: () => stop });
    await flush();
    stop = true;
    deleteOf("1").respond(noContent());
    expect((await done).stopReason).toBeNull();
  });
});

describe("useBulkDelete — 화면 상태", () => {
  it("중지는 실행 중에만 의미가 있다", async () => {
    const { hook } = setup();
    act(() => hook.result.current.stop());
    expect(hook.result.current.stopping).toBe(false);
    await waitFor(() => expect(hook.result.current.phase).toBe("idle"));
  });
});
