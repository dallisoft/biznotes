import { expect, test } from "@playwright/test";
import {
  cleanupCreatedNotebooks,
  createNotebookAndOpen,
  gotoDashboard,
  gotoHomeViaBreadcrumb,
} from "./helpers";

// 테스트가 만든 노트북(「PW 」 접두)은 테스트마다 생성 응답의 id로 API 삭제하고, 파일 실행 전후 접두 개수가 같음을
// 확인한다(ui-ux-redesign P6 — BL-183 잔여, AC-6.2).
cleanupCreatedNotebooks("phase13");

test("전체 검색 — 메모를 검색해 노트북으로 이동", async ({ page }) => {
  await gotoDashboard(page);
  const title = `PW 검색 ${Date.now()}`;
  const noteTitle = `회의메모-${Date.now()}`;

  // 노트북 + 메모 생성
  await createNotebookAndOpen(page, title);
  await page.getByTestId("right-tab-notes").click();
  await page.getByTestId("new-note").click();
  await page.getByTestId("note-title-input").fill(noteTitle);
  await page.getByTestId("note-content-input").fill("검색 테스트용 본문입니다.");
  await page.getByTestId("note-save").click();
  await expect(page.getByTestId("note-list").getByText(noteTitle)).toBeVisible();

  // 브레드크럼 「노트북」으로 노트북 홈에 → 커맨드 팔레트 열고 전체 검색(옛 「←」 버튼 대체 — BL-183)
  await gotoHomeViaBreadcrumb(page);
  await page.getByTestId("open-search").click();
  await page.getByTestId("global-search-input").fill(noteTitle);
  const results = page.getByTestId("global-search-results");
  const noteResult = results.getByRole("button", { name: new RegExp(`^${noteTitle}`) });
  await expect(noteResult).toBeVisible({ timeout: 10_000 });

  // 클릭 → 해당 노트북으로 이동
  await noteResult.click();
  await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
});

test("비주얼 워크플로우 빌더 — 노드 캔버스 렌더 + 노드 추가", async ({ page }) => {
  // 편집 다이얼로그는 뷰포트 높이로 제한되고 본문만 내부 스크롤한다(BL-125) — 정의가 길어도
  // 상단 탭 헤더가 화면 위로 잘리지 않으므로 일반 뷰포트에서 검증한다.
  await page.setViewportSize({ width: 1440, height: 1080 });
  await gotoDashboard(page);
  await page.goto("/admin/agents");

  // 표는 기본 10행 페이지네이션(kind·name 정렬)이라 시드·샘플 에이전트가 늘면 "마인드맵"이 1쪽
  // 밖으로 밀린다(2026-10-04 실측: 26번째 행). 데이터 개수에 의존하지 않도록 한 쪽에 모두 띄운다.
  await page.getByLabel("페이지당 개수").click();
  await page.getByRole("option", { name: "50" }).click();

  // workflow kind 에이전트(마인드맵) 편집
  const row = page.getByRole("row", { name: /마인드맵/ });
  await row.getByRole("button", { name: "편집" }).click();
  // 다이얼로그 열림 애니메이션이 끝나기를 기다린 후 탭 전환 (stable 대기 우회)
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.waitForTimeout(400);
  await page.getByTestId("tab-visual").click({ force: true });

  const builder = page.getByTestId("workflow-builder");
  await expect(builder).toBeVisible();
  // 기존 정의의 노드 3개(search/map/save) 렌더
  await expect(builder.getByText("rag_search · search")).toBeVisible();
  await expect(builder.getByText("llm · map")).toBeVisible();
  await expect(builder.getByText("save_artifact · save")).toBeVisible();

  // 팔레트에서 http 노드 추가 → 캔버스에 표시 + 고아 노드 경고
  await builder.getByRole("button", { name: "+ http" }).click();
  await expect(builder.getByText("http · http-1")).toBeVisible();
  await expect(page.getByTestId("dag-warning")).toBeVisible();
});

test("시맨틱 검색 — 의미 기반으로 소스 본문 도달", async ({ page }) => {
  test.setTimeout(120_000);
  await gotoDashboard(page);
  const title = `PW 시맨틱 ${Date.now()}`;
  const sourceTitle = `코끼리 노트 ${Date.now()}`;

  // 노트북 + 텍스트 소스 (의미 매칭용 고유 내용)
  await createNotebookAndOpen(page, title);
  await page.getByTestId("add-source").click();
  await page.getByRole("tab", { name: "텍스트" }).click();
  await page.getByPlaceholder("제목 (선택)").fill(sourceTitle);
  await page
    .getByPlaceholder("텍스트를 붙여넣으세요")
    .fill("보라색 코끼리는 바나나 우유를 가장 좋아한다. 매일 아침 두 잔씩 마신다.");
  await page.getByRole("button", { name: "등록" }).click();
  await expect(
    page.getByRole("checkbox", { name: new RegExp(`${sourceTitle} 선택`) }),
  ).toBeEnabled({ timeout: 60_000 });

  // 브레드크럼으로 노트북 홈 → 팔레트 → 키워드 미매치 질의 → 의미 검색 액션
  await gotoHomeViaBreadcrumb(page);
  await page.getByTestId("open-search").click();
  await page.getByTestId("global-search-input").fill("코끼리가 좋아하는 음료");
  // 300ms 디바운스 후 query state 가 업데이트되어야 semantic action 이 렌더됨
  await expect(page.getByTestId("semantic-search-action")).toBeVisible({ timeout: 5_000 });
  await page.getByTestId("semantic-search-action").click();

  // 본문 그룹에 해당 소스 매치 → 클릭 시 노트북 진입
  const results = page.getByTestId("global-search-results");
  await expect(results.getByText(sourceTitle).first()).toBeVisible({ timeout: 15_000 });
  await results.getByText(sourceTitle).first().click();
  await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
});
