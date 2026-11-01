import { expect, test, type Page } from "@playwright/test";
import { cleanupCreatedNotebooks, createNotebookAndOpen, fulfillJson, gotoDashboard } from "./helpers";

// #180 — 스튜디오 확장 1차: 띠에 7개 카드(마인드맵 + 결과물 6종)가 시드 시스템 스킬·에이전트에 묶여 켜지고,
// 카드마다 생성 다이얼로그(카드 아이콘·소스 선택기)를 연다. 묶인 에이전트는 스킬·워크플로우 탭 목록에 없다.
// slide-deck-studio — 8번째 카드 「슬라이드 자료」는 전용 맞춤설정 다이얼로그를 연다. 개요 워크플로우(숨김 카드)는
// 띠에 없고, 두 워크플로우 모두 탭 목록에 없다.
// 실행(생성)은 하지 않으므로 LLM이 필요 없다. 소스 목록은 응답을 가로채 준비된 소스 2개로 채운다.
cleanupCreatedNotebooks("studio-cards");

const CARDS = [
  { id: "mindmap", icon: "🧠", agent: "마인드맵" },
  { id: "report", icon: "📊", agent: "보고서 생성" },
  { id: "briefing", icon: "📋", agent: "브리핑 문서" },
  { id: "study-guide", icon: "📖", agent: "스터디 가이드" },
  { id: "quiz", icon: "❓", agent: "퀴즈 생성" },
  { id: "flashcards", icon: "🗂️", agent: "플래시카드 생성" },
];
const SLIDE_AGENTS = ["슬라이드 자료", "슬라이드 자료 개요"];

async function stubReadySources(page: Page, notebookId: string) {
  const now = new Date().toISOString();
  const sources = ["커피 교재", "차 교재"].map((title, i) => ({
    id: `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`,
    notebook_id: notebookId,
    type: "text",
    title,
    origin_url: null,
    mime_type: "text/plain",
    size_bytes: 100,
    status: "ready",
    error_message: null,
    token_count: 10,
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

async function openAgentPanel(page: Page) {
  await gotoDashboard(page);
  const notebookId = await createNotebookAndOpen(page, `PW 스튜디오 카드 ${Date.now()}`);
  await stubReadySources(page, notebookId);
  await page.reload();
  await page.getByTestId("right-tab-agents").click();
  await expect(page.getByTestId("studio-strip")).toBeVisible();
}

test("스튜디오 띠의 8개 카드가 켜지고, 카드마다 아이콘을 쓴 생성 다이얼로그를 연다", async ({ page }) => {
  await openAgentPanel(page);
  const strip = page.getByTestId("studio-strip");
  await expect(strip.locator('[data-testid^="studio-card-"]')).toHaveCount(CARDS.length + 1);
  const slideCard = strip.getByTestId("studio-card-slide-deck");
  await expect(slideCard, "슬라이드 자료 카드가 시드 워크플로우에 묶여 켜진다").toBeEnabled({ timeout: 15_000 });
  await slideCard.click();
  const slideDialog = page.getByTestId("slide-deck-dialog");
  await expect(slideDialog.getByRole("heading", { name: "슬라이드 자료 맞춤설정" })).toBeVisible();
  // 기본값 = 소스 패널 선택(준비된 소스 전부가 선택된 상태로 열린다)
  await expect(slideDialog.getByTestId("slide-deck-sources")).toHaveText(/^소스 2개/);
  await page.keyboard.press("Escape");
  await expect(slideDialog).toHaveCount(0);
  for (const c of CARDS) {
    const card = strip.getByTestId(`studio-card-${c.id}`);
    await expect(card, `${c.id} 카드가 시드에 묶여 켜진다`).toBeEnabled({ timeout: 15_000 });
    await card.click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByRole("heading")).toContainText(c.icon);
    await expect(dialog.getByRole("heading")).toContainText(c.agent);
    await expect(dialog.getByTestId("source-picker-list")).toBeVisible();
    await expect(dialog.getByTestId("input-policy-badges")).toHaveCount(0);
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
  }
});

test("스튜디오에 묶인 에이전트는 스킬·워크플로우 탭 목록에 없다", async ({ page }) => {
  await openAgentPanel(page);
  await expect(page.getByTestId("studio-card-quiz")).toBeEnabled({ timeout: 15_000 });
  for (const tab of ["스킬", "워크플로우"]) {
    await page.getByRole("tab", { name: tab }).click();
    const grid = page.getByTestId("agent-grid");
    // 탭에는 샘플·그 밖의 시스템 항목이 남는다 — 목록이 그려진 뒤에 부재를 확인한다
    await expect(grid.locator("li").first()).toBeVisible({ timeout: 15_000 });
    for (const name of [...CARDS.map((c) => c.agent), ...SLIDE_AGENTS]) {
      await expect(grid.getByRole("button", { name: `${name} 설정` })).toHaveCount(0);
    }
  }
});

test("1280x720에서 스튜디오 띠가 「만들기」 갤러리 높이를 빼앗지 않는다(회귀 — 카드 8개)", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await openAgentPanel(page);
  const strip = page.getByTestId("studio-strip");
  await expect(strip.getByTestId("studio-card-quiz")).toBeEnabled({ timeout: 15_000 });
  // 띠의 부모 = 갤러리 영역(띠 + 「만들기」 머리 + 카드 목록)
  const gallery = strip.locator("xpath=..");
  const stripBox = (await strip.boundingBox())!;
  const galleryBox = (await gallery.boundingBox())!;
  expect(stripBox.height, "띠 높이가 갤러리의 절반 이하").toBeLessThanOrEqual(galleryBox.height * 0.5);
  // 타일은 컴팩트(아이콘 + 이름) — 설명 문구가 타일 안에 보이지 않는다
  const tile = (await strip.getByTestId("studio-card-quiz").boundingBox())!;
  expect(tile.height).toBeLessThanOrEqual(40);
  // 「슬라이드 개요」는 스킬 패키지에서 모든 소스를 반영하는 시드 워크플로우로 옮겼다(#282) — 워크플로우 탭에 있다
  await page.getByRole("tab", { name: "워크플로우" }).click();
  await expect(page.getByTestId("agent-grid").locator("li").first()).toBeInViewport({ timeout: 15_000 });
  await expect(page.getByRole("button", { name: "슬라이드 개요 설정" })).toBeVisible();
});
