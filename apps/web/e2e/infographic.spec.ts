import { expect, test } from "@playwright/test";
import { cleanupCreatedNotebooks, createNotebookAndOpen, gotoDashboard } from "./helpers";

// #181 — 시드 워크플로우 「인포그래픽 데이터」 실행 → 결과를 열면 전용 뷰어(원문 폴백 아님)가 보인다.
// 실 LLM이 필요하다(mindmap-studio와 같은 전제). 만든 노트북(「PW 」 접두)은 생성 응답 id로 지운다.
cleanupCreatedNotebooks("infographic");

const SOURCE_TEXT =
  "2025년 국내 커피 시장 규모는 8조 6천억 원으로 전년 대비 7% 성장했다. " +
  "아메리카노가 전체 판매의 42%, 라테가 27%, 콜드브루가 11%를 차지했다. " +
  "1인당 연간 커피 소비량은 405잔으로 세계 평균 152잔의 2.7배다. " +
  "카페 수는 10만 개를 넘었고, 그중 프랜차이즈 비중은 35%다.";

test("인포그래픽 데이터 실행 결과가 전용 뷰어로 보인다", async ({ page }) => {
  test.setTimeout(240_000);
  await gotoDashboard(page);
  await createNotebookAndOpen(page, "PW 인포그래픽");

  await page.getByTestId("add-source").click();
  await page.getByRole("tab", { name: "텍스트" }).click();
  await page.getByPlaceholder("제목 (선택)").fill("커피 시장");
  await page.getByPlaceholder("텍스트를 붙여넣으세요").fill(SOURCE_TEXT);
  await page.getByRole("button", { name: "등록" }).click();
  await expect(page.getByRole("checkbox", { name: /커피 시장 선택/ })).toBeEnabled({ timeout: 60_000 });

  await page.getByTestId("right-tab-agents").click();
  await page.getByRole("tab", { name: "워크플로우" }).click();
  await page.getByRole("button", { name: "인포그래픽 데이터 설정" }).click();
  await page.getByTestId("run-agent").click();

  const runList = page.getByTestId("run-list");
  await expect(runList.getByText("완료").first()).toBeVisible({ timeout: 180_000 });
  await runList.getByRole("button", { name: /infographic\.json/ }).first().click();
  const detail = page.getByTestId("run-detail-view");
  const viewer = detail.getByTestId("infographic-viewer");
  await expect(viewer).toBeVisible({ timeout: 15_000 });
  await expect(detail.getByTestId("infographic-fallback")).toHaveCount(0);
  await expect(viewer.getByRole("heading").first()).not.toHaveText("");
  expect(await viewer.getByTestId("infographic-stat").count()).toBeGreaterThan(0);
});
