import { readFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";
import { API_BASE_URL, cleanupCreatedNotebooks, createNotebookAndOpen, gotoDashboard } from "./helpers";

// mindmap-agent-skill §13 — S1(스튜디오 → 생성 → 결과 항목 → 뷰어 → JSON·PNG 다운로드)와
// S2(노드 클릭 → 펼침 + 채팅 질문 즉시 표시 → 근거 번호 답변 → 같은 세션 일반 질문 → 표식 확인 → 전체화면 중 클릭).
// 실 LLM이 필요하다. 만든 노트북(「PW 」 접두)은 생성 응답 id로 지운다.
cleanupCreatedNotebooks("mindmap-studio");

const SOURCE_TEXT =
  "에스프레소는 9기압의 압력으로 25~30초간 추출한다. 핸드 드립은 92~96℃ 물을 원두에 부어 내린다. " +
  "콜드브루는 찬물에 12~24시간 우려낸다. 원두는 아라비카·로부스타로 나뉘고, 로스팅은 라이트·미디엄·다크 단계가 있다. " +
  "특수문자 확인: <태그> & \"따옴표\" 'apos'.";

async function addTextSource(page: Page) {
  await page.getByTestId("add-source").click();
  await page.getByRole("tab", { name: "텍스트" }).click();
  await page.getByPlaceholder("제목 (선택)").fill("커피 & 추출 <노트>");
  await page.getByPlaceholder("텍스트를 붙여넣으세요").fill(SOURCE_TEXT);
  await page.getByRole("button", { name: "등록" }).click();
  await expect(page.getByRole("checkbox", { name: /커피 & 추출 <노트> 선택/ })).toBeEnabled({
    timeout: 60_000,
  });
}

async function createMindmap(page: Page) {
  await page.getByTestId("right-tab-agents").click();
  const card = page.getByTestId("studio-card-mindmap");
  await expect(card).toBeEnabled({ timeout: 15_000 });
  const runPosts: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && /\/agent-runs$/.test(new URL(r.url()).pathname)) runPosts.push(r.url());
  });
  await card.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  // 카드 클릭만으로는 실행하지 않는다(즉시 실행 0)
  expect(runPosts).toHaveLength(0);
  await expect(dialog.getByTestId("source-picker-list")).toBeVisible();
  await dialog.getByTestId("run-agent").click();
  await expect(dialog).toBeHidden();
  expect(runPosts).toHaveLength(1);
  const item = page.locator('[data-testid^="studio-run-item-"]').first();
  await expect(item).toBeVisible({ timeout: 180_000 });
  return item;
}

async function openViewer(page: Page) {
  const item = await createMindmap(page);
  await expect(item).toContainText("소스 1개");
  const title = item.getByTestId("studio-run-title");
  await expect(title).not.toHaveText("", { timeout: 15_000 });
  await title.click();
  const view = page.getByTestId("run-detail-view");
  await expect(view.getByTestId("run-detail-breadcrumb")).toHaveText("스튜디오 › 마인드맵");
  await expect(view.getByTestId("mindmap-canvas")).toBeVisible({ timeout: 15_000 });
  return view;
}

/** 다음 채팅 전송(POST SSE)의 본문이 끝날 때까지 기다리는 약속 — 클릭 전에 만든다. */
function nextChatStreamDone(page: Page): Promise<unknown> {
  return page
    .waitForResponse(
      (r) =>
        r.request().method() === "POST" && /\/chat\/sessions\/[^/]+\/messages$/.test(new URL(r.url()).pathname),
      { timeout: 120_000 },
    )
    .then((r) => r.finished());
}

function pngSize(buf: Buffer): { width: number; height: number } {
  return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
}

test.describe.configure({ retries: 0 });

