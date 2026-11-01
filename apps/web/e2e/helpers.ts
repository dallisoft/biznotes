import path from "node:path";
import { expect, test, type Browser, type Page, type Route } from "@playwright/test";

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);

export function assertLoopbackUrl(url: string | undefined, label: string): void {
  if (!url) throw new Error(`${label}: URL이 정의되지 않음`);
  const parsed = new URL(url);
  if (!LOOPBACK_HOSTS.has(parsed.hostname)) {
    throw new Error(`${label}: loopback URL만 허용됨: ${url}`);
  }
}

export async function installRagLoopbackGuard(
  page: Page,
  baseURL: string | undefined,
): Promise<{ blocked: string[] }> {
  const apiBaseURL = process.env.E2E_API_BASE_URL ?? "http://localhost:28000";
  assertLoopbackUrl(baseURL, "Playwright baseURL");
  assertLoopbackUrl(apiBaseURL, "E2E API baseURL");
  const blocked: string[] = [];

  await page.route("**/*", async (route) => {
    const requestUrl = route.request().url();
    const pathname = new URL(requestUrl).pathname;
    if (!pathname.includes("/api/v1/admin/rag/")) {
      await route.fallback();
      return;
    }
    try {
      assertLoopbackUrl(requestUrl, "브라우저 admin/rag 요청");
      await route.continue();
    } catch {
      blocked.push(requestUrl);
      await route.abort("blockedbyclient");
    }
  });
  return { blocked };
}

/** storageState(auth.setup.ts)로 이미 인증된 상태 — 대시보드 진입만 확인 */
export async function gotoDashboard(page: Page) {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "노트북", exact: true })).toBeVisible({
    timeout: 10_000,
  });
}

/**
 * 노트북 홈의 「새 노트북」(`create-notebook`)으로 노트북을 만들고, 그 노트북 작업 화면이 열릴 때까지 기다린다
 * (ui-ux-redesign P3 — HCI-3 「생성 후 새 노트북으로 이동」, BL-183). 노트북 홈에서 부른다.
 * 끝 조건: URL이 만든 노트북(`/notebooks/{id}`, 대화 쿼리 `?s=` 허용)이고 노트북 헤더의 제목 `h1`이 보인다.
 * 만든 노트북 id를 돌려준다(정리용). 생성 응답을 받는 즉시 그 id를 page별 정리 목록에 넣는다 — 뒤 단계(이동·h1)에서
 * 실패해도 `cleanupCreatedNotebooks`를 부른 스펙은 그 노트북을 지운다.
 */
export async function createNotebookAndOpen(page: Page, title: string): Promise<string> {
  const created = page.waitForResponse(
    (r) => new URL(r.url()).pathname === "/api/v1/notebooks" && r.request().method() === "POST",
  );
  await page.getByTestId("create-notebook").click();
  await page.getByLabel("제목").fill(title);
  await page.getByRole("button", { name: "만들기" }).click();
  const res = await created;
  expect(res.status(), await res.text()).toBe(201);
  const { id } = (await res.json()) as { id: string };
  rememberCreatedNotebook(page, id);
  await page.waitForURL(new RegExp(`/notebooks/${id}(\\?|$)`));
  await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
  return id;
}

/**
 * 노트북 작업 화면 헤더의 브레드크럼 「노트북」 링크로 노트북 홈에 돌아간다(옛 「←」 버튼 대체 — BL-183).
 * 사이드바 주 메뉴에도 같은 이름의 「노트북」 링크가 있으므로 브레드크럼(`nav[aria-label=위치]`) 안에서 찾는다.
 */
export async function gotoHomeViaBreadcrumb(page: Page) {
  await page
    .getByRole("navigation", { name: "위치" })
    .getByRole("link", { name: "노트북", exact: true })
    .click();
  await page.waitForURL((url) => url.pathname === "/");
  await expect(page.getByRole("heading", { name: "노트북", exact: true })).toBeVisible({ timeout: 10_000 });
}

/** 브라우저가 쓰는 공개 API 주소(compose NEXT_PUBLIC_API_BASE_URL 기본값과 같다). */
export const API_BASE_URL = process.env.E2E_API_BASE_URL ?? "http://localhost:28000";

