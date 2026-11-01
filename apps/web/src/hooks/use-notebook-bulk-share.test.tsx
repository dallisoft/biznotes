import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { API_BASE_URL } from "@/lib/api";
import { PASSWORD_CHANGE_REQUIRED } from "@/lib/api-client";
import { BULK_MESSAGES, type BulkTarget, type SharePermission } from "@/lib/notebook-bulk";
import { BULK_CONCURRENCY, type BulkShareRequest, runBulkShare, useBulkShare } from "./use-notebook-bulk";

// core-v0.20.0 AC-3.3(일괄 공유 그룹 대상화 — §5.8·§5.3)·AC-3.4(요청 본문 정확 대조·DELETE 0), notebook-bulk-actions
// AC-3.3·3.4·3.5·3.8의 그룹판. runBulkShare·useBulkShare·decideShare·runBounded·classifyError·api-client 본문을 그대로
// 실행하고 네트워크 경계(global fetch)만 대체한다. 앞 묶음은 응답을 요청마다 손으로 돌려주고, 마지막 묶음은 코어 v0.20.0
// 공유 API를 흉내 낸 메모리 서버가 응답한다.

type Call = {
  method: string;
  path: string;
  body: Record<string, unknown> | null;
  settled: boolean;
  respond: (res: Response) => void;
  fail: (e: unknown) => void;
};

let calls: Call[];

