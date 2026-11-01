import { expect, test, type Page } from "@playwright/test";
import { fulfillJson } from "./helpers";

// core-v0.11.0-upgrade AC-3.8 — 비밀번호 변경 흐름의 UI 동작(새 API 형태)을 mock으로 보인다.
// P3 시점 실스택은 구 API라 새 경로는 mock으로만 확인한다(구 API 호환 증거는
// password-compat-oldapi.spec.ts의 mock 없는 3건). 실 API 흐름은 P4 password-change.spec.ts.
//
// mock 대상은 `/api/v1/auth/me`·`/api/v1/auth/password`·대표 403 엔드포인트(`/api/v1/notebooks`)뿐이다.
// **새 컨텍스트(storageState 미사용)** — 실제 세션이 없으므로 다른 API는 실서버에서 401로 끝나고,
// 앱 진입 여부는 mock한 `/auth/me`가 정한다.
test.use({ storageState: { cookies: [], origins: [] } });

const UNSUPPORTED = "이 서버는 비밀번호 변경을 지원하지 않습니다";

function meNew(mustChange: boolean) {
  return {
    id: "00000000-0000-4000-8000-0000000000e2",
    email: "mock-user@example.com",
    name: "Mock 사용자",
    role: "user",
    is_active: true,
    must_change_password: mustChange,
    created_at: "2026-10-10T00:00:00Z",
  };
}
function meOld() {
  const me: Record<string, unknown> = meNew(false);
  delete me.must_change_password; // 구 API(코어 v0.10.x) — 키가 없다
  return me;
}

async function mockMe(page: Page, current: () => unknown) {
  await page.route("**/api/v1/auth/me", (route) => fulfillJson(route, 200, current()));
}

/** `POST /api/v1/auth/password` 기록 — 응답은 `respond`가 정한다. */
async function mockPasswordChange(
  page: Page,
  respond: (body: Record<string, string>) => { status: number; body: unknown },
) {
  const posted: Record<string, string>[] = [];
  await page.route("**/api/v1/auth/password", async (route) => {
    if (route.request().method() !== "POST") return fulfillJson(route, 204, null);
    const body = route.request().postDataJSON() as Record<string, string>;
    posted.push(body);
    const out = respond(body);
    await fulfillJson(route, out.status, out.body);
  });
  return posted;
}

function tokenResponse(user: unknown) {
  return { access_token: "mock-access", refresh_token: "mock-refresh", token_type: "bearer", user };
}

async function fillChangeForm(page: Page, current: string, next: string) {
  await page.getByLabel("현재 비밀번호").fill(current);
  await page.getByLabel("새 비밀번호", { exact: true }).fill(next);
  await page.getByLabel("새 비밀번호 확인").fill(next);
}

test("새 API로 캐시한 브라우저가 구 API로 바뀌면 변경 화면은 fresh 판정으로 미지원 안내, POST 0회(mock 전환)", async ({
  page,
}) => {
  let shape: "new" | "old" = "new";
  await mockMe(page, () => (shape === "new" ? meNew(false) : meOld()));
  const posted = await mockPasswordChange(page, () => ({ status: 500, body: {} }));

  await page.goto("/workflows");
  // 메뉴 트랜지션으로 클릭이 불안정해지지 않게(셸 스모크와 같은 처리)
  await page.addStyleTag({
    content: "*,*::before,*::after{transition:none!important;animation:none!important}",
  });
  await page.getByTestId("user-menu").click();
  const item = page.getByRole("menuitem", { name: "비밀번호 변경" });
  await expect(item).toBeVisible(); // 캐시된 me(새 형태) 기준으로 메뉴가 보인다

  shape = "old"; // 같은 브라우저 컨텍스트에서 API가 구 형태로 바뀐다(API 롤백)
  const probe = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/v1/auth/me");
  await item.click();
  await expect(page).toHaveURL(/\/change-password\?redirect=%2Fworkflows$/);
  await probe;

  await expect(page.getByText(UNSUPPORTED)).toBeVisible();
  await expect(page.getByRole("button", { name: "비밀번호 변경" })).toHaveCount(0);
  expect(posted, "구 형태로 판정되면 POST /auth/password를 부르지 않는다").toHaveLength(0);
});

test("비밀번호 변경 — 403 code를 받으면 변경 화면으로 간다(mock)", async ({ page }) => {
  await mockMe(page, () => meNew(false)); // 캐시는 변경 불필요(예: 로그인 뒤 관리자가 재설정)
  let forbidden = 0;
  await page.route("**/api/v1/notebooks**", (route) => {
    forbidden += 1;
    return fulfillJson(route, 403, {
      detail: { code: "password_change_required", detail: "비밀번호를 변경해야 합니다" },
    });
  });

  await page.goto("/workflows");

  await expect(page).toHaveURL(/\/change-password\?redirect=%2Fworkflows$/);
  expect(forbidden).toBeGreaterThan(0);
  // 변경 화면은 fresh 프로브로 지원을 판정해 제출을 연다
  await expect(page.getByRole("button", { name: "비밀번호 변경" })).toBeEnabled();
});

