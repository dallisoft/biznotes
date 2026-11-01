import { expect, type Locator, type Page, test } from "@playwright/test";
import { fetchReindexRunCounts } from "./db";
import { assertLoopbackUrl, installRagLoopbackGuard } from "./helpers";

// 재인덱싱 실시간 진행률(reindex-progress PLAN D-15 · AC-17)의 실행 검증.
//
// 실행 전제:
//  - E-02는 **실제 전체 재인덱싱을 발행하는 쓰기 스펙**이다. `E2E_REINDEX_WRITE=1`을 준
//    로컬 dev 스택 검증 실행(scripts/smoke-web.sh)에서만 동작하고 기본은 skip이다.
//  - 같은 플래그를 쓰는 쓰기 스펙은 모두 `installRagLoopbackGuard`를 `await`로 설치한다 —
//    UI host(baseURL)·preflight API host·브라우저 실제 admin/rag 요청 URL이 loopback이 아니면
//    전송 전에 실패한다(우회 변수 없음). E-01은 이 가드를 실제 요청으로 매번 검증한다.
//  - run_id=null·이상치(집계 제외)·120초 초과는 green이 아니라 실패다(HCI-15).

const WRITE_ENABLED = process.env.E2E_REINDEX_WRITE === "1";
// 브라우저가 쓰는 공개 API 주소(compose NEXT_PUBLIC_API_BASE_URL 기본값과 같다, skills.spec.ts 선례)
const API_BASE_URL = process.env.E2E_API_BASE_URL ?? "http://localhost:28000";
const MISSING_RUN_ID = "00000000-0000-4000-8000-000000000000";
const DETAIL_PATH = /\/api\/v1\/admin\/rag\/reindex\/[0-9a-f-]{36}$/i;
const TERMINAL_SLA_MS = 120_000;

let blocked: string[] = [];

test.describe.configure({ retries: 0 });

test.beforeEach(async ({ page, baseURL }) => {
  ({ blocked } = await installRagLoopbackGuard(page, baseURL));
});

/** 진행 바 ARIA와 가시 텍스트를 한 번에 읽는다(렌더 사이에 걸치지 않도록 단일 evaluate). */
async function readProgress(panel: Locator) {
  return panel.evaluate((root) => {
    const bar = root.querySelector('[role="progressbar"]');
    return {
      now: bar?.getAttribute("aria-valuenow") ?? null,
      max: bar?.getAttribute("aria-valuemax") ?? null,
      valueText: bar?.getAttribute("aria-valuetext") ?? null,
      text: root.querySelector('[data-testid="reindex-progress-text"]')?.textContent ?? null,
    };
  });
}

async function expectProgressConsistent(panel: Locator) {
  const p = await readProgress(panel);
  expect(p.now, "aria-valuenow").not.toBeNull();
  expect(p.max, "aria-valuemax").not.toBeNull();
  expect(p.valueText?.startsWith(`완료 ${p.now} / 대상 ${p.max}`), JSON.stringify(p)).toBe(true);
  expect(p.text, JSON.stringify(p)).toMatch(new RegExp(`^${p.now} / ${p.max} \\(\\d+%\\)$`));
  return p;
}

/** 노트북별 인덱스 현황 표의 `재인덱싱 대기` 열 값(화면에 보이는 현재 페이지만 — 최대 10행). */
async function pendingColumn(page: Page): Promise<string[]> {
  const headers = (await page.getByRole("columnheader").allTextContents()).map((h) => h.trim());
  const index = headers.indexOf("재인덱싱 대기");
  if (index < 0) return ["<재인덱싱 대기 열 없음>"];
  return page
    .locator("tbody tr")
    .evaluateAll(
      (rows, i) => rows.map((row) => row.children[i]?.textContent?.trim() ?? ""),
      index,
    );
}

type IndexStatusRow = { notebook_id: string; title: string; reindex_pending: number };

/**
 * `재인덱싱 대기` 판정의 진실 공급원 — `GET /admin/rag/index-status` 응답 **전체**(모든 노트북).
 * 화면 표는 페이지네이션으로 한 페이지만 보이므로, 표만 보면 2페이지 이후의 대기를 놓친다(BL-150 P1-05).
 */
async function pendingAll(page: Page): Promise<IndexStatusRow[]> {
  const response = await page.request.get(`${API_BASE_URL}/api/v1/admin/rag/index-status`);
  expect(response.ok(), `GET /index-status ${response.status()}`).toBe(true);
  return (await response.json()) as IndexStatusRow[];
}