/** 시드 관리자 — 로그인 화면부터 시작하는 스펙(새 컨텍스트)이 쓴다. auth.setup.ts와 같은 기본값. */
export const ADMIN_CREDENTIALS = {
  email: process.env.E2E_ADMIN_EMAIL ?? "admin@biznotes.dev",
  password: process.env.E2E_ADMIN_PASSWORD ?? "admin1234!",
};

/** 시드 일반 사용자(apps/api/scripts/seed.py의 SEED_DEMO_* 기본값) — 비인가 관점 스펙이 새 컨텍스트로 로그인한다. */
export const DEMO_CREDENTIALS = {
  email: process.env.E2E_DEMO_EMAIL ?? "demo@biznotes.dev",
  password: process.env.E2E_DEMO_PASSWORD ?? "demo1234!",
};

/**
 * 로그인 화면에서 로그인한다(새 컨텍스트용). 이동 결과는 호출자가 판정한다.
 * 스펙은 이 함수를 직접 부르지 않고 429 재시도가 있는 `loginViaUiAfterRateLimit`을 쓴다(ui-ux-redesign HCI-P2-3, P6에서 전 스펙 적용).
 */
export async function loginViaUi(page: Page, email: string, password: string) {
  await page.goto("/login");
  await page.getByLabel("이메일").fill(email);
  await page.getByLabel("비밀번호").fill(password);
  await page.getByRole("button", { name: "로그인" }).click();
}

/**
 * 로그인 레이트 리밋의 고정 창(biznotes_core `RateLimiter("login", limit=10, window_seconds=60)` — IP 기준) + 여유 1초.
 * 게이트처럼 여러 스펙이 잇달아 로그인하면(실행 단위마다 auth.setup, 실 API 비밀번호 변경 스펙의 여러 세션) 앞 스펙의
 * 로그인 수만으로 다음 스펙의 로그인이 429를 받을 수 있다.
 */
export const LOGIN_RATE_LIMIT_WINDOW_MS = 61_000;

/**
 * `loginViaUi`와 같되, 로그인 요청이 429(레이트 리밋)면 창이 지난 뒤 한 번만 다시 시도하고 마지막 로그인 응답을 돌려준다.
 * 다른 오류(401 등)는 다시 시도하지 않는다 — 판정은 호출자가 한다. 호출 테스트는 대기 시간만큼 `test.setTimeout`을 늘린다.
 */
export async function loginViaUiAfterRateLimit(page: Page, email: string, password: string) {
  for (let attempt = 0; ; attempt += 1) {
    const response = page.waitForResponse(
      (r) => new URL(r.url()).pathname === "/api/v1/auth/login" && r.request().method() === "POST",
    );
    await loginViaUi(page, email, password);
    const res = await response;
    if (res.status() !== 429 || attempt >= 1) return res;
    console.log(`로그인 429(레이트 리밋) — ${LOGIN_RATE_LIMIT_WINDOW_MS / 1000}초 뒤 한 번 더 시도한다`);
    await page.waitForTimeout(LOGIN_RATE_LIMIT_WINDOW_MS);
  }
}

/**
 * `page.route` 응답 — web(:23000)과 api(:28000)는 출처가 달라 CORS 헤더를 붙여 돌려준다
 * (fetch가 `credentials: "include"`라 허용 출처는 요청 출처 그대로여야 한다).
 */
export async function fulfillJson(route: Route, status: number, body: unknown) {
  const origin = route.request().headers()["origin"] ?? "http://localhost:23000";
  const headers = {
    "access-control-allow-origin": origin,
    "access-control-allow-credentials": "true",
    "access-control-allow-headers": "content-type",
    "access-control-allow-methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
  };
  if (route.request().method() === "OPTIONS") {
    await route.fulfill({ status: 204, headers });
    return;
  }
  await route.fulfill({ status, headers, contentType: "application/json", body: JSON.stringify(body) });
}

/** 컨텍스트의 인증 쿠키로 노트북을 만든다(스펙이 만들고 `deleteNotebookViaApi`로 지운다). */
export async function createNotebookViaApi(page: Page, title: string): Promise<string> {
  const res = await page.request.post(`${API_BASE_URL}/api/v1/notebooks`, { data: { title } });
  expect(res.status(), await res.text()).toBe(201);
  return ((await res.json()) as { id: string }).id;
}

