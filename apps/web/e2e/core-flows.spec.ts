import { expect, test } from "@playwright/test";
import {
  cleanupCreatedNotebooks,
  createNotebookAndOpen,
  gotoDashboard,
  gotoHomeViaBreadcrumb,
} from "./helpers";

// 테스트가 만든 노트북(「PW 」 접두)은 테스트마다 생성 응답의 id로 API 삭제하고, 파일 실행 전후 접두 개수가 같음을
// 확인한다(ui-ux-redesign P6 — BL-183 잔여, AC-6.2).
cleanupCreatedNotebooks("core-flows");

// 노트북을 만들면 바로 그 노트북 작업 화면으로 간다(ui-ux-redesign P3 — HCI-3). 작업 화면에는 제목 h1과
// 브레드크럼 「노트북」이 있다(BL-183의 heading 전제 해소).

test("1. 로그인 → 대시보드", async ({ page }) => {
  await gotoDashboard(page);
  await expect(page.getByRole("tab", { name: "전체" })).toBeVisible();
});

test("2. 노트북 생성 → 카드 노출", async ({ page }) => {
  await gotoDashboard(page);
  const title = `PW 노트북 ${Date.now()}`;
  await createNotebookAndOpen(page, title);
  // 브레드크럼으로 노트북 홈에 돌아가면 새 카드가 있다
  await gotoHomeViaBreadcrumb(page);
  await expect(
    page.getByTestId("notebook-card").filter({ hasText: title }),
  ).toBeVisible();
});

test("3. 텍스트 소스 추가 → ready", async ({ page }) => {
  await gotoDashboard(page);
  const title = `PW 소스 ${Date.now()}`;
  await createNotebookAndOpen(page, title);

  await page.getByTestId("add-source").click();
  await page.getByRole("tab", { name: "텍스트" }).click();
  await page.getByPlaceholder("제목 (선택)").fill("녹차 메모");
  await page
    .getByPlaceholder("텍스트를 붙여넣으세요")
    .fill("녹차는 70~80도의 물에서 2분간 우린다. 홍차는 끓는 물에 3~5분이 적당하다.");
  await page.getByRole("button", { name: "등록" }).click();

  // 소스 목록에 나타나고 처리 완료(체크박스 활성)까지 대기
  const sourceItem = page.getByTestId("source-list").getByText("녹차 메모");
  await expect(sourceItem).toBeVisible();
  await expect(
    page.getByRole("checkbox", { name: /녹차 메모 선택/ }),
  ).toBeEnabled({ timeout: 60_000 });
});

test("4. 채팅 질의 → 답변 + 인용", async ({ page }) => {
  await gotoDashboard(page);
  const title = `PW 채팅 ${Date.now()}`;
  await createNotebookAndOpen(page, title);

  // 소스 등록
  await page.getByTestId("add-source").click();
  await page.getByRole("tab", { name: "텍스트" }).click();
  await page.getByPlaceholder("제목 (선택)").fill("커피 메모");
  await page
    .getByPlaceholder("텍스트를 붙여넣으세요")
    .fill("에스프레소는 9기압의 압력으로 25~30초간 추출한다.");
  await page.getByRole("button", { name: "등록" }).click();
  await expect(page.getByRole("checkbox", { name: /커피 메모 선택/ })).toBeEnabled({
    timeout: 60_000,
  });

  // 질의
  await page.getByTestId("chat-input").fill("에스프레소 추출 압력은?");
  await page.getByRole("button", { name: "전송" }).click();

  // 답변 말풍선 + 근거(인용) — 자율 모드가 실제로 그리는 근거를 단언한다(ui-ux-redesign P3 owner 판정, 2026-10-15).
  // 웹은 모든 질문을 자율 모드로 보내고(hooks/use-chat.ts `autonomous: true`), API 자율 스트림은
  // 「자율 모드는 도구로 컨텍스트를 발견하므로 사전 검색 인용은 비운다」(apps/api/app/api/v1/chat.py autonomous_stream)
  // — retrieval·done의 citations가 항상 빈 배열이라 인용 칩 [1]과 칩 클릭 → 소스 패널 전환(Phase 18c)은 이 경로에 없다.
  // 그 대신 답변 말풍선 아래 근거 배지(agent-evidence)에 이 노트북의 소스가 「소스 커피 메모」로 보인다.
  // 근거 배지는 클릭 동작이 없는 표시 요소라 소스 패널 전환 단계는 남기지 않는다.
  const messages = page.getByTestId("chat-messages");
  await expect(
    messages.getByTestId("markdown-content").filter({ hasText: /에스프레소/ }).first(),
  ).toBeVisible({ timeout: 30_000 });
  const evidence = messages.getByTestId("agent-evidence").last();
  await expect(
    evidence.getByTestId("evidence-badge-source").filter({ hasText: "커피 메모" }),
  ).toBeVisible({ timeout: 30_000 });
});

test("5. 에이전트 실행 → 결과 + 아티팩트", async ({ page }) => {
  await gotoDashboard(page);
  const title = `PW 에이전트 ${Date.now()}`;
  await createNotebookAndOpen(page, title);

  // 소스 등록 (RAG 컨텍스트용)
  await page.getByTestId("add-source").click();
  await page.getByRole("tab", { name: "텍스트" }).click();
  await page.getByPlaceholder("제목 (선택)").fill("자료");
  await page
    .getByPlaceholder("텍스트를 붙여넣으세요")
    .fill("콜드브루는 상온에서 12~24시간 우려낸다.");
  await page.getByRole("button", { name: "등록" }).click();
  await expect(page.getByRole("checkbox", { name: /자료 선택/ })).toBeEnabled({
    timeout: 60_000,
  });

  // 우측 패널을 에이전트 탭으로 전환
  await page.getByTestId("right-tab-agents").click();
  // 스킬 탭 → 슬라이드 개요 설정 다이얼로그 열어 커스텀 주제로 실행
  // (카드 본체 클릭은 기본 프롬프트로 즉시 실행되므로 설정 버튼 사용. 브리핑 문서는 #180부터 스튜디오 카드라 탭에 없다)
  await page.getByRole("tab", { name: "스킬" }).click();
  await page.getByRole("button", { name: "슬라이드 개요 설정" }).click();
  await page.locator("#field-topic").fill("콜드브루 만드는 법");
  await page.getByTestId("run-agent").click();

  // 실행 결과 succeeded + 아티팩트 칩
  const runList = page.getByTestId("run-list");
  await expect(runList.getByText("완료").first()).toBeVisible({ timeout: 90_000 });
  // 실행 결과를 열면 모달이 아니라 우측 패널 안 상세가 열리고, 「결과」 탭에 문서 본문이 먼저 보인다
  await runList.getByRole("button", { name: /실행 결과 보기/ }).first().click();
  const detail = page.getByTestId("run-detail-view");
  await expect(detail).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(detail.getByRole("tab", { name: "결과" })).toHaveAttribute("aria-selected", "true");
  await expect(detail.getByTestId("artifact-content").getByText(/콜드브루/).first()).toBeVisible({
    timeout: 15_000,
  });
  // 산출물 칩으로 열어도 같은 상세의 결과 탭으로 열린다
  await detail.getByTestId("run-detail-back").click();
  await runList.getByRole("button", { name: /slides\.md/ }).first().click();
  await expect(detail.getByTestId("artifact-content").getByText(/콜드브루/).first()).toBeVisible({
    timeout: 15_000,
  });
});
