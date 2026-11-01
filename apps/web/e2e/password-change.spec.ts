import { randomUUID } from "node:crypto";
import path from "node:path";
import { expect, test, type Browser, type Page } from "@playwright/test";
import {
  API_BASE_URL,
  gotoDashboard,
  LOGIN_RATE_LIMIT_WINDOW_MS,
  loginViaUiAfterRateLimit,
} from "./helpers";

// core-v0.11.0-upgrade AC-4.5 — **P4 전용(실 API, 코어 v0.11.0)**. 비밀번호 강제 변경(#165)과 본인
// 변경을 실스택에서 끝까지 본다. P3(구 API)에서는 돌리지 않는다 — P3의 새 경로 UI 증거는
// password-change-mock.spec.ts, 구 API 호환 증거는 password-compat-oldapi.spec.ts다.
//
// 관리자(storageState) API로 `e2e-p4-` 접두 사용자를 만들고, **beforeAll·afterAll에서 그 접두 사용자를
// 관리자 API 목록으로 찾아 모두 지운다**(응답 코드·남은 수 출력). 모듈 변수에 기대지 않으므로 retries로
// 워커가 다시 떠도, 이전 실행이 중간에 끊겨도 남는 사용자가 없다(멱등). 남기면 P4 롤백 리허설의 JWT
// 판단 게이트(`password_changed_at IS NOT NULL` 사용자 0)가 멈춘다. 비밀번호는 실행마다 무작위·비출력.

// auth.setup.ts STORAGE_STATE — browser.newContext의 상대 경로는 cwd 기준이라 이 파일 기준 절대 경로로
const ADMIN_STORAGE_STATE = path.resolve(__dirname, "../playwright/.auth/admin.json");
const E2E_EMAIL_PREFIX = "e2e-p4-";
const E2E_EMAIL_DOMAIN = "@biznotes.dev";

function newPassword(): string {
  return `E2e-${randomUUID().replaceAll("-", "").slice(0, 16)}`;
}

/** 관리자 API로 사용자 생성 — 코어가 must_change_password=true로 만든다. */
async function createUser(page: Page, label: string, password: string) {
  const email = `${E2E_EMAIL_PREFIX}${label}-${Date.now()}${E2E_EMAIL_DOMAIN}`;
  const res = await page.request.post(`${API_BASE_URL}/api/v1/admin/users`, {
    data: { email, name: `E2E ${label}`, password, role: "user" },
  });
  expect(res.status(), "관리자 사용자 생성").toBe(201);
  const user = (await res.json()) as { id: string; must_change_password?: boolean };
  expect(user.must_change_password, "관리자가 만든 사용자는 강제 변경 대상").toBe(true);
  return { id: user.id, email };
}

async function changePasswordOnPage(page: Page, current: string, next: string) {
  await page.getByLabel("현재 비밀번호").fill(current);
  await page.getByLabel("새 비밀번호", { exact: true }).fill(next);
  await page.getByLabel("새 비밀번호 확인").fill(next);
  await page.getByRole("button", { name: "비밀번호 변경" }).click();
}

/** BL-172(AC-4.9) — 변경 성공 토스트. 다른 기기 세션이 끊긴다는 안내다. */
const PASSWORD_CHANGED_TOAST = "비밀번호를 변경했습니다. 다른 기기에서는 다시 로그인해야 합니다";

/** 새 컨텍스트 로그인 — 앞 스펙들의 로그인으로 레이트 리밋 창이 차 있으면 한 번 기다렸다 다시 한다(HCI-P2-3, helpers 참고). */
async function loginAs(page: Page, email: string, password: string) {
  const login = await loginViaUiAfterRateLimit(page, email, password);
  expect(login.status(), `${email} 로그인 응답`).toBe(200);
}

async function freshContext(browser: Browser) {
  return browser.newContext({ storageState: { cookies: [], origins: [] } });
}

/** 관리자 API 목록에서 이 스펙의 사용자(`e2e-p4-…@biznotes.dev`)를 찾아 모두 지우고 남은 수를 단언한다. */
async function purgeE2eUsers(browser: Browser, phase: string) {
  const admin = await browser.newContext({ storageState: ADMIN_STORAGE_STATE });
  const listE2eUsers = async () => {
    const res = await admin.request.get(`${API_BASE_URL}/api/v1/admin/users`);
    expect(res.status(), "관리자 사용자 목록").toBe(200);
    return ((await res.json()) as { id: string; email: string }[]).filter(
      (u) => u.email.startsWith(E2E_EMAIL_PREFIX) && u.email.endsWith(E2E_EMAIL_DOMAIN),
    );
  };
  try {
    for (const user of await listE2eUsers()) {
      const status = await admin.request
        .delete(`${API_BASE_URL}/api/v1/admin/users/${user.id}`)
        .then((r) => r.status(), (err: unknown) => `요청 실패: ${String(err)}`);
      console.log(`password-change(${phase}): 사용자 ${user.email} (${user.id}) 삭제 → ${status}`);
      expect.soft(status, `사용자 ${user.email} 정리`).toBe(204);
    }
    const left = await listE2eUsers();
    console.log(`password-change(${phase}): 남은 ${E2E_EMAIL_PREFIX} 사용자 ${left.length}명`);
    expect.soft(left, `${phase} 정리 뒤 ${E2E_EMAIL_PREFIX} 사용자 0`).toEqual([]);
  } finally {
    await admin.close();
  }
}

