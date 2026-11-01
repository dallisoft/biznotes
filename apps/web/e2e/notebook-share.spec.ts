import { randomUUID } from "node:crypto";
import path from "node:path";
import {
  expect,
  test,
  type APIRequestContext,
  type Browser,
  type BrowserContext,
  type Locator,
  type Page,
  type Response,
  type Route,
} from "@playwright/test";
import {
  API_BASE_URL,
  assertLoopbackUrl,
  createNotebookViaApi,
  fulfillJson,
  LOGIN_RATE_LIMIT_WINDOW_MS,
  loginViaUiAfterRateLimit,
} from "./helpers";

// core-v0.20.0 P3 AC-3.6(계획 §5.8 공유 재설계 2단계) — 공유 관리 대화상자, 실 API.
//
// 쓰기 스펙이라 opt-in이다: `E2E_SHARE_WRITE=1`일 때만 실행하고 없으면 전부 skip한다(로컬·검증 전용 스택에서만 켠다).
// 데이터 위생(e2e/AGENTS.md): 관리자 API로 `e2e-share-` 접두 임시 사용자 U(소유자)·V(구성원·공개 확인)와
// 임시 그룹 G1(U·V)·G2(V)를 만들고, 시작 전·종료 뒤 이 접두 사용자·그룹만 지운다(사용자 삭제 시 소유 노트북·공유 CASCADE).
// 비밀번호는 실행마다 무작위이고 출력하지 않는다. 공유 관리자 storageState로는 화면 조작을 하지 않는다(준비·정리 API만).
//
// 로그인 속도 제한(코어 `RateLimiter("login", 10/60s)`, IP 기준): 이 스펙의 UI 로그인은 U·V 두 번뿐이다(세어서 출력하고
// 2 이하를 단언). 429면 `loginViaUiAfterRateLimit`이 창이 지난 뒤 한 번 다시 시도한다 — 제한 시간을 그만큼 늘린다.
//
// 선택자는 P3 UI 계약(reports/phase-3-parallel-common.md 「UI 계약」)의 testid·역할·접근 가능 이름만 쓴다. 공유 관련 요청은
// 네트워크로 기록해 요청 본문과 `DELETE` 경로 id를 대조한다.

const SHARE_WRITE = process.env.E2E_SHARE_WRITE === "1";
const ADMIN_STORAGE_STATE = path.resolve(__dirname, "../playwright/.auth/admin.json");
const PREFIX = "e2e-share-";
const DOMAIN = "@biznotes.dev";
const RUN_ID = `${Date.now().toString(36)}${randomUUID().slice(0, 4)}`;
const MAX_LOGINS = 2;

type TempUser = { id: string; email: string; context: BrowserContext | null; page: Page | null };
type Group = { id: string; name: string };
type SentRequest = { method: string; path: string; body: Record<string, unknown> | null };

let admin: BrowserContext;
let logins = 0;
const users: Record<"u" | "v", TempUser> = {
  u: { id: "", email: "", context: null, page: null },
  v: { id: "", email: "", context: null, page: null },
};
const groups: Record<"g1" | "g2", Group> = { g1: { id: "", name: "" }, g2: { id: "", name: "" } };
const notebook = { id: "", title: `E2E 공유 관리 ${RUN_ID}` };

function newPassword(): string {
  return `E2e-${randomUUID().replaceAll("-", "").slice(0, 16)}`;
}

function pageOf(user: TempUser): Page {
  if (!user.page) throw new Error(`${user.email} 로그인 안 됨`);
  return user.page;
}

