import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  API_BASE_URL,
  createNotebookViaApi,
  DEMO_CREDENTIALS as DEMO,
  deleteNotebookViaApi,
  gotoDashboard,
  LOGIN_RATE_LIMIT_WINDOW_MS,
  loginViaUiAfterRateLimit,
} from "./helpers";

// ui-ux-redesign P2 — 단일 셸·모바일 드로어·스크롤 구획·랜드마크(plan §6.2.2·§6.2.3·§6.8, AC-2.3·AC-2.5·AC-2.11).
// 공유 storageState는 시드 관리자다. 일반 사용자 테스트는 새 컨텍스트로 demo 계정에 로그인한다(로그아웃하지 않는다 —
// 로그아웃은 그 세션 토큰을 denylist에 올린다). 이 스펙이 만든 노트북·워크플로우는 finally에서 API로 지운다.

const MOBILE = { width: 390, height: 844 };
const DESKTOP = { width: 1280, height: 720 };
const RUN_ID = `${Date.now()}`;

/** Base UI 열림·닫힘 트랜지션 때문에 항목이 "not stable"로 판정되지 않게 한다(셸 스모크와 같은 처리). */
async function disableMotion(page: Page) {
  await page.addStyleTag({
    content: "*,*::before,*::after{transition:none!important;animation:none!important}",
  });
}

async function openDrawer(page: Page): Promise<Locator> {
  await page.getByRole("button", { name: "메뉴 열기" }).click();
  const drawer = page.getByTestId("nav-drawer");
  await expect(drawer).toBeVisible();
  return drawer;
}

async function focusIsInside(locator: Locator): Promise<boolean> {
  return locator.evaluate((el) => el.contains(document.activeElement));
}

/**
 * 포커스가 드로어 안에 **자리 잡는지** — Base UI 포커스 가둠은 경계에서 잠깐 가드 요소(팝업 바깥 형제)에 포커스를
 * 줬다가 곧바로 처음·마지막 항목으로 옮긴다. 그 순간값이 아니라 자리 잡은 값을 본다(밖에 머물면 시간 초과로 실패).
 */
async function expectFocusSettlesInside(drawer: Locator, label: string) {
  await expect.poll(() => focusIsInside(drawer), { message: label, timeout: 2_000 }).toBe(true);
}

/** Tab을 눌러 대상에 포커스가 닿을 때까지(최대 limit회). 닿은 횟수를 돌려준다. */
async function tabUntilFocused(page: Page, target: Locator, limit = 90): Promise<number> {
  for (let i = 1; i <= limit; i += 1) {
    await page.keyboard.press("Tab");
    if (await target.evaluate((el) => el === document.activeElement)) return i;
  }
  throw new Error(`Tab ${limit}회 안에 대상에 포커스가 닿지 않았다`);
}

/** 「관리」를 펼친다. 이미 펼쳐져 있었으면(저장값) true. */
async function ensureAdminGroupOpen(scope: Locator): Promise<boolean> {
  const toggle = scope.getByRole("button", { name: "관리" });
  const wasOpen = (await toggle.getAttribute("aria-expanded")) === "true";
  if (!wasOpen) await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(scope.getByTestId("admin-nav-group").getByRole("link")).toHaveCount(11);
  return wasOpen;
}

