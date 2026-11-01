import { expect, test } from "@playwright/test";
import { API_BASE_URL, installRagLoopbackGuard } from "./helpers";

// core-v0.15.0-upgrade AC-2.8 — 코어 v0.15.0 #209 리랭커 UI의 실스택 검증.
// ① admin/rag: 리랭킹을 켜면 드롭다운이 `GET /admin/rag/reranker`로 기본 로컬 리랭커를 보여 준다.
//    **저장하지 않고** 이탈한다 — 설정 행(`system_settings.rag`)을 만들지 않는다(기본값 보존 계약).
// ② admin/llm: 모델 등록 대화상자에 `reranker` 능력이 있고 단독으로만 선택된다(등록하지 않는다).

test.beforeEach(async ({ page, baseURL }) => {
  await installRagLoopbackGuard(page, baseURL);
});

test("admin/rag: 리랭킹을 켜면 드롭다운 기본값이 로컬 리랭커다(저장 없이 이탈)", async ({
  page,
}, testInfo) => {
  const before = await page.request.get(`${API_BASE_URL}/api/v1/admin/rag/settings`);
  expect(before.status()).toBe(200);
  const saved = (await before.json()) as {
    chunk_size: number;
    rerank_enabled: boolean;
    rerank_model: string;
    rerank_timeout_seconds: number;
  };
  // 기본 설정 스택에서만 의미가 있다 — 리랭킹이 이미 켜져 있으면 토글이 끄기가 된다
  expect(saved.rerank_enabled, "이 스펙은 리랭킹 꺼짐(기본값) 상태를 전제한다").toBe(false);

  const writes: string[] = [];
  page.on("request", (request) => {
    if (request.method() !== "GET" && request.url().includes("/api/v1/admin/rag/settings")) {
      writes.push(`${request.method()} ${request.url()}`);
    }
  });

  await page.goto("/admin/rag");
  await expect(page.getByRole("heading", { name: "RAG 관리" })).toBeVisible({ timeout: 15_000 });
  // 설정 응답 뒤에만 폼이 열린다(응답 전에는 로딩 상태) — 저장 막대는 바꾸기 전에는 없다(AC-5.5)
  await expect(page.getByLabel("청크 크기")).toHaveValue(String(saved.chunk_size));
  const saveBar = page.getByRole("region", { name: "저장되지 않은 변경 사항" });
  await expect(saveBar).toHaveCount(0);
  await expect(page.getByRole("combobox", { name: "리랭커" })).toHaveCount(0);

  const status = page.waitForResponse(
    (r) => r.url().endsWith("/api/v1/admin/rag/reranker") && r.request().method() === "GET",
  );
  await page.getByRole("switch", { name: /리랭킹/ }).click();
  expect((await status).status()).toBe(200);

  // 스위치를 켜면 폼이 바뀌어 저장 막대가 보인다 — 저장하지 않고 이탈한다
  await expect(saveBar).toBeVisible();
  const reranker = page.getByRole("combobox", { name: "리랭커" });
  await expect(reranker).toHaveText(/BAAI\/bge-reranker-base \(로컬\)/);
  await expect(page.getByText("사용 중: BAAI/bge-reranker-base")).toBeVisible();
  await expect(page.getByRole("alert").filter({ hasText: "리랭크할 수 없습니다" })).toHaveCount(0);
  await expect(page.getByLabel(/리랭크 타임아웃/)).toHaveValue(String(saved.rerank_timeout_seconds));
  // AC-2.7 ③ 증거용 화면(저장 전 상태) — test-results/ 아래에 남는다
  await page.screenshot({
    path: testInfo.outputPath("admin-rag-reranker-default.png"),
    fullPage: true,
  });

  await reranker.click();
  await expect(page.getByRole("option", { name: "자동(기본 리랭커, 없으면 로컬)" })).toBeVisible();
  await expect(page.getByRole("option", { name: "BAAI/bge-reranker-base (로컬)" })).toBeVisible();
  await page.keyboard.press("Escape");

  await page.goto("/admin/llm");
  await expect(page.getByRole("button", { name: "모델 추가" })).toBeVisible({ timeout: 15_000 });

  expect(writes).toEqual([]);
  const after = await page.request.get(`${API_BASE_URL}/api/v1/admin/rag/settings`);
  expect(await after.json()).toEqual(saved);
});

test("admin/llm: 모델 등록 대화상자의 reranker·image 능력은 단독으로만 선택된다", async ({ page }) => {
  await page.goto("/admin/llm");
  await page.getByRole("button", { name: "모델 추가" }).click({ timeout: 15_000 });
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();

  const box = (modality: string) =>
    dialog.getByRole("checkbox", { name: new RegExp(`^${modality}\\b`) });
  await expect(box("reranker")).toBeVisible();
  // 단독 능력: reranker(코어 v0.15.0)·image(코어 v0.23.0 #338)
  await expect(dialog.getByText("(단독)")).toHaveCount(2);
  await expect(box("llm")).toBeChecked(); // 등록 기본 능력
  await expect(box("reranker")).not.toBeChecked();

  await box("reranker").click();
  await expect(box("reranker")).toBeChecked();
  await expect(box("llm")).not.toBeChecked();

  await box("vlm").click();
  await expect(box("vlm")).toBeChecked();
  await expect(box("reranker")).not.toBeChecked();

  // image를 고르면 나머지가 해제되고 장당 가격 입력이 나타난다
  await expect(dialog.getByLabel(/장당 가격/)).toHaveCount(0);
  await box("image").click();
  await expect(box("image")).toBeChecked();
  await expect(box("vlm")).not.toBeChecked();
  await expect(dialog.getByLabel(/장당 가격/)).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
});
