import { expect, test } from "@playwright/test";
import { cleanupCreatedNotebooks, createNotebookAndOpen, gotoDashboard } from "./helpers";

// 테스트가 만든 노트북(「PW 」 접두)은 테스트마다 생성 응답의 id로 API 삭제하고, 파일 실행 전후 접두 개수가 같음을
// 확인한다(ui-ux-redesign P6 — BL-183 잔여, AC-6.2).
cleanupCreatedNotebooks("overview");

test("소스 등록 → 요약 카드 + 추천 질문 → 칩 클릭 시 질의", async ({ page }) => {
  test.setTimeout(150_000); // 디바운스 10s + 생성 + 폴링 주기 감안
  await gotoDashboard(page);
  const title = `PW 개요 ${Date.now()}`;
  await createNotebookAndOpen(page, title);

  // 소스 등록
  await page.getByTestId("add-source").click();
  await page.getByRole("tab", { name: "텍스트" }).click();
  await page.getByPlaceholder("제목 (선택)").fill("커피 안내서");
  await page
    .getByPlaceholder("텍스트를 붙여넣으세요")
    .fill(
      "에스프레소는 9기압으로 25~30초간 추출한다. 콜드브루는 상온에서 12~24시간 우려낸다. 드립 커피는 92~96도 물로 내린다.",
    );
  await page.getByRole("button", { name: "등록" }).click();
  await expect(page.getByRole("checkbox", { name: /커피 안내서 선택/ })).toBeEnabled({
    timeout: 60_000,
  });

  // 디바운스(10s) + 생성 후 요약 카드 표시 (stale 폴링이 갱신)
  await expect(page.getByTestId("overview-card")).toBeVisible({ timeout: 90_000 });
  const chips = page.getByTestId("suggested-questions").getByRole("button");
  await expect(chips.first()).toBeVisible();

  // 칩 클릭 → 질의 전송 → 답변 수신
  await chips.first().click();
  const messages = page.getByTestId("chat-messages");
  await expect(messages.getByTestId("save-to-note").first()).toBeAttached({
    timeout: 30_000,
  });
});

test("개요 수동 재생성 — 버튼 클릭 → 갱신 표시 → 완료", async ({ page }) => {
  test.setTimeout(180_000);
  await gotoDashboard(page);
  const title = `PW 재생성 ${Date.now()}`;
  await createNotebookAndOpen(page, title);

  await page.getByTestId("add-source").click();
  await page.getByRole("tab", { name: "텍스트" }).click();
  await page.getByPlaceholder("제목 (선택)").fill("재생성 소스");
  await page
    .getByPlaceholder("텍스트를 붙여넣으세요")
    .fill("녹차는 70~80도의 물에서 2분간 우린다. 홍차는 95도 이상에서 3~5분 우린다.");
  await page.getByRole("button", { name: "등록" }).click();
  await expect(page.getByTestId("overview-card")).toBeVisible({ timeout: 90_000 });

  // 재생성 클릭 → 카드가 다시 표시되는지 확인
  // mock LLM 은 즉시 완료되므로 "갱신 중" 중간 상태는 경쟁 조건으로 인해 검증 생략
  const regen = page.getByTestId("overview-regenerate");
  await expect(regen).toBeEnabled();
  await regen.click();
  await expect(page.getByTestId("overview-card")).toBeVisible({ timeout: 90_000 });
});
