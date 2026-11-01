import { expect, test } from "@playwright/test";
import { loginViaUi } from "./helpers";

// #93 — 로그인 실패 토스트는 상태별 고정 문구만 보인다. 서버 영문 메시지(401 「Invalid email or password」)와
// FastAPI 검증 422 본문의 `input`(입력한 비밀번호)은 화면 어디에도 나오지 않는다. 실 API로 두 번 실패한다
// (로그인 레이트 리밋 10회/60초 안). 공유 관리자 storageState를 쓰지 않는다.
test.use({ storageState: { cookies: [], origins: [] } });

test("로그인 실패 — 401·422 모두 고정 문구이고 입력 비밀번호·서버 메시지가 보이지 않는다", async ({ page }) => {
  const password = `pw-${Date.now().toString(36)}`;

  // 401 — 없는 계정
  const unauthorized = page.waitForResponse(
    (r) => new URL(r.url()).pathname === "/api/v1/auth/login" && r.request().method() === "POST",
  );
  await loginViaUi(page, `nobody-${Date.now().toString(36)}@example.com`, password);
  expect((await unauthorized).status()).toBe(401);
  await expect(page.getByText("로그인하지 못했습니다")).toBeVisible();
  await expect(page.getByText("이메일 또는 비밀번호가 올바르지 않습니다.")).toBeVisible();
  await expect(page.getByText("Invalid email or password")).toHaveCount(0);

  // 422 — 브라우저 type=email은 통과시키지만 서버 EmailStr이 거부하는 주소. 검증 본문 input에 입력값이 실린다
  const invalid = page.waitForResponse(
    (r) => new URL(r.url()).pathname === "/api/v1/auth/login" && r.request().method() === "POST",
  );
  await loginViaUi(page, "a@b", password);
  const res = await invalid;
  expect(res.status()).toBe(422);
  await expect(page.getByText("이메일 또는 비밀번호 형식이 올바르지 않습니다.")).toBeVisible();
  await expect(page.locator("[data-sonner-toaster]")).not.toContainText(password);
  await expect(page.locator("[data-sonner-toaster]")).not.toContainText("a@b");
  await expect(page.locator("body")).not.toContainText("value is not a valid email");
  await page.screenshot({ path: "test-results/login-error.png", fullPage: true });
});
