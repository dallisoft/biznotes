import { randomUUID } from "node:crypto";
import path from "node:path";
import {
  expect,
  test,
  type APIRequestContext,
  type Browser,
  type BrowserContext,
  type Page,
} from "@playwright/test";
import {
  API_BASE_URL,
  assertLoopbackUrl,
  createNotebookViaApi,
  fulfillJson,
  LOGIN_RATE_LIMIT_WINDOW_MS,
  loginViaUiAfterRateLimit,
} from "./helpers";

// notebook-bulk-actions — 대시보드 테이블 뷰·선택 모델(Phase 1: 토글·선택·필터 해제), 일괄 삭제(Phase 2),
// 일괄 공유(Phase 3), 실스택.
//
// ui-ux-redesign P3(노트북 홈 — plan §6.6.2, AC-3.6·3.7): 보기 전환은 radiogroup(「카드 | 표」, aria-checked),
// 탭 「공유받은 노트북」, 표 「권한」은 한국어 라벨, 카드·행 동작은 「{제목} 동작 더보기」, 카드 제목은 h2,
// 생성 후 새 노트북으로 이동(HCI-3), 카드 동작 표시 규칙(HCI-4 — 마우스는 hover·포커스·선택 중, 터치는 항상).
// 테스트 제목은 e2e 게이트 정본(scripts/e2e-gate.json)과 같아야 하므로 바꾸지 않는다.
//
// e2e 데이터 안전 규칙(plan §10, PR-06): 공유 dev DB에는 admin의 실제 노트북이 있으므로 admin 세션으로는
// 대시보드 선택을 하지 않는다. admin API로 `e2e-bulk-` 접두 임시 사용자 A·B와 임시 그룹 G(A·B — core-v0.20.0 P3
// 그룹 공유)를 만들고, 선택 조작은 A 컨텍스트의 A 노트북에서만 한다. 시작 전·종료 뒤 이 접두 사용자·그룹만 지우고
// (사용자 삭제 시 소유 노트북·공유도 CASCADE), admin 소유 노트북 수가 실행 전후 같은지 단언한다. 비밀번호는 실행마다
// 무작위·비출력. 공유는 그룹 대상만 보낸다(사용자 대상 공유 0 — core-v0.20.0 AC-3.4·§5.8).

const ADMIN_STORAGE_STATE = path.resolve(__dirname, "../playwright/.auth/admin.json");
const E2E_EMAIL_PREFIX = "e2e-bulk-";
const E2E_EMAIL_DOMAIN = "@biznotes.dev";
const RUN_ID = `${Date.now().toString(36)}${randomUUID().slice(0, 4)}`;
const HYDRATION_ERROR = /hydrat|Minified React error #(418|423|425)/i;

type TempUser = { id: string; email: string; context: BrowserContext; page: Page };

let admin: BrowserContext;
let adminOwnedBefore: number;
let userA: TempUser;
let userB: TempUser;
/** A·B가 속한 임시 그룹 — 일괄 공유 대상이자 「공유받은 노트북」 준비(B1 → G)에 쓴다. */
const group = { id: "", name: `${E2E_EMAIL_PREFIX}${RUN_ID}-g` };
const titles = {
  a1: `E2E 일괄 A1 ${RUN_ID}`,
  a2: `E2E 일괄 A2 ${RUN_ID}`,
  b1: `E2E 일괄 B1 ${RUN_ID}`,
};
const ids: Record<keyof typeof titles, string> = { a1: "", a2: "", b1: "" };

function newPassword(): string {
  return `E2e-${randomUUID().replaceAll("-", "").slice(0, 16)}`;
}

async function adminOwnedNotebookCount(request: APIRequestContext): Promise<number> {
  const res = await request.get(`${API_BASE_URL}/api/v1/notebooks?filter=mine`);
  expect(res.status(), "admin 소유 노트북 목록").toBe(200);
  return ((await res.json()) as unknown[]).length;
}

/** 이 스펙의 임시 사용자(`e2e-bulk-…@biznotes.dev`)만 찾아 지우고 남은 수를 단언한다(앞선 시도의 잔여 포함). */
async function purgeE2eUsers(request: APIRequestContext, phase: string) {
  const listE2eUsers = async () => {
    const res = await request.get(`${API_BASE_URL}/api/v1/admin/users`);
    expect(res.status(), "관리자 사용자 목록").toBe(200);
    return ((await res.json()) as { id: string; email: string }[]).filter(
      (u) => u.email.startsWith(E2E_EMAIL_PREFIX) && u.email.endsWith(E2E_EMAIL_DOMAIN),
    );
  };
  for (const user of await listE2eUsers()) {
    const status = await request
      .delete(`${API_BASE_URL}/api/v1/admin/users/${user.id}`)
      .then((r) => r.status(), (err: unknown) => `요청 실패: ${String(err)}`);
    console.log(`dashboard-bulk(${phase}): 임시 사용자 ${user.id} 삭제 → ${status}`);
    expect.soft(status, `임시 사용자 ${user.id} 정리`).toBe(204);
  }
  const left = await listE2eUsers();
  console.log(`dashboard-bulk(${phase}): 남은 ${E2E_EMAIL_PREFIX} 사용자 ${left.length}명`);
  expect.soft(left, `${phase} 정리 뒤 ${E2E_EMAIL_PREFIX} 사용자 0`).toEqual([]);

  // 임시 그룹(이름 접두 `e2e-bulk-`) — 사용자 뒤에 지운다
  const listE2eGroups = async () => {
    const res = await request.get(`${API_BASE_URL}/api/v1/admin/groups`);
    expect(res.status(), "관리자 그룹 목록").toBe(200);
    return ((await res.json()) as { id: string; name: string }[]).filter((g) => g.name.startsWith(E2E_EMAIL_PREFIX));
  };
  for (const g of await listE2eGroups()) {
    const status = await request
      .delete(`${API_BASE_URL}/api/v1/admin/groups/${g.id}`)
      .then((r) => r.status(), (err: unknown) => `요청 실패: ${String(err)}`);
    console.log(`dashboard-bulk(${phase}): 임시 그룹 ${g.id} 삭제 → ${status}`);
    expect.soft(status, `임시 그룹 ${g.id} 정리`).toBe(204);
  }
  const leftGroups = await listE2eGroups();
  console.log(`dashboard-bulk(${phase}): 남은 ${E2E_EMAIL_PREFIX} 그룹 ${leftGroups.length}개`);
  expect.soft(leftGroups, `${phase} 정리 뒤 ${E2E_EMAIL_PREFIX} 그룹 0`).toEqual([]);
}

