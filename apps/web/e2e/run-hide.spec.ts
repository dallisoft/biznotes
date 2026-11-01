import { expect, test } from "@playwright/test";
import { API_BASE_URL, cleanupCreatedNotebooks, createNotebookAndOpen, gotoDashboard } from "./helpers";

// #220 AC-7 — 「최근 결과」의 실패 항목 1건을 케밥 「숨기기」로 숨기면 목록에서 사라지고(확인 대화상자 없음),
// 「되돌리기」 토스트를 누르면 다시 보인다. #221 — 숨긴 항목은 「숨긴 결과 N개 보기」에서 「다시 보이기」로도 되돌린다. 숨긴 동안에도 단건 조회는 그 run을 돌려준다(집계·로그 유지 — 코어 #371).
// 실패 run은 실스택에서 실제로 만든다. LLM·외부 요청이 필요 없도록 비-JSON 리터럴을 검증하는 `json_validate` 노드
// 하나짜리 draft 워크플로우를 API로 만들어 소유자 테스트 실행(`can_run_owner_draft`)한다 — 재시도 프롬프트가 없어
// 결정적으로 `failed`가 된다. mock은 쓰지 않는다. draft라 에이전트 탭 목록에는 보이지 않는다.
// 노트북은 UI로 만들고(「PW 」 접두) 테스트가 끝나면 생성 응답의 id로 지운다(cleanupCreatedNotebooks).
// 워크플로우는 finally에서 지운다(run은 FK CASCADE로 함께 지워진다).
cleanupCreatedNotebooks("run-hide");

test("실패 항목 숨기기 — 케밥 「숨기기」로 목록에서 빠지고 「되돌리기」·「숨긴 결과」의 「다시 보이기」로 돌아온다", async ({ page }) => {
  test.setTimeout(120_000);
  await gotoDashboard(page);
  const notebookId = await createNotebookAndOpen(page, `PW 실행 숨기기 ${Date.now()}`);

  const created = await page.request.post(`${API_BASE_URL}/api/v1/workflows`, {
    data: {
      name: `e2e-run-hide-${Date.now().toString(36)}`,
      definition: {
        nodes: [{ id: "check", type: "json_validate", source: "not json", schema: "mindmap" }],
        edges: [],
      },
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  const workflowId = ((await created.json()) as { id: string }).id;

  try {
    const started = await page.request.post(`${API_BASE_URL}/api/v1/notebooks/${notebookId}/agent-runs`, {
      data: { agent_id: workflowId, params: {} },
    });
    expect(started.status(), await started.text()).toBe(202);
    const runId = ((await started.json()) as { id: string }).id;

    // 에이전트 패널은 노트북을 열 때 이미 마운트돼 빈 실행 목록을 캐시했다. 탭 전환은 CSS 표시만 바꿔 다시 읽지 않고,
    // 끝난 run만 있는 목록은 폴링하지 않는다 — API로 만든 run이 보이도록 새로 고친다(studio-cards와 같은 처리)
    await page.reload();
    // Base UI 메뉴의 열림·닫힘 트랜지션이 항목을 「not stable」로 만들지 않게 끈다(shell-smoke와 같은 처리)
    await page.addStyleTag({
      content: "*,*::before,*::after{transition:none!important;animation:none!important}",
    });
    await page.getByTestId("right-tab-agents").click();
    const runItem = page.getByTestId(`run-item-${runId}`);
    await expect(
      runItem.locator('[data-slot="status-badge"]').filter({ hasText: /^실패$/ }),
    ).toBeVisible({ timeout: 60_000 });

    // 대기는 클릭 전에 건다 — 숨김 요청은 확인 대화상자 없이 바로 나간다
    const hidden = page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname === `/api/v1/agent-runs/${runId}/hide` &&
        r.request().method() === "POST",
    );
    await runItem.getByTestId("run-item-menu").click();
    await page.getByRole("menuitem", { name: "숨기기" }).click();
    expect((await hidden).status()).toBe(200);
    await expect(page.getByRole("alertdialog")).toHaveCount(0);

    await expect(runItem).toHaveCount(0);
    await expect(page.getByText("결과를 숨겼습니다")).toBeVisible();
    // 목록에서만 빠진다 — 단건 조회는 그대로 돌려준다
    const list = await page.request.get(`${API_BASE_URL}/api/v1/notebooks/${notebookId}/agent-runs`);
    expect(((await list.json()) as { id: string }[]).map((r) => r.id)).not.toContain(runId);
    const single = await page.request.get(`${API_BASE_URL}/api/v1/agent-runs/${runId}`);
    expect(single.status()).toBe(200);
    expect(((await single.json()) as { status: string }).status).toBe("failed");

    const unhidden = page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname === `/api/v1/agent-runs/${runId}/unhide` &&
        r.request().method() === "POST",
    );
    await page.getByRole("button", { name: "되돌리기" }).click();
    expect((await unhidden).status()).toBe(200);
    await expect(runItem).toBeVisible({ timeout: 15_000 });
    await expect(runItem.getByTestId("run-item-menu")).toBeVisible();
    await page.screenshot({ path: "test-results/run-hide.png", fullPage: true });

    // #221 — 다시 숨긴 뒤 「숨긴 결과 1개 보기」를 펼쳐 「다시 보이기」로 되돌린다(토스트를 쓰지 않는 경로)
    const hiddenAgain = page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname === `/api/v1/agent-runs/${runId}/hide` &&
        r.request().method() === "POST",
    );
    await runItem.getByTestId("run-item-menu").click();
    await page.getByRole("menuitem", { name: "숨기기" }).click();
    expect((await hiddenAgain).status()).toBe(200);
    await expect(runItem).toHaveCount(0);
    await page.getByRole("button", { name: "숨긴 결과 1개 보기" }).click();
    const hiddenItem = page.getByTestId(`hidden-run-item-${runId}`);
    await expect(hiddenItem.getByText("실패", { exact: true })).toBeVisible();
    await page.screenshot({ path: "test-results/run-hide-hidden-list.png", fullPage: true });

    const shownAgain = page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname === `/api/v1/agent-runs/${runId}/unhide` &&
        r.request().method() === "POST",
    );
    await hiddenItem.getByTestId("hidden-run-unhide").click();
    expect((await shownAgain).status()).toBe(200);
    await expect(runItem).toBeVisible({ timeout: 15_000 });
    await expect(hiddenItem).toHaveCount(0);
    await expect(page.getByTestId("hidden-run-toggle")).toHaveCount(0);
  } finally {
    const status = await page.request
      .delete(`${API_BASE_URL}/api/v1/workflows/${workflowId}`)
      .then((res) => res.status(), (err: unknown) => `요청 실패: ${String(err)}`);
    console.log(`run-hide: workflow ${workflowId} 정리 → ${status}`);
    expect.soft(status, `워크플로우 ${workflowId} 정리`).toBe(204);
  }
});
