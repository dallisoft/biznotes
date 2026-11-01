import { expect, test } from "@playwright/test";
import { cleanupCreatedNotebooks, createNotebookAndOpen, gotoDashboard } from "./helpers";

// ui-ux-improvements ③: 마지막 답변을 다시 생성하면 질문은 한 번만 남고 답변 한 개가 새로 저장된다.
cleanupCreatedNotebooks("chat-regenerate");

test("답변 → 「다시 생성」 → 질문 1개·답변 1개가 남는다(새로고침 뒤에도)", async ({ page }) => {
  await gotoDashboard(page);
  await createNotebookAndOpen(page, `PW 다시 생성 ${Date.now()}`);

  await page.getByTestId("add-source").click();
  await page.getByRole("tab", { name: "텍스트" }).click();
  await page.getByPlaceholder("제목 (선택)").fill("보리차 메모");
  await page.getByPlaceholder("텍스트를 붙여넣으세요").fill("보리차는 물 1리터에 볶은 보리 20g을 넣고 10분 끓인다.");
  await page.getByRole("button", { name: "등록" }).click();
  await expect(page.getByRole("checkbox", { name: /보리차 메모 선택/ })).toBeEnabled({ timeout: 60_000 });

  const question = "보리차는 몇 분 끓이나요?";
  const messages = page.getByTestId("chat-messages");
  await page.getByTestId("chat-input").fill(question);
  await page.getByRole("button", { name: "전송" }).click();
  const regenerate = messages.getByRole("button", { name: "다시 생성" });
  await expect(regenerate).toBeVisible({ timeout: 60_000 });

  const posted = page.waitForRequest(
    (r) => r.method() === "POST" && r.url().includes("/messages") && (r.postData() ?? "").includes('"regenerate":true'),
  );
  await regenerate.click();
  await posted;
  await expect(messages.getByRole("button", { name: "다시 생성" })).toBeVisible({ timeout: 60_000 });

  for (const scope of ["live", "reload"]) {
    if (scope === "reload") await page.reload();
    await expect(messages.getByText(question, { exact: true })).toHaveCount(1);
    await expect(messages.getByTestId("markdown-content")).toHaveCount(1);
  }
});
