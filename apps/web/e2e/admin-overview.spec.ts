import { expect, type Request, test } from "@playwright/test";
import {
  assertLoopbackUrl,
  DEMO_CREDENTIALS as DEMO,
  LOGIN_RATE_LIMIT_WINDOW_MS,
  loginViaUiAfterRateLimit,
} from "./helpers";

// 관리 개요(/admin) — plan §6.2.6·§8.3, AC-2.4·AC-2.7b. 읽기 전용: GET과 로그인만 한다(데이터 변경 없음).
//
// AC-2.4  일반 사용자(시드 demo)는 /admin·/admin/users에서 노트북 홈으로 이동하고, 그동안 관리 API 요청이 0건.
// AC-2.7b 관리자 첫 진입 GET 정확히 9건 → 60초 동안 추가 0건 → 「새로고침」으로 다시 9건.
//         무거운 카드(인덱스 = /admin/rag/index-status)의 응답 시간을 기록하고
//         2초 미만인지 확인한다 — 넘으면 §6.2.6에 따라 온디맨드 조회로 바꿔야 한다는 신호다.

const ADMIN_API_PREFIX = "/api/v1/admin/";

/** 개요가 첫 진입·새로고침마다 보내는 GET 9개(hooks/use-admin-overview.ts ADMIN_OVERVIEW_PATHS와 같은 집합). */
const OVERVIEW_GETS = [
  "/api/v1/admin/users",
  "/api/v1/admin/llm/providers",
  "/api/v1/admin/llm/providers/status",
  "/api/v1/admin/llm/models",
  "/api/v1/admin/mcp-servers",
  "/api/v1/admin/rag/index-status",
  "/api/v1/admin/rag/reindex?active_only=true&limit=100",
  "/api/v1/admin/skills/review-requests?status=pending",
  "/api/v1/admin/audit-logs?limit=100",
].sort();

const HEAVY = { index: "/api/v1/admin/rag/index-status" } as const;
const HEAVY_LIMIT_MS = 2_000;

const pathOf = (url: string) => {
  const u = new URL(url);
  return `${u.pathname}${u.search}`;
};

test.describe("관리 개요 가드 — 비인가 관점(AC-2.4)", () => {
  // demo로 새로 로그인한다(공유 관리자 storageState를 쓰지 않는다).
  test.use({ storageState: { cookies: [], origins: [] } });

  test("AC-2.4 일반 사용자(demo)가 /admin·/admin/users로 가면 노트북 홈에 도착하고 관리 API 요청은 0건이다", async ({
    page,
    baseURL,
  }) => {
    // 앞 스펙들의 로그인으로 레이트 리밋 창이 차 있으면 한 번 기다렸다 다시 로그인한다(helpers 참고)
    test.setTimeout(60_000 + LOGIN_RATE_LIMIT_WINDOW_MS);
    assertLoopbackUrl(baseURL, "Playwright baseURL");
    const adminRequests: string[] = [];
    page.on("request", (req) => {
      if (new URL(req.url()).pathname.startsWith(ADMIN_API_PREFIX)) {
        adminRequests.push(`${req.method()} ${pathOf(req.url())}`);
      }
    });

    const login = await loginViaUiAfterRateLimit(page, DEMO.email, DEMO.password);
    expect(login.status(), "demo 로그인 응답").toBe(200);
    await expect(page.getByRole("heading", { name: "노트북", exact: true })).toBeVisible({
      timeout: 10_000,
    });

    for (const target of ["/admin", "/admin/users"]) {
      await page.goto(target);
      await page.waitForURL((url) => url.pathname === "/", { timeout: 10_000 });
      await expect(page.getByRole("heading", { name: "노트북", exact: true })).toBeVisible({
        timeout: 10_000,
      });
      await expect(page.getByRole("heading", { name: "관리 개요" })).toHaveCount(0);
      await page.waitForLoadState("networkidle");
    }
    expect(adminRequests, adminRequests.join("\n")).toEqual([]);
  });
});