/** 이 스펙의 임시 사용자·그룹(접두 `e2e-share-`)만 지우고 남은 수를 soft로 단언한다(앞선 시도의 잔여 포함). */
async function purge(request: APIRequestContext, phase: string) {
  const listUsers = async () => {
    const res = await request.get(`${API_BASE_URL}/api/v1/admin/users`);
    expect(res.status(), "관리자 사용자 목록").toBe(200);
    return ((await res.json()) as { id: string; email: string }[]).filter(
      (u) => u.email.startsWith(PREFIX) && u.email.endsWith(DOMAIN),
    );
  };
  const listGroups = async () => {
    const res = await request.get(`${API_BASE_URL}/api/v1/admin/groups`);
    expect(res.status(), "관리자 그룹 목록").toBe(200);
    return ((await res.json()) as Group[]).filter((g) => g.name.startsWith(PREFIX));
  };
  // 사용자 먼저(소유 노트북·공유 CASCADE), 그다음 그룹
  for (const user of await listUsers()) {
    const status = await request
      .delete(`${API_BASE_URL}/api/v1/admin/users/${user.id}`)
      .then((r) => r.status(), (err: unknown) => `요청 실패: ${String(err)}`);
    console.log(`notebook-share(${phase}): 임시 사용자 ${user.id} 삭제 → ${status}`);
    expect.soft(status, `임시 사용자 ${user.id} 정리`).toBe(204);
  }
  for (const group of await listGroups()) {
    const status = await request
      .delete(`${API_BASE_URL}/api/v1/admin/groups/${group.id}`)
      .then((r) => r.status(), (err: unknown) => `요청 실패: ${String(err)}`);
    console.log(`notebook-share(${phase}): 임시 그룹 ${group.id} 삭제 → ${status}`);
    expect.soft(status, `임시 그룹 ${group.id} 정리`).toBe(204);
  }
  const leftUsers = await listUsers();
  const leftGroups = await listGroups();
  console.log(`notebook-share(${phase}): 남은 ${PREFIX} 사용자 ${leftUsers.length}명 · 그룹 ${leftGroups.length}개`);
  expect.soft(leftUsers, `${phase} 정리 뒤 ${PREFIX} 사용자 0`).toEqual([]);
  expect.soft(leftGroups, `${phase} 정리 뒤 ${PREFIX} 그룹 0`).toEqual([]);
}

async function createUser(label: "u" | "v"): Promise<{ password: string }> {
  const email = `${PREFIX}${RUN_ID}-${label}${DOMAIN}`;
  const password = newPassword();
  const res = await admin.request.post(`${API_BASE_URL}/api/v1/admin/users`, {
    data: { email, name: `E2E share ${label}`, password, role: "user" },
  });
  expect(res.status(), `임시 사용자 ${label} 생성`).toBe(201);
  users[label].id = ((await res.json()) as { id: string }).id;
  users[label].email = email;
  console.log(`notebook-share: 임시 사용자 ${label} 생성 ${users[label].id}`);
  return { password };
}

async function createGroup(label: "g1" | "g2", members: TempUser[]) {
  const name = `${PREFIX}${RUN_ID}-${label}`;
  const res = await admin.request.post(`${API_BASE_URL}/api/v1/admin/groups`, { data: { name } });
  expect(res.status(), `임시 그룹 ${label} 생성`).toBe(201);
  groups[label] = { id: ((await res.json()) as { id: string }).id, name };
  for (const member of members) {
    const added = await admin.request.post(`${API_BASE_URL}/api/v1/admin/groups/${groups[label].id}/members`, {
      data: { user_id: member.id },
    });
    expect(added.status(), `${label} 구성원 추가`).toBe(201);
  }
  console.log(`notebook-share: 임시 그룹 ${label} 생성 ${groups[label].id}`);
}

/** 새 컨텍스트에서 로그인 → 강제 비밀번호 변경 → 노트북 홈까지. 로그인 수를 센다(속도 제한). */
async function loginReady(browser: Browser, user: TempUser, temporary: string) {
  const context = await browser.newContext({ storageState: { cookies: [], origins: [] } });
  const page = await context.newPage();
  logins += 1;
  console.log(`notebook-share: UI 로그인 ${logins}회째 (상한 ${MAX_LOGINS})`);
  const login = await loginViaUiAfterRateLimit(page, user.email, temporary);
  expect(login.status(), `${user.email} 로그인 응답`).toBe(200);
  await expect(page).toHaveURL(/\/change-password(\?|$)/);
  const next = newPassword();
  await page.getByLabel("현재 비밀번호").fill(temporary);
  await page.getByLabel("새 비밀번호", { exact: true }).fill(next);
  await page.getByLabel("새 비밀번호 확인").fill(next);
  await page.getByRole("button", { name: "비밀번호 변경" }).click();
  await expect(page.getByRole("heading", { name: "노트북", exact: true })).toBeVisible({ timeout: 10_000 });
  // Base UI 열림·닫힘 트랜지션 때문에 항목이 「not stable」로 판정되는 것을 막는다(shell-smoke·shell-mobile과 같은 처리)
  await page.addInitScript(() => {
    const style = document.createElement("style");
    style.textContent = "*,*::before,*::after{transition:none!important;animation:none!important}";
    document.addEventListener("DOMContentLoaded", () => document.head.appendChild(style));
  });
  user.context = context;
  user.page = page;
}