/** admin API로 만들고, 새 컨텍스트에서 로그인 → 강제 비밀번호 변경 → 목록 API 200까지 마친다. */
async function createReadyUser(browser: Browser, label: string): Promise<TempUser> {
  const email = `${E2E_EMAIL_PREFIX}${RUN_ID}-${label}${E2E_EMAIL_DOMAIN}`;
  const temporary = newPassword();
  const res = await admin.request.post(`${API_BASE_URL}/api/v1/admin/users`, {
    data: { email, name: `E2E bulk ${label}`, password: temporary, role: "user" },
  });
  expect(res.status(), "임시 사용자 생성").toBe(201);
  const { id } = (await res.json()) as { id: string };
  console.log(`dashboard-bulk: 임시 사용자 ${label} 생성 ${id}`);

  const context = await browser.newContext({ storageState: { cookies: [], origins: [] } });
  const page = await context.newPage();
  // 앞 스펙들의 로그인으로 레이트 리밋 창이 차 있으면 한 번 기다렸다 다시 로그인한다(HCI-P2-3, helpers 참고)
  const login = await loginViaUiAfterRateLimit(page, email, temporary);
  expect(login.status(), `${label} 로그인 응답`).toBe(200);
  await expect(page).toHaveURL(/\/change-password(\?|$)/);
  const next = newPassword();
  await page.getByLabel("현재 비밀번호").fill(temporary);
  await page.getByLabel("새 비밀번호", { exact: true }).fill(next);
  await page.getByLabel("새 비밀번호 확인").fill(next);
  await page.getByRole("button", { name: "비밀번호 변경" }).click();
  await expect(page.getByRole("heading", { name: "노트북", exact: true })).toBeVisible({
    timeout: 10_000,
  });
  const list = await page.request.get(`${API_BASE_URL}/api/v1/notebooks`);
  expect(list.status(), `${label} 목록 API`).toBe(200);
  return { id, email, context, page };
}

function card(page: Page, title: string) {
  return page.getByTestId("notebook-card").filter({ hasText: title });
}

function selectBox(page: Page, title: string) {
  return page.getByRole("checkbox", { name: `${title} 선택`, exact: true });
}

async function gotoUserDashboard(page: Page) {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "노트북", exact: true })).toBeVisible({
    timeout: 10_000,
  });
  await expect(card(page, titles.a1).or(page.getByTestId("notebook-table"))).toBeVisible();
}

test.describe.configure({ mode: "serial" });

test.beforeAll(async ({ browser }) => {
  // 임시 사용자 2명의 로그인 각각이 429면 한 번씩 기다린다(createReadyUser) — 이 훅의 제한 시간을 그만큼 늘린다
  test.setTimeout(60_000 + 2 * LOGIN_RATE_LIMIT_WINDOW_MS);
  // 첫 변경 요청 전에 Web·API 주소가 모두 loopback인지 확인한다
  assertLoopbackUrl(test.info().project.use.baseURL, "Playwright baseURL");
  assertLoopbackUrl(API_BASE_URL, "E2E API baseURL");

  admin = await browser.newContext({ storageState: ADMIN_STORAGE_STATE });
  await purgeE2eUsers(admin.request, "시작 전");
  adminOwnedBefore = await adminOwnedNotebookCount(admin.request);

  userA = await createReadyUser(browser, "a");
  userB = await createReadyUser(browser, "b");

  ids.a1 = await createNotebookViaApi(userA.page, titles.a1);
  ids.a2 = await createNotebookViaApi(userA.page, titles.a2);
  const fav = await userA.page.request.post(`${API_BASE_URL}/api/v1/notebooks/${ids.a1}/favorite`);
  expect(fav.ok(), "A1 즐겨찾기").toBe(true);

  // 임시 그룹 G(A·B) — 그룹은 관리자만 만든다(core-v0.20.0 §5.8)
  const created = await admin.request.post(`${API_BASE_URL}/api/v1/admin/groups`, { data: { name: group.name } });
  expect(created.status(), "임시 그룹 생성").toBe(201);
  group.id = ((await created.json()) as { id: string }).id;
  for (const member of [userA, userB]) {
    const added = await admin.request.post(`${API_BASE_URL}/api/v1/admin/groups/${group.id}/members`, {
      data: { user_id: member.id },
    });
    expect(added.status(), "임시 그룹 구성원 추가").toBe(201);
  }
  console.log(`dashboard-bulk: 임시 그룹 생성 ${group.id}`);

  // B 소유 노트북을 그룹 G에 read로 공유 — A 화면에서 비소유(비활성) 항목이 된다(사용자 대상 공유를 쓰지 않는다)
  ids.b1 = await createNotebookViaApi(userB.page, titles.b1);
  const share = await userB.page.request.post(`${API_BASE_URL}/api/v1/notebooks/${ids.b1}/share`, {
    data: { group_id: group.id, permission: "read" },
  });
  expect(share.status(), "B1 → G 공유").toBe(201);
});

test.afterAll(async () => {
  await userA?.context.close();
  await userB?.context.close();
  if (!admin) return;
  try {
    const adminOwnedAfter = await adminOwnedNotebookCount(admin.request);
    console.log(`dashboard-bulk: admin 소유 노트북 수 실행 전 ${adminOwnedBefore} → 후 ${adminOwnedAfter}`);
    expect.soft(adminOwnedAfter, "admin 소유 노트북 수 불변").toBe(adminOwnedBefore);
    await purgeE2eUsers(admin.request, "종료 뒤");
  } finally {
    await admin.close();
  }
});

