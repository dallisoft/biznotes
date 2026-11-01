import { expect, test } from "@playwright/test";
import { cleanupCreatedNotebooks, createNotebookAndOpen, gotoDashboard } from "./helpers";

// ui-ux-improvements ②·B5: 답변의 소스 근거를 누르면 우측 패널이 읽기 폭(45%)으로 넓어지며 원문이 열리고,
// 「목록」으로 돌아오면 원래 폭이다. 보낸 질문은 답변이 오기 전에도 목록에 보인다.
cleanupCreatedNotebooks("notebook-reading-mode");

test("질문 즉시 표시 → 소스 근거로 원문 열기(읽기 폭) → 목록으로 돌아오면 원래 폭", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await gotoDashboard(page);
  await createNotebookAndOpen(page, `PW 읽기 모드 ${Date.now()}`);

  await page.getByTestId("add-source").click();
  await page.getByRole("tab", { name: "텍스트" }).click();
  await page.getByPlaceholder("제목 (선택)").fill("커피 메모");
  await page
    .getByPlaceholder("텍스트를 붙여넣으세요")
    .fill("에스프레소는 9기압의 압력으로 25~30초간 추출한다. 드립 커피는 92도 물로 내린다.");
  await page.getByRole("button", { name: "등록" }).click();
  await expect(page.getByRole("checkbox", { name: /커피 메모 선택/ })).toBeEnabled({ timeout: 60_000 });

  const question = "에스프레소 추출 압력은?";
  await page.getByTestId("chat-input").fill(question);
  await page.getByRole("button", { name: "전송" }).click();
  const messages = page.getByTestId("chat-messages");
  // B5: 답변이 오기 전에도 질문 말풍선이 보인다
  await expect(messages.getByText(question)).toBeVisible({ timeout: 3_000 });

  const badge = messages.getByRole("button", { name: "소스 커피 메모 원문 보기" }).last();
  await expect(badge).toBeVisible({ timeout: 60_000 });

  const right = page.locator('[data-panel-id="right"]');
  const before = Number(await right.getAttribute("data-panel-size"));
  expect(before).toBeLessThan(45);

  await badge.click();
  const detail = page.getByTestId("source-detail-view");
  await expect(detail).toBeVisible();
  await expect.poll(async () => Number(await right.getAttribute("data-panel-size"))).toBe(45);

  await detail.getByTestId("source-detail-back").click();
  await expect.poll(async () => Number(await right.getAttribute("data-panel-size"))).toBe(before);
});