test.describe("loopback 가드", () => {
  // 앱 CSP(connect-src 'self' + API host)는 렌더러에서 비 loopback fetch를 먼저 막아 요청이
  // 네트워크 계층(page.route)까지 오지 않는다. E-01은 route 가드 자체를 검증하므로 이 묶음만
  // CSP를 우회한다(CSP는 별도의 방어층으로 그대로 유지된다).
  test.use({ bypassCSP: true });

  test("가드: 비 loopback admin/rag 요청(192.0.2.1, bare·/prefix 경로 2건)은 await 설치 완료 뒤 전송 전 abort되어 blocked에 순서대로 기록되고, loopback 요청은 통과하며, assertLoopbackUrl은 비 loopback 문자열에 throw한다", async ({
    page,
  }) => {
    // loopback 출처 페이지에서 요청을 시작한다(about:blank의 opaque 출처는 사설망 요청 정책에 걸릴 수 있다).
    await page.goto("/");
    expect(blocked).toEqual([]);

    const unsafe = [
      "http://192.0.2.1/api/v1/admin/rag/reindex?limit=1",
      "http://192.0.2.1/prefix/api/v1/admin/rag/reindex?limit=1",
    ];
    for (const url of unsafe) {
      // TEST-NET(RFC 5737)은 라우팅되지 않는다 — 가드가 없으면 reject가 아니라 timeout으로 실패한다.
      const outcome = await page.evaluate(async (requestUrl) => {
        try {
          await fetch(requestUrl, { mode: "no-cors", signal: AbortSignal.timeout(10_000) });
          return "sent";
        } catch (error) {
          return error instanceof DOMException && error.name === "TimeoutError" ? "timeout" : "rejected";
        }
      }, url);
      expect(outcome, url).toBe("rejected");
    }
    expect(blocked).toEqual(unsafe);

    // loopback 요청은 가드를 통과해 실제 응답을 받는다(인증·CORS 결과와 무관).
    const loopbackUrl = `${API_BASE_URL}/api/v1/admin/rag/reindex?limit=1`;
    const loopbackResponse = page.waitForResponse(
      (r) => r.url() === loopbackUrl || r.url() === new URL(loopbackUrl).toString(),
    );
    await page.evaluate(async (requestUrl) => {
      await fetch(requestUrl, { mode: "no-cors" }).catch(() => undefined);
    }, loopbackUrl);
    expect((await loopbackResponse).status()).toBeGreaterThan(0);
    expect(blocked).toEqual(unsafe);

    expect(() => assertLoopbackUrl(unsafe[0], "test")).toThrow(/loopback/);
    expect(() => assertLoopbackUrl(undefined, "test")).toThrow();
    expect(() => assertLoopbackUrl("http://127.0.0.1:23000", "test")).not.toThrow();
  });
});

