import { expect, type Request, test } from "@playwright/test";
import {
  assertLoopbackUrl,
  DEMO_CREDENTIALS as DEMO,
  LOGIN_RATE_LIMIT_WINDOW_MS,
  loginViaUiAfterRateLimit,
} from "./helpers";

// 관리 「감사 로그」 화면(이슈 #236) — 별도 경로 `/admin/audit`. 읽기 전용: GET과 로그인만 한다.
//
// ① 관리자(공유 storageState, 실 API): 「사용 제한」 화면에는 감사 로그가 없고(감사 로그 GET 0건), 사이드바 「감사 로그」로
//    이동하면 최근 100건(`/admin/audit-logs?limit=100`)을 읽어 표나 빈 상태를 보인다. 현재 항목만 활성이다.
// ② 일반 사용자(시드 demo, 새 컨텍스트): `/admin/audit`로 가면 노트북 홈으로 이동하고 관리 API가 0건이다.

const AUDIT_LOGS = "/api/v1/admin/audit-logs";

const pathOf = (url: string) => {
  const u = new URL(url);
  return `${u.pathname}${u.search}`;
};

test.describe("관리 감사 로그 — 관리자(#236)", () => {
  test("사용 제한 화면에는 감사 로그가 없고, 사이드바 「감사 로그」에서 최근 100건을 읽는다", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(err.message));
    page.on("console", (msg) => {
      if (msg.type() === "error" && /Content Security Policy|CSP/i.test(msg.text())) errors.push(msg.text());
    });
    const auditGets: string[] = [];
    page.on("request", (req: Request) => {
      if (req.method() === "GET" && new URL(req.url()).pathname === AUDIT_LOGS) auditGets.push(pathOf(req.url()));
    });

    await page.goto("/admin/limits");
    await expect(page.getByRole("heading", { level: 1, name: "사용 제한", exact: true })).toBeVisible({ timeout: 15_000 });
    await page.waitForLoadState("networkidle");
    await expect(page.getByRole("heading", { name: "감사 로그", exact: true })).toHaveCount(0);
    expect(auditGets, "사용 제한 화면은 감사 로그를 부르지 않는다").toEqual([]);

    const nav = page.getByTestId("app-sidebar").getByTestId("admin-nav-group");
    await expect(nav.getByRole("link", { name: "사용 제한", exact: true })).toHaveAttribute("aria-current", "page");
    const first = page.waitForResponse((r) => new URL(r.url()).pathname === AUDIT_LOGS);
    await nav.getByRole("link", { name: "감사 로그", exact: true }).click();
    await page.waitForURL((url) => url.pathname === "/admin/audit");
    const res = await first;
    expect(res.status()).toBe(200);
    expect(new URL(res.url()).searchParams.get("limit")).toBe("100");

    await expect(page.getByRole("heading", { level: 1, name: "감사 로그", exact: true })).toBeVisible();
    await expect(nav.getByRole("link", { name: "감사 로그", exact: true })).toHaveAttribute("aria-current", "page");
    await expect(nav.getByRole("link", { name: "사용 제한", exact: true })).not.toHaveAttribute("aria-current");

    const rows = (await res.json()) as unknown[];
    const region = page.getByTestId("audit-log");
    if (rows.length === 0) {
      await expect(region.getByText("아직 감사 기록이 없습니다")).toBeVisible();
    } else {
      await expect(region.getByRole("table")).toBeVisible();
      await expect(region.getByRole("combobox", { name: "사용자 필터" })).toHaveText(/모든 사용자/);
      await expect(region.getByRole("combobox", { name: "액션 필터" })).toHaveText(/모든 액션/);
      await expect(page.getByTestId("audit-count")).toHaveText(`${rows.length.toLocaleString("ko-KR")}건`);
    }
    expect(auditGets).toEqual([`${AUDIT_LOGS}?limit=100`]);
    expect(errors, errors.join("\n")).toEqual([]);
  });
});

test.describe("관리 감사 로그 — 비인가 관점(#236)", () => {
  // demo로 새로 로그인한다(공유 관리자 storageState를 쓰지 않는다).
  test.use({ storageState: { cookies: [], origins: [] } });

  test("일반 사용자(demo)는 /admin/audit에서 노트북 홈으로 이동하고 관리 API가 0건이다", async ({ page, baseURL }) => {
    test.setTimeout(60_000 + LOGIN_RATE_LIMIT_WINDOW_MS);
    assertLoopbackUrl(baseURL, "Playwright baseURL");
    const adminRequests: string[] = [];
    page.on("request", (req) => {
      if (new URL(req.url()).pathname.startsWith("/api/v1/admin/")) {
        adminRequests.push(`${req.method()} ${pathOf(req.url())}`);
      }
    });

    const login = await loginViaUiAfterRateLimit(page, DEMO.email, DEMO.password);
    expect(login.status(), "demo 로그인 응답").toBe(200);
    await expect(page.getByRole("heading", { name: "노트북", exact: true })).toBeVisible({ timeout: 10_000 });

    await page.goto("/admin/audit");
    await page.waitForURL((url) => url.pathname === "/", { timeout: 10_000 });
    await expect(page.getByRole("heading", { name: "노트북", exact: true })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("audit-log")).toHaveCount(0);
    await page.waitForLoadState("networkidle");
    expect(adminRequests, adminRequests.join("\n")).toEqual([]);
  });
});