test("S1 스튜디오 마인드맵 → 결과 항목 → 뷰어 → 접기·펼치기 → JSON·PNG 다운로드", async ({ page }) => {
  test.setTimeout(300_000);
  // 다운로드 뒤 object URL 해제를 센다
  await page.addInitScript(() => {
    const w = window as unknown as { __revoked: number };
    w.__revoked = 0;
    const orig = URL.revokeObjectURL.bind(URL);
    URL.revokeObjectURL = (u: string) => {
      w.__revoked += 1;
      orig(u);
    };
  });
  await gotoDashboard(page);
  await createNotebookAndOpen(page, `PW 마인드맵 ${Date.now()}`);
  await addTextSource(page);
  const view = await openViewer(page);

  const nodes = view.getByTestId("mindmap-node");
  const initial = await nodes.count();
  expect(initial).toBeGreaterThan(1); // 루트 + 1단계
  const toggles = view.getByTestId("mindmap-node-toggle");
  const collapsed = view.locator('[data-testid="mindmap-node-toggle"][aria-expanded="false"]');
  if ((await collapsed.count()) > 0) {
    await collapsed.first().click();
    await expect.poll(() => nodes.count()).toBeGreaterThan(initial);
  }
  await view.getByTestId("mindmap-collapse-all").click();
  await expect(nodes).toHaveCount(1);
  await view.getByTestId("mindmap-expand-all").click();
  await expect.poll(() => nodes.count()).toBeGreaterThanOrEqual(initial);
  expect(await toggles.count()).toBeGreaterThan(0);

  // JSON 다운로드 — 파일이 JSON으로 읽힌다
  const [jsonDl] = await Promise.all([
    page.waitForEvent("download"),
    view.getByTestId("mindmap-download-json").click(),
  ]);
  expect(jsonDl.suggestedFilename()).toMatch(/\.json$/);
  const jsonPath = await jsonDl.path();
  const parsed = JSON.parse(readFileSync(jsonPath, "utf8")) as { root: { id: string; label: string } };
  expect(parsed.root.label.length).toBeGreaterThan(0);

  // PNG 다운로드 — 시그니처·IHDR·크기, 다시 읽어 배경이 아닌 픽셀 비율 > 1%
  const [pngDl] = await Promise.all([
    page.waitForEvent("download"),
    view.getByTestId("mindmap-download-png").click(),
  ]);
  expect(pngDl.suggestedFilename()).toMatch(/\.png$/);
  const png = readFileSync(await pngDl.path());
  expect([...png.subarray(0, 8)]).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  const { width, height } = pngSize(png);
  expect(width).toBeGreaterThan(0);
  expect(height).toBeGreaterThan(0);
  expect(png.length).toBeGreaterThan(2048);
  const ratio = await page.evaluate(async (b64) => {
    const img = new Image();
    await new Promise<void>((resolve, reject) => {
      img.onload = () => resolve();
      img.onerror = () => reject(new Error("png load"));
      img.src = `data:image/png;base64,${b64}`;
    });
    const c = document.createElement("canvas");
    c.width = img.width;
    c.height = img.height;
    const ctx = c.getContext("2d") as CanvasRenderingContext2D;
    ctx.drawImage(img, 0, 0);
    const data = ctx.getImageData(0, 0, c.width, c.height).data;
    let ink = 0;
    for (let i = 0; i < data.length; i += 4) {
      if (data[i] < 245 || data[i + 1] < 245 || data[i + 2] < 245) ink += 1;
    }
    return ink / (data.length / 4);
  }, png.toString("base64"));
  console.log(`mindmap PNG ${width}x${height}, ${png.length} bytes, ink ratio ${(ratio * 100).toFixed(2)}%`);
  expect(ratio).toBeGreaterThan(0.01);
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { __revoked: number }).__revoked))
    .toBeGreaterThanOrEqual(1);

  // 「소스로 이동」(P4 AC-4.5) — 스킬 산출의 source_ref는 서버가 cite 번호로 채운 것이다(biznotes-core#303)
  const jumps = view.getByTestId("mindmap-node-source");
  await expect.poll(() => jumps.count()).toBeGreaterThan(0);
  console.log(`mindmap source jumps ${await jumps.count()} / nodes ${await nodes.count()}`);
  await jumps.first().click();
  const detail = page.getByTestId("source-detail-view");
  await expect(detail).toBeVisible({ timeout: 15_000 });
  await expect(detail).toContainText("커피 & 추출 <노트>");
});

