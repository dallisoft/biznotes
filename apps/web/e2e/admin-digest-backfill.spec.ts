import { expect, test } from "@playwright/test";
import { API_BASE_URL, installRagLoopbackGuard } from "./helpers";

// core-v0.30.0-upgrade PR-2 AC-10 — 관리 › RAG 관리 「소스 요약」 섹션의 실스택 확인(읽기 전용).
// 백필을 **시작하지 않는다**(LLM 비용) — 화면이 API의 마지막 진행률을 그대로 보이고, 범위를 바꾸면 그
// 범위의 진행률을 읽는지만 본다.

test.beforeEach(async ({ page, baseURL }) => {
  await installRagLoopbackGuard(page, baseURL);
});

test("admin/rag 「소스 요약」: 모든 노트북의 마지막 진행률을 API와 같게 보이고 시작 요청을 보내지 않는다", async ({
  page,
}) => {
  const res = await page.request.get(`${API_BASE_URL}/api/v1/admin/source-digests/backfill`);
  expect(res.status()).toBe(200);
  const { progress } = (await res.json()) as { progress: { status: string; total: number } | null };

  const posts: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/source-digests/backfill")) {
      posts.push(request.url());
    }
  });

  await page.goto("/admin/rag");
  const section = page.getByTestId("digest-backfill");
  await expect(section.getByRole("heading", { name: "소스 요약" })).toBeVisible({ timeout: 15_000 });
  const status = section.getByTestId("digest-backfill-status");
  if (progress === null) {
    await expect(status).toContainText("요약을 만든 기록이 없습니다");
  } else {
    await expect(status).toContainText(`전체 ${progress.total.toLocaleString("ko-KR")}`);
  }
  await expect(section.getByRole("button", { name: "요약 만들기…" })).toBeVisible();
  expect(posts).toEqual([]);
});