test("모바일 드로어 — 메뉴 버튼으로 열고 주 메뉴·로그아웃 확인, 항목 이동 시 닫힘, Esc 닫힘 후 포커스 복귀, Tab 순환", async ({
  page,
}) => {
  await page.setViewportSize(MOBILE);
  await gotoDashboard(page);
  await disableMotion(page);
  const menuButton = page.getByRole("button", { name: "메뉴 열기" });
  await expect(menuButton).toBeVisible();
  // 데스크톱 사이드바는 그리지 않는다(모바일은 상단 바 + 드로어)
  await expect(page.getByTestId("app-sidebar")).toHaveCount(0);

  // 1) 열기 → 주 메뉴 3개와 「로그아웃」
  let drawer = await openDrawer(page);
  const primary = drawer.getByTestId("primary-nav").getByRole("link");
  await expect(primary).toHaveText(["노트북", "스킬", "워크플로우"]);
  await expect(drawer.getByRole("button", { name: "로그아웃" })).toBeVisible();
  await expect(drawer.getByRole("button", { name: "로그아웃" })).toBeInViewport();

  // 2) 포커스 가둠 — 첫 요소(「메뉴 닫기」)에서 Shift+Tab은 마지막 요소(테마)로, 거기서 Tab은 다시 처음으로
  const closeButton = drawer.getByRole("button", { name: "메뉴 닫기" });
  const themeButton = drawer.getByTestId("theme-switcher");
  await closeButton.focus();
  await page.keyboard.press("Shift+Tab");
  await expect(themeButton).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(closeButton).toBeFocused();
  // 연속 Tab·Shift+Tab 동안 포커스가 드로어 밖으로 나가지 않는다
  for (let i = 0; i < 30; i += 1) {
    await page.keyboard.press("Tab");
    await expectFocusSettlesInside(drawer, `Tab ${i + 1}회째 포커스가 드로어 안`);
  }
  for (let i = 0; i < 30; i += 1) {
    await page.keyboard.press("Shift+Tab");
    await expectFocusSettlesInside(drawer, `Shift+Tab ${i + 1}회째 포커스가 드로어 안`);
  }

  // 3) Esc로 닫힘 → 포커스가 메뉴 버튼으로
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(menuButton).toBeFocused();

  // 4) 항목을 누르면 이동하고 닫힌다
  drawer = await openDrawer(page);
  await drawer.getByTestId("primary-nav").getByRole("link", { name: "스킬" }).click();
  await expect(page).toHaveURL(/\/skills$/);
  await expect(page.getByTestId("nav-drawer")).toBeHidden();
  await expect(page.getByRole("heading", { name: "스킬", exact: true })).toBeVisible();

  // 5) 바깥(오버레이) 클릭으로도 닫힌다
  drawer = await openDrawer(page);
  await page.mouse.click(MOBILE.width - 10, MOBILE.height / 2);
  await expect(drawer).toBeHidden();
});

test("모바일 드로어 — 일반 사용자에게는 관리 그룹과 관리 링크가 없다", async ({ browser }) => {
  // 앞 스펙들의 로그인으로 레이트 리밋 창이 차 있으면 한 번 기다렸다 다시 로그인한다(helpers 참고)
  test.setTimeout(60_000 + LOGIN_RATE_LIMIT_WINDOW_MS);
  const context = await browser.newContext({
    storageState: { cookies: [], origins: [] },
    viewport: MOBILE,
  });
  const page = await context.newPage();
  try {
    const login = await loginViaUiAfterRateLimit(page, DEMO.email, DEMO.password);
    expect(login.status(), "demo 로그인 응답").toBe(200);
    await expect(page.getByRole("heading", { name: "노트북", exact: true })).toBeVisible({
      timeout: 10_000,
    });
    await disableMotion(page);
    const adminLinks = page.locator('a[href="/admin"], a[href^="/admin/"]');

    // 드로어: 주 메뉴·노트북·계정은 있고 「관리」·/admin 링크는 0개
    const drawer = await openDrawer(page);
    await expect(drawer.getByTestId("primary-nav").getByRole("link")).toHaveCount(3); // 일반 사용자 메뉴
    await expect(drawer.getByRole("button", { name: "로그아웃" })).toBeVisible();
    await expect(drawer.getByTestId("drawer-account-section")).toBeVisible();
    await expect(drawer.getByRole("button", { name: "관리" })).toHaveCount(0);
    await expect(drawer.getByTestId("admin-nav-group")).toHaveCount(0);
    await expect(adminLinks).toHaveCount(0);
    await page.keyboard.press("Escape");
    await expect(drawer).toBeHidden();

    // 같은 사용자의 데스크톱 사이드바에도 없다
    await page.setViewportSize(DESKTOP);
    const sidebar = page.getByTestId("app-sidebar");
    await expect(sidebar).toBeVisible();
    await expect(sidebar.getByTestId("primary-nav").getByRole("link")).toHaveCount(5);
    await expect(sidebar.getByRole("button", { name: "관리" })).toHaveCount(0);
    await expect(adminLinks).toHaveCount(0);
  } finally {
    await context.close();
  }
});