test("S2 노드 클릭 → 펼침 + 질문 즉시 표시 → 근거 답변 → 일반 질문 → 표식 → 전체화면 중 클릭", async ({ page }) => {
  test.setTimeout(360_000);
  await gotoDashboard(page);
  await createNotebookAndOpen(page, `PW 마인드맵 채팅 ${Date.now()}`);
  await addTextSource(page);
  const view = await openViewer(page);
  const messages = page.getByTestId("chat-messages");

  // 1단계 노드(루트가 아닌 첫 노드) 라벨 클릭
  const level1 = view.locator('[data-testid="mindmap-node"][data-depth="1"]').first();
  const label = level1.locator("button[data-mindmap-node-id]");
  const labelText = (await label.textContent())?.trim() ?? "";
  const hadToggle = (await level1.getByTestId("mindmap-node-toggle").count()) > 0;
  const firstStream = nextChatStreamDone(page);
  await label.click();
  if (hadToggle) {
    await expect(level1.getByTestId("mindmap-node-toggle")).toHaveAttribute("aria-expanded", "true");
  }
  // 답변 완료 전에 질문이 사용자 메시지로 보인다
  const pending = page.getByTestId("pending-user-message").filter({ hasText: labelText.slice(0, 10) });
  await expect(pending).toBeVisible({ timeout: 10_000 });
  await expect(pending.getByTestId("mindmap-prompt-badge")).toBeVisible();

  // 답변 완료 — 인용 칩 ≥ 1 또는 근거 부족 배너
  await firstStream;
  await expect(page.getByTestId("pending-user-message")).toHaveCount(0, { timeout: 15_000 });
  const lastAnswer = messages.getByTestId("markdown-content").last();
  await expect(lastAnswer).toBeVisible({ timeout: 15_000 });
  const chips = await lastAnswer.getByRole("button", { name: /^\d+$/ }).count();
  const banner = await messages.getByTestId("insufficient-context-banner").count();
  console.log(`S2 인용 칩 ${chips}개, 근거 부족 배너 ${banner}개`);
  expect(chips + banner).toBeGreaterThan(0);

  // 같은 세션 일반 질문 1회
  await page.getByTestId("chat-input").fill("콜드브루는 얼마나 우려내나요?");
  const secondStream = nextChatStreamDone(page);
  await page.getByRole("button", { name: "전송" }).click();
  await expect(messages.getByText("콜드브루는 얼마나 우려내나요?")).toBeVisible();
  await secondStream;

  // API로 마인드맵 user 메시지의 표식 확인
  const sessionId = new URL(page.url()).searchParams.get("s");
  expect(sessionId).toBeTruthy();
  const res = await page.request.get(`${API_BASE_URL}/api/v1/chat/sessions/${sessionId}/messages`);
  expect(res.status()).toBe(200);
  const rows = (await res.json()) as { role: string; content: string; token_usage: Record<string, number> }[];
  const marked = rows.filter((r) => r.role === "user" && r.token_usage?.mindmap_prompt === 1);
  const plain = rows.filter((r) => r.role === "user" && r.token_usage?.mindmap_prompt !== 1);
  expect(marked).toHaveLength(1);
  expect(plain.map((r) => r.content)).toContain("콜드브루는 얼마나 우려내나요?");

  // 전체화면(미지원이면 오버레이) 중 노드 클릭 → 해제 → 채팅에 새 질문
  await view.getByTestId("mindmap-fullscreen").click();
  const viewer = page.getByTestId("mindmap-viewer");
  await expect(viewer).not.toHaveAttribute("data-fullscreen", "none");
  console.log(`S2 전체화면 방식: ${await viewer.getAttribute("data-fullscreen")}`);
  const before = await messages.getByTestId("mindmap-prompt-badge").count();
  const thirdStream = nextChatStreamDone(page);
  await viewer.locator('[data-testid="mindmap-node"][data-depth="0"] button[data-mindmap-node-id]').click();
  await expect(viewer).toHaveAttribute("data-fullscreen", "none");
  await expect.poll(() => messages.getByTestId("mindmap-prompt-badge").count(), { timeout: 10_000 }).toBe(before + 1);
  await thirdStream;
});
