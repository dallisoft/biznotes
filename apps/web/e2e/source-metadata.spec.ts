import { expect, test } from "@playwright/test";
import { createNotebookViaApi, deleteNotebookViaApi, fulfillJson, gotoDashboard } from "./helpers";

// core-v0.11.0-upgrade AC-3.6·AC-3.8(v7 AC-25 승계) — 소스 상세의 문서 제목 보조 줄과 인제스트
// 경고를 실브라우저로 본다. P3 실스택은 구 API(코어 v0.10.6 — 응답에 두 필드가 없다)라 소스 목록·
// 상세만 mock한다. 실 API 케이스(S-3b·S-3)는 P4 전용 파일 source-metadata-live.spec.ts로
// 분리했다(skip 경로를 만들지 않는다 — PLAN §9 P3 작업 8). 노트북은 이 스펙이 만들고 지운다.

// core-v0.16.0-upgrade AC-1.4 ⑤: 재인덱싱 안내 2종(코어 #230 — reindex_kept_previous·reindex_deferred)을 더해
// 실브라우저 문구·muted 스타일을 본다. 재인덱싱 경고 화면은 라이브 L-8 스크린샷으로도 쓴다.
test("소스 상세 — 경고 5종과 문서 제목 렌더(mock)", async ({ page }) => {
  await gotoDashboard(page);
  const notebookId = await createNotebookViaApi(page, `P3 소스 메타 mock ${Date.now()}`);
  try {
    const source = {
      id: "00000000-0000-4000-8000-00000000a501",
      notebook_id: notebookId,
      type: "file",
      title: "mock-report.pdf",
      origin_url: null,
      mime_type: "application/pdf",
      size_bytes: 4096,
      status: "ready",
      error_message: null,
      token_count: 120,
      chunk_count: 2,
      created_at: "2026-10-10T00:00:00Z",
      updated_at: "2026-10-10T00:00:00Z",
      document_title: "2026 업무 계획 <b>초안</b>",
      warnings: [
        { code: "text_truncated", params: { omitted: 3 } },
        { code: "hidden_text", params: { chars: 42, ratio: 0.05 } },
        { code: "future_code", params: {} },
        { code: "reindex_kept_previous", params: { reason: "parse" } },
        {
          code: "reindex_deferred",
          params: { reason: "embedding_gate", attempt: 2, max_attempts: 10, retry_in_seconds: 90 },
        },
      ],
    };
    await page.route(`**/api/v1/notebooks/${notebookId}/sources`, (route) =>
      fulfillJson(route, 200, [source]),
    );
    await page.route(`**/api/v1/sources/${source.id}`, (route) =>
      fulfillJson(route, 200, { ...source, parsed_preview: "# 본문", summary: null, keywords: null }),
    );
    await page.route(`**/api/v1/sources/${source.id}/guide`, (route) =>
      fulfillJson(route, 404, { detail: "없음" }),
    );

    await page.goto(`/notebooks/${notebookId}`);
    await page.getByRole("button", { name: "mock-report.pdf" }).click();
    // 목록 아래에 펼치지 않고 소스 패널 전체가 상세 화면이 된다
    await expect(page.getByTestId("source-detail-view")).toBeVisible();
    await expect(page.getByTestId("source-list")).toHaveCount(0);

    const docTitle = page.getByTestId("source-document-title");
    await expect(docTitle).toBeVisible();
    // 외부 문자열의 태그는 텍스트 그대로(T-6)
    await expect(docTitle).toContainText("2026 업무 계획 <b>초안</b>");
    await expect(docTitle.locator("b")).toHaveCount(0);

    const warnings = page.getByTestId("source-warning");
    await expect(warnings).toHaveCount(5);
    await expect(warnings.nth(0)).toHaveText("본문이 처리 상한에 닿아 일부가 잘렸습니다(생략 3개).");
    await expect(warnings.nth(0)).toHaveAttribute("data-security", "false");
    await expect(warnings.nth(1)).toHaveText("원본 문서에 숨은 글자가 있습니다(42자, 비율 5.0%).");
    await expect(warnings.nth(1)).toHaveAttribute("data-security", "true");
    await expect(warnings.nth(2)).toHaveText("알 수 없는 인제스트 경고(future_code)");
    await expect(warnings.nth(3)).toHaveText("재인덱싱에 실패해 이전 색인을 그대로 씁니다(사유: 원본 파싱 실패).");
    await expect(warnings.nth(3)).toHaveAttribute("data-security", "false");
    await expect(warnings.nth(4)).toHaveText(
      "재인덱싱이 미뤄졌습니다(임베딩 동시성 상한) — 약 90초 뒤 다시 시도합니다(2/10회). 그동안 이전 색인으로 검색됩니다.",
    );
    await expect(warnings.nth(4)).toHaveAttribute("data-security", "false");
    await page.screenshot({ path: "test-results/p3-source-metadata-mock.png", fullPage: true });
  } finally {
    await deleteNotebookViaApi(page, notebookId, "source-metadata");
  }
});
