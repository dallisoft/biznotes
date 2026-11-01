import { expect, test } from "@playwright/test";
import { API_BASE_URL, installRagLoopbackGuard } from "./helpers";

// core-v0.13.0-upgrade AC-11 — 프로바이더 임베딩 동시 요청 상한(코어 #196)을 어드민 화면에서
// 설정 → 배지·온디맨드 게이트 조회 → 해제한다. 실제 임베딩 프로바이더를 건드리지 않도록
// 스펙 전용 프로바이더를 API로 만들고 finally에서 지운다.

test.beforeEach(async ({ page, baseURL }) => {
  await installRagLoopbackGuard(page, baseURL);
});

test("admin/llm: 임베딩 동시 요청 상한 설정 → 게이트 상태 조회 → 해제", async ({ page }) => {
  const name = `e2e-gate-${Date.now().toString(36)}`;
  const created = await page.request.post(`${API_BASE_URL}/api/v1/admin/llm/providers`, {
    data: { name, type: "openai", api_key: "e2e-dummy" },
  });
  expect(created.status(), await created.text()).toBe(201);
  const providerId = ((await created.json()) as { id: string }).id;

  try {
    await page.goto("/admin/llm");
    const row = page.getByRole("row").filter({ hasText: name });
    await expect(row).toBeVisible({ timeout: 15_000 });
    await expect(row.getByText(/임베딩 상한/)).toHaveCount(0);

    await row.getByRole("button", { name: "수정" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel(/임베딩 동시 요청 상한/).fill("3");
    await dialog.getByRole("button", { name: "저장" }).click();
    await expect(dialog).toBeHidden();

    await expect(row.getByText("임베딩 상한 3")).toBeVisible();
    await row.getByRole("button", { name: "게이트 상태" }).click();
    const popover = page.getByTestId("embedding-gate-popover");
    await expect(popover.getByText("0/3")).toBeVisible();
    await page.keyboard.press("Escape");

    await row.getByRole("button", { name: "수정" }).click();
    await expect(dialog.getByLabel(/임베딩 동시 요청 상한/)).toHaveValue("3");
    await dialog.getByLabel(/임베딩 동시 요청 상한/).fill("");
    await dialog.getByRole("button", { name: "저장" }).click();
    await expect(dialog).toBeHidden();

    await expect(row.getByText(/임베딩 상한/)).toHaveCount(0);
    await expect(row.getByRole("button", { name: "게이트 상태" })).toHaveCount(0);
  } finally {
    const status = await page.request
      .delete(`${API_BASE_URL}/api/v1/admin/llm/providers/${providerId}`)
      .then((res) => res.status(), (err: unknown) => `요청 실패: ${String(err)}`);
    console.log(`admin-llm-provider-concurrency: provider ${providerId} 정리 → ${status}`);
    expect.soft(status).toBe(204);
  }
});
