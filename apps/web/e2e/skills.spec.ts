import { expect, type APIRequestContext, type Page, test } from "@playwright/test";

// 스킬 UI e2e 최소 스펙(core-v0.10.2-upgrade 계획 S-9 · HCI-11 (a) · Phase 3-7).
// 목록 렌더 → 상세, 그리고 publish 응답의 두 분기(authored → 200 `published`,
// ZIP 가져오기(imported) → 202 `review_pending`)를 브라우저 경로로 확인한다.
//
// 실행 전제:
//  - publish 스펙은 실제 스킬을 만들고 공개·검토 요청을 남기는 **쓰기 스펙**이다.
//    admin-embedding.spec.ts의 `E2E_REINDEX_WRITE` 관행대로 기본은 skip이며,
//    `E2E_SKILLS_WRITE=1` 을 준 로컬·검증 전용 실행에서만 동작한다. 운영 스택에서는 실행하지 않는다.
//    ui-ux-redesign 게이트는 owner 판정 E-2(2026-10-15)에 따라 이 두 건을 dev에서 실제로 돌린다 —
//    게이트 실행기(e2e-gate-check.py)가 정본의 `stack: "SW"` 항목이 든 이 파일의 실행 단위에만
//    `E2E_SKILLS_WRITE=1`을 넣는다(호출자 환경의 같은 변수는 지운다).
//  - 이 스위트의 storageState는 admin 계정이다. 검토 필요 여부는 실행자 role이 아니라 스킬
//    출처(가져온 스킬)로 정해지므로 admin이 올려도 imported publish는 202다(코어 exposure 관문).
//  - 만든 스킬 ID는 응답 직후 수집하고, 테스트 본문과 별도 timeout을 쓰는 afterEach의
//    APIRequestContext로 `DELETE /api/v1/skills/{id}`를 호출한다. 본문 timeout 뒤에도 정리한다.
//  - 코어 삭제 경로는 frozen 스킬의 wrapper agent를 지우지 않는다(BL-141). 따라서 쓰기 스펙
//    1회당 wrapper가 최대 2건 남을 수 있으며, 정리 정책은 Phase 3 회고 HCI에서 판정한다.

const WRITE_ENABLED = process.env.E2E_SKILLS_WRITE === "1";
// 브라우저가 쓰는 공개 API 주소(compose NEXT_PUBLIC_API_BASE_URL 기본값과 같다)
const API_BASE_URL = process.env.E2E_API_BASE_URL ?? "http://localhost:28000";
const OPERATION_TIMEOUT = 15_000;

const PUBLISH_URL = /\/api\/v1\/skills\/[^/]+\/versions\/[^/]+\/publish$/;
const createdSkillIds = new Set<string>();

/**
 * 「새 스킬」 → 이름·설명 다이얼로그 → 「만들기」(ui-ux-redesign P4 AC-4.2 — 즉시 「제목 없는 스킬」을 만들지 않는다).
 * 생성 응답(201)의 스킬 ID를 정리 목록에 넣고 돌려준다.
 */
async function createSkillViaDialog(page: Page, name: string, description: string): Promise<string> {
  await page.getByRole("button", { name: "새 스킬" }).click();
  const dialog = page.getByRole("dialog", { name: "새 스킬" });
  await expect(dialog).toBeVisible();
  const submit = dialog.getByRole("button", { name: "만들기" });
  await expect(submit, "이름이 비면 만들기 비활성").toBeDisabled();
  await dialog.getByLabel("이름").fill(name);
  await dialog.getByLabel("설명 (선택)").fill(description);
  const created = page.waitForResponse(
    (r) => new URL(r.url()).pathname === "/api/v1/skills" && r.request().method() === "POST",
    { timeout: OPERATION_TIMEOUT },
  );
  await submit.click();
  const createdResponse = await created;
  expect(createdResponse.status()).toBe(201);
  const createdBody = (await createdResponse.json()) as { id?: string };
  expect(createdBody.id).toBeTruthy();
  createdSkillIds.add(createdBody.id!);
  return createdBody.id!;
}