test("AC-1.1·AC-1.2 카드/테이블 토글 — 테이블 컬럼, 새로고침 뒤 복원, hydration 오류 없음", async () => {
  const page = userA.page;
  const consoleErrors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });
  page.on("pageerror", (err) => consoleErrors.push(String(err)));

  await page.evaluate(() => localStorage.removeItem("biznotes:dashboard-view"));
  await gotoUserDashboard(page);
  await expect(page.getByTestId("view-mode-card")).toHaveAttribute("aria-checked", "true");
  await expect(page.getByRole("radiogroup", { name: "보기 방식" }).getByRole("radio")).toHaveText(["카드", "표"]);
  await expect(card(page, titles.a1)).toBeVisible();
  const cardOrder = await page
    .getByTestId("notebook-card")
    .locator("h2")
    .allTextContents();

  await page.getByTestId("view-mode-table").click();
  const table = page.getByTestId("notebook-table");
  await expect(table).toBeVisible();
  await expect(page.getByTestId("notebook-card")).toHaveCount(0);
  await expect(table.getByRole("columnheader")).toHaveText([
    "",
    "제목",
    "구분",
    "권한",
    "태그",
    "소스 수",
    "생성일",
    "수정일",
    "즐겨찾기",
    "메뉴",
  ]);
  const rowOrder = (
    await page.getByTestId("notebook-row").locator("td:nth-child(2) a span.truncate").allTextContents()
  ).map((t) => t.trim());
  expect(rowOrder).toEqual(cardOrder);
  const sharedRow = page.getByTestId("notebook-row").filter({ hasText: titles.b1 });
  // AC-3.7 — 「권한」은 원시값(owner·read) 대신 한국어 라벨, 비소유 구분은 「공유받음」
  await expect(sharedRow.locator("td").nth(2)).toHaveText("공유받음");
  await expect(sharedRow.locator("td").nth(3)).toHaveText("읽기 전용");
  await expect(sharedRow.getByRole("button", { name: `${titles.b1} 동작 더보기` })).toHaveCount(0);
  const ownRow = page.getByTestId("notebook-row").filter({ hasText: titles.a1 });
  await expect(ownRow.locator("td").nth(2)).toHaveText("내 노트북");
  await expect(ownRow.locator("td").nth(3)).toHaveText("소유자");
  await expect(ownRow.getByRole("button", { name: `${titles.a1} 동작 더보기` })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem("biznotes:dashboard-view"))).toBe("table");

  await page.reload();
  await expect(page.getByTestId("notebook-table")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByTestId("view-mode-table")).toHaveAttribute("aria-checked", "true");
  await page.screenshot({ path: "test-results/bulk-p1-table-view.png", fullPage: true });

  // 잘못된 저장값이면 card
  await page.evaluate(() => localStorage.setItem("biznotes:dashboard-view", "bogus"));
  await page.reload();
  await expect(card(page, titles.a1)).toBeVisible({ timeout: 10_000 });
  await expect(page.getByTestId("view-mode-card")).toHaveAttribute("aria-checked", "true");

  expect(consoleErrors.filter((m) => HYDRATION_ERROR.test(m)), "hydration 오류").toEqual([]);
});

test("AC-1.3·AC-1.6·AC-1.7 소유 노트북만 선택 — 비소유 비활성, 모두 선택 mixed/checked, 선택 해제", async () => {
  const page = userA.page;
  await page.evaluate(() => localStorage.setItem("biznotes:dashboard-view", "card"));
  await gotoUserDashboard(page);

  const sharedBox = page.getByRole("checkbox", {
    name: `${titles.b1} 선택 불가 — 소유자만 일괄 작업할 수 있습니다`,
  });
  await expect(sharedBox).toHaveAttribute("aria-disabled", "true");
  await expect(card(page, titles.b1).locator('[title="소유자만 일괄 작업할 수 있습니다"]')).toHaveCount(1);
  await expect(page.getByTestId("bulk-selected-count")).toHaveCount(0);

  const selectAll = page.getByRole("checkbox", { name: "표시된 노트북 모두 선택" });
  await expect(selectAll).toHaveAttribute("aria-checked", "false");
  await selectBox(page, titles.a1).click();
  await expect(selectAll).toHaveAttribute("aria-checked", "mixed");
  await expect(page.getByTestId("bulk-selected-count")).toHaveText("1개 선택됨");

  await selectAll.click(); // mixed → 선택 가능 전체(A 소유 2개)
  await expect(selectAll).toHaveAttribute("aria-checked", "true");
  await expect(page.getByTestId("bulk-selected-count")).toHaveText("2개 선택됨");
  await expect(selectBox(page, titles.a2)).toHaveAttribute("aria-checked", "true");
  await expect(sharedBox).toHaveAttribute("aria-checked", "false");
  await page.screenshot({ path: "test-results/bulk-p1-card-selection.png", fullPage: true });

  await selectAll.click(); // checked → 0
  await expect(page.getByTestId("bulk-selected-count")).toHaveCount(0);

  await selectBox(page, titles.a2).click();
  await page.getByRole("button", { name: "선택 해제" }).click();
  await expect(page.getByTestId("bulk-selected-count")).toHaveCount(0);
  await expect(selectBox(page, titles.a2)).toHaveAttribute("aria-checked", "false");
});

/** 카드 동작(체크박스·즐겨찾기·더보기)의 계산된 불투명도 — HCI-4 표시 규칙 확인용. */
async function controlOpacity(page: Page, title: string) {
  const c = card(page, title);
  const read = (l: ReturnType<Page["locator"]>) => l.evaluate((el) => getComputedStyle(el).opacity);
  return {
    checkbox: await read(c.locator("[data-slot=notebook-select]")),
    favorite: await read(c.getByTestId("notebook-favorite-toggle")),
    more: await read(c.getByRole("button", { name: `${title} 동작 더보기` }).locator("..")),
  };
}

const HIDDEN = { checkbox: "0", favorite: "0", more: "0" };
const SHOWN = { checkbox: "1", favorite: "1", more: "1" };

