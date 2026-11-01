import { expect, test, type Page } from "@playwright/test";
import { cleanupCreatedNotebooks, createNotebookAndOpen, fulfillJson, gotoDashboard } from "./helpers";

// #182 — 스튜디오 마인드맵 생성 다이얼로그: 긴 소스 제목·다수 선택에서도 내용이 패널 안에 있고,
// 카드 아이콘·설명을 쓰며 입력 정책 배지를 보이지 않는다. 실행(생성)은 하지 않으므로 LLM이 필요 없다.
// 소스 목록은 응답을 가로채 준비된 소스 N개로 채운다(인제스천 대기 없음).
cleanupCreatedNotebooks("mindmap-dialog");

const LONG_TITLE =
  "llm-wiki: Karpathy의 방법론으로 흩어진 정보를 지식 그래프로 키우는 개인 지식저장소 구축 실전 가이드 — 아주 긴 제목";

async function stubReadySources(page: Page, notebookId: string, count: number) {
  const now = new Date().toISOString();
  const sources = Array.from({ length: count }, (_, i) => ({
    id: `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`,
    notebook_id: notebookId,
    type: "web",
    title: i % 2 === 0 ? `${LONG_TITLE} ${i}` : `LLMWikiKnowledgeBasesForAIAgentsWithoutSpaces${"x".repeat(60)}${i}`,
    origin_url: "https://example.com/",
    mime_type: "text/html",
    size_bytes: 1000,
    status: "ready",
    error_message: null,
    token_count: 100,
    chunk_count: 1,
    created_at: now,
    updated_at: now,
    code: `S${i}`,
  }));
  await page.route(`**/api/v1/notebooks/${notebookId}/sources`, (route) =>
    route.request().method() === "GET" || route.request().method() === "OPTIONS"
      ? fulfillJson(route, 200, sources)
      : route.fallback(),
  );
}

async function openStudioDialog(page: Page) {
  await gotoDashboard(page);
  const notebookId = await createNotebookAndOpen(page, "PW 마인드맵 다이얼로그");
  await stubReadySources(page, notebookId, 35);
  await page.reload();
  // 768px 미만은 오른쪽 패널 탭 대신 하단 탭바로 에이전트 패널을 연다
  const mobile = (page.viewportSize()?.width ?? 1280) < 768;
  await page.getByTestId(mobile ? "mobile-tab-agents" : "right-tab-agents").click();
  const card = page.getByTestId("studio-card-mindmap");
  await expect(card).toBeEnabled({ timeout: 15_000 });
  await card.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByTestId("source-picker-list")).toBeVisible();
  return dialog;
}

async function expectInsideDialog(page: Page) {
  const dialog = page.getByRole("dialog");
  const box = (await dialog.boundingBox())!;
  const right = box.x + box.width + 0.5;
  for (const loc of [
    dialog.locator("textarea, input[type=text]").first(),
    dialog.getByTestId("source-picker-list"),
    dialog.getByTestId("run-agent"),
    dialog.getByRole("button", { name: /전체 (선택|해제)/ }),
  ]) {
    const b = (await loc.boundingBox())!;
    expect(b.x + b.width, `${loc} 오른쪽 끝이 패널 안`).toBeLessThanOrEqual(right);
  }
  const overflow = await dialog.evaluate((el) => el.scrollWidth - el.clientWidth);
  expect(overflow, "패널 가로 넘침 없음").toBeLessThanOrEqual(0);
}

test("긴 소스 제목 35개에서도 다이얼로그 내용이 패널 안에 있다", async ({ page }) => {
  await openStudioDialog(page);
  await expectInsideDialog(page);
});

test("모바일 폭(375px)에서도 다이얼로그 내용이 패널 안에 있다", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 });
  await openStudioDialog(page);
  await expectInsideDialog(page);
  const box = (await page.getByRole("dialog").boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(375);
});

test("스튜디오 다이얼로그는 카드 아이콘·설명을 쓰고 입력 정책 배지를 보이지 않는다", async ({ page }) => {
  const dialog = await openStudioDialog(page);
  await expect(dialog.getByRole("heading")).toContainText("🧠");
  await expect(dialog.getByRole("heading")).not.toContainText("🧩");
  await expect(dialog.getByText("소스의 주제를 가지로 펼쳐 봅니다")).toBeVisible();
  await expect(dialog.getByTestId("input-policy-badges")).toHaveCount(0);
});
