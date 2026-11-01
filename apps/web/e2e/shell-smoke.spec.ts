import { expect, test } from "@playwright/test";
import { ADMIN_CREDENTIALS, LOGIN_RATE_LIMIT_WINDOW_MS, loginViaUiAfterRateLimit } from "./helpers";

// 셸 상호작용 스모크 — 테마 스위처/컬러 프리셋/사용자 메뉴.
// 목적: ui-redesign-phase-g의 Base UI #31류(메뉴 오픈 시 클라이언트 예외) 회귀 자동 포착
// + CSP enforce 전환(§3c) 하에서 인라인 테마 스크립트/메뉴가 콘솔 위반 없이 동작하는지 감시.
//
// 끝에서 로그아웃하므로 **자기 세션으로 로그인한다**(새 컨텍스트). 공유 storageState로 로그아웃하면
// 서버가 그 access·refresh를 denylist에 올려, 파일 순서상 뒤에 도는 스펙(source-metadata·
// workflow-editor-surface 등)이 401로 로그인 화면에 떨어진다(core-v0.11.0-upgrade P3 실측).
test.use({ storageState: { cookies: [], origins: [] } });

test("셸: 테마 전환 · 컬러 프리셋 · 사용자 메뉴", async ({ page }) => {
  // 앞 스펙들의 로그인으로 레이트 리밋 창이 차 있으면 한 번 기다렸다 다시 로그인한다(HCI-P2-3, helpers 참고)
  test.setTimeout(60_000 + LOGIN_RATE_LIMIT_WINDOW_MS);
  // 메뉴 오픈/조작 중 발생하는 클라이언트 예외·CSP 위반을 수집해 단언
  const clientErrors: string[] = [];
  page.on("pageerror", (err) => clientErrors.push(err.message));
  page.on("console", (msg) => {
    const text = msg.text();
    if (msg.type() === "error" && /Content Security Policy|CSP/i.test(text)) {
      clientErrors.push(text);
    }
  });

  const login = await loginViaUiAfterRateLimit(page, ADMIN_CREDENTIALS.email, ADMIN_CREDENTIALS.password);
  expect(login.status(), "관리자 로그인 응답").toBe(200);
  await expect(page.getByRole("heading", { name: "노트북", exact: true })).toBeVisible({
    timeout: 10_000,
  });
  // Base UI 드롭다운의 open/close 트랜지션 때문에 메뉴 항목이 "not stable"로 판정돼
  // 클릭이 불안정해진다 — 테스트 중 전환/애니메이션 비활성화로 안정화.
  await page.addStyleTag({
    content: "*,*::before,*::after{transition:none!important;animation:none!important}",
  });
  const html = page.locator("html");

  // 화면 모드 라디오는 closeOnClick=false(Base UI 기본) — 메뉴를 한 번 열고 순차 선택한다.
  await page.getByTestId("theme-switcher").click();

  // 1) 화면 모드: 라이트 → 다크 토글 (html.dark 클래스)
  await page.getByRole("menuitemradio", { name: "라이트" }).click();
  await expect(html).not.toHaveClass(/dark/);
  await page.getByRole("menuitemradio", { name: "다크" }).click();
  await expect(html).toHaveClass(/dark/);

  // 2) 브랜드 컬러 프리셋: 틸 → html[data-theme=teal] + localStorage (같은 열린 메뉴 내)
  await page.getByRole("menuitemradio", { name: "틸" }).click();
  await expect(html).toHaveAttribute("data-theme", "teal");
  expect(await page.evaluate(() => localStorage.getItem("color-theme"))).toBe("teal");

  // 메뉴 닫기 (사용자 메뉴 열기 전)
  await page.keyboard.press("Escape");

  // 3) 사용자 메뉴: 열기 → 이메일/항목 렌더 → 로그아웃 → 로그인 화면 복귀
  await page.getByTestId("user-menu").click();
  await expect(page.getByText(ADMIN_CREDENTIALS.email)).toBeVisible();
  await page.getByRole("menuitem", { name: "로그아웃" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("button", { name: "로그인" })).toBeVisible();

  // 셸 조작 전 구간에서 클라이언트 예외·CSP 위반이 없어야 한다
  expect(clientErrors, clientErrors.join("\n")).toEqual([]);
});