test("AC-1.8 카드 체크박스 클릭·Space는 노트북 페이지로 이동하지 않는다", async ({ browser }) => {
  const page = userA.page;
  await page.evaluate(() => localStorage.setItem("biznotes:dashboard-view", "card"));
  await gotoUserDashboard(page);

  // HCI-4·AC-3.6 — 마우스 기기(pointer: fine): 카드 동작은 hover·포커스·선택 중에만 보인다(A2 — 즐겨찾기 아님)
  expect(await page.evaluate(() => matchMedia("(pointer: fine)").matches)).toBe(true);
  await page.mouse.move(0, 0);
  await expect.poll(() => controlOpacity(page, titles.a2)).toEqual(HIDDEN);
  // 즐겨찾기한 A1의 채운 별은 상태 표시라 항상 보인다
  await expect.poll(async () => (await controlOpacity(page, titles.a1)).favorite).toBe("1");
  await card(page, titles.a2).hover();
  await expect.poll(() => controlOpacity(page, titles.a2)).toEqual(SHOWN);
  await page.mouse.move(0, 0);
  await expect.poll(() => controlOpacity(page, titles.a2)).toEqual(HIDDEN);
  await selectBox(page, titles.a2).focus(); // 키보드 포커스(카드 안 어느 요소든)
  await expect.poll(() => controlOpacity(page, titles.a2)).toEqual(SHOWN);
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await expect.poll(() => controlOpacity(page, titles.a2)).toEqual(HIDDEN);
  await selectBox(page, titles.a2).click(); // 선택 중이면 포인터가 떠나도 보인다
  await page.mouse.move(0, 0);
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await expect.poll(() => controlOpacity(page, titles.a2)).toEqual(SHOWN);
  await selectBox(page, titles.a2).click();
  await expect(page.getByTestId("bulk-selected-count")).toHaveCount(0);

  // 터치 기기(pointer: coarse — hasTouch): hover 없이도 항상 보이고, 체크박스 히트 영역은 44px이다.
  // 같은 사용자의 쿠키를 복사한 컨텍스트라 로그인을 다시 하지 않는다(로그인 레이트 리밋).
  const touch = await browser.newContext({
    storageState: await userA.context.storageState(),
    hasTouch: true,
    viewport: { width: 390, height: 844 },
  });
  try {
    const t = await touch.newPage();
    expect(await t.evaluate(() => matchMedia("(pointer: coarse)").matches)).toBe(true);
    await gotoUserDashboard(t);
    await expect.poll(() => controlOpacity(t, titles.a2)).toEqual(SHOWN);
    const hit = await selectBox(t, titles.a2).evaluate((el) => {
      const after = getComputedStyle(el, "::after");
      const box = el.getBoundingClientRect();
      return {
        width: box.width - parseFloat(after.left) - parseFloat(after.right),
        height: box.height - parseFloat(after.top) - parseFloat(after.bottom),
      };
    });
    expect(hit.width).toBeGreaterThanOrEqual(44);
    expect(hit.height).toBeGreaterThanOrEqual(44);
    for (const name of [`${titles.a2} 동작 더보기`]) {
      const box = await card(t, titles.a2).getByRole("button", { name }).boundingBox();
      expect(box?.width ?? 0).toBeGreaterThanOrEqual(44);
      expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
    }
    // 390 폭에서 가로 넘침이 없다(카드 1열)
    const overflow = await t.evaluate(() => {
      const scroller = document.querySelector("[data-slot=page-container]")?.parentElement;
      return scroller ? scroller.scrollWidth - scroller.clientWidth : -1;
    });
    expect(overflow).toBe(0);
    await t.screenshot({ path: "test-results/home-p3-touch-390.png", fullPage: true });
  } finally {
    await touch.close();
  }
  await page.evaluate(() => {
    const w = window as unknown as { __historyCalls: number };
    w.__historyCalls = 0;
    for (const method of ["pushState", "replaceState"] as const) {
      const original = history[method].bind(history);
      history[method] = (...args: Parameters<History["pushState"]>) => {
        w.__historyCalls += 1;
        return original(...args);
      };
    }
  });
  const dashboardUrl = page.url();

  const box = selectBox(page, titles.a1);
  await box.click();
  await expect(box).toHaveAttribute("aria-checked", "true");
  await box.focus();
  await page.keyboard.press("Space");
  await expect(box).toHaveAttribute("aria-checked", "false");
  await page.keyboard.press("Space");
  await expect(box).toHaveAttribute("aria-checked", "true");

  // 이동이 일어났다면 시작될 시간을 둔 뒤 부재를 단언한다
  await page.waitForTimeout(1_000);
  expect(page.url()).toBe(dashboardUrl);
  expect(await page.evaluate(() => (window as unknown as { __historyCalls: number }).__historyCalls)).toBe(0);
  await expect(page.getByTestId("bulk-selected-count")).toHaveText("1개 선택됨");

  // 대조군 — 카드 본문(링크 오버레이)을 누르면 실제로 이동한다
  await page.getByRole("link", { name: `${titles.a1} 열기` }).click();
  await expect(page).toHaveURL(/\/notebooks\/[0-9a-f-]+$/);
});

test("AC-1.5 필터를 바꾸면 선택이 비고, 카드 ↔ 테이블 전환은 선택을 유지한다", async () => {
  const page = userA.page;
  await page.evaluate(() => localStorage.setItem("biznotes:dashboard-view", "card"));
  await gotoUserDashboard(page);
  const count = page.getByTestId("bulk-selected-count");

  await page.getByRole("checkbox", { name: "표시된 노트북 모두 선택" }).click();
  await expect(count).toHaveText("2개 선택됨");

  await page.getByTestId("view-mode-table").click();
  await expect(page.getByTestId("notebook-table")).toBeVisible();
  await expect(count).toHaveText("2개 선택됨");
  await expect(selectBox(page, titles.a1)).toHaveAttribute("aria-checked", "true");
  await page.getByTestId("view-mode-card").click();
  await expect(card(page, titles.a1)).toBeVisible();
  await expect(count).toHaveText("2개 선택됨");

  // 탭 필터 — 「내 노트북」에도 같은 두 노트북이 남지만 선택은 비운다
  await page.getByRole("tab", { name: "내 노트북" }).click();
  await expect(card(page, titles.b1)).toHaveCount(0);
  await expect(card(page, titles.a2)).toBeVisible();
  await expect(count).toHaveCount(0);
  await expect(selectBox(page, titles.a1)).toHaveAttribute("aria-checked", "false");

  // 즐겨찾기만
  await selectBox(page, titles.a1).click();
  await expect(count).toHaveText("1개 선택됨");
  await page.getByTestId("favorites-only-toggle").click();
  await expect(card(page, titles.a2)).toHaveCount(0);
  await expect(card(page, titles.a1)).toBeVisible();
  await expect(count).toHaveCount(0);
});