/** 페이지가 보낸 공유 관련 요청 — `GET /shares`·`POST /share`·`DELETE /shares/{id}`·노트북 `PATCH`(프리플라이트 제외). */
function recordShareRequests(page: Page): { sent: SentRequest[]; dispose: () => void } {
  const sent: SentRequest[] = [];
  const onRequest = (req: { method(): string; url(): string; postData(): string | null }) => {
    const p = new URL(req.url()).pathname;
    if (req.method() === "OPTIONS") return;
    const shareish = /^\/api\/v1\/notebooks\/[0-9a-f-]+\/shares?(\/|$)/.test(p);
    const patch = req.method() === "PATCH" && /^\/api\/v1\/notebooks\/[0-9a-f-]+$/.test(p);
    if (!shareish && !patch) return;
    const raw = req.postData();
    sent.push({ method: req.method(), path: p, body: raw ? (JSON.parse(raw) as Record<string, unknown>) : null });
  };
  page.on("request", onRequest);
  return { sent, dispose: () => page.off("request", onRequest) };
}

const isShareWrite = (method: string) => (r: Response) =>
  r.request().method() === method && new URL(r.url()).pathname.startsWith(`/api/v1/notebooks/${notebook.id}/share`);

async function sharesOf(request: APIRequestContext): Promise<{ id: string; group_id: string; permission: string }[]> {
  const res = await request.get(`${API_BASE_URL}/api/v1/notebooks/${notebook.id}/shares`);
  expect(res.status(), "공유 목록").toBe(200);
  return (await res.json()) as { id: string; group_id: string; permission: string }[];
}

/** 노트북 홈(카드 보기)에서 그 노트북의 소유자 메뉴 「공유」로 공유 관리 대화상자를 연다 — 열림이 안정된 뒤 돌려준다. */
async function openShareDialog(page: Page): Promise<Locator> {
  await page.evaluate(() => localStorage.setItem("biznotes:dashboard-view", "card"));
  await page.goto("/");
  const card = page.getByTestId("notebook-card").filter({ hasText: notebook.title });
  await expect(card).toBeVisible({ timeout: 10_000 });
  await card.hover();
  await card.getByRole("button", { name: `${notebook.title} 동작 더보기` }).click();
  await page.getByRole("menuitem", { name: "공유" }).click();
  const dialog = page.getByTestId("share-dialog");
  await expect(dialog).toHaveAttribute("data-open", "");
  await expect(page.getByRole("dialog", { name: "노트북 공유" })).toBeVisible();
  // 목록은 공유·그룹 조회가 끝나야 그려진다(행 표기를 정한다). 0행이면 빈 <ul>이라 크기가 0이어서 「보임」이 아니다 —
  // 붙음(attached)으로 본다(core-v0.20.0 P3 통합 e2e 1차 — toBeVisible이 hidden으로 10초 시간 초과, 0행 안내는 형제 share-list-empty)
  await expect(dialog.getByTestId("share-list")).toBeAttached({ timeout: 10_000 });
  return dialog;
}

/** 선택 control(트리거)을 열어 그 이름의 선택지를 고른다 — 선택지가 보인 뒤 누른다. */
async function choose(page: Page, trigger: Locator, name: string) {
  await trigger.click();
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
  const option = page.getByRole("option", { name, exact: true });
  await expect(option).toBeVisible();
  await option.click();
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
}