test("스크롤 구획 — 관리 펼침·노트북 11개·긴 이름에서 마지막 노트북까지 도달하고 로그아웃·사용자 메뉴가 항상 보인다", async ({
  page,
}) => {
  test.setTimeout(180_000);
  await gotoDashboard(page);
  const ids: string[] = [];
  try {
    // 60자 이름 11개 — 처음 만든 것이 가장 오래돼 10위 밖으로 밀리고, 그 노트북을 연다(현재 1개 추가 표시)
    for (let k = 1; k <= 11; k += 1) {
      const head = `셸 스크롤 ${RUN_ID} ${String(k).padStart(2, "0")} `;
      ids.push(await createNotebookViaApi(page, head.padEnd(60, "가")));
    }
    const current = ids[0];

    for (const viewport of [
      { width: 320, height: 568 },
      { width: 390, height: 844 },
      { width: 640, height: 360 }, // 1280×720 200% 확대에 해당
    ]) {
      await test.step(`드로어 ${viewport.width}×${viewport.height}`, async () => {
        await page.setViewportSize(viewport);
        await page.goto(`/notebooks/${current}`);
        await disableMotion(page);
        const drawer = await openDrawer(page);
        const wasOpen = await ensureAdminGroupOpen(drawer);
        const logout = drawer.getByRole("button", { name: "로그아웃" });
        await expect(logout).toBeInViewport(); // 스크롤 없이 보인다

        const list = drawer.getByTestId("sidebar-notebook-list");
        const currentLink = list.locator(`a[href="/notebooks/${current}"]`);
        await expect(currentLink).toHaveAttribute("aria-current", "page");
        // 관리가 펼친 채로 열리면(저장값) 진입 시 현재 노트북이 스크롤 영역 안에 보인다(활성 항목 nearest)
        if (wasOpen) await expect(currentLink).toBeInViewport();
        const notebookLinks = list.locator('a[href^="/notebooks/"]:not([href*="?"])');
        expect(await notebookLinks.count()).toBeGreaterThanOrEqual(10);
        const last = notebookLinks.last();

        // 키보드: 「메뉴 닫기」에서 Tab으로 마지막 노트북 링크·「모든 노트북 보기」까지
        await drawer.getByRole("button", { name: "메뉴 닫기" }).focus();
        await tabUntilFocused(page, last);
        await expect(last).toBeInViewport();
        await page.keyboard.press("Tab");
        await expect(drawer.getByRole("link", { name: /^모든 노트북 보기/ })).toBeFocused();
        await expect(logout).toBeInViewport();

        // 스크롤: 맨 위로 되돌린 뒤 스크롤 영역을 굴려 마지막 링크에 닿는다(창이 아니라 영역이 스크롤된다)
        const scroll = drawer.locator('[data-slot="sidebar-scroll"]');
        // 포커스를 고정 머리(스크롤 영역 밖)로 옮겨 포커스 이동에 따른 스크롤과 구분한다
        await drawer.getByRole("button", { name: "메뉴 닫기" }).focus();
        await scroll.evaluate((el) => el.scrollTo({ top: 0 }));
        await expect(last).not.toBeInViewport();
        await expect(logout).toBeInViewport();
        await last.scrollIntoViewIfNeeded();
        await expect(last).toBeInViewport();
        expect(await scroll.evaluate((el) => el.scrollTop), "스크롤 영역이 굴러갔다").toBeGreaterThan(0);
        expect(await page.evaluate(() => window.scrollY), "창은 스크롤되지 않는다").toBe(0);
        await expect(logout).toBeInViewport();

        await page.keyboard.press("Escape");
        await expect(drawer).toBeHidden();
      });
    }

    for (const viewport of [DESKTOP, { width: 1024, height: 480 }]) {
      await test.step(`데스크톱 사이드바 ${viewport.width}×${viewport.height}`, async () => {
        await page.setViewportSize(viewport);
        await page.goto(`/notebooks/${current}`);
        await disableMotion(page);
        const sidebar = page.getByTestId("app-sidebar");
        await expect(sidebar).toBeVisible();
        const wasOpen = await ensureAdminGroupOpen(sidebar);
        const userMenu = sidebar.getByTestId("user-menu");
        await expect(userMenu).toBeInViewport();

        const list = sidebar.getByTestId("sidebar-notebook-list");
        const currentLink = list.locator(`a[href="/notebooks/${current}"]`);
        await expect(currentLink).toHaveAttribute("aria-current", "page");
        // 진입 시 현재 노트북이 사이드바 스크롤 영역 안에 보인다(1024×480은 관리 11개 아래로 밀려 있어 스크롤이 필요)
        if (wasOpen) await expect(currentLink).toBeInViewport();
        const last = list.locator('a[href^="/notebooks/"]:not([href*="?"])').last();
        await sidebar.getByTestId("open-search").focus();
        await tabUntilFocused(page, last);
        await expect(last).toBeInViewport();
        await expect(userMenu).toBeInViewport();
        await page.keyboard.press("Tab");
        await expect(sidebar.getByRole("link", { name: /^모든 노트북 보기/ })).toBeFocused();
        await expect(userMenu).toBeInViewport();
      });
    }
  } finally {
    for (const id of ids) await deleteNotebookViaApi(page, id, "shell-mobile 스크롤");
  }
});