test.beforeAll(async ({ browser }) => purgeE2eUsers(browser, "시작 전"));
test.afterAll(async ({ browser }) => purgeE2eUsers(browser, "종료 뒤"));

test("강제 변경 — 관리자가 만든 사용자는 변경 화면으로 유도되고 변경 후 앱에 들어간다(실 API)", async ({
  page,
  browser,
}) => {
  test.setTimeout(60_000 + LOGIN_RATE_LIMIT_WINDOW_MS); // 로그인 1회의 429 대기
  await gotoDashboard(page); // 관리자 세션
  const temporary = newPassword();
  const user = await createUser(page, "forced", temporary);

  const context = await freshContext(browser);
  const userPage = await context.newPage();
  try {
    await loginAs(userPage, user.email, temporary);
    await expect(userPage).toHaveURL(/\/change-password(\?|$)/);
    await expect(userPage.getByTestId("change-password-forced")).toBeVisible();
    await expect(userPage.getByRole("link", { name: "취소" })).toHaveCount(0);
    // 변경 전에는 다른 API가 403 password_change_required다(코어 게이트)
    const blocked = await userPage.request.get(`${API_BASE_URL}/api/v1/notebooks`);
    expect(blocked.status()).toBe(403);
    expect(((await blocked.json()) as { detail: { code: string } }).detail.code).toBe(
      "password_change_required",
    );
    await userPage.screenshot({ path: "test-results/p4-forced-change-password.png", fullPage: true });

    await changePasswordOnPage(userPage, temporary, newPassword());
    await expect(userPage.getByText(PASSWORD_CHANGED_TOAST)).toBeVisible();

    await expect(userPage.getByRole("heading", { name: "노트북", exact: true })).toBeVisible({
      timeout: 10_000,
    });
    const me = await userPage.request.get(`${API_BASE_URL}/api/v1/auth/me`);
    expect(me.status()).toBe(200);
    expect(((await me.json()) as { must_change_password: boolean }).must_change_password).toBe(false);
  } finally {
    await context.close();
  }
});

test("본인 변경 — 사용자 메뉴에서 바꾸면 다른 세션이 끊긴다(실 API)", async ({ page, browser }) => {
  test.setTimeout(60_000 + 2 * LOGIN_RATE_LIMIT_WINDOW_MS); // 로그인 2회 각각의 429 대기
  await gotoDashboard(page); // 관리자 세션
  const temporary = newPassword();
  const user = await createUser(page, "self", temporary);
  const first = newPassword();
  const second = newPassword();

  const contextA = await freshContext(browser);
  const contextB = await freshContext(browser);
  const pageA = await contextA.newPage();
  const pageB = await contextB.newPage();
  try {
    // 세션 A: 첫 로그인 강제 변경을 마쳐 일반 상태로 만든다
    await loginAs(pageA, user.email, temporary);
    await expect(pageA).toHaveURL(/\/change-password(\?|$)/);
    await changePasswordOnPage(pageA, temporary, first);
    await expect(pageA.getByRole("heading", { name: "노트북", exact: true })).toBeVisible({
      timeout: 10_000,
    });

    // 세션 B: 같은 사용자의 다른 기기
    await loginAs(pageB, user.email, first);
    await expect(pageB.getByRole("heading", { name: "노트북", exact: true })).toBeVisible({
      timeout: 10_000,
    });

    // 세션 A: 사용자 메뉴 → 비밀번호 변경(본인 변경 — 취소 경로가 있다)
    await pageA.addStyleTag({
      content: "*,*::before,*::after{transition:none!important;animation:none!important}",
    });
    await pageA.getByTestId("user-menu").click();
    await pageA.getByRole("menuitem", { name: "비밀번호 변경" }).click();
    await expect(pageA).toHaveURL(/\/change-password(\?|$)/);
    await expect(pageA.getByRole("link", { name: "취소" })).toBeVisible();
    await changePasswordOnPage(pageA, first, second);
    // 다른 기기(세션 B)가 곧 끊긴다는 안내(BL-172) — 아래에서 실제로 끊기는 것을 확인한다
    await expect(pageA.getByText(PASSWORD_CHANGED_TOAST)).toBeVisible();
    await expect(pageA.getByRole("heading", { name: "노트북", exact: true })).toBeVisible({
      timeout: 10_000,
    });

    // 세션 B: 변경 전에 발급된 토큰(access·refresh)이 끊겨 로그인 화면으로 간다(pwv)
    const stale = await pageB.request.get(`${API_BASE_URL}/api/v1/auth/me`);
    expect(stale.status(), "다른 세션의 access").toBe(401);
    await pageB.goto("/");
    await expect(pageB).toHaveURL(/\/login(\?|$)/, { timeout: 10_000 });

    // 세션 A는 새 토큰으로 계속 쓴다
    const meA = await pageA.request.get(`${API_BASE_URL}/api/v1/auth/me`);
    expect(meA.status()).toBe(200);
  } finally {
    await contextA.close();
    await contextB.close();
  }
});