test.describe("노트북 공유 관리(core-v0.20.0 AC-3.6)", () => {
  test.skip(!SHARE_WRITE, "쓰기 스펙 — E2E_SHARE_WRITE=1일 때만 실행한다");
  test.describe.configure({ mode: "serial", retries: 0 });

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(90_000 + MAX_LOGINS * LOGIN_RATE_LIMIT_WINDOW_MS);
    // 첫 변경 요청 전에 Web·API 주소가 모두 loopback인지 확인한다
    assertLoopbackUrl(test.info().project.use.baseURL, "Playwright baseURL");
    assertLoopbackUrl(API_BASE_URL, "E2E API baseURL");

    admin = await browser.newContext({ storageState: ADMIN_STORAGE_STATE });
    await purge(admin.request, "시작 전");
    const u = await createUser("u");
    const v = await createUser("v");
    await createGroup("g1", [users.u, users.v]);
    await createGroup("g2", [users.v]); // U는 속하지 않는다 — U의 선택지에 없어야 한다

    await loginReady(browser, users.u, u.password);
    await loginReady(browser, users.v, v.password);
    notebook.id = await createNotebookViaApi(pageOf(users.u), notebook.title);
  });

  test.afterAll(async () => {
    console.log(`notebook-share: UI 로그인 ${logins}회(상한 ${MAX_LOGINS})`);
    expect.soft(logins, "로그인 수 상한(속도 제한 10/60s)").toBeLessThanOrEqual(MAX_LOGINS);
    for (const user of Object.values(users)) await user.context?.close();
    if (!admin) return;
    try {
      await purge(admin.request, "종료 뒤");
    } finally {
      await admin.close();
    }
  });

  test("그룹 공유 → 목록 → 권한 변경(같은 id) → 해제 — 선택지는 소유자 소속 그룹뿐이고 본문은 정확히 {group_id, permission}이다", async () => {
    const page = pageOf(users.u);
    const rec = recordShareRequests(page);
    let shareId = "";
    try {
      const dialog = await openShareDialog(page);
      await expect(dialog.getByTestId("share-row")).toHaveCount(0);

      // 선택지 = U가 속한 그룹(G1)뿐 — G2(V만)는 없다
      const groupSelect = dialog.getByTestId("share-group-select");
      await groupSelect.click();
      await expect(groupSelect).toHaveAttribute("aria-expanded", "true");
      await expect(page.getByRole("option")).toHaveText([groups.g1.name]);
      await page.keyboard.press("Escape");
      await expect(groupSelect).toHaveAttribute("aria-expanded", "false");
      await expect(page.getByRole("dialog", { name: "노트북 공유" })).toBeVisible();

      // ① 그룹 공유(읽기 전용)
      await choose(page, groupSelect, groups.g1.name);
      const permissionSelect = dialog.getByTestId("share-permission-select");
      await expect(permissionSelect).toHaveText(/읽기 전용/);
      const created = page.waitForResponse(isShareWrite("POST"));
      await dialog.getByTestId("share-submit").click();
      const createdRes = await created;
      expect(createdRes.status(), "그룹 공유").toBe(201);
      shareId = ((await createdRes.json()) as { id: string }).id;
      const row = dialog.getByTestId("share-row");
      await expect(row).toHaveCount(1);
      await expect(row).toHaveAttribute("data-share-id", shareId);
      await expect(row).toHaveAttribute("data-share-kind", "group");
      await expect(row).toHaveAttribute("data-permission", "read");
      await expect(row).toContainText(groups.g1.name);
      await expect(row).toContainText("읽기 전용");
      await expect(page.getByRole("dialog", { name: "노트북 공유" })).toBeVisible(); // 관리 대화상자 — 열린 채

      // ② 그 행의 권한 변경(같은 POST — 같은 행 id)
      const changed = page.waitForResponse(isShareWrite("POST"));
      await choose(page, row.getByTestId("share-row-permission"), "편집 가능");
      const changedRes = await changed;
      expect(changedRes.status(), "권한 변경").toBe(201);
      expect(((await changedRes.json()) as { id: string }).id, "같은 행 id").toBe(shareId);
      await expect(row).toHaveAttribute("data-permission", "write");
      await expect(row).toHaveAttribute("data-share-id", shareId);
      await expect(dialog.getByTestId("share-row")).toHaveCount(1);

      // ③ 해제 — 확인 문구에 대상 표기·권한, 그 행 id로만 DELETE
      await row.getByTestId("share-row-revoke").click();
      const confirm = page.getByRole("alertdialog");
      await expect(confirm).toBeVisible();
      await expect(confirm).toContainText(`${groups.g1.name} · 편집 가능`);
      const deleted = page.waitForResponse(isShareWrite("DELETE"));
      await confirm.getByRole("button", { name: "해제", exact: true }).click();
      expect((await deleted).status(), "해제").toBe(204);
      await expect(dialog.getByTestId("share-row")).toHaveCount(0);
      await page.screenshot({ path: "test-results/notebook-share-group-flow.png", fullPage: true });
    } finally {
      rec.dispose();
    }

    const writes = rec.sent.filter((r) => r.method !== "GET");
    expect(writes).toEqual([
      { method: "POST", path: `/api/v1/notebooks/${notebook.id}/share`, body: { group_id: groups.g1.id, permission: "read" } },
      { method: "POST", path: `/api/v1/notebooks/${notebook.id}/share`, body: { group_id: groups.g1.id, permission: "write" } },
      { method: "DELETE", path: `/api/v1/notebooks/${notebook.id}/shares/${shareId}`, body: null },
    ]);
    expect(JSON.stringify(rec.sent)).not.toMatch(/user_email|user_id/);
    expect(await sharesOf(pageOf(users.u).request)).toEqual([]);
  });

  test("전체 공개 토글 → 다른 사용자(V)의 목록에 「공개」 → 끄면 사라진다 — 본문은 정확히 {visibility}", async () => {
    const page = pageOf(users.u);
    const viewer = pageOf(users.v);
    const rec = recordShareRequests(page);
    try {
      let dialog = await openShareDialog(page);
      const toggle = dialog.getByRole("switch", { name: "전체 공개" });
      await expect(toggle).toHaveAttribute("data-testid", "share-public-toggle");
      await expect(toggle).toHaveAttribute("aria-checked", "false");
      const on = page.waitForResponse(
        (r) => r.request().method() === "PATCH" && new URL(r.url()).pathname === `/api/v1/notebooks/${notebook.id}`,
      );
      await toggle.click();
      expect((await on).status(), "공개 켜기").toBe(200);
      await expect(toggle).toHaveAttribute("aria-checked", "true");
      await page.screenshot({ path: "test-results/notebook-share-public-on.png", fullPage: true });

      // V(G1 구성원이지만 공유 행은 없다)의 「전체」 목록에 「공개」로 보인다
      await viewer.evaluate(() => localStorage.setItem("biznotes:dashboard-view", "card"));
      await viewer.goto("/");
      const seen = viewer.getByTestId("notebook-card").filter({ hasText: notebook.title });
      await expect(seen).toBeVisible({ timeout: 10_000 });
      await expect(seen.getByText("공개", { exact: true })).toBeVisible();

      // 끄기 — 대화상자를 다시 열어 같은 토글로
      dialog = await openShareDialog(page);
      const toggleAgain = dialog.getByRole("switch", { name: "전체 공개" });
      await expect(toggleAgain).toHaveAttribute("aria-checked", "true");
      const off = page.waitForResponse(
        (r) => r.request().method() === "PATCH" && new URL(r.url()).pathname === `/api/v1/notebooks/${notebook.id}`,
      );
      await toggleAgain.click();
      expect((await off).status(), "공개 끄기").toBe(200);
      await expect(toggleAgain).toHaveAttribute("aria-checked", "false");
    } finally {
      rec.dispose();
    }

    expect(rec.sent.filter((r) => r.method === "PATCH")).toEqual([
      { method: "PATCH", path: `/api/v1/notebooks/${notebook.id}`, body: { visibility: "public" } },
      { method: "PATCH", path: `/api/v1/notebooks/${notebook.id}`, body: { visibility: "private" } },
    ]);
    await viewer.goto("/");
    await expect(viewer.getByRole("heading", { name: "노트북", exact: true })).toBeVisible();
    await expect(viewer.getByTestId("notebook-card").filter({ hasText: notebook.title })).toHaveCount(0);
    expect((await viewer.request.get(`${API_BASE_URL}/api/v1/notebooks/${notebook.id}`)).status()).toBe(404);
  });

  test("사용자(이메일) 대상 공유는 실 API에서 422이고 행이 생기지 않는다(코어 v0.28.0 #278 3단계)", async () => {
    const request = pageOf(users.u).request;
    const before = await sharesOf(request);
    for (const data of [
      { user_email: users.v.email, permission: "read" },
      { user_id: users.v.id, permission: "read" },
    ]) {
      const res = await request.post(`${API_BASE_URL}/api/v1/notebooks/${notebook.id}/share`, { data });
      expect(res.status(), `사용자 대상 ${Object.keys(data)[0]}`).toBe(422);
    }
    expect(await sharesOf(request)).toEqual(before);
  });

  // PR #154 P3-03 — 공유 행이 많으면 대화상자가 뷰포트를 넘어 위(닫기)·아래(마지막 행·전체 공개)가 잘리던 결함.
  // 목록 GET과 그 가짜 행의 DELETE만 page.route로 대체한다: 판정 대상은 화면 배치(클라이언트)이고, 실제 행 20개를 만들려면 그룹 20개가
  // 필요해 dev DB를 어지럽힌다. 전체 공개 토글은 실제 API다
  // (U 소유 노트북 — 켠 뒤 다시 끈다). 판정은 요소가 뷰포트 안에서 클릭 가능한지(scrollIntoViewIfNeeded → toBeInViewport → 클릭)다.
  test("공유 행이 많아도(20행) 390×844에서 대화상자가 뷰포트 안에 있고 마지막 행 해제·전체 공개 토글·닫기에 닿는다", async () => {
    const page = pageOf(users.u);
    const original = page.viewportSize();
    const VIEW = { width: 390, height: 844 };
    const fake = (n: number) => `${randomUUID().slice(0, 8)}-${String(n).padStart(4, "0")}-4000-8000-${randomUUID().slice(-12)}`;
    type ShareOut = { id: string; notebook_id: string; group_id: string; permission: "read" | "write" };
    let rows: ShareOut[] = [
      { id: fake(0), notebook_id: notebook.id, group_id: groups.g1.id, permission: "read" },
      ...Array.from({ length: 19 }, (_, i): ShareOut => ({
        id: fake(i + 1), notebook_id: notebook.id, group_id: fake(100 + i), permission: i % 2 ? "write" : "read",
      })),
    ];
    const fakeIds = new Set(rows.map((r) => r.id));
    const deleted: string[] = [];
    const sharesUrl = `${API_BASE_URL}/api/v1/notebooks/${notebook.id}/shares`;
    const onList = async (route: Route) => {
      const m = route.request().method();
      if (m === "GET" || m === "OPTIONS") return fulfillJson(route, 200, rows);
      return route.fallback();
    };
    const onRow = async (route: Route) => {
      const m = route.request().method();
      const id = new URL(route.request().url()).pathname.split("/").pop() ?? "";
      if (!fakeIds.has(id)) return route.fallback(); // 가짜 행만 — 실제 행 DELETE는 서버로
      if (m === "OPTIONS") return fulfillJson(route, 204, null);
      if (m !== "DELETE") return route.fallback();
      deleted.push(id);
      rows = rows.filter((r) => r.id !== id);
      const origin = route.request().headers()["origin"] ?? "http://localhost:23000";
      await route.fulfill({
        status: 204,
        headers: { "access-control-allow-origin": origin, "access-control-allow-credentials": "true" },
      });
    };
    const rec = recordShareRequests(page);
    await page.setViewportSize(VIEW);
    await page.route(sharesUrl, onList);
    await page.route(`${sharesUrl}/*`, onRow);
    let lastId = "";
    try {
      const dialog = await openShareDialog(page);
      const shareRows = dialog.getByTestId("share-row");
      await expect(shareRows).toHaveCount(20);

      // 대화상자 상자 전체가 뷰포트 안이다(넘치면 위·아래가 잘린다)
      const box = await dialog.boundingBox();
      expect(box, "대화상자 상자").not.toBeNull();
      if (box) {
        console.log(`notebook-share(P3-03): 대화상자 상자 ${JSON.stringify(box)} · 뷰포트 ${VIEW.width}×${VIEW.height}`);
        expect(box.y, "대화상자 위 끝 ≥ 0").toBeGreaterThanOrEqual(0);
        expect(box.y + box.height, "대화상자 아래 끝 ≤ 뷰포트 높이").toBeLessThanOrEqual(VIEW.height);
        expect(box.x, "대화상자 왼쪽 끝 ≥ 0").toBeGreaterThanOrEqual(0);
        expect(box.x + box.width, "대화상자 오른쪽 끝 ≤ 뷰포트 너비").toBeLessThanOrEqual(VIEW.width);
      }
      const close = dialog.getByRole("button", { name: "닫기", exact: true });
      await expect(close, "닫기 — 처음 화면").toBeInViewport({ ratio: 1 });
      await close.click({ trial: true });
      await page.screenshot({ path: "test-results/notebook-share-many-rows-390-top.png" }); // 제목·닫기·첫 행들(토스트 없음)

      // 마지막 행 해제 — 스크롤해 닿고, 그 행 id로만 DELETE
      const last = shareRows.last();
      lastId = (await last.getAttribute("data-share-id")) ?? "";
      expect(lastId, "마지막 행 id").toBe(rows[rows.length - 1].id);
      const revoke = last.getByTestId("share-row-revoke");
      await revoke.scrollIntoViewIfNeeded();
      await expect(revoke, "마지막 행 해제 버튼").toBeInViewport({ ratio: 1 });
      await revoke.click({ trial: true });
      await page.screenshot({ path: "test-results/notebook-share-many-rows-390-bottom.png" }); // 본문을 끝까지 스크롤 — 마지막 행·전체 공개, 제목·닫기는 그대로
      await revoke.click();
      const confirm = page.getByRole("alertdialog");
      await expect(confirm).toBeVisible();
      await confirm.getByRole("button", { name: "해제", exact: true }).click();
      await expect(shareRows).toHaveCount(19);
      expect(deleted, "가짜 행 DELETE").toEqual([lastId]);

      // 전체 공개 토글 — 스크롤해 닿고 켜고 끈다(실제 PATCH)
      const toggle = dialog.getByRole("switch", { name: "전체 공개" });
      await toggle.scrollIntoViewIfNeeded();
      await expect(toggle, "전체 공개 토글").toBeInViewport({ ratio: 1 });
      await toggle.click({ trial: true });
      for (const want of ["true", "false"]) {
        const patched = page.waitForResponse(
          (r) => r.request().method() === "PATCH" && new URL(r.url()).pathname === `/api/v1/notebooks/${notebook.id}`,
        );
        await toggle.click();
        expect((await patched).status(), `전체 공개 → ${want}`).toBe(200);
        await expect(toggle).toHaveAttribute("aria-checked", want);
      }
      await page.screenshot({ path: "test-results/notebook-share-many-rows-390.png" });

      // 닫기 — 본문을 끝까지 스크롤한 뒤에도 그대로 보이고 누르면 닫힌다
      await expect(close, "닫기 — 스크롤 뒤").toBeInViewport({ ratio: 1 });
      await close.click();
      await expect(page.getByRole("dialog", { name: "노트북 공유" })).toHaveCount(0);
    } finally {
      rec.dispose();
      await page.unroute(sharesUrl, onList);
      await page.unroute(`${sharesUrl}/*`, onRow);
      if (original) await page.setViewportSize(original);
    }

    expect(rec.sent.filter((r) => r.method !== "GET")).toEqual([
      { method: "DELETE", path: `/api/v1/notebooks/${notebook.id}/shares/${lastId}`, body: null },
      { method: "PATCH", path: `/api/v1/notebooks/${notebook.id}`, body: { visibility: "public" } },
      { method: "PATCH", path: `/api/v1/notebooks/${notebook.id}`, body: { visibility: "private" } },
    ]);
    expect(JSON.stringify(rec.sent)).not.toMatch(/user_email|user_id/);
  });
});