// P1-06 — 카드 회귀(core-flows 1·2와 openNotebook에 해당하는 흐름)를 임시 사용자 A로 확인한다. 카드의 즐겨찾기·
// 소유자 메뉴·단건 삭제 확인은 notebook-actions.tsx(공용 RowActions)이므로 실브라우저에서 한 번씩 누른다. 만든 노트북은
// A 소유이고 이 테스트 안에서 카드 메뉴로 지운다(남아도 A 삭제 때 CASCADE).
// ui-ux-redesign P3: 만들면 새 노트북 작업 화면으로 바로 이동한다(HCI-3) — 홈으로 돌아와 카드 노출을 확인한다.
test("P1-06 카드 회귀 — UI로 생성 → 카드 노출 → 카드로 열기 → 즐겨찾기 → 소유자 메뉴 → 단건 삭제", async () => {
  const page = userA.page;
  const title = `E2E 카드 회귀 ${RUN_ID}`;
  await page.evaluate(() => localStorage.setItem("biznotes:dashboard-view", "card"));
  await gotoUserDashboard(page);

  const createdRes = page.waitForResponse(
    (res) => res.request().method() === "POST" && /\/api\/v1\/notebooks$/.test(res.url()),
  );
  await page.getByTestId("create-notebook").click();
  await page.getByLabel("제목").fill(title);
  await page.getByRole("button", { name: "만들기" }).click();
  const res = await createdRes;
  expect(res.status(), "노트북 생성").toBe(201);
  const newId = ((await res.json()) as { id: string }).id;
  const href = `/notebooks/${newId}`;
  // 생성 후 이동(HCI-3, AC-3.6) — 새 노트북 작업 화면, 사이드바의 그 노트북 링크·채팅 입력
  await expect(page).toHaveURL(new RegExp(`${href}$`));
  await expect(page.getByText("노트북을 만들었습니다")).toBeVisible();
  await expect(page.getByRole("link", { name: `📓 ${title}` })).toHaveAttribute("href", href);
  await expect(page.getByPlaceholder(/소스에 대해 질문하세요/)).toBeVisible();

  // 홈으로 돌아오면 카드가 있고, 카드 링크 오버레이는 같은 노트북으로 간다
  await gotoUserDashboard(page);
  const created = card(page, title);
  await expect(created).toBeVisible();
  await expect(created.getByTestId("notebook-meta")).toHaveText("소스 0개 · 방금 전 수정");
  await expect(selectBox(page, title)).toHaveAttribute("aria-checked", "false");
  const openLink = page.getByRole("link", { name: `${title} 열기` });
  await expect(openLink).toHaveAttribute("href", href);
  await openLink.click();
  await expect(page).toHaveURL(new RegExp(`${href}$`));
  await expect(page.getByPlaceholder(/소스에 대해 질문하세요/)).toBeVisible();

  await gotoUserDashboard(page);
  // 즐겨찾기 토글(공용 NotebookFavoriteButton)
  const favorite = created.getByTestId("notebook-favorite-toggle");
  await expect(favorite).toHaveAttribute("aria-pressed", "false");
  await created.hover();
  await favorite.click();
  await expect(favorite).toHaveAttribute("aria-pressed", "true");
  await expect(page).toHaveURL(/\/$/); // 즐겨찾기 클릭은 이동하지 않는다

  // 소유자 동작 더보기(공용 RowActions) → 단건 삭제 확인(질문형) → DELETE 204 → 토스트, 홈에 머문다
  await created.hover();
  await created.getByRole("button", { name: `${title} 동작 더보기` }).click();
  await expect(page.getByRole("menuitem")).toHaveText(["이름 변경", "공유", "태그 관리", "삭제"]);
  await page.getByRole("menuitem", { name: "삭제" }).click();
  const dialog = page.getByRole("alertdialog", { name: `‘${title}’ 노트북을 삭제할까요?` });
  await expect(dialog).toContainText("소스·메모·대화가 모두 삭제되며 되돌릴 수 없습니다.");
  await expect(dialog.getByRole("button", { name: "취소" })).toBeFocused();
  const deleted = page.waitForResponse(
    (r) => r.request().method() === "DELETE" && /\/api\/v1\/notebooks\/[0-9a-f-]+$/.test(r.url()),
  );
  await dialog.getByRole("button", { name: "삭제" }).click();
  expect((await deleted).status()).toBe(204);
  await expect(card(page, title)).toHaveCount(0);
  await expect(page.getByText("노트북을 삭제했습니다")).toBeVisible();
  await expect(page).toHaveURL(/\/$/);
});

// ---- Phase 2 — 일괄 삭제(AC-2.9). 모든 일괄 동작은 임시 사용자 A 컨텍스트에서 A 소유 노트북에만 실행한다. ----

const NOTEBOOK_DELETE = /\/api\/v1\/notebooks\/([0-9a-f-]+)$/;

/** 페이지가 실제로 보낸 노트북 DELETE의 id(프리플라이트 OPTIONS 제외). */
function recordDeletes(page: Page): { ids: string[]; dispose: () => void } {
  const ids: string[] = [];
  const onRequest = (req: { method(): string; url(): string }) => {
    const match = NOTEBOOK_DELETE.exec(req.url());
    if (req.method() === "DELETE" && match) ids.push(match[1]);
  };
  page.on("request", onRequest);
  return { ids, dispose: () => page.off("request", onRequest) };
}

async function notebookStatus(page: Page, id: string): Promise<number> {
  return (await page.request.get(`${API_BASE_URL}/api/v1/notebooks/${id}`)).status();
}

test("AC-2.9 일괄 삭제 — A 소유 2개만 지우고, 공유받은 노트북은 비활성 체크박스로 남는다", async () => {
  const page = userA.page;
  await page.evaluate(() => localStorage.setItem("biznotes:dashboard-view", "card"));
  await gotoUserDashboard(page);
  const deleteButton = page.getByTestId("bulk-delete");
  await expect(deleteButton).toBeDisabled(); // AC-1.7 재확인 — 0개면 비활성
  // AC-3.6 — 0개면 일괄 「삭제」도 「공유」와 같은 outline·비활성 모양이다(위험 글자·틴트 없음)
  const shareButton = page.getByTestId("bulk-share");
  await expect(shareButton).toBeDisabled();
  const look = (l: typeof deleteButton) =>
    l.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { color: cs.color, background: cs.backgroundColor };
    });
  expect(await look(deleteButton)).toEqual(await look(shareButton));

  const sharedBox = page.getByRole("checkbox", {
    name: `${titles.b1} 선택 불가 — 소유자만 일괄 작업할 수 있습니다`,
  });
  await expect(sharedBox).toHaveAttribute("aria-disabled", "true");
  await page.getByRole("checkbox", { name: "표시된 노트북 모두 선택" }).click();
  await expect(page.getByTestId("bulk-selected-count")).toHaveText("2개 선택됨");
  await expect(sharedBox).toHaveAttribute("aria-checked", "false");
  await expect(deleteButton).toBeEnabled();
  expect((await look(deleteButton)).color).not.toBe((await look(shareButton)).color); // 위험 글자

  const shownOrder = (await page.getByTestId("notebook-card").locator("h2").allTextContents()).filter(
    (t) => t === titles.a1 || t === titles.a2,
  );
  expect(shownOrder).toHaveLength(2);
  const sent = recordDeletes(page);
  try {
    await deleteButton.click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog.getByRole("heading")).toHaveText("노트북 2개를 영구 삭제할까요?");
    // 확인 목록 = 캡처한 선택(화면 표시 순서), 공유받은 B1은 없다
    await expect(dialog.getByRole("list", { name: "삭제할 노트북" }).getByRole("listitem")).toHaveText(
      shownOrder,
    );
    await expect(dialog).toContainText("되돌릴 수 없습니다");
    await dialog.getByRole("button", { name: "노트북 2개 영구 삭제" }).click();
    await expect(dialog.getByTestId("bulk-result-summary")).toHaveText(
      "삭제 2 · 실패 0 · 확인 불가 0 · 미실행 0",
    );
    await page.screenshot({ path: "test-results/bulk-p2-delete-result.png", fullPage: true });
    await dialog.getByRole("button", { name: "닫기" }).click();
  } finally {
    sent.dispose();
  }

  // 네트워크 경계: DELETE는 A 소유 2개에만, 공유받은 B1에는 0건
  expect([...sent.ids].sort()).toEqual([ids.a1, ids.a2].sort());
  await expect(card(page, titles.a1)).toHaveCount(0);
  await expect(card(page, titles.a2)).toHaveCount(0);
  await expect(card(page, titles.b1)).toBeVisible();
  await expect(sharedBox).toHaveAttribute("aria-disabled", "true");
  await expect(page.getByTestId("bulk-selected-count")).toHaveCount(0);
  await expect(deleteButton).toBeDisabled();

  // 서버 상태: A 노트북은 삭제(404), B1은 소유자 B에게 그대로
  expect(await notebookStatus(page, ids.a1)).toBe(404);
  expect(await notebookStatus(page, ids.a2)).toBe(404);
  expect(await notebookStatus(userB.page, ids.b1)).toBe(200);
});