test.describe("관리 개요 스냅샷 — 요청 수·응답 시간(AC-2.7b)", () => {
  test("AC-2.7b 관리자 첫 진입 GET 정확히 9건 · 60초 동안 추가 0건 · 「새로고침」 시 다시 9건(무거운 카드 응답 시간 기록)", async ({
    page,
    baseURL,
  }) => {
    test.setTimeout(180_000);
    assertLoopbackUrl(baseURL, "Playwright baseURL");

    const gets: Request[] = [];
    const finished = new Set<Request>();
    const statusOf = new Map<Request, number>();
    page.on("request", (req) => {
      if (req.method() === "GET" && new URL(req.url()).pathname.startsWith(ADMIN_API_PREFIX)) {
        gets.push(req);
      }
    });
    page.on("response", (res) => {
      if (gets.includes(res.request())) statusOf.set(res.request(), res.status());
    });
    page.on("requestfinished", (req) => {
      if (gets.includes(req)) finished.add(req);
    });
    page.on("requestfailed", (req) => {
      if (gets.includes(req)) finished.add(req);
    });

    const batchReport = (batch: Request[]) => ({
      paths: batch.map((r) => pathOf(r.url())).sort(),
      statuses: batch.map((r) => `${statusOf.get(r) ?? "없음"} ${pathOf(r.url())}`),
      heavyMs: Object.fromEntries(
        Object.entries(HEAVY).map(([key, path]) => {
          const req = batch.find((r) => pathOf(r.url()) === path);
          return [key, req ? Math.round(req.timing().responseEnd) : -1];
        }),
      ),
    });

    // 1) 첫 진입 — 정확히 9건, 모두 200(재시도 없음)
    await page.goto("/admin");
    await expect(page.getByRole("heading", { level: 1, name: "관리 개요" })).toBeVisible({
      timeout: 15_000,
    });
    await expect.poll(() => finished.size, { timeout: 15_000 }).toBe(9);
    await expect(page.locator('[data-testid^="overview-card-"][data-state="success"]')).toHaveCount(7);
    const first = batchReport(gets.slice(0, 9));
    expect(gets).toHaveLength(9);
    expect(first.paths).toEqual(OVERVIEW_GETS);
    expect(first.statuses.every((s) => s.startsWith("200 ")), first.statuses.join("\n")).toBe(true);

    // 2) 60초 대기 — 폴링 없음
    await page.waitForTimeout(60_000);
    expect(
      gets.map((r) => pathOf(r.url())),
      "60초 동안 관리 API GET이 추가되면 안 된다",
    ).toHaveLength(9);

    // 3) 새로고침 — 다시 정확히 9건
    await page.getByRole("button", { name: "새로고침" }).click();
    await expect.poll(() => finished.size, { timeout: 15_000 }).toBe(18);
    await expect(page.getByRole("button", { name: "새로고침" })).toBeEnabled();
    await page.waitForTimeout(2_000);
    expect(gets).toHaveLength(18);
    const second = batchReport(gets.slice(9));
    expect(second.paths).toEqual(OVERVIEW_GETS);
    expect(second.statuses.every((s) => s.startsWith("200 ")), second.statuses.join("\n")).toBe(true);
    await expect(page.locator('[data-testid^="overview-card-"][data-state="success"]')).toHaveCount(7);

    // 4) 무거운 카드 응답 시간 — Playwright `request.timing().responseEnd`(요청 시작 → 응답 끝, ms)
    const timing = { first: first.heavyMs, refresh: second.heavyMs, limitMs: HEAVY_LIMIT_MS };
    console.log(`[admin-overview-timing] ${JSON.stringify(timing)}`);
    test.info().annotations.push({ type: "admin-overview-timing", description: JSON.stringify(timing) });
    for (const [label, ms] of Object.entries({ ...prefix("첫 진입", first.heavyMs), ...prefix("새로고침", second.heavyMs) })) {
      expect(ms, `${label} 응답 시간 측정값`).toBeGreaterThanOrEqual(0);
      expect(ms, `${label} — 2초를 넘으면 §6.2.6에 따라 온디맨드 조회로 바꿔야 한다`).toBeLessThan(HEAVY_LIMIT_MS);
    }
  });
});

function prefix(label: string, values: Record<string, number>) {
  return Object.fromEntries(Object.entries(values).map(([k, v]) => [`${label} ${k}`, v]));
}
