import { expect, test, type Page } from "@playwright/test";
import { API_BASE_URL, cleanupCreatedNotebooks, createNotebookAndOpen, gotoDashboard } from "./helpers";

// 소스 상세는 목록 아래에 펼치지 않고 소스 패널 전체를 차지한다(메모 보기·에이전트 실행 상세와 같은 방식).
// 데스크톱은 우측 패널의 소스 탭, 모바일은 하단 탭 「소스」 화면에서 같게 동작하는지 실스택으로 본다.
// 테스트가 만든 노트북(「PW 」 접두)은 테스트마다 생성 응답의 id로 API 삭제한다(AC-6.2).
cleanupCreatedNotebooks("source-detail-panel");

const SOURCE_TITLE = "원두 로스팅 메모";
const SOURCE_TEXT = [
  "# 원두 로스팅 메모",
  "",
  "## 라이트 로스트",
  "산미와 꽃향이 살아 있다. 핸드드립에 어울리며 92~94도의 물을 쓴다.",
  "",
  "## 미디엄 로스트",
  "단맛과 균형이 좋다. 에스프레소와 드립 모두에 무난하다.",
  "",
  "## 다크 로스트",
  "쓴맛과 바디감이 강하다. 우유를 넣는 음료에 어울린다.",
  "",
  // 본문이 패널 높이를 넘어 세로 스크롤되는지 보려고 기록을 길게 둔다
  "## 로스팅 기록",
  ...Array.from({ length: 40 }, (_, i) => `- ${i + 1}회차: 1차 크랙 ${8 + (i % 4)}분, 배출 ${200 + i}도`),
  "",
  "마지막 기록 끝.",
].join("\n");

/** 상세 본문이 패널 높이 안에서 세로 스크롤되는지 — 끝까지 내리면 마지막 줄이 보인다. */
async function expectBodyScrollsWithinPanel(page: Page) {
  const body = page.getByTestId("source-detail-body");
  const overflow = await body.evaluate((el) => el.scrollHeight - el.clientHeight);
  expect(overflow, "본문이 패널보다 길어 스크롤 영역이 생긴다").toBeGreaterThan(0);
  await expect(page.getByTestId("source-detail-back")).toBeInViewport();
  await body.evaluate((el) => el.scrollTo(0, el.scrollHeight));
  await expect(body.getByText("마지막 기록 끝.")).toBeInViewport();
  // 본문만 스크롤되고 머리글(「목록」)은 그대로 보인다
  await expect(page.getByTestId("source-detail-back")).toBeInViewport();
  await body.evaluate((el) => el.scrollTo(0, 0));
}

/** 노트북을 만들고 API로 텍스트 소스를 넣은 뒤, 목록에서 그 소스가 ready(체크박스 활성)가 될 때까지 기다린다. */
async function openNotebookWithReadySource(page: Page, title: string) {
  await gotoDashboard(page);
  const notebookId = await createNotebookAndOpen(page, title);
  const res = await page.request.post(`${API_BASE_URL}/api/v1/notebooks/${notebookId}/sources`, {
    data: { type: "text", title: SOURCE_TITLE, text: SOURCE_TEXT },
  });
  expect(res.status(), await res.text()).toBeLessThan(300);
  // API로 넣은 소스는 열린 목록에 없으므로 다시 읽는다(처리 중이면 목록이 2초마다 폴링한다)
  await page.reload();
  return notebookId;
}

test.describe("데스크톱 1280", () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test("소스를 누르면 우측 패널 전체가 소스 상세가 되고 「목록」으로 돌아온다", async ({ page }) => {
    await openNotebookWithReadySource(page, `PW 소스상세 ${Date.now()}`);
    await expect(page.getByRole("checkbox", { name: `${SOURCE_TITLE} 선택` })).toBeEnabled({
      timeout: 60_000,
    });
    await expect(page.getByTestId("right-tab-files")).toHaveAttribute("aria-selected", "true");

    const opener = page.getByTestId("source-list").getByRole("button", { name: SOURCE_TITLE });
    await opener.click();

    const panel = page.locator("#nb-right-panel-files");
    const detail = panel.getByTestId("source-detail-view");
    await expect(detail).toBeVisible();
    await expect(page.getByTestId("source-list")).toHaveCount(0);
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(detail.getByRole("heading", { level: 2, name: SOURCE_TITLE })).toBeFocused();
    await expect(detail).toContainText("준비됨 · 청크");
    await expect(detail.getByText("마크다운", { exact: true })).toBeVisible();
    await expect(detail.getByText("산미와 꽃향이 살아 있다", { exact: false })).toBeVisible();
    // 채팅은 그대로 보이고, 상세는 우측 패널(소스 탭 패널)의 높이·폭 전체를 차지한다
    await expect(page.getByTestId("chat-input")).toBeVisible();
    const [panelBox, detailBox] = [await panel.boundingBox(), await detail.boundingBox()];
    expect(panelBox && detailBox, "패널·상세 영역").toBeTruthy();
    expect(Math.abs(detailBox!.y - panelBox!.y)).toBeLessThanOrEqual(1);
    expect(Math.abs(detailBox!.height - panelBox!.height)).toBeLessThanOrEqual(1);
    expect(Math.abs(detailBox!.width - panelBox!.width)).toBeLessThanOrEqual(2);
    expect(detailBox!.x).toBeGreaterThan(1280 / 2);
    await expectBodyScrollsWithinPanel(page);
    await page.screenshot({ path: "test-results/source-detail-desktop-1280.png" });

    await detail.getByTestId("source-detail-back").click();
    await expect(page.getByTestId("source-detail-view")).toHaveCount(0);
    await expect(opener).toBeFocused();
  });
});

test.describe("모바일 390", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("하단 탭 「소스」 화면 전체가 소스 상세가 되고 「목록」으로 돌아온다", async ({ page }) => {
    await openNotebookWithReadySource(page, `PW 소스상세 모바일 ${Date.now()}`);
    await page.getByTestId("mobile-tab-files").click();
    await expect(page.getByRole("checkbox", { name: `${SOURCE_TITLE} 선택` })).toBeEnabled({
      timeout: 60_000,
    });

    const opener = page.getByTestId("source-list").getByRole("button", { name: SOURCE_TITLE });
    await opener.click();

    const detail = page.getByTestId("source-detail-view");
    await expect(detail).toBeVisible();
    await expect(page.getByTestId("source-list")).toHaveCount(0);
    await expect(page.getByTestId("chat-input")).toBeHidden();
    await expect(page.getByTestId("mobile-tabbar")).toBeVisible();
    await expect(page.getByTestId("mobile-tab-files")).toHaveAttribute("aria-pressed", "true");
    await expect(detail.getByRole("heading", { level: 2, name: SOURCE_TITLE })).toBeFocused();
    await expect(detail.getByText("산미와 꽃향이 살아 있다", { exact: false })).toBeVisible();
    const detailBox = await detail.boundingBox();
    expect(detailBox, "상세 영역").toBeTruthy();
    // 화면 폭 전체(패널 테두리 2px 정도만 뺀다)
    expect(detailBox!.width).toBeGreaterThanOrEqual(390 - 4);
    await expectBodyScrollsWithinPanel(page);
    await page.screenshot({ path: "test-results/source-detail-mobile-390.png" });

    await detail.getByTestId("source-detail-back").click();
    await expect(page.getByTestId("source-detail-view")).toHaveCount(0);
    await expect(opener).toBeFocused();
  });
});
