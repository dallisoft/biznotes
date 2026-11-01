import { expect, test } from "@playwright/test";
import { API_BASE_URL, gotoDashboard } from "./helpers";

// core-v0.11.0-upgrade AC-4.5 — **P4 전용(실 API, 코어 v0.11.0)**. P3(구 API)에서는 돌리지 않는다
// (source-metadata.spec.ts의 mock 케이스와 파일을 나눴다 — skip 경로 없음, PLAN §9 P3 작업 8).
//
// 필수 입력(없으면 실패 — 건너뛰지 않는다): P4 라이브 스모크가 보존한 S-3b 노트북·소스
//   E2E_SOURCE_META_NOTEBOOK_ID  — S-3·S-3b가 든 노트북
//   E2E_SOURCE_META_SOURCE_ID    — S-3b(사용자 제목으로 만든 https://example.com 웹 소스)
// S-3(제목 없이 만든 같은 URL — 자리표시자 제목이 문서 제목으로 바뀐다)은 같은 노트북에서
// `title == document_title`인 ready 웹 소스로 찾는다.

const NOTEBOOK_ID = process.env.E2E_SOURCE_META_NOTEBOOK_ID ?? "";
const S3B_SOURCE_ID = process.env.E2E_SOURCE_META_SOURCE_ID ?? "";

interface LiveSource {
  id: string;
  type: string;
  title: string;
  status: string;
  document_title?: string | null;
}

function requireInputs() {
  expect(NOTEBOOK_ID, "E2E_SOURCE_META_NOTEBOOK_ID(S-3b 노트북) 필수").toMatch(/^[0-9a-f-]{36}$/i);
  expect(S3B_SOURCE_ID, "E2E_SOURCE_META_SOURCE_ID(S-3b 소스) 필수").toMatch(/^[0-9a-f-]{36}$/i);
}

test("소스 상세 — S-3b 문서 제목 표시(실 API)", async ({ page }) => {
  requireInputs();
  await gotoDashboard(page);
  const res = await page.request.get(`${API_BASE_URL}/api/v1/sources/${S3B_SOURCE_ID}`);
  expect(res.status()).toBe(200);
  const s3b = (await res.json()) as LiveSource;
  expect(s3b.status, "S-3b ready").toBe("ready");
  expect(s3b.document_title, "S-3b 문서 제목").toBe("Example Domain");
  expect(s3b.title, "S-3b는 사용자 제목이 유지된다").not.toBe(s3b.document_title);

  await page.goto(`/notebooks/${NOTEBOOK_ID}`);
  // 소스 항목을 누르면 목록 대신 소스 패널 전체가 상세 화면이 된다
  await page.locator(`[data-source-id="${S3B_SOURCE_ID}"] button[data-source-opener]`).click();
  const docTitle = page.getByTestId("source-detail-view").getByTestId("source-document-title");
  await expect(docTitle).toBeVisible();
  await expect(docTitle).toContainText("Example Domain");
  await page.screenshot({ path: "test-results/p4-s3b-source-detail.png", fullPage: true });
});

test("소스 상세 — S-3 제목이 같으면 보조 줄 없음(실 API)", async ({ page }) => {
  requireInputs();
  await gotoDashboard(page);
  const res = await page.request.get(`${API_BASE_URL}/api/v1/notebooks/${NOTEBOOK_ID}/sources`);
  expect(res.status()).toBe(200);
  const sources = (await res.json()) as LiveSource[];
  const s3 = sources.find(
    (s) =>
      s.type === "web" &&
      s.status === "ready" &&
      !!s.document_title &&
      s.title === s.document_title &&
      s.id !== S3B_SOURCE_ID,
  );
  expect(s3, "S-3(title == document_title인 ready 웹 소스)이 노트북에 있어야 한다").toBeTruthy();
  const s3Id = (s3 as LiveSource).id;

  await page.goto(`/notebooks/${NOTEBOOK_ID}`);
  await page.locator(`[data-source-id="${s3Id}"] button[data-source-opener]`).click();
  // 상세가 실제로 열린 뒤에(본문 마크다운 영역까지) 「없음」을 판정한다
  const detail = page.getByTestId("source-detail-view");
  await expect(detail.getByRole("heading", { name: (s3 as LiveSource).title })).toBeVisible();
  await expect(detail.getByText("마크다운", { exact: true })).toBeVisible();
  await expect(detail.getByTestId("source-document-title")).toHaveCount(0);
});