test("랜드마크 — 로그인 후 앱 경로마다 main은 1개이고 첫 Tab은 본문으로 건너뛰기, Enter로 본문 포커스", async ({
  page,
}) => {
  test.setTimeout(300_000);
  await page.setViewportSize(DESKTOP);
  await gotoDashboard(page);

  const notebookId = await createNotebookViaApi(page, `셸 랜드마크 ${RUN_ID}`);
  let workflowId: string | null = null;
  try {
    const wf = await page.request.post(`${API_BASE_URL}/api/v1/workflows`, {
      data: { name: `셸 랜드마크 ${RUN_ID}` },
    });
    expect(wf.status(), await wf.text()).toBe(201);
    workflowId = ((await wf.json()) as { id: string }).id;
    const skills = await page.request.get(`${API_BASE_URL}/api/v1/skills`);
    expect(skills.status()).toBe(200);
    const skillList = (await skills.json()) as { id: string; can_edit?: boolean }[];
    expect(skillList.length, "스킬 상세·편집 경로에 쓸 스킬이 1개 이상").toBeGreaterThan(0);
    const skillId = skillList[0].id;
    // 편집 화면은 편집 권한이 있어야 bleed(편집기) 분기를 그린다 — 없으면 권한 없음 분기만 확인한다
    const editable = skillList.find((s) => s.can_edit);
    const editSkillId = (editable ?? skillList[0]).id;
    if (!editable) {
      test.info().annotations.push({
        type: "note",
        description: "편집 가능한 스킬이 없어 /skills/{id}/edit의 bleed 높이 단언은 건너뛴다(권한 없음 분기만 확인)",
      });
    }

    // plan §6.2.7 사이트맵의 (app) 경로 — 셸 밖(로그인·비밀번호 변경)은 제외
    const paths = [
      "/",
      `/notebooks/${notebookId}`,
      "/skills",
      `/skills/${skillId}`,
      `/skills/${editSkillId}/edit`,
      "/workflows",
      `/workflows/${workflowId}/edit`,
      "/admin",
      "/admin/users",
      "/admin/llm",
      "/admin/rag",
      "/admin/agents",
      "/admin/skills",
      "/admin/mcp",
      "/admin/audit",
    ];

    const skip = page.getByRole("link", { name: "본문으로 건너뛰기" });
    const main = page.locator("#main-content");
    // bleed 화면(트랙 pages): 최상위 래퍼가 h-full로 main의 확정 높이를 채운다
    const bleed = new Set([`/notebooks/${notebookId}`, `/workflows/${workflowId}/edit`]);
    if (editable) bleed.add(`/skills/${editSkillId}/edit`);

    async function checkPath(path: string, viewport: { width: number; height: number }) {
      await page.goto(path);
      await expect(main).toBeVisible({ timeout: 15_000 });
      // main 수는 soft로 모든 경로를 끝까지 본다(한 번 실행으로 위반 경로 전체가 기록에 남는다)
      await expect.soft(page.locator("main"), `${path}: 첫 렌더의 main 수`).toHaveCount(1, { timeout: 2_000 });
      // 데이터가 온 뒤의 분기도 확인한다(폴링 화면은 idle이 오지 않으므로 상한만 둔다)
      await page.waitForLoadState("networkidle", { timeout: 3_000 }).catch(() => undefined);
      await expect
        .soft(page.locator("main"), `${path}: 데이터 로드 뒤 main 수`)
        .toHaveCount(1, { timeout: 2_000 });

      // 높이·스크롤 계약(P2 통합): main은 확정 높이(창 − 모바일 상단 바)이고 스크롤은 페이지가 소유한다 — main 자체 넘침 0
      const box = await main.evaluate((el) => ({
        client: el.clientHeight,
        overflow: el.scrollHeight - el.clientHeight,
        child: (el.firstElementChild as HTMLElement | null)?.getBoundingClientRect().height ?? -1,
      }));
      const expectedHeight = viewport.width < 768 ? viewport.height - 56 : viewport.height;
      expect.soft(Math.abs(box.client - expectedHeight), `${path}: main 높이 ${box.client} ≈ ${expectedHeight}`).toBeLessThanOrEqual(1);
      expect.soft(box.overflow, `${path}: main 자체 넘침(px) — 스크롤은 페이지 소유`).toBeLessThanOrEqual(1);
      if (bleed.has(path)) {
        expect.soft(Math.abs(box.child - box.client), `${path}: bleed 래퍼가 main 높이를 채운다`).toBeLessThanOrEqual(1);
      }

      await page.keyboard.press("Tab");
      await expect(skip, `${path}: 첫 Tab`).toBeFocused();
      await expect(skip).toBeInViewport();
      await page.keyboard.press("Enter");
      await expect(main, `${path}: Enter 뒤 포커스`).toBeFocused();
    }

    for (const path of paths) {
      await test.step(path, () => checkPath(path, DESKTOP));
    }

    // 모바일 셸(상단 바 아래 main)에서도 같은 규칙
    await page.setViewportSize(MOBILE);
    for (const path of ["/", `/notebooks/${notebookId}`]) {
      await test.step(`모바일 ${path}`, () => checkPath(path, MOBILE));
    }
  } finally {
    if (workflowId) {
      const status = await page.request
        .delete(`${API_BASE_URL}/api/v1/workflows/${workflowId}`)
        .then((res) => res.status(), (err: unknown) => `요청 실패: ${String(err)}`);
      console.log(`shell-mobile 랜드마크: workflow ${workflowId} 정리 → ${status}`);
      expect.soft(status, `워크플로우 ${workflowId} 정리`).toBe(204);
    }
    await deleteNotebookViaApi(page, notebookId, "shell-mobile 랜드마크");
  }
});

test("버전 줄 — 「biznotes」 아래에 앱 버전이 한 줄로 보인다(데스크톱 사이드바·모바일 드로어)", async ({
  page,
}) => {
  const LINE = /^v\d+\.\d+\.\d+\S*$/;
  await page.setViewportSize(DESKTOP);
  await gotoDashboard(page);
  const line = page.getByTestId("app-sidebar").getByTestId("app-versions");
  await expect(line).toHaveText(LINE);
  // 기본 폭(264px)에서는 말줄임 없이 다 보이고, 브랜드 행(높이 56) 안의 한 줄이다
  expect(await line.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
  expect((await line.boundingBox())?.height).toBeLessThanOrEqual(16);

  await page.setViewportSize(MOBILE);
  await gotoDashboard(page);
  const drawer = await openDrawer(page);
  await expect(drawer.getByTestId("app-versions")).toHaveText(LINE);
  await expect(page.getByRole("dialog", { name: "biznotes" })).toBeVisible();
});
