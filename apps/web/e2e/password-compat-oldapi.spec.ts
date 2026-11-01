import { expect, test, type Page } from "@playwright/test";
import {
  ADMIN_CREDENTIALS,
  API_BASE_URL,
  createNotebookViaApi,
  deleteNotebookViaApi,
  gotoDashboard,
  LOGIN_RATE_LIMIT_WINDOW_MS,
  loginViaUiAfterRateLimit,
} from "./helpers";

// core-v0.11.0-upgrade AC-3.8 — 새 web이 **구 API(코어 v0.10.6, 실스택 그대로)**에서 비밀번호 변경
// 표면을 숨기고 나머지가 동작함을 보인다. 롤백 시 web을 되돌리지 않는 결정(D-25′)의 직접 증거라
// **mock이 없다**(page.route 0). 대상이 구 API인지 먼저 확인하고, 새 API면 실패한다(오인 방지).
//
// 실행 전제: `make build s=web` 뒤 web을 재생성한 dev 스택(api는 구 이미지), 시드 관리자 계정.

const UNSUPPORTED = "이 서버는 비밀번호 변경을 지원하지 않습니다";

/** 대상 API가 구 API인지 — `/auth/me` 응답에 `must_change_password` 키가 없어야 한다. */
async function assertOldApi(page: Page) {
  const res = await page.request.get(`${API_BASE_URL}/api/v1/auth/me`);
  expect(res.status(), "인증된 /auth/me").toBe(200);
  const me = (await res.json()) as Record<string, unknown>;
  expect(Object.keys(me), "구 API 전제 — 새 API면 이 스펙의 대상이 아니다").not.toContain(
    "must_change_password",
  );
}

/** `/api/v1/auth/password`로 나간 요청 수(어떤 메서드든). */
function countPasswordRequests(page: Page): () => number {
  let count = 0;
  page.on("request", (req) => {
    if (new URL(req.url()).pathname === "/api/v1/auth/password") count += 1;
  });
  return () => count;
}

test("구 API — 사용자 메뉴에 비밀번호 변경이 없다(실 구 API)", async ({ page }) => {
  await gotoDashboard(page);
  await assertOldApi(page);
  await page.addStyleTag({
    content: "*,*::before,*::after{transition:none!important;animation:none!important}",
  });

  await page.getByTestId("user-menu").click();
  // 메뉴가 실제로 열린 뒤에 「없음」을 판정한다(빈 화면에서의 없음은 증거가 아니다)
  await expect(page.getByRole("menuitem", { name: "로그아웃" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "비밀번호 변경" })).toHaveCount(0);
  await expect(page.getByTestId("user-menu-change-password")).toHaveCount(0);
  await page.keyboard.press("Escape");
});

test("구 API — /change-password 직접 진입은 미지원 안내(실 구 API)", async ({ page }) => {
  await gotoDashboard(page);
  await assertOldApi(page);
  const passwordRequests = countPasswordRequests(page);

  const probe = page.waitForResponse(
    (r) => new URL(r.url()).pathname === "/api/v1/auth/me" && r.request().method() === "GET",
  );
  await page.goto("/change-password?redirect=%2F");
  expect((await probe).status(), "변경 화면의 fresh 프로브").toBe(200);

  await expect(page.getByText(UNSUPPORTED)).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("button", { name: "비밀번호 변경" })).toHaveCount(0);
  await expect(page.getByLabel("새 비밀번호", { exact: true })).toHaveCount(0);
  await page.screenshot({ path: "test-results/p3-oldapi-change-password-unsupported.png", fullPage: true });
  expect(passwordRequests(), "구 API에 POST /auth/password를 부르지 않는다").toBe(0);

  // 돌아가기는 내부 경로(redirect)로
  await page.getByRole("link", { name: "돌아가기" }).click();
  await expect(page.getByRole("heading", { name: "노트북", exact: true })).toBeVisible();
});

test.describe("로그인부터(새 컨텍스트)", () => {
  // storageState 없이 실제 로그인 화면부터 — 로그인 응답(user)에 키가 없어도 정상 경로로 간다
  test.use({ storageState: { cookies: [], origins: [] } });

  test("구 API — 로그인·대시보드·채팅 1턴(실 구 API)", async ({ page }) => {
    // 앞 스펙들의 로그인으로 레이트 리밋 창이 차 있으면 한 번 기다렸다 다시 로그인한다(ui-ux-redesign HCI-P2-3, helpers 참고)
    test.setTimeout(120_000 + LOGIN_RATE_LIMIT_WINDOW_MS);
    const passwordRequests = countPasswordRequests(page);

    const login = await loginViaUiAfterRateLimit(page, ADMIN_CREDENTIALS.email, ADMIN_CREDENTIALS.password);
    expect(login.status(), "관리자 로그인 응답").toBe(200);
    const loginBody = (await login.json()) as { user: Record<string, unknown> };
    expect(Object.keys(loginBody.user), "구 API 로그인 응답").not.toContain("must_change_password");

    // 변경 화면이 아니라 대시보드로 간다
    await expect(page.getByRole("heading", { name: "노트북", exact: true })).toBeVisible({
      timeout: 10_000,
    });
    expect(new URL(page.url()).pathname).toBe("/");
    await assertOldApi(page);

    // 채팅 1턴 — 노트북은 이 테스트가 만들고 지운다
    const notebookId = await createNotebookViaApi(page, `P3 구 API 채팅 ${Date.now()}`);
    try {
      await page.goto(`/notebooks/${notebookId}`);
      await page.getByTestId("chat-input").fill("안녕하세요. 한 문장으로 인사해 주세요.");
      const sent = page.waitForResponse(
        (r) =>
          /\/api\/v1\/chat\/sessions\/[^/]+\/messages$/.test(new URL(r.url()).pathname) &&
          r.request().method() === "POST",
        { timeout: 60_000 },
      );
      await page.getByRole("button", { name: "전송" }).click();
      const res = await sent;
      expect(res.status(), "채팅 SSE 응답").toBe(200);
      await res.finished();

      const messages = page.getByTestId("chat-messages");
      await expect(messages.getByText("안녕하세요. 한 문장으로 인사해 주세요.")).toBeVisible();
      // 저장된 어시스턴트 메시지(스트리밍 아님)에만 「메모에 저장」이 붙는다 — 1턴 완료 표지
      await expect(messages.getByTestId("save-to-note")).toHaveCount(1, { timeout: 60_000 });
    } finally {
      await deleteNotebookViaApi(page, notebookId, "password-compat-oldapi");
    }
    expect(passwordRequests()).toBe(0);
  });
});