test("비밀번호 변경 — 서버 오류 문구 매핑(mock)", async ({ page }) => {
  await mockMe(page, () => meNew(true));
  const replies = [
    { status: 400, body: { detail: { code: "invalid_current_password", detail: "현재 비밀번호가 올바르지 않습니다" } } },
    { status: 422, body: { detail: { code: "invalid_new_password", reason: "too_short", detail: "x" } } },
    { status: 422, body: { detail: { code: "invalid_new_password", reason: "too_long_bytes", detail: "x" } } },
    { status: 422, body: { detail: { code: "invalid_new_password", reason: "same_as_current", detail: "x" } } },
    { status: 429, body: { detail: "Too Many Requests" } },
  ];
  const expected = [
    "현재 비밀번호가 올바르지 않습니다.",
    "새 비밀번호는 8자 이상이어야 합니다.",
    "새 비밀번호가 너무 깁니다(UTF-8 72바이트 이하).",
    "새 비밀번호는 현재 비밀번호와 달라야 합니다.",
    "요청이 너무 많습니다. 잠시 후 다시 시도하세요.",
  ];
  const posted = await mockPasswordChange(page, () => replies.shift() ?? { status: 500, body: {} });

  await page.goto("/change-password");
  await expect(page.getByTestId("change-password-forced")).toBeVisible();
  // 강제 상태 — 「나중에」·취소 경로가 없다(로그아웃만)
  await expect(page.getByRole("link", { name: "취소" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "로그아웃" })).toBeVisible();

  const current = "Mock-Current-1";
  const next = "Mock-Next-Password-2";
  await fillChangeForm(page, current, next);
  for (const message of expected) {
    await page.getByRole("button", { name: "비밀번호 변경" }).click();
    const alert = page.getByTestId("change-password-error");
    await expect(alert).toHaveText(message);
    await expect(alert).not.toContainText(current);
    await expect(alert).not.toContainText(next);
  }
  expect(posted).toHaveLength(expected.length);
  expect(posted[0]).toEqual({ current_password: current, new_password: next });
  expect(new URL(page.url()).pathname).toBe("/change-password");
});

test("비밀번호 변경 — 성공하면 원래 경로로 돌아간다(mock)", async ({ page }) => {
  let forced = true;
  await mockMe(page, () => meNew(forced));
  const posted = await mockPasswordChange(page, () => {
    forced = false; // 서버가 플래그를 내린다
    return { status: 200, body: tokenResponse(meNew(false)) };
  });

  // 강제 변경 계정이 앱 화면을 열면 레이아웃이 변경 화면으로 보낸다(AC-3.2)
  await page.goto("/workflows");
  await expect(page).toHaveURL(/\/change-password\?redirect=%2Fworkflows$/);
  await expect(page.getByTestId("change-password-forced")).toBeVisible();
  await page.screenshot({ path: "test-results/p3-mock-forced-change-password.png", fullPage: true });

  await fillChangeForm(page, "Mock-Current-1", "Mock-Next-Password-2");
  await page.getByRole("button", { name: "비밀번호 변경" }).click();

  await expect(page).toHaveURL(/\/workflows$/);
  // BL-172(AC-4.9): 성공 토스트는 셸 밖 → 셸 안 이동 뒤에도 보인다(루트 Toaster)
  await expect(
    page.getByText("비밀번호를 변경했습니다. 다른 기기에서는 다시 로그인해야 합니다"),
  ).toBeVisible();
  await expect(page.getByTestId("user-menu")).toBeVisible(); // 앱 셸에 들어왔다
  expect(posted).toHaveLength(1);
});

test("비밀번호 변경 — 외부 redirect는 무시한다(mock)", async ({ page }) => {
  let forced = true;
  await mockMe(page, () => meNew(forced));
  await mockPasswordChange(page, () => {
    forced = false;
    return { status: 200, body: tokenResponse(meNew(false)) };
  });
  const outbound: string[] = [];
  page.on("request", (req) => {
    if (new URL(req.url()).hostname === "attacker.example") outbound.push(req.url());
  });

  await page.goto("/change-password?redirect=%2F%2Fattacker.example%2Fp");
  await fillChangeForm(page, "Mock-Current-1", "Mock-Next-Password-2");
  await page.getByRole("button", { name: "비밀번호 변경" }).click();

  await expect(page.getByRole("heading", { name: "노트북", exact: true })).toBeVisible();
  const landed = new URL(page.url());
  expect(landed.origin).toBe(new URL(test.info().project.use.baseURL ?? "http://localhost:23000").origin);
  expect(landed.pathname).toBe("/");
  expect(outbound).toEqual([]);
});
