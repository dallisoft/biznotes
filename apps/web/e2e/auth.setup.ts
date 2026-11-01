import { expect, test as setup } from "@playwright/test";
import { ADMIN_CREDENTIALS, LOGIN_RATE_LIMIT_WINDOW_MS, loginViaUiAfterRateLimit } from "./helpers";

export const STORAGE_STATE = "playwright/.auth/admin.json";

// 전체 스위트에서 로그인 1회 — 로그인 레이트 리밋(10/분) 회피 + 속도 개선
setup("authenticate", async ({ page }) => {
  // 게이트는 실행 단위(스펙 파일)마다 이 setup으로 로그인한다. 짧은 단위가 잇달아 돌면 앞 단위들의 로그인으로
  // 레이트 리밋 창이 찰 수 있으므로 429면 한 번 기다렸다 다시 로그인한다(ui-ux-redesign HCI-P2-3, helpers 참고)
  setup.setTimeout(60_000 + LOGIN_RATE_LIMIT_WINDOW_MS);
  const login = await loginViaUiAfterRateLimit(page, ADMIN_CREDENTIALS.email, ADMIN_CREDENTIALS.password);
  expect(login.status(), "관리자 로그인 응답").toBe(200);
  await expect(page.getByRole("heading", { name: "노트북", exact: true })).toBeVisible({
    timeout: 10_000,
  });
  await page.context().storageState({ path: STORAGE_STATE });
});