beforeEach(() => {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(
      (url: string, init: RequestInit = {}) =>
        new Promise<Response>((resolve, reject) => {
          const call: Call = {
            method: init.method ?? "GET",
            path: url.replace(API_BASE_URL, ""),
            body: typeof init.body === "string" ? JSON.parse(init.body) : null,
            settled: false,
            respond: (res) => {
              call.settled = true;
              resolve(res);
            },
            fail: (e) => {
              call.settled = true;
              reject(e);
            },
          };
          calls.push(call);
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
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const G = "group-research";
const OTHER_G = "group-other";
type Perm = "read" | "write";
/** 공유 행 — 기본은 대상 그룹 G. `{ user }`면 옛 사용자 행, `{ group }`이면 다른 그룹 행. */
const share = (id: string, nb: string, perm: Perm, target: { group?: string; user?: string } = { group: G }) => ({
  id,
  notebook_id: nb,
  user_id: target.user ?? null,
  group_id: target.user ? null : (target.group ?? G),
  permission: perm,
});

const targets = (...ids: string[]): BulkTarget[] => ids.map((id) => ({ id, title: `노트북 ${id}` }));
const request = (ids: string[], permission: SharePermission = "read", groupId = G): BulkShareRequest => ({
  targets: targets(...ids),
  groupId,
  permission,
});

const sharesPath = (nb: string) => `/api/v1/notebooks/${nb}/shares`;
const sharePath = (nb: string) => `/api/v1/notebooks/${nb}/share`;
const find = (method: string, path: string) => {
  const c = calls.find((x) => x.method === method && x.path === path);
  if (!c) throw new Error(`${method} ${path} 요청 없음`);
  return c;
};
const getOf = (nb: string) => find("GET", sharesPath(nb));
const postOf = (nb: string) => find("POST", sharePath(nb));
const posts = () => calls.filter((c) => c.method === "POST" && c.path.endsWith("/share"));
const gets = () => calls.filter((c) => c.method === "GET");
const refreshes = () => calls.filter((c) => c.path === "/api/v1/auth/refresh");
const sharingCalls = () => calls.filter((c) => c.path !== "/api/v1/auth/refresh");

describe("runBulkShare — 그룹 대상 결정·요청 본문(core-v0.20.0 AC-3.3·AC-3.4)", () => {
  it("항목마다 GET → 결정표 → 필요할 때만 POST {group_id, permission}이고, DELETE·PATCH·사용자 대상 0이다 — 끝나면 목록·공유 목록 캐시를 한 번씩 무효화", async () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const hook = renderHook(() => useBulkShare(), { wrapper });

    let finished: Promise<unknown> = Promise.resolve();
    act(() => {
      finished = hook.result.current.start(request(["n1", "n2", "n3", "n4"], "write"));
    });
    await flush();
    expect(hook.result.current.phase).toBe("running");
    expect(hook.result.current.progress).toEqual({ done: 0, total: 4 });
    // probe 없음 — 처음부터 상한(3)만큼 결정용 GET을 함께 보낸다
    expect(sharingCalls().map((c) => `${c.method} ${c.path}`)).toEqual(
      ["n1", "n2", "n3"].map((n) => `GET ${sharesPath(n)}`),
    );

    getOf("n1").respond(json(200, [])); // 없음 → 공유
    getOf("n2").respond(json(200, [share("old-2", "n2", "read")])); // read → write 추가(같은 행)
    getOf("n3").respond(json(200, [share("old-3", "n3", "write")])); // 이미 write → 건너뜀
    await flush();
    // n4: 다른 그룹·사용자의 write 행은 결정에 쓰지 않는다
    getOf("n4").respond(
      json(200, [share("o4", "n4", "write", { group: OTHER_G }), share("u4", "n4", "write", { user: "u" })]),
    );
    await flush();
    postOf("n1").respond(json(201, share("s1", "n1", "write")));
    postOf("n2").respond(json(201, share("old-2", "n2", "write"))); // 코어 v0.19.0+: 같은 행(old-2)이 write로 올라간다
    postOf("n4").respond(json(201, share("s4", "n4", "write")));
    await act(() => finished);

    expect(posts().map((c) => [c.path, c.body])).toStrictEqual([
      [sharePath("n1"), { group_id: G, permission: "write", mode: "upgrade_only" }],
      [sharePath("n2"), { group_id: G, permission: "write", mode: "upgrade_only" }],
      [sharePath("n4"), { group_id: G, permission: "write", mode: "upgrade_only" }],
    ]);
    expect(calls.some((c) => c.method === "DELETE" || c.method === "PATCH" || c.method === "PUT")).toBe(false);

    const report = hook.result.current.report;
    expect(report?.stopReason).toBeNull();
    expect(report?.results.map((r) => [r.id, r.status, r.message])).toEqual([
      ["n1", "succeeded", BULK_MESSAGES.shared],
      ["n2", "succeeded", BULK_MESSAGES.addedWrite],
      ["n3", "skipped", "이미 편집 가능 권한으로 공유됨"],
      ["n4", "succeeded", BULK_MESSAGES.shared],
    ]);
    expect(hook.result.current.phase).toBe("done");
    expect(hook.result.current.progress).toEqual({ done: 4, total: 4 });
    expect(invalidate.mock.calls).toEqual([[{ queryKey: ["notebooks"] }], [{ queryKey: ["notebook-shares"] }]]);
  });

  it("read 요청 — 같은 그룹 행이 read·write면 POST 0으로 건너뛰고(결정 시점 기준), 다른 그룹 행만 있으면 read로 공유한다", async () => {
    const done = runBulkShare(request(["n1", "n2", "n3"], "read"), { shouldStop: () => false });
    await flush();
    getOf("n1").respond(json(200, [share("r1", "n1", "read")]));
    getOf("n2").respond(json(200, [share("w2", "n2", "write")]));
    getOf("n3").respond(json(200, [share("x3", "n3", "write", { group: OTHER_G })]));
    await flush();
    postOf("n3").respond(json(201, share("s3", "n3", "read")));
    const report = await done;

    expect(posts().map((c) => [c.path, c.body])).toStrictEqual([[sharePath("n3"), { group_id: G, permission: "read", mode: "upgrade_only" }]]);
    expect(report.results.map((r) => [r.status, r.message])).toEqual([
      ["skipped", "이미 읽기 전용 권한으로 공유됨"],
      ["skipped", "이미 편집 가능 권한으로 공유됨"],
      ["succeeded", BULK_MESSAGES.shared],
    ]);
  });

  it("동시 요청은 3개 이하이고 항목마다 GET → (필요할 때만) POST다", async () => {
    const ids = ["n1", "n2", "n3", "n4", "n5", "n6", "n7", "n8"];
    const done = runBulkShare(request(ids), { shouldStop: () => false });
    let maxInFlight = 0;
    let settledReport = false;
    void done.then(() => {
      settledReport = true;
    });
    await flush();
    // 가장 오래 기다린 요청부터 하나씩 응답한다. n5는 이미 read가 있어 건너뛴다.
    while (!settledReport) {
      const pending = sharingCalls().filter((c) => !c.settled);
      maxInFlight = Math.max(maxInFlight, pending.length);
      expect(pending.length).toBeLessThanOrEqual(BULK_CONCURRENCY);
      const oldest = pending[0];
      if (!oldest) break;
      const nb = oldest.path.split("/")[4];
      if (oldest.method === "GET") oldest.respond(json(200, nb === "n5" ? [share("old-5", nb, "read")] : []));
      else oldest.respond(json(201, share(`s-${nb}`, nb, "read")));
      await flush();
    }
    const report = await done;

    expect(maxInFlight).toBe(BULK_CONCURRENCY);
    for (const nb of ids) {
      const order = sharingCalls().filter((c) => c.path.split("/")[4] === nb).map((c) => c.method);
      expect(order).toEqual(nb === "n5" ? ["GET"] : ["GET", "POST"]);
    }
    expect(report.results.map((r) => r.status)).toEqual([
      "succeeded",
      "succeeded",
      "succeeded",
      "succeeded",
      "skipped",
      "succeeded",
      "succeeded",
      "succeeded",
    ]);
  });

  it("시작한 뒤 입력 배열·그룹·권한을 바꿔도 실행 대상·그룹·권한은 바뀌지 않는다(제출 시 캡처)", async () => {
    const req = request(["n1", "n2"], "read");
    const done = runBulkShare(req, { shouldStop: () => false });
    const mutable = req as { targets: BulkTarget[]; groupId: string; permission: SharePermission };
    mutable.targets.push({ id: "n9", title: "끼어든 노트북" });
    mutable.targets[1] = { id: "n8", title: "바뀐 노트북" };
    mutable.groupId = "group-mallory";
    mutable.permission = "write";
    await flush();
    getOf("n1").respond(json(200, []));
    getOf("n2").respond(json(200, []));
    await flush();
    postOf("n1").respond(json(201, share("s1", "n1", "read")));
    postOf("n2").respond(json(201, share("s2", "n2", "read")));
    const report = await done;

    expect(calls.some((c) => c.path.includes("/n8/") || c.path.includes("/n9/"))).toBe(false);
    expect(posts().map((c) => c.body)).toStrictEqual([
      { group_id: G, permission: "read", mode: "upgrade_only" },
      { group_id: G, permission: "read", mode: "upgrade_only" },
    ]);
    expect(report.results.map((r) => [r.id, r.title])).toEqual([
      ["n1", "노트북 n1"],
      ["n2", "노트북 n2"],
    ]);
  });
});

describe("runBulkShare — 실패 분기(core-v0.20.0 AC-3.3)", () => {
  it("결정용 GET 실패(500·응답 손실·목록이 아닌 응답)는 그 항목만 실패이고 쓰기 요청을 보내지 않는다 — 나머지는 계속한다", async () => {
    const done = runBulkShare(request(["n1", "n2", "n3", "n4"]), { shouldStop: () => false });
    await flush();
    getOf("n1").respond(json(500, { detail: "boom" }));
    getOf("n2").fail(new TypeError("Failed to fetch"));
    getOf("n3").respond(json(200, { detail: "not a list" }));
    await flush();
    getOf("n4").respond(json(200, []));
    await flush();
    postOf("n4").respond(json(201, share("s4", "n4", "read")));
    const report = await done;

    expect(posts().map((c) => c.path)).toEqual([sharePath("n4")]);
    expect(report.stopReason).toBeNull();
    expect(report.results.map((r) => [r.status, r.message, r.unconfirmed])).toEqual([
      ["failed", BULK_MESSAGES.server, false],
      ["failed", BULK_MESSAGES.shareCheckLost, false],
      ["failed", BULK_MESSAGES.shareCheckLost, false],
      ["succeeded", BULK_MESSAGES.shared, false],
    ]);
  });

  it("POST 500은 그 항목만 실패, 응답 손실은 「확인 불가」(처리됐을 수 있음) — 둘 다 되돌리지 않고 나머지는 계속한다", async () => {
    const done = runBulkShare(request(["n1", "n2", "n3"]), { shouldStop: () => false });
    await flush();
    for (const nb of ["n1", "n2", "n3"]) getOf(nb).respond(json(200, []));
    await flush();
    postOf("n1").respond(json(500, { detail: "boom" }));
    postOf("n2").fail(new TypeError("Failed to fetch"));
    postOf("n3").respond(json(201, share("s3", "n3", "read")));
    const report = await done;

    expect(report.stopReason).toBeNull();
    expect(report.results.map((r) => [r.status, r.message, r.unconfirmed])).toEqual([
      ["failed", BULK_MESSAGES.server, false],
      ["failed", BULK_MESSAGES.unconfirmed, true],
      ["succeeded", BULK_MESSAGES.shared, false],
    ]);
    expect(sharingCalls()).toHaveLength(6); // 재시도·보상 0
  });

  it("POST 404 \"Target group not found\"면 대상 중단 — 새 항목을 시작하지 않고 진행 중이던 항목은 결과대로 기록한다(D-08)", async () => {
    const done = runBulkShare(request(["n1", "n2", "n3", "n4", "n5"]), { shouldStop: () => false });
    await flush();
    getOf("n1").respond(json(200, []));
    getOf("n2").respond(json(200, []));
    await flush();
    expect(postOf("n2").body).toStrictEqual({ group_id: G, permission: "read", mode: "upgrade_only" }); // 진행 중(POST 대기)
    postOf("n1").respond(json(404, { detail: "Target group not found" }));
    await flush();
    getOf("n3").respond(json(200, [])); // 중단 뒤 GET이 끝난 항목 — POST를 보내지 않는다
    await flush();
    postOf("n2").respond(json(201, share("s2", "n2", "read")));
    const report = await done;

    expect(gets().map((c) => c.path)).toEqual(["n1", "n2", "n3"].map(sharesPath)); // n4·n5 시작 0
    expect(posts().map((c) => c.path)).toEqual(["n1", "n2"].map(sharePath));
    expect(report.stopReason).toBe("target_group_missing");
    expect(report.results.map((r) => [r.id, r.status, r.message])).toEqual([
      ["n1", "failed", BULK_MESSAGES.targetGroupMissing],
      ["n2", "succeeded", BULK_MESSAGES.shared],
      ["n3", "not_run", BULK_MESSAGES.notRun],
      ["n4", "not_run", BULK_MESSAGES.notRun],
      ["n5", "not_run", BULK_MESSAGES.notRun],
    ]);
  });

  it("GET이 401(refresh 실패)이면 전역 중단 — 다른 진행 중 GET이 끝난 뒤 새 POST는 0건이다(PR-04)", async () => {
    const done = runBulkShare(request(["n1", "n2", "n3", "n4", "n5"]), { shouldStop: () => false });
    await flush();
    getOf("n1").respond(json(401, { detail: "Not authenticated" }));
    await flush();
    expect(refreshes()).toHaveLength(1); // api-client 내부 refresh(세지 않는다)
    refreshes()[0].respond(json(401, { detail: "expired" }));
    await flush();
    getOf("n2").respond(json(200, []));
    getOf("n3").respond(json(200, []));
    const report = await done;

    expect(posts()).toHaveLength(0);
    expect(gets().map((c) => c.path)).toEqual(["n1", "n2", "n3"].map(sharesPath));
    expect(report.stopReason).toBe("auth_expired");
    expect(report.results.map((r) => [r.status, r.message])).toEqual([
      ["failed", BULK_MESSAGES.authExpired],
      ["not_run", BULK_MESSAGES.notRun],
      ["not_run", BULK_MESSAGES.notRun],
      ["not_run", BULK_MESSAGES.notRun],
      ["not_run", BULK_MESSAGES.notRun],
    ]);
  });

  it("POST가 403 password_change_required면 전역 중단이다", async () => {
    const done = runBulkShare(request(["n1", "n2", "n3", "n4"]), { shouldStop: () => false });
    await flush();
    getOf("n1").respond(json(200, []));
    await flush();
    postOf("n1").respond(json(403, { detail: { code: PASSWORD_CHANGE_REQUIRED, message: "change" } }));
    await flush();
    getOf("n2").respond(json(200, []));
    getOf("n3").respond(json(200, []));
    const report = await done;
    expect(posts()).toHaveLength(1);
    expect(report.stopReason).toBe("password_change_required");
    expect(report.results.map((r) => r.status)).toEqual(["failed", "not_run", "not_run", "not_run"]);
  });

  it("실행 중 사용자가 중지하면 진행 중 GET 뒤 POST를 보내지 않고 미실행으로 남긴다 — 건너뜀은 그대로 기록한다", async () => {
    let stop = false;
    const done = runBulkShare(request(["n1", "n2", "n3", "n4"]), { shouldStop: () => stop });
    await flush();
    stop = true;
    getOf("n1").respond(json(200, []));
    getOf("n2").respond(json(200, [share("w2", "n2", "write")])); // 요청이 필요 없는 항목은 건너뜀
    getOf("n3").respond(json(200, []));
    const report = await done;
    expect(posts()).toHaveLength(0);
    expect(report.stopReason).toBe("user");
    expect(report.results.map((r) => r.status)).toEqual(["not_run", "skipped", "not_run", "not_run"]);
  });

  it("중지 판정 함수가 던지면 새 요청을 보내지 않는 쪽으로 닫는다", async () => {
    const report = await runBulkShare(request(["n1", "n2"]), {
      shouldStop: () => {
        throw new Error("boom");
      },
    });
    expect(calls).toEqual([]);
    expect(report.results.map((r) => r.status)).toEqual(["not_run", "not_run"]);
  });
});

// ---- 코어 v0.22.0 공유 API를 흉내 낸 메모리 서버(core-v0.20.0 §5.3·§5.8, 코어 v0.21.0 #300) ----
// `POST /share {group_id}`는 코어 `services/notebook_shares.upsert_notebook_share`와 같이 같은 그룹이면 가장 먼저 만든
// 행의 권한을 바꾸고 그 행을 돌려준다 — 기본(`mode` 없음·`set`)은 요청 값(하향 포함), `upgrade_only`는 기존과 요청 중
// 높은 쪽이다. 소유자 소속이 아닌 그룹(여기서는 G 밖)은 404다. 요청은
// 받은 순서대로 기록한다. 다른 탭의 변경은 요청 처리 직전에 서버 상태를 직접 바꿔 끼워 넣는다(기록에 남지 않는다).

type ShareRow = ReturnType<typeof share>;
type ServerHook = (method: string, nb: string, rows: ShareRow[]) => ShareRow[] | void;

function installShareServer(
  initial: ShareRow[],
  opts: { before?: ServerHook; fail?: (method: string, nb: string) => "lost" | undefined } = {},
) {
  const server = { rows: initial.map((r) => ({ ...r })), log: [] as string[], statuses: [] as number[] };
  let seq = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const method = init.method ?? "GET";
      const path = url.replace(API_BASE_URL, "");
      server.log.push(typeof init.body === "string" ? `${method} ${path} ${init.body}` : `${method} ${path}`);
      const body = typeof init.body === "string" ? (JSON.parse(init.body) as Record<string, string>) : {};
      const nb = path.split("/")[4];
      const next = opts.before?.(method, nb, server.rows);
      if (next) server.rows = next;

      let res: Response;
      if (method === "GET" && path === sharesPath(nb)) {
        res = json(200, server.rows.filter((r) => r.notebook_id === nb));
      } else if (method === "POST" && path === sharePath(nb) && body.group_id !== G) {
        res = json(404, { detail: "Target group not found" });
      } else if (method === "POST" && path === sharePath(nb)) {
        const permission = body.permission as Perm;
        const same = server.rows.filter((r) => r.notebook_id === nb && r.group_id === body.group_id);
        let kept: ShareRow;
        if (same.length === 0) {
          kept = share(`srv-${++seq}`, nb, permission);
          server.rows.push(kept);
        } else {
          kept = same[0];
          const keepHigher = body.mode === "upgrade_only" && kept.permission === "write";
          kept.permission = keepHigher ? "write" : permission;
          server.rows = server.rows.filter((r) => !same.slice(1).includes(r));
        }
        res = json(201, kept);
      } else {
        res = json(500, { detail: `unexpected ${method} ${path}` });
      }
      server.statuses.push(res.status);
      if (opts.fail?.(method, nb) === "lost") throw new TypeError("Failed to fetch");
      return res;
    }),
  );
  return server;
}

const logGet = (nb: string) => `GET ${sharesPath(nb)}`;
const logPost = (nb: string, permission: SharePermission, groupId = G) =>
  `POST ${sharePath(nb)} ${JSON.stringify({ group_id: groupId, permission, mode: "upgrade_only" })}`;
const rowsOf = (server: { rows: ShareRow[] }, nb: string) =>
  server.rows.filter((r) => r.notebook_id === nb).map((r) => [r.id, r.group_id ?? r.user_id, r.permission]);
const run = (ids: string[], permission: SharePermission, groupId = G) =>
  runBulkShare(request(ids, permission, groupId), { shouldStop: () => false });

describe("runBulkShare — 메모리 서버(코어 v0.22.0 동작, core-v0.20.0 AC-3.3)", () => {
  it("결정 시점에 같은 그룹이 같거나 높은 권한이면 POST 0 — 행·id 그대로, 다른 그룹·사용자 행은 건드리지 않는다", async () => {
    const initial = [
      share("w1", "n1", "write"),
      share("u1", "n1", "read", { user: "legacy-user" }),
      share("o2", "n2", "write", { group: OTHER_G }),
    ];
    const server = installShareServer(initial);
    const report = await run(["n1", "n2"], "read");

    expect(server.log).toEqual([logGet("n1"), logGet("n2"), logPost("n2", "read")]);
    expect(server.log.some((l) => l.startsWith("DELETE"))).toBe(false);
    expect(report.results.map((r) => [r.id, r.status])).toEqual([
      ["n1", "skipped"],
      ["n2", "succeeded"],
    ]);
    expect(rowsOf(server, "n1")).toEqual([
      ["w1", G, "write"],
      ["u1", "legacy-user", "read"],
    ]);
    expect(rowsOf(server, "n2")).toEqual([
      ["o2", OTHER_G, "write"],
      ["srv-1", G, "read"],
    ]);
  });

  it("요청 write — 같은 그룹 read 행은 같은 id로 write가 되어 added_write다(POST 1, 행 수 그대로)", async () => {
    const server = installShareServer([share("r1", "n1", "read")]);
    const report = await run(["n1"], "write");
    expect(server.log).toEqual([logGet("n1"), logPost("n1", "write")]);
    expect(report.results.map((r) => [r.status, r.message])).toEqual([["succeeded", BULK_MESSAGES.addedWrite]]);
    expect(rowsOf(server, "n1")).toEqual([["r1", G, "write"]]);
  });

  it("경쟁 하향 방지(PR-16 해소, core-v0.22.0 D4) — GET(없음) 뒤·POST 전에 다른 탭이 write를 주면 upgrade_only POST(read) 뒤에도 write이고 추가 요청 0", async () => {
    // 코어 v0.21.0 #300 `mode: "upgrade_only"` — 서버가 기존(write)과 요청(read) 중 높은 쪽을 남긴다
    const server = installShareServer([], {
      before: (method, nb, rows) =>
        method === "POST" && nb === "n1" ? [...rows, share("tab-w", "n1", "write")] : undefined,
    });
    const report = await run(["n1"], "read");

    expect(server.log).toEqual([logGet("n1"), logPost("n1", "read")]); // 추가 요청(재확인·보상·DELETE) 0
    expect(report.results.map((r) => [r.status, r.message])).toEqual([["succeeded", BULK_MESSAGES.shared]]);
    expect(rowsOf(server, "n1")).toEqual([["tab-w", G, "write"]]); // 다른 탭이 올린 권한을 내리지 않았다
  });

  it("응답 손실(서버는 반영) 뒤 다시 실행하면 결정용 GET이 반영된 행을 보여 건너뜀이고 DELETE는 0건이다", async () => {
    let lose = true;
    const server = installShareServer([], {
      fail: (method, nb) => (lose && method === "POST" && nb === "n1" ? "lost" : undefined),
    });
    const first = await run(["n1", "n2"], "read");
    expect(first.results.map((r) => [r.id, r.status, r.unconfirmed])).toEqual([
      ["n1", "failed", true],
      ["n2", "succeeded", false],
    ]);
    expect(rowsOf(server, "n1")).toEqual([["srv-1", G, "read"]]); // 반영된 행을 되돌리지 않는다

    lose = false;
    server.log.length = 0;
    const second = await run(["n1"], "read");
    expect(server.log).toEqual([logGet("n1")]);
    expect(second.results.map((r) => [r.status, r.message])).toEqual([["skipped", "이미 읽기 전용 권한으로 공유됨"]]);
    expect(rowsOf(server, "n1")).toEqual([["srv-1", G, "read"]]);
  });

  it("소유자 소속이 아닌 그룹(서버 404)이면 대상 중단 — 4번째 항목은 시작하지 않고 공유 행은 생기지 않는다", async () => {
    const server = installShareServer([]);
    const report = await run(["n1", "n2", "n3", "n4"], "read", OTHER_G);
    expect(report.stopReason).toBe("target_group_missing");
    const postStatuses = server.log
      .map((line, i) => [line, server.statuses[i]] as const)
      .filter(([line]) => line.startsWith("POST"))
      .map(([, status]) => status);
    expect(postStatuses.length).toBeGreaterThanOrEqual(1);
    expect(new Set(postStatuses)).toEqual(new Set([404]));
    expect(report.results[3].status).toBe("not_run");
    expect(report.results.filter((r) => r.status === "failed").every((r) => r.message === BULK_MESSAGES.targetGroupMissing)).toBe(true);
    expect(server.log.some((l) => l.startsWith("DELETE"))).toBe(false);
    expect(server.rows).toEqual([]);
  });
});