/** 비압축(stored) ZIP 한 개를 만든다 — 테스트 의존성을 늘리지 않으려고 직접 쓴다. */
function storedZip(files: Record<string, string>): Buffer {
  const crcTable = Array.from({ length: 256 }, (_, n) => {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    return c >>> 0;
  });
  const crc32 = (buf: Buffer) => {
    let c = 0xffffffff;
    for (const byte of buf) c = crcTable[(c ^ byte) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  };
  const locals: Buffer[] = [];
  const centrals: Buffer[] = [];
  let offset = 0;
  for (const [path, text] of Object.entries(files)) {
    const name = Buffer.from(path, "utf8");
    const data = Buffer.from(text, "utf8");
    const crc = crc32(data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0x0800, 6); // UTF-8 파일명
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(name.length, 26);
    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(0x0800, 8);
    central.writeUInt32LE(crc, 16);
    central.writeUInt32LE(data.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(name.length, 28);
    central.writeUInt32LE(offset, 42);
    locals.push(local, name, data);
    centrals.push(central, name);
    offset += local.length + name.length + data.length;
  }
  const centralSize = centrals.reduce((n, b) => n + b.length, 0);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(Object.keys(files).length, 8);
  end.writeUInt16LE(Object.keys(files).length, 10);
  end.writeUInt32LE(centralSize, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, ...centrals, end]);
}

async function deleteSkill(request: APIRequestContext, skillId: string) {
  // afterEach에서 던지면 본문의 원래 실패를 덮어쓴다 — 요청 예외도 값으로 받고 soft로 단언한다.
  const status = await request
    .delete(`${API_BASE_URL}/api/v1/skills/${skillId}`, { timeout: OPERATION_TIMEOUT })
    .then((res) => res.status(), (err: unknown) => `요청 실패: ${String(err)}`);
  expect.soft([204, 404], `스킬 ${skillId} 정리`).toContain(status);
  if (status === 204 || status === 404) createdSkillIds.delete(skillId);
}

test.afterEach(async ({ request }) => {
  for (const skillId of [...createdSkillIds]) await deleteSkill(request, skillId);
});

test("스킬 목록이 렌더되고 카드에서 상세로 이동한다", async ({ page }) => {
  await page.goto("/skills");
  await expect(page.getByRole("heading", { name: "스킬", exact: true })).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.getByRole("button", { name: "ZIP 가져오기" })).toBeVisible();
  await expect(page.getByRole("button", { name: "새 스킬" })).toBeVisible();

  const grid = page.getByTestId("skill-grid");
  await expect(grid).toBeVisible({ timeout: 15_000 });
  // 카드 = 목록 항목. 제목(h3) 안의 링크가 카드 전체를 덮는다(P4 — 더보기 메뉴와 함께 둘 수 있게)
  const firstCard = grid.getByRole("listitem").first();
  const name = (await firstCard.getByRole("heading").innerText()).trim();
  await firstCard.getByRole("link", { name, exact: true }).click();

  await page.waitForURL(/\/skills\/[^/]+$/, { timeout: OPERATION_TIMEOUT });
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible({ timeout: 15_000 });
  // 옛 「← 스킬 목록」 링크는 브레드크럼 「스킬 / {이름}」으로 바뀌었다(plan §9 P4 e2e 영향)
  await expect(
    page.getByRole("navigation", { name: "위치" }).getByRole("link", { name: "스킬", exact: true }),
  ).toBeVisible();
});

