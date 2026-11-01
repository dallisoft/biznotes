import { expect, test } from "@playwright/test";
import { cleanupCreatedNotebooks, createNotebookAndOpen, gotoDashboard } from "./helpers";

// 테스트가 만든 노트북(「PW 」 접두)은 테스트마다 생성 응답의 id로 API 삭제하고, 파일 실행 전후 접두 개수가 같음을
// 확인한다(ui-ux-redesign P6 — BL-183 잔여, AC-6.2).
cleanupCreatedNotebooks("phase17-mobile");

// 모바일 뷰포트 스모크 — 375px에서 하단 탭바로 소스·채팅·에이전트·메모 전환
test.use({ viewport: { width: 375, height: 740 } });

test("모바일: 하단 탭바로 패널 전환", async ({ page }) => {
  await gotoDashboard(page);
  await createNotebookAndOpen(page, `PW 모바일 ${Date.now()}`);

  // 하단 탭바 노출 + 기본 채팅 화면
  const tabbar = page.getByTestId("mobile-tabbar");
  await expect(tabbar).toBeVisible();
  await expect(page.getByTestId("chat-input")).toBeVisible();

  // [ui-ux-redesign AC-3.4] 하단 탭은 「채팅·소스·메모·에이전트」 4개다.
  // 각 탭의 터치 영역은 44×44px 이상이다.
  const tabs = tabbar.getByRole("button");
  await expect(tabs).toHaveText(["채팅", "소스", "메모", "에이전트"]);
  await expect(page.getByTestId("mobile-tab-export")).toHaveCount(0);
  for (const view of ["chat", "files", "notes", "agents"]) {
    const box = await page.getByTestId(`mobile-tab-${view}`).boundingBox();
    expect(box, `mobile-tab-${view}`).not.toBeNull();
    expect(box!.height, `mobile-tab-${view} 높이`).toBeGreaterThanOrEqual(44);
    expect(box!.width, `mobile-tab-${view} 폭`).toBeGreaterThanOrEqual(44);
  }

  // 소스 탭 → 소스 추가 버튼 노출, 채팅 입력은 숨김
  await page.getByTestId("mobile-tab-files").click();
  await expect(page.getByTestId("add-source")).toBeVisible();
  await expect(page.getByTestId("chat-input")).toBeHidden();

  // 에이전트 탭 → 에이전트 그리드
  await page.getByTestId("mobile-tab-agents").click();
  await expect(page.getByTestId("agent-grid")).toBeVisible();

  // 메모 탭 → 메모 패널 (새 메모 버튼 등 노출), 그리드 숨김
  await page.getByTestId("mobile-tab-notes").click();
  await expect(page.getByTestId("agent-grid")).toBeHidden();

  // 다시 채팅으로 복귀
  await page.getByTestId("mobile-tab-chat").click();
  await expect(page.getByTestId("chat-input")).toBeVisible();
});
