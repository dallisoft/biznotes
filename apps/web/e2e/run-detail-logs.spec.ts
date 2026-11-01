import { expect, test } from "@playwright/test";
import { cleanupCreatedNotebooks, createNotebookAndOpen, fulfillJson, gotoDashboard } from "./helpers";

// core-v0.20.0-upgrade AC-2.11(계획 §5.5 — AC-2.3 화면): 실행 상세 「실행 로그」 탭의 노드 행이 실패 노드에만
// 코드별 공개 문구(`nodeErrorLabel(error_code)`)와 `error_message`를 보이고, 성공·실행 중 행에는 보이지 않는다.
// 실행은 실스택에서 실제로 만든다(core-flows 5와 같은 「슬라이드 개요」 설정 경로). 그 실행의 로그 GET
// (`/api/v1/agent-runs/{id}/logs`)만 `page.route`로 바꾼다(계획 §9 허용 대체 ⑥ — GET, 쓰기 없음). 실패 코드를
// 실제로 만드는 경로(http 노드 `127.0.0.1` → `ssrf_blocked`)는 라이브 L2-2가 맡는다.
// 노트북은 UI로 만들고(「PW 」 접두) 테스트가 끝나면 생성 응답의 id로 지운다(cleanupCreatedNotebooks).
cleanupCreatedNotebooks("run-detail-logs");

/** 실행 목록의 끝난 상태 라벨(lib/agent-types `RUN_STATUS_LABEL`) — 끝난 뒤에 노트북을 지운다. */
const TERMINAL_STATUS = /^(완료|실패|취소됨)$/;

const MESSAGE = {
  ssrf: "노드 [fetch] 실행 실패: 요청 대상 주소가 허용되지 않습니다",
  model: "노드 [draft] 실행 실패: 모델 응답을 받지 못했습니다",
  rerank: "노드 [rerank] 실행 실패: 리랭커 응답이 없습니다",
  unknown: "노드 [future] 실행 실패: 새 부류의 오류",
};

/** 로그 GET mock 본문 — 실패 행(알려진 코드 3개·모르는 코드·null)과 성공·실행 중 행. */
function mockLogs(runId: string) {
  const row = (
    id: string,
    node_id: string,
    node_type: string,
    status: string,
    error_code: string | null,
    error_message: string | null,
  ) => ({
    id,
    run_id: runId,
    node_id,
    node_type,
    status,
    error_code,
    error_message,
    token_usage: null,
    cost_usd: null,
    duration_ms: status === "running" ? null : 12,
    started_at: "2026-10-20T00:00:00Z",
    finished_at: status === "running" ? null : "2026-10-20T00:00:01Z",
  });
  return [
    row("log-ok", "search", "rag_search", "succeeded", null, null),
    row("log-running", "summary", "llm", "running", null, null),
    row("log-ssrf", "fetch", "http", "failed", "ssrf_blocked", MESSAGE.ssrf),
    row("log-model", "draft", "llm", "failed", "model_call_failed", MESSAGE.model),
    row("log-rerank", "rerank", "rag_search", "failed", "rerank_unavailable", MESSAGE.rerank),
    row("log-unknown", "future", "http", "failed", "future_node_error", MESSAGE.unknown),
    row("log-null", "legacy", "llm", "failed", null, null),
  ];
}