test("authored 스킬 공유 → publish 200 published", async ({ page }) => {
  test.skip(!WRITE_ENABLED, "쓰기 스펙(스킬 생성·공개) — E2E_SKILLS_WRITE=1 로 명시 실행할 때만 동작한다");

  await page.goto("/skills");
  await expect(page.getByRole("heading", { name: "스킬", exact: true })).toBeVisible({ timeout: 15_000 });
  const authoredName = `e2e-authored-${Date.now()}`;
  const skillId = await createSkillViaDialog(page, authoredName, "e2e authored skill");
  await page.waitForURL(new RegExp(`/skills/${skillId}/edit$`), { timeout: OPERATION_TIMEOUT });
  // 다이얼로그에서 정한 이름이 그대로 표시 이름이다(「제목 없는 스킬」이 아님)
  await expect(page.getByLabel("표시 이름")).toHaveValue(authoredName, { timeout: 15_000 });

  // P2 R-4: 편집 가능한 스킬의 편집기 분기 — main은 1개, bleed 래퍼가 셸 main의 확정 높이를 채운다
  await expect(page.locator("main")).toHaveCount(1);
  const fill = await page.locator("#main-content").evaluate((el) => ({
    main: el.clientHeight,
    child: (el.firstElementChild as HTMLElement | null)?.getBoundingClientRect().height ?? -1,
  }));
  expect(Math.abs(fill.child - fill.main), `bleed 래퍼 높이 ${fill.child} ≈ main ${fill.main}`).toBeLessThanOrEqual(1);

  // 새 스킬의 frontmatter name·description은 식별 가능하게 채워 동결 검증을 통과시킨다.
  await page.getByLabel("name", { exact: true }).fill(authoredName);
  await page
    .getByPlaceholder("무엇을 하고 언제 쓰는지")
    .fill("e2e authored skill for the publish 200 branch.");
  const saved = page.waitForResponse(
    (r) => new URL(r.url()).pathname.endsWith(`/api/v1/skills/${skillId}/frontmatter`) &&
      r.request().method() === "PUT",
    { timeout: OPERATION_TIMEOUT },
  );
  await page.getByRole("button", { name: "frontmatter 저장" }).click();
  expect((await saved).status()).toBe(200);

  const publish = page.waitForResponse(
    (r) => PUBLISH_URL.test(new URL(r.url()).pathname) && r.request().method() === "POST",
    { timeout: OPERATION_TIMEOUT },
  );
  await page.getByRole("button", { name: "조직에 공유" }).click();
  const res = await publish;
  expect(res.status()).toBe(200);
  expect((await res.json()).status).toBe("published");
  await expect(page.getByText(/공개\(org\)했습니다/)).toBeVisible();
});

test("ZIP 가져오기(imported) 스킬 공유 → publish 202 review_pending", async ({ page }) => {
  test.skip(!WRITE_ENABLED, "쓰기 스펙(스킬 가져오기·검토 요청) — E2E_SKILLS_WRITE=1 로 명시 실행할 때만 동작한다");

  const name = `e2e-imported-${Date.now()}`;
  const zip = storedZip({
    "SKILL.md": `---\nname: ${name}\ndescription: e2e imported skill for the publish review branch.\n---\n\n# E2E\n\nSummarize the notes.\n`,
  });

  await page.goto("/skills");
  await expect(page.getByRole("button", { name: "ZIP 가져오기" })).toBeVisible({ timeout: 15_000 });
  const imported = page.waitForResponse(
    (r) => new URL(r.url()).pathname === "/api/v1/skills/import-zip" &&
      r.request().method() === "POST",
    { timeout: OPERATION_TIMEOUT },
  );
  await page.locator('input[type="file"][accept=".zip"]').setInputFiles({
    name: `${name}.zip`,
    mimeType: "application/zip",
    buffer: zip,
  });
  const importedResponse = await imported;
  expect(importedResponse.status()).toBe(201);
  const importedBody = (await importedResponse.json()) as { id?: string };
  expect(importedBody.id).toBeTruthy();
  const skillId = importedBody.id!;
  createdSkillIds.add(skillId);
  await page.waitForURL(/\/skills\/[^/]+\/edit$/, { timeout: OPERATION_TIMEOUT });

  const publish = page.waitForResponse(
    (r) => PUBLISH_URL.test(new URL(r.url()).pathname) && r.request().method() === "POST",
    { timeout: OPERATION_TIMEOUT },
  );
  await page.getByRole("button", { name: "조직에 공유" }).click();
  const res = await publish;
  expect(res.status()).toBe(202);
  const body = await res.json();
  expect(body.status).toBe("review_pending");
  expect(body.request_id).toBeTruthy();
  await expect(page.getByText("관리자 검토 대기 — 승인되면 조직에 공개됩니다")).toBeVisible();
});