test("AC-2.9 실패 주입(500 1건) — 그 항목은 선택에 남고, 실행 중 Esc로 닫히지 않는다", async () => {
  const page = userA.page;
  const fail = { title: `E2E 일괄 실패 ${RUN_ID}`, id: "" };
  const ok1 = { title: `E2E 일괄 성공1 ${RUN_ID}`, id: "" };
  const ok2 = { title: `E2E 일괄 성공2 ${RUN_ID}`, id: "" };
  for (const n of [ok1, fail, ok2]) n.id = await createNotebookViaApi(page, n.title);

  // 실패 대상 DELETE만 3초 뒤 500으로 응답한다(서버에는 도달하지 않음). 그 밖의 요청은 그대로 보낸다.
  const failUrl = `${API_BASE_URL}/api/v1/notebooks/${fail.id}`;
  let injected = 0;
  await page.route(failUrl, async (route) => {
    const method = route.request().method();
    if (method === "OPTIONS") return fulfillJson(route, 204, null);
    if (method !== "DELETE") return route.fallback();
    injected += 1;
    await new Promise((r) => setTimeout(r, 3_000));
    await fulfillJson(route, 500, { detail: "injected" });
  });

  const sent = recordDeletes(page);
  try {
    await page.evaluate(() => localStorage.setItem("biznotes:dashboard-view", "table"));
    await page.goto("/");
    await expect(page.getByTestId("notebook-table")).toBeVisible({ timeout: 10_000 });
    for (const n of [ok1, fail, ok2]) await selectBox(page, n.title).click();
    await expect(page.getByTestId("bulk-selected-count")).toHaveText("3개 선택됨");

    await page.getByTestId("bulk-delete").click();
    const dialog = page.getByRole("alertdialog");
    await dialog.getByRole("button", { name: "노트북 3개 영구 삭제" }).click();
    await expect(dialog.getByTestId("bulk-progress")).toHaveText("2 / 3"); // 실패 대상 응답 대기 중
    await page.keyboard.press("Escape");
    await expect(dialog).toBeVisible();
    await expect(dialog.getByTestId("bulk-progress")).toBeVisible();

    await expect(dialog.getByTestId("bulk-result-summary")).toHaveText(
      "삭제 2 · 실패 1 · 확인 불가 0 · 미실행 0",
      { timeout: 10_000 },
    );
    const failedRow = dialog.getByTestId("bulk-result-item").filter({ hasText: fail.title });
    await expect(failedRow).toHaveAttribute("data-status", "failed");
    await expect(failedRow).toContainText("서버 오류");
    await page.screenshot({ path: "test-results/bulk-p2-partial-failure.png", fullPage: true });
    await dialog.getByRole("button", { name: "닫기" }).click();
  } finally {
    sent.dispose();
    await page.unroute(failUrl);
  }

  expect(injected).toBe(1);
  expect([...sent.ids].sort()).toEqual([ok1.id, fail.id, ok2.id].sort());
  await expect(page.getByTestId("notebook-row").filter({ hasText: ok1.title })).toHaveCount(0);
  await expect(page.getByTestId("notebook-row").filter({ hasText: ok2.title })).toHaveCount(0);
  await expect(page.getByTestId("bulk-selected-count")).toHaveText("1개 선택됨");
  await expect(selectBox(page, fail.title)).toHaveAttribute("aria-checked", "true");
  expect(await notebookStatus(page, ok1.id)).toBe(404);
  expect(await notebookStatus(page, ok2.id)).toBe(404);
  expect(await notebookStatus(page, fail.id)).toBe(200); // 남은 노트북은 A 삭제 때 CASCADE로 정리된다
});

// ---- Phase 3 — 일괄 공유(AC-3.9, core-v0.20.0 AC-3.3·AC-3.6 그룹판). 공유는 임시 사용자 A 컨텍스트에서 A 소유 노트북에만,
// 대상은 A·B가 속한 임시 그룹 G다. 요청 본문은 정확히 {group_id, permission}이고 일괄 공유 경로의 DELETE는 0이다. ----

type SentRequest = { method: string; url: string; body: Record<string, unknown> | null };

/** 페이지가 실제로 보낸 공유 관련 요청(GET /shares·POST /share·DELETE /shares/{id}, 프리플라이트 제외). */
function recordShareRequests(page: Page): { sent: SentRequest[]; groups: string[]; dispose: () => void } {
  const sent: SentRequest[] = [];
  const groups: string[] = [];
  const onRequest = (req: { method(): string; url(): string; postData(): string | null }) => {
    const url = req.url();
    if (url.includes("/api/v1/admin/groups")) groups.push(url);
    if (req.method() === "OPTIONS" || !/\/api\/v1\/notebooks\/[0-9a-f-]+\/shares?(\/|$)/.test(url)) return;
    const raw = req.postData();
    sent.push({ method: req.method(), url, body: raw ? (JSON.parse(raw) as Record<string, unknown>) : null });
  };
  page.on("request", onRequest);
  return { sent, groups, dispose: () => page.off("request", onRequest) };
}

type ShareRow = { id: string; user_id: string | null; group_id: string | null; permission: string };

async function sharesOf(page: Page, notebookId: string): Promise<ShareRow[]> {
  const res = await page.request.get(`${API_BASE_URL}/api/v1/notebooks/${notebookId}/shares`);
  expect(res.status(), `shares ${notebookId}`).toBe(200);
  return (await res.json()) as ShareRow[];
}

async function openTableWith(page: Page, selectTitles: string[]) {
  await page.evaluate(() => localStorage.setItem("biznotes:dashboard-view", "table"));
  await page.goto("/");
  await expect(page.getByTestId("notebook-table")).toBeVisible({ timeout: 10_000 });
  for (const title of selectTitles) await selectBox(page, title).click();
  await expect(page.getByTestId("bulk-selected-count")).toHaveText(`${selectTitles.length}개 선택됨`);
}

