import { expect, test } from "@playwright/test";
import { cleanupCreatedNotebooks, createNotebookAndOpen, gotoDashboard } from "./helpers";

// ui-ux-improvements ⑥: 대화한 뒤 홈에 「최근 대화 이어가기」 카드가 생기고, 누르면 그 노트북의 그 대화로 돌아간다.
cleanupCreatedNotebooks("home-recent-chats");

test("대화 → 홈 「최근 대화 이어가기」 → 그 대화로 돌아가기", async ({ page }) => {
  await gotoDashboard(page);
  const title = `PW 이어가기 ${Date.now()}`;
  await createNotebookAndOpen(page, title);

  await page.getByTestId("add-source").click();
  await page.getByRole("tab", { name: "텍스트" }).click();
  await page.getByPlaceholder("제목 (선택)").fill("홍차 메모");
  await page.getByPlaceholder("텍스트를 붙여넣으세요").fill("홍차는 끓는 물에 3~5분 우린다.");
  await page.getByRole("button", { name: "등록" }).click();
  await expect(page.getByRole("checkbox", { name: /홍차 메모 선택/ })).toBeEnabled({ timeout: 60_000 });

  const question = `홍차 우리는 시간은? ${Date.now()}`;
  await page.getByTestId("chat-input").fill(question);
  await page.getByRole("button", { name: "전송" }).click();
  await expect(page.getByTestId("chat-messages").getByText(question)).toBeVisible({ timeout: 60_000 });
  // 답변이 저장된 뒤(「메모에 저장」은 저장된 답변에만 붙는다) 홈으로 간다 — 스트리밍 중에 떠나면
  // 대화가 저장되지 않아 「최근 대화」에 나오지 않는다
  await expect(page.getByTestId("chat-messages").getByTestId("save-to-note").first()).toBeAttached({
    timeout: 60_000,
  });
  const notebookUrl = new URL(page.url());

  await gotoDashboard(page);
  const region = page.getByRole("region", { name: "최근 대화 이어가기" });
  const card = region.getByRole("link").filter({ hasText: question.slice(0, 60) });
  await expect(card).toBeVisible();
  await expect(card).toContainText(title);

  await card.click();
  await expect(page).toHaveURL(new RegExp(`${notebookUrl.pathname}\\?s=`));
  await expect(page.getByTestId("chat-messages").getByText(question)).toBeVisible();
});