test("실행 상세 실행 로그 — 실패 행에만 코드별 문구와 오류 내용(로그 GET mock)", async ({ page }) => {
  // 실제 실행이 끝날 때까지 기다린다(core-flows 5와 같은 상한 + 여유)
  test.setTimeout(180_000);
  await gotoDashboard(page);
  await createNotebookAndOpen(page, `PW 실행 로그 ${Date.now()}`);

  await page.getByTestId("right-tab-agents").click();
  await page.getByRole("tab", { name: "스킬" }).click();
  // 브리핑 문서는 #180부터 스튜디오 카드(성공 시 스튜디오 항목)라, 일반 행으로 남는 탭의 스킬을 쓴다
  await page.getByRole("button", { name: "슬라이드 개요 설정" }).click();
  await page.locator("#field-topic").fill("실행 로그 문구 확인");
  // 대기는 클릭 전에 건다 — 실행 생성 응답(202)에서 run id를 얻는다
  const createdRun = page.waitForResponse(
    (r) =>
      /^\/api\/v1\/notebooks\/[^/]+\/agent-runs$/.test(new URL(r.url()).pathname) &&
      r.request().method() === "POST",
  );
  await page.getByTestId("run-agent").click();
  const created = await createdRun;
  expect(created.status(), await created.text()).toBe(202);
  const runId = ((await created.json()) as { id: string }).id;

  const runItem = page.getByTestId(`run-item-${runId}`);
  await expect(
    runItem.locator('[data-slot="status-badge"]').filter({ hasText: TERMINAL_STATUS }),
  ).toBeVisible({ timeout: 150_000 });

  // 이 실행의 로그 GET만 바꾼다 — 다른 경로·메서드는 실서버로 흘려보낸다
  const logsPath = `/api/v1/agent-runs/${runId}/logs`;
  let mockedLogGets = 0;
  await page.route("**/api/v1/agent-runs/*/logs", async (route) => {
    const request = route.request();
    if (request.method() === "GET" && new URL(request.url()).pathname === logsPath) {
      mockedLogGets += 1;
      await fulfillJson(route, 200, mockLogs(runId));
      return;
    }
    await route.fallback();
  });

  // 상세가 열리면 로그를 조회한다 — 응답 대기는 클릭 전에 건다
  const logsLoaded = page.waitForResponse(
    (r) => new URL(r.url()).pathname === logsPath && r.request().method() === "GET",
  );
  await runItem.getByRole("button", { name: /실행 결과 보기/ }).click();
  const detail = page.getByTestId("run-detail-view");
  await expect(detail).toBeVisible({ timeout: 15_000 });
  expect((await logsLoaded).status()).toBe(200);
  await detail.getByRole("tab", { name: "실행 로그" }).click();

  const row = (id: string) => detail.getByTestId(`execution-log-${id}`);
  const label = (id: string) => row(id).getByTestId("node-error-label");
  const message = (id: string) => row(id).getByTestId("node-error-message");

  // 알려진 코드 3개 — 계획 §5.5 문구 + 코어가 고정한 오류 내용
  await expect(label("log-ssrf")).toHaveText("내부 주소로의 요청이 차단되었습니다");
  await expect(message("log-ssrf")).toHaveText(MESSAGE.ssrf);
  await expect(label("log-model")).toHaveText("모델 호출에 실패했습니다");
  await expect(message("log-model")).toHaveText(MESSAGE.model);
  await expect(label("log-rerank")).toHaveText("리랭커를 사용할 수 없어 검색을 진행하지 못했습니다");
  await expect(message("log-rerank")).toHaveText(MESSAGE.rerank);

  // 모르는 코드·null → 일반 문구(코드 원문을 보이지 않는다), null 오류 내용은 줄 없음
  await expect(label("log-unknown")).toHaveText("노드 실행에 실패했습니다");
  await expect(message("log-unknown")).toHaveText(MESSAGE.unknown);
  await expect(row("log-unknown")).not.toContainText("future_node_error");
  await expect(label("log-null")).toHaveText("노드 실행에 실패했습니다");
  await expect(message("log-null")).toHaveCount(0);

  // 성공·실행 중 행 — 행이 렌더된 뒤에 문구 부재를 본다
  for (const id of ["log-ok", "log-running"]) {
    await expect(row(id)).toBeVisible();
    await expect(row(id).getByTestId("node-error-label")).toHaveCount(0);
    await expect(row(id).getByTestId("node-error-message")).toHaveCount(0);
  }
  await expect(row("log-ok")).toContainText("search");
  await expect(row("log-running")).toContainText("summary");

  expect(mockedLogGets, "이 실행의 로그 GET은 mock 응답으로만 받았다").toBeGreaterThanOrEqual(1);
  await page.screenshot({ path: "test-results/p2-run-detail-logs.png", fullPage: true });
});