test("AC-3.9 일괄 공유 — 새로 2개 공유·이미 write인 1개 건너뜀, 기존 write 행 보존, B의 공유 탭에 3개", async () => {
  const page = userA.page;
  const s1 = { title: `E2E 공유1 ${RUN_ID}`, id: "" };
  const s2 = { title: `E2E 공유2 ${RUN_ID}`, id: "" };
  const pre = { title: `E2E 공유 기존write ${RUN_ID}`, id: "" };
  // 기존 write 그룹 공유 노트북(pre) — read 일괄에서 결정용 GET이 같은 그룹 write를 보고 건너뛴다(POST·DELETE 0)
  for (const n of [s1, s2, pre]) n.id = await createNotebookViaApi(page, n.title);
  const preShare = await page.request.post(`${API_BASE_URL}/api/v1/notebooks/${pre.id}/share`, {
    data: { group_id: group.id, permission: "write" },
  });
  expect(preShare.status(), "기존 write 그룹 공유").toBe(201);
  const preShareId = ((await preShare.json()) as { id: string }).id;

  await openTableWith(page, [s1.title, s2.title, pre.title]);
  const rec = recordShareRequests(page);
  try {
    await page.getByTestId("bulk-share").click();
    const dialog = page.getByTestId("bulk-share-dialog");
    await expect(dialog.getByRole("heading")).toHaveText("노트북 3개 공유");
    const listed = await dialog.getByRole("list", { name: "공유할 노트북" }).getByRole("listitem").allTextContents();
    expect([...listed].sort()).toEqual([s1.title, s2.title, pre.title].sort());
    await expect(dialog.getByRole("textbox")).toHaveCount(0); // 이메일 입력 없음 — 대상은 그룹
    const groupSelect = dialog.getByTestId("bulk-share-group-select");
    await groupSelect.click();
    await expect(page.getByRole("option")).toHaveText([group.name]); // A가 속한 그룹(G)뿐
    await page.getByRole("option", { name: group.name, exact: true }).click();
    await expect(groupSelect).toHaveAttribute("aria-expanded", "false");
    await expect(dialog.getByTestId("bulk-share-permission")).toHaveText(/읽기 전용/);
    await expect(dialog.getByTestId("bulk-share-summary")).toHaveText(`노트북 3개 · ${group.name} 그룹 · 읽기 전용`);
    await dialog.getByTestId("bulk-share-submit").click();
    await expect(dialog.getByTestId("bulk-result-summary")).toHaveText(
      "공유 2 · 건너뜀 1 · 실패 0 · 확인 불가 0 · 미실행 0",
      { timeout: 10_000 },
    );
    const preRow = dialog.getByTestId("bulk-result-item").filter({ hasText: pre.title });
    await expect(preRow).toHaveAttribute("data-status", "skipped");
    await expect(preRow).toContainText("이미 편집 가능 권한으로 공유됨");
    for (const n of [s1, s2]) {
      const row = dialog.getByTestId("bulk-result-item").filter({ hasText: n.title });
      await expect(row).toHaveAttribute("data-status", "succeeded");
      await expect(row).toContainText("공유됨");
    }
    await page.screenshot({ path: "test-results/bulk-p3-share-result.png", fullPage: true });
    await dialog.getByRole("button", { name: "닫기" }).click();
  } finally {
    rec.dispose();
  }

  // 네트워크 경계(core-v0.20.0 AC-3.3·3.4): 항목마다 결정용 GET 1회, 필요한 항목(s1·s2)에만 POST {group_id, permission, mode}
  // (mode는 하향 금지 쓰기 "upgrade_only" — core-v0.22.0-upgrade D4),
  // pre에는 GET뿐, 일괄 공유 경로 전체에서 DELETE 0·사용자 대상 0
  const path = (r: SentRequest) => r.url.replace(API_BASE_URL, "");
  const gets = rec.sent.filter((r) => r.method === "GET").map(path);
  expect([...gets].sort()).toEqual([s1, s2, pre].map((n) => `/api/v1/notebooks/${n.id}/shares`).sort());
  const posts = rec.sent.filter((r) => r.method === "POST");
  expect(posts.map(path).sort()).toEqual([s1, s2].map((n) => `/api/v1/notebooks/${n.id}/share`).sort());
  for (const p of posts) expect(p.body).toEqual({ group_id: group.id, permission: "read", mode: "upgrade_only" });
  expect(rec.sent.filter((r) => path(r).includes(`/notebooks/${pre.id}/`)).map((r) => r.method), "pre에는 결정 GET뿐").toEqual(["GET"]);
  expect(rec.sent.filter((r) => r.method === "DELETE"), "공유 행 DELETE 0").toEqual([]);
  expect(rec.sent.some((r) => r.method === "PATCH" || r.method === "PUT")).toBe(false);
  expect(JSON.stringify(rec.sent.map((r) => r.body))).not.toMatch(/user_email|user_id/);
  expect(rec.groups, "/admin/groups 요청 0건").toEqual([]);

  // 선택: 공유·건너뜀은 모두 빠진다
  await expect(page.getByTestId("bulk-selected-count")).toHaveCount(0);

  // 서버 상태: (nb, G) 행은 노트북마다 1개, 기존 write 행은 같은 id로 write 그대로
  for (const n of [s1, s2]) {
    const rows = (await sharesOf(page, n.id)).filter((r) => r.group_id === group.id);
    expect(rows.map((r) => r.permission), `${n.title} G 행`).toEqual(["read"]);
  }
  const preRows = (await sharesOf(page, pre.id)).filter((r) => r.group_id === group.id);
  expect(preRows.map((r) => [r.id, r.permission])).toEqual([[preShareId, "write"]]);

  // 수신자 B(G 구성원): 공유 탭에 3개, 권한은 기존 write 유지·새 공유 read
  const bPage = userB.page;
  await bPage.evaluate(() => localStorage.setItem("biznotes:dashboard-view", "card"));
  await bPage.goto("/");
  await bPage.getByRole("tab", { name: "공유받은 노트북" }).click();
  for (const n of [s1, s2, pre]) await expect(card(bPage, n.title)).toBeVisible({ timeout: 10_000 });
  const bTitles = await bPage.getByTestId("notebook-card").locator("h2").allTextContents();
  expect(bTitles.filter((t) => t.includes(RUN_ID) && t.startsWith("E2E 공유"))).toHaveLength(3);
  const permOf = async (id: string) =>
    ((await (await bPage.request.get(`${API_BASE_URL}/api/v1/notebooks/${id}`)).json()) as { permission: string })
      .permission;
  expect(await permOf(pre.id)).toBe("write");
  expect(await permOf(s1.id)).toBe("read");
  expect(await permOf(s2.id)).toBe("read");
  await bPage.screenshot({ path: "test-results/bulk-p3-recipient-shared-tab.png", fullPage: true });
});