test("전체 재인덱싱이 run을 만들고 패널이 120초 내 완료로 닫히며 DB에서 queued=0·done+failed=requested다", async ({
  page,
}, testInfo) => {
  test.setTimeout(180_000);
  test.skip(
    !WRITE_ENABLED,
    "쓰기 스펙(실제 재인덱싱 발행) — E2E_REINDEX_WRITE=1 로 명시 실행할 때만 동작한다",
  );

  // --- 읽기 전용 preflight(storageState 쿠키) — 어긋나면 환경 사유와 함께 실패 ---
  const runsResponse = await page.request.get(`${API_BASE_URL}/api/v1/admin/rag/reindex?limit=100`);
  expect(runsResponse.ok(), `preflight: GET /reindex ${runsResponse.status()}`).toBe(true);
  const runs = (await runsResponse.json()) as Array<{ id: string; queued: number }>;
  expect(
    runs.filter((run) => run.queued > 0).map((run) => run.id),
    "preflight: 이미 열린 재인덱싱 run이 있으면 이 run의 종결을 단독 검증할 수 없다",
  ).toEqual([]);
  const indexResponse = await page.request.get(`${API_BASE_URL}/api/v1/admin/rag/index-status`);
  expect(indexResponse.ok(), `preflight: GET /index-status ${indexResponse.status()}`).toBe(true);
  const rows = (await indexResponse.json()) as Array<{
    source_count: number;
    processing: number;
    reindex_pending: number;
  }>;
  const total = (key: "source_count" | "processing" | "reindex_pending") =>
    rows.reduce((sum, row) => sum + row[key], 0);
  expect(total("source_count"), "preflight: 재인덱싱할 소스가 없다").toBeGreaterThan(0);
  expect(total("processing"), "preflight: 인제스천 처리 중인 소스가 있다").toBe(0);
  expect(total("reindex_pending"), "preflight: 재인덱싱 대기 소스가 남아 있다").toBe(0);

  // --- 실제 발행 ---
  await page.goto("/admin/rag");
  await expect(page.getByRole("heading", { name: "RAG 관리" })).toBeVisible({ timeout: 15_000 });
  // 위험 구역의 「전체 재인덱싱…」 — 이름 부분 일치로 고른다(ui-ux-redesign AC-5.9)
  const fullButton = page.getByRole("button", { name: "전체 재인덱싱" });
  await expect(fullButton).toHaveCount(1);
  await expect(fullButton).toBeEnabled({ timeout: 15_000 }); // 부트스트랩 잠금 해제 = 활성 run 없음

  const postResponse = page.waitForResponse(
    (r) => r.url().includes("/api/v1/admin/rag/reindex") && r.request().method() === "POST",
  );
  const detailResponse = page.waitForResponse(
    (r) => r.request().method() === "GET" && DETAIL_PATH.test(new URL(r.url()).pathname),
  );
  await fullButton.click();
  await page.getByRole("alertdialog").getByRole("button", { name: "재인덱싱" }).click();
  const post = await postResponse;
  expect(post.ok(), `POST /reindex ${post.status()}`).toBe(true);
  const start = (await post.json()) as { run_id: string | null; enqueued: number; skipped: number };
  expect(blocked, "브라우저 admin/rag 요청 중 비 loopback 차단 0건").toEqual([]);
  expect(start.run_id, `전체 재인덱싱은 run_id를 생성해야 함: ${JSON.stringify(start)}`).not.toBeNull();
  const runId = start.run_id as string;
  await testInfo.attach("reindex-run-id", { body: runId, contentType: "text/plain" });
  console.log(`reindex run_id=${runId} enqueued=${start.enqueued} skipped=${start.skipped}`);

  // 폴링 GET /reindex/{run_id} 응답을 최소 1회 포착
  const detail = await detailResponse;
  expect(detail.url()).toContain(runId);
  expect(detail.ok(), `GET /reindex/${runId} ${detail.status()}`).toBe(true);
  const firstDetail = (await detail.json()) as { id: string; requested: number; queued: number };
  expect(firstDetail.id).toBe(runId);
  expect(firstDetail.requested).toBe(start.enqueued);

  const panel = page.getByTestId("reindex-progress");
  await expect(panel).toBeVisible();
  const progress = panel.getByRole("progressbar", { name: "재인덱싱 진행률" });
  await expect(progress).toBeVisible();
  await expect(panel.getByText(/run 상태 확인 중/)).toHaveCount(0);
  await expectProgressConsistent(panel);

  // --- 120초 내 종결(완료 / 완료 · 실패 있음), 이상치(집계 제외)는 실패 ---
  const status = panel.getByTestId("reindex-status");
  await expect(status).toHaveText(/^완료( · 실패 있음)?$/, { timeout: TERMINAL_SLA_MS });
  await expect(panel.getByText(/집계 제외/)).toHaveCount(0);
  const final = await expectProgressConsistent(panel);
  expect(final.now, "종결 run은 완료+실패 = 대상").toBe(final.max);
  expect(final.valueText).toBe(`완료 ${final.now} / 대상 ${final.max}`);

  // `재인덱싱 대기`가 전부 0 — 판정은 index-status **응답 전체**(모든 노트북·모든 페이지)로 한다(BL-150 P1-05).
  // 행이 1개 이상이고 모든 행이 0이 될 때까지 기다리며, 실패하면 남은 행(제목·대기 수)을 메시지에 남긴다.
  let all: IndexStatusRow[] = [];
  await expect
    .poll(
      async () => {
        all = await pendingAll(page);
        return all.length > 0 && all.every((row) => row.reindex_pending === 0);
      },
      { timeout: 30_000, message: "재인덱싱 대기가 전부 0이 아님(응답 전체)" },
    )
    .toBe(true);
  const remaining = all.filter((row) => row.reindex_pending !== 0);
  expect(all.length, "index-status 행 수").toBeGreaterThan(0);
  expect(remaining, JSON.stringify(remaining)).toEqual([]);
  // 화면 표(현재 페이지)도 응답과 같게 전부 0이어야 한다 — 표가 응답을 따라왔는지 대조
  let visible: string[] = [];
  await expect
    .poll(
      async () => {
        visible = await pendingColumn(page);
        return visible.length > 0 && visible.every((value) => value === "0");
      },
      { timeout: 30_000, message: "표의 재인덱싱 대기 열(현재 페이지)이 전부 0이 아님" },
    )
    .toBe(true);
  expect(visible.length, JSON.stringify(visible)).toBe(Math.min(all.length, 10));

  // 같은 테스트 안에서 run_id 바인딩 DB 조회(읽기 전용)
  const counts = await fetchReindexRunCounts(runId);
  console.log(`reindex run ${runId} DB counts=${JSON.stringify(counts)}`);
  expect(counts.queued).toBe(0);
  expect(counts.done + counts.failed + counts.removed).toBe(counts.requested);
  expect(blocked).toEqual([]);
});

test("fetchReindexRunCounts: 존재하지 않는 run_id는 0행으로 throw, UUID 형식 아니면 즉시 throw", async () => {
  await expect(fetchReindexRunCounts(MISSING_RUN_ID)).rejects.toThrow(/정확히 1행/);
  await expect(fetchReindexRunCounts("not-a-uuid")).rejects.toThrow(/UUID 형식/);
  await expect(fetchReindexRunCounts(`${MISSING_RUN_ID}\nSELECT 1`)).rejects.toThrow(/UUID 형식/);
});
