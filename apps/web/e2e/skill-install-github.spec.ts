import { expect, test } from "@playwright/test";
import { API_BASE_URL, cleanupCreatedNotebooks, createNotebookAndOpen, gotoDashboard } from "./helpers";

// #259 PR-2 — 채팅에서 GitHub 저장소 URL로 스킬 설치(설계 노트 docs/plans/skill-install-github.md §5 PR-2 6).
//
// 실네트워크 라이브 스펙이다: dev api가 api.github.com·codeload.github.com에 실제로 간다. 커밋 SHA를 고정한
// `/tree/<sha>/skills/humanize` 주소라 저장소가 바뀌어도 같은 archive를 받는다. 네트워크가 없으면 skip한다.
// 전제: dev 스택의 표준 스킬 런타임이 켜져 있다(`SKILL_STANDARD_RUNTIME_ENABLED=true`, #259 PR-1).
// 만든 스킬은 응답의 id로 afterEach에서 API로 지운다(storageState = admin, 설치 결과는 admin private 스킬).
cleanupCreatedNotebooks("skill-install-github");

const SHA = "b37e0206d31fffd9149bf57168d4d62eb2086bee";
const REQUEST = `https://github.com/epoko77-ai/im-not-ai/tree/${SHA}/skills/humanize 이곳의 스킬을 설치해줘`;
const createdSkillIds = new Set<string>();

test.beforeAll(async () => {
  const online = await fetch("https://codeload.github.com", { method: "HEAD", signal: AbortSignal.timeout(5_000) })
    .then(() => true)
    .catch(() => false);
  test.skip(!online, "GitHub에 닿지 않는다 — 실네트워크 스펙이라 건너뛴다");
});

test.afterEach(async ({ request }) => {
  for (const id of createdSkillIds) {
    const status = await request
      .delete(`${API_BASE_URL}/api/v1/skills/${id}`)
      .then((res) => res.status(), (err: unknown) => `요청 실패: ${String(err)}`);
    console.log(`skill-install-github: skill ${id} 정리 → ${status}`);
    expect.soft([204, 404], `스킬 ${id} 정리`).toContain(status);
  }
  createdSkillIds.clear();
});

test("GitHub URL 설치 요청 → 확인 카드 → 설치 → 스킬 링크, 새로고침하면 요약 답변만 남는다", async ({ page }) => {
  test.setTimeout(120_000);
  await gotoDashboard(page);
  await createNotebookAndOpen(page, `PW 스킬 설치 ${Date.now()}`);

  await page.getByTestId("chat-input").fill(REQUEST);
  await page.getByRole("button", { name: "전송" }).click();

  const card = page.getByTestId("skill-install-card");
  await expect(card).toBeVisible({ timeout: 60_000 });
  await expect(card.getByTestId("skill-install-source")).toHaveText("epoko77-ai/im-not-ai@b37e020");
  await expect(card.getByTestId("skill-install-license")).toHaveText("MIT");
  const rows = card.getByTestId("skill-install-candidate");
  await expect(rows).toHaveCount(1); // /tree/<sha>/skills/humanize — humanize-korean 등은 경로 밖이다
  await expect(rows.first()).toHaveAttribute("data-path", "skills/humanize");
  await expect(card.getByRole("checkbox", { name: "humanize 설치" })).toBeChecked();
  // 이 계정에 같은 출처가 이미 설치돼 있으면 설치하지 않고 멈춘다 — 재설치는 남의 실행이 만든 스킬에
  // version을 더하고, 정리 단계가 그 스킬을 지울 수 있다(리뷰 지적). 정리 대상은 이 실행이 새로 만든 것뿐이다.
  await expect(card.getByText(/다시 설치/), "같은 출처가 이미 설치돼 있다 — 먼저 그 스킬을 지우고 다시 실행").toHaveCount(0);

  const installed = page.waitForResponse(
    (r) => new URL(r.url()).pathname === "/api/v1/skill-installs/github" && r.request().method() === "POST",
  );
  await card.getByTestId("skill-install-submit").click();
  const res = await installed;
  expect(res.status(), await res.text()).toBe(200);
  const body = (await res.json()) as { items: { skill_id: string | null; status: string; version: number }[] };
  for (const item of body.items) if (item.skill_id && item.status === "installed") createdSkillIds.add(item.skill_id);
  expect(body.items.map((i) => i.status)).toEqual(["installed"]);

  const result = card.getByTestId("skill-install-result");
  const link = result.getByRole("link", { name: "humanize" });
  await expect(link).toHaveAttribute("href", `/skills/${body.items[0].skill_id}`);
  await expect(result).toContainText("v1 설치됨");

  // 카드는 저장하지 않는다 — 새로고침하면 요약 답변만 남고, 스킬은 그대로다
  await page.reload();
  const messages = page.getByTestId("chat-messages");
  await expect(messages.getByText(/epoko77-ai\/im-not-ai@b37e020에서 스킬 후보 1개를 찾았습니다/)).toBeVisible();
  await expect(page.getByTestId("skill-install-card")).toHaveCount(0);
  const skill = await page.request.get(`${API_BASE_URL}/api/v1/skills/${body.items[0].skill_id}`);
  expect(skill.status()).toBe(200);
  expect((await skill.json()) as { visibility: string }).toMatchObject({ visibility: "private" });
});