/**
 * 노트북 정리 — finally에서 부르므로 던지지 않고 soft로 단언한다(본문의 원래 실패를 덮지 않는다).
 * 대상·응답 코드를 출력해 실행 기록에 남긴다.
 */
export async function deleteNotebookViaApi(page: Page, notebookId: string, label: string) {
  const status = await page.request
    .delete(`${API_BASE_URL}/api/v1/notebooks/${notebookId}`)
    .then((res) => res.status(), (err: unknown) => `요청 실패: ${String(err)}`);
  console.log(`${label}: notebook ${notebookId} 정리 → ${status}`);
  expect.soft(status, `노트북 ${notebookId} 정리`).toBe(204);
}

/** auth.setup.ts가 저장하는 관리자 storageState — `browser.newContext`의 상대 경로는 cwd 기준이라 이 파일 기준 절대 경로로 둔다. */
const ADMIN_STORAGE_STATE = path.resolve(__dirname, "../playwright/.auth/admin.json");

/** UI로 노트북을 만드는 스펙의 제목 접두 — AC-6.2 「실행 전후 『PW 』 접두 개수 비교」의 대상이다. */
export const E2E_NOTEBOOK_PREFIX = "PW ";

/** `createNotebookAndOpen`이 받은 생성 응답(201)의 노트북 id — 그 테스트의 page별로 모은다. */
const createdNotebookIds = new WeakMap<Page, string[]>();

function rememberCreatedNotebook(page: Page, notebookId: string) {
  createdNotebookIds.set(page, [...(createdNotebookIds.get(page) ?? []), notebookId]);
}

/** 관리자 소유(`filter=mine`) 노트북 중 제목이 `E2E_NOTEBOOK_PREFIX`로 시작하는 수. */
async function countAdminPrefixedNotebooks(browser: Browser): Promise<number> {
  const admin = await browser.newContext({ storageState: ADMIN_STORAGE_STATE });
  try {
    const res = await admin.request.get(`${API_BASE_URL}/api/v1/notebooks?filter=mine`);
    expect(res.status(), "관리자 소유 노트북 목록").toBe(200);
    const notebooks = (await res.json()) as { title: string }[];
    return notebooks.filter((n) => n.title.startsWith(E2E_NOTEBOOK_PREFIX)).length;
  } finally {
    await admin.close();
  }
}

/**
 * BL-183 잔여(ui-ux-redesign P6 — AC-6.2, HCI-P3-12): 스펙 파일 최상위에서 한 번 부른다.
 * - `afterEach`: 그 테스트가 `createNotebookAndOpen`으로 만든 노트북만 API로 지운다. 대상은 생성 응답의 id뿐이다 —
 *   제목·접두로 찾아 지우지 않으므로 다른 실행·사용자의 노트북은 건드리지 않는다. 본문이 실패하거나 시간을 넘겨도 돈다.
 * - `beforeAll`·`afterAll`: 이 파일 실행 전후의 관리자 소유 「PW 」 접두 노트북 수를 출력하고, 같음을 soft로 단언한다
 *   (AC-6.2 증거 — 게이트 로그에 남는다).
 */
export function cleanupCreatedNotebooks(label: string) {
  let before: number | undefined;
  test.beforeAll(async ({ browser }) => {
    before = await countAdminPrefixedNotebooks(browser);
    console.log(`${label}: 실행 전 「${E2E_NOTEBOOK_PREFIX}」 접두 노트북 ${before}개`);
  });
  test.afterEach(async ({ page }) => {
    const ids = createdNotebookIds.get(page) ?? [];
    createdNotebookIds.delete(page);
    for (const id of ids) await deleteNotebookViaApi(page, id, label);
  });
  test.afterAll(async ({ browser }) => {
    if (before === undefined) return;
    const after = await countAdminPrefixedNotebooks(browser);
    console.log(`${label}: 실행 후 「${E2E_NOTEBOOK_PREFIX}」 접두 노트북 ${after}개(실행 전 ${before}개)`);
    expect.soft(after, `${label}: 실행 전후 「${E2E_NOTEBOOK_PREFIX}」 접두 노트북 수가 같다(AC-6.2)`).toBe(before);
  });
}
