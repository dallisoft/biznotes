import { expect, test } from "@playwright/test";
import { cleanupCreatedNotebooks, createNotebookAndOpen, gotoDashboard } from "./helpers";

// 테스트가 만든 노트북(「PW 」 접두)은 테스트마다 생성 응답의 id로 API 삭제하고, 파일 실행 전후 접두 개수가 같음을
// 확인한다(ui-ux-redesign P6 — BL-183 잔여, AC-6.2).
cleanupCreatedNotebooks("notes");

test("메모 작성 → 편집 → 소스 변환", async ({ page }) => {
  await gotoDashboard(page);
  const title = `PW 메모 ${Date.now()}`;
  await createNotebookAndOpen(page, title);

  // 메모 탭 → 새 메모
  await page.getByTestId("right-tab-notes").click();
  await page.getByTestId("new-note").click();
  await page.getByTestId("note-title-input").fill("회의 정리");
  await page.getByTestId("note-content-input").fill("결정사항: 다음 주까지 초안 작성.");
  await page.getByTestId("note-save").click();

  // 목록에 노출
  const noteList = page.getByTestId("note-list");
  await expect(noteList.getByText("회의 정리")).toBeVisible();

  // 편집 — 기존 메모는 뷰어로 먼저 열림 → "편집" 버튼으로 전환. 보기·편집 모두 모달이 아니라 우측 패널 안이다
  await noteList.getByText("회의 정리").click();
  await expect(page.getByTestId("note-detail-view").getByTestId("note-viewer")).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.getByTestId("note-edit").click();
  await page.getByTestId("note-title-input").fill("회의 정리 (수정)");
  await page.getByTestId("note-save").click();
  await expect(noteList.getByText("회의 정리 (수정)")).toBeVisible();

  // 소스로 변환 → 소스 탭의 소스 목록에 등장
  await noteList.getByText("회의 정리 (수정)").click();
  await page.getByRole("button", { name: "소스로 변환" }).click();
  await page.getByTestId("right-tab-files").click();
  await expect(
    page.getByTestId("source-list").getByText("회의 정리 (수정)"),
  ).toBeVisible({ timeout: 30_000 });
});

test("채팅 답변 → 메모에 저장 → 목록 확인", async ({ page }) => {
  await gotoDashboard(page);
  const title = `PW 챗메모 ${Date.now()}`;
  await createNotebookAndOpen(page, title);

  // 소스 등록 (질의 컨텍스트)
  await page.getByTestId("add-source").click();
  await page.getByRole("tab", { name: "텍스트" }).click();
  await page.getByPlaceholder("제목 (선택)").fill("녹차 자료");
  await page
    .getByPlaceholder("텍스트를 붙여넣으세요")
    .fill("녹차는 70~80도의 물에서 2분간 우린다.");
  await page.getByRole("button", { name: "등록" }).click();
  await expect(page.getByRole("checkbox", { name: /녹차 자료 선택/ })).toBeEnabled({
    timeout: 60_000,
  });

  // 질의 → 답변 대기
  await page.getByTestId("chat-input").fill("녹차 우리는 온도는?");
  await page.getByRole("button", { name: "전송" }).click();
  const saveButton = page.getByTestId("save-to-note").first();
  await expect(saveButton).toBeAttached({ timeout: 30_000 });

  // 메모에 저장
  await saveButton.click();

  // 메모 탭에서 확인 (origin 뱃지 포함)
  await page.getByTestId("right-tab-notes").click();
  const noteList = page.getByTestId("note-list");
  await expect(noteList.getByText("채팅에서 저장")).toBeVisible({ timeout: 10_000 });

  // [note-provenance Phase 3] 생성 근거 섹션 — 서버가 결합한 본문의 출처를 확인할 수 있다
  await noteList.getByText("채팅에서 저장").click();
  const provenance = page.getByTestId("note-provenance");
  await expect(provenance).toBeVisible();
  await expect(provenance).toContainText("자율 채팅");
});