test("AC-3.9 공유 실패 주입(500 1건) — 그 항목만 실패로 선택에 남고 실행 중 Esc로 닫히지 않는다", async () => {
  const page = userA.page;
  const ok1 = { title: `E2E 공유성공1 ${RUN_ID}`, id: "" };
  const ok2 = { title: `E2E 공유성공2 ${RUN_ID}`, id: "" };
  const fail = { title: `E2E 공유실패 ${RUN_ID}`, id: "" };
  for (const n of [ok1, ok2, fail]) n.id = await createNotebookViaApi(page, n.title);

  // 실패 대상의 POST /share만 2초 뒤 500으로 응답한다(서버에는 도달하지 않음). GET /shares는 그대로 보낸다.
  const failUrl = `${API_BASE_URL}/api/v1/notebooks/${fail.id}/share`;
  let injected = 0;
  await page.route(failUrl, async (route) => {
    const method = route.request().method();
    if (method === "OPTIONS") return fulfillJson(route, 204, null);
    if (method !== "POST") return route.fallback();
    injected += 1;
    await new Promise((r) => setTimeout(r, 2_000));
    await fulfillJson(route, 500, { detail: "injected" });
  });

  const rec = recordShareRequests(page);
  try {
    await openTableWith(page, [ok1.title, fail.title, ok2.title]);
    await page.getByTestId("bulk-share").click();
    const dialog = page.getByTestId("bulk-share-dialog");
    const groupSelect = dialog.getByTestId("bulk-share-group-select");
    await groupSelect.click();
    await page.getByRole("option", { name: group.name, exact: true }).click();
    await expect(groupSelect).toHaveAttribute("aria-expanded", "false");
    await dialog.getByTestId("bulk-share-submit").click();
    await expect(dialog.getByTestId("bulk-progress")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(dialog).toBeVisible();
    await expect(dialog.getByTestId("bulk-progress")).toBeVisible();

    await expect(dialog.getByTestId("bulk-result-summary")).toHaveText(
      "공유 2 · 건너뜀 0 · 실패 1 · 확인 불가 0 · 미실행 0",
      { timeout: 10_000 },
    );
    await expect(dialog.getByTestId("bulk-result-notice")).toHaveCount(0); // 중단 없음 — 나머지는 계속했다
    const failedRow = dialog.getByTestId("bulk-result-item").filter({ hasText: fail.title });
    await expect(failedRow).toHaveAttribute("data-status", "failed");
    await expect(failedRow).toContainText("서버 오류");
    for (const n of [ok1, ok2]) {
      await expect(dialog.getByTestId("bulk-result-item").filter({ hasText: n.title })).toHaveAttribute(
        "data-status",
        "succeeded",
      );
    }
    await page.screenshot({ path: "test-results/bulk-p3-partial-failure.png", fullPage: true });
    await dialog.getByRole("button", { name: "닫기" }).click();
  } finally {
    rec.dispose();
    await page.unroute(failUrl);
  }

  expect(injected).toBe(1);
  // 항목마다 GET 1회·POST 1회 — 재시도·보상·DELETE 없음
  const path = (r: SentRequest) => r.url.replace(API_BASE_URL, "");
  expect(rec.sent.filter((r) => r.method === "GET").map(path).sort()).toEqual(
    [ok1, ok2, fail].map((n) => `/api/v1/notebooks/${n.id}/shares`).sort(),
  );
  expect(rec.sent.filter((r) => r.method === "POST").map(path).sort()).toEqual(
    [ok1, ok2, fail].map((n) => `/api/v1/notebooks/${n.id}/share`).sort(),
  );
  expect(rec.sent.filter((r) => r.method === "DELETE")).toEqual([]);
  // 실패 항목만 선택에 남는다
  await expect(page.getByTestId("bulk-selected-count")).toHaveText("1개 선택됨");
  await expect(selectBox(page, fail.title)).toHaveAttribute("aria-checked", "true");
  // 서버 상태: 성공 둘은 G에 read로 공유됐고(B가 읽을 수 있다), 실패 대상은 공유되지 않았다(주입한 500은 서버에 닿지 않았다)
  for (const n of [ok1, ok2]) expect(await notebookStatus(userB.page, n.id)).toBe(200);
  expect(await notebookStatus(userB.page, fail.id)).toBe(404);
  expect((await sharesOf(page, fail.id)).filter((r) => r.group_id === group.id)).toEqual([]);
});

test("core-v0.20.0 AC-3.3 일괄 공유 — 선택한 모든 노트북에 같은 그룹 write 공유가 있으면 read 일괄은 POST 0으로 모두 건너뛴다(결정 시점 기준)", async () => {
  const page = userA.page;
  const w1 = { title: `E2E 공유 전부write1 ${RUN_ID}`, id: "" };
  const w2 = { title: `E2E 공유 전부write2 ${RUN_ID}`, id: "" };
  const preShareIds = new Map<string, string>();
  for (const n of [w1, w2]) {
    n.id = await createNotebookViaApi(page, n.title);
    const res = await page.request.post(`${API_BASE_URL}/api/v1/notebooks/${n.id}/share`, {
      data: { group_id: group.id, permission: "write" },
    });
    expect(res.status(), `기존 write 그룹 공유 ${n.title}`).toBe(201);
    preShareIds.set(n.id, ((await res.json()) as { id: string }).id);
  }

  await openTableWith(page, [w1.title, w2.title]);
  const rec = recordShareRequests(page);
  try {
    await page.getByTestId("bulk-share").click();
    const dialog = page.getByTestId("bulk-share-dialog");
    const groupSelect = dialog.getByTestId("bulk-share-group-select");
    await groupSelect.click();
    await page.getByRole("option", { name: group.name, exact: true }).click();
    await expect(groupSelect).toHaveAttribute("aria-expanded", "false");
    await expect(dialog.getByTestId("bulk-share-permission")).toHaveText(/읽기 전용/);
    await dialog.getByTestId("bulk-share-submit").click();
    await expect(dialog.getByTestId("bulk-result-summary")).toHaveText(
      "공유 0 · 건너뜀 2 · 실패 0 · 확인 불가 0 · 미실행 0",
      { timeout: 10_000 },
    );
    for (const n of [w1, w2]) {
      const row = dialog.getByTestId("bulk-result-item").filter({ hasText: n.title });
      await expect(row).toHaveAttribute("data-status", "skipped");
      await expect(row).toContainText("이미 편집 가능 권한으로 공유됨");
    }
    await page.screenshot({ path: "test-results/bulk-core0200-group-all-skipped.png", fullPage: true });
    await dialog.getByRole("button", { name: "닫기" }).click();
  } finally {
    rec.dispose();
  }

  // 결정용 GET 2건뿐 — POST·DELETE 0
  expect(rec.sent.map((r) => r.method), "공유 요청은 GET 2건뿐").toEqual(["GET", "GET"]);
  expect(rec.groups).toEqual([]);
  // 건너뜀은 선택에서 빠진다
  await expect(page.getByTestId("bulk-selected-count")).toHaveCount(0);
  // 서버 상태: 기존 write 행이 같은 id·write로 그대로다
  for (const n of [w1, w2]) {
    const rows = (await sharesOf(page, n.id)).filter((r) => r.group_id === group.id);
    expect(rows.map((r) => [r.id, r.permission]), `${n.title} G 행`).toEqual([[preShareIds.get(n.id), "write"]]);
  }
});
