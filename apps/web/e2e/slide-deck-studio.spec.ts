import { readFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import JSZip from "jszip";
import { API_BASE_URL, cleanupCreatedNotebooks, createNotebookAndOpen, gotoDashboard } from "./helpers";

// docs/plans/slide-deck-studio.md AC-14 — 스튜디오 「슬라이드 자료」를 실스택에서 끝까지 돈다:
// 맞춤설정(발표자 슬라이드·짧게·개요 검토) → 개요 run → 「검토」 → 의견 → 「계속」 → 덱 run → 덱 뷰어
// (확대 보기·발표 모드·PPTX 다운로드·PDF 인쇄). 실 LLM이 필요하다(infographic·mindmap-studio와 같은 전제).
// PPTX는 내려받은 파일을 JSZip으로 열어 슬라이드·노트 수를 센다(AC-11).
// PDF는 window.print를 대역해 인쇄용 덱만 남긴 뒤 page.pdf로 쪽 수·쪽 크기(13.333in × 7.5in = 960 × 540pt)를 잰다.
// 삽화(core-v0.23.0-upgrade AC-13)는 시드 mock 이미지 모델(`mock-image`, 결정적 PNG)로 덱 1회를 돌려 뷰어·인쇄·PPTX의
// 그림 수를 산출물 수와 대조한다.
// 방안 ④(#218): 개요는 고른 소스 전부의 요약을 읽고(검토 화면의 소스 요약), 덱은 장마다 그 장의 근거 소스로 검색해
// 쓴다(뷰어의 반영 요약·장마다 「근거」). 의견을 넣고 「계속」하면 덱 run이 개요를 고친 뒤 장을 쓴다.
cleanupCreatedNotebooks("slide-deck-studio");

const SOURCE_TEXT =
  "안드레이 카파시의 LLM 위키는 AI가 스스로 구축하고 유지보수하는 에이전틱 지식 베이스다. " +
  "사람은 지식을 수집하고 구조화하는 데는 능숙하지만 유지보수에는 실패한다 — 문서를 하나 더할 때마다 " +
  "요약·교차 참조·모순 확인 비용이 기하급수로 늘어난다. RAG는 매번 처음부터 정보를 조립하지만 LLM 위키는 " +
  "지식을 복리로 축적한다. 위키는 원본 소스, AI 관리 영역, 시스템 통제 규칙의 3계층으로 이뤄지고 " +
  "수집(Ingest)·질의(Query)·점검(Lint) 세 오퍼레이션이 플라이휠을 돌린다. 점검은 모순·고립 페이지·공백을 " +
  "스스로 진단한다. 한 사례는 2,188개 문서를 50달러로 구축했다.";

test("개요 검토를 거쳐 만든 슬라이드 자료를 보고, 발표하고, PPTX·PDF로 내보낸다", async ({ page }) => {
  test.setTimeout(420_000);
  await gotoDashboard(page);
  await createNotebookAndOpen(page, "PW 슬라이드 자료");

  await page.getByTestId("add-source").click();
  await page.getByRole("tab", { name: "텍스트" }).click();
  await page.getByPlaceholder("제목 (선택)").fill("LLM 위키");
  await page.getByPlaceholder("텍스트를 붙여넣으세요").fill(SOURCE_TEXT);
  await page.getByRole("button", { name: "등록" }).click();
  await expect(page.getByRole("checkbox", { name: /LLM 위키 선택/ })).toBeEnabled({ timeout: 60_000 });

  // 맞춤설정 — 실제 브라우저에서 라디오를 누른 뒤에도 설명 입력란에 타이핑된다(단위 테스트는 jsdom 우회)
  await page.getByTestId("right-tab-agents").click();
  const card = page.getByTestId("studio-card-slide-deck");
  await expect(card).toBeEnabled({ timeout: 15_000 });
  await card.click();
  const dialog = page.getByTestId("slide-deck-dialog");
  await dialog.getByRole("radio", { name: /발표자 슬라이드/ }).click();
  await dialog.getByRole("radio", { name: "짧게" }).click();
  await dialog.getByTestId("slide-deck-description").click();
  await page.keyboard.type("초보자를 위한 단계별 안내");
  await expect(dialog.getByTestId("slide-deck-description")).toHaveValue("초보자를 위한 단계별 안내");
  await dialog.getByTestId("slide-deck-sources").click();
  await expect(dialog.getByTestId("source-picker-list")).toBeVisible();
  await dialog.getByTestId("slide-deck-sources-done").click();
  await dialog.getByRole("switch", { name: "슬라이드 개요 검토" }).click();
  await dialog.getByTestId("slide-deck-generate").click();
  await expect(dialog).toHaveCount(0);

  // 개요 run → 「검토」
  const runList = page.getByTestId("run-list");
  const review = runList.getByTestId("studio-run-review");
  await expect(review).toBeVisible({ timeout: 180_000 });
  await review.click();
  const outline = page.getByTestId("slide-outline-review");
  await expect(outline).toBeVisible({ timeout: 15_000 });
  expect(await outline.getByRole("list", { name: "슬라이드 개요" }).getByRole("listitem").count()).toBeGreaterThanOrEqual(3);
  // 고른 소스 전부를 요약으로 읽었다(#218 — 누락 0)
  await expect(outline.getByTestId("slide-outline-sources")).toContainText("고른 소스 1개를 모두 요약으로 읽고");
  // 요약을 만든 모델(#239, 코어 v0.27.0) — 이 개요 run이 처음 만든다(인제스트 때 미리 만들었으면 캐시)
  await expect(outline.getByTestId("slide-outline-digest").locator("summary")).toHaveText(/^요약: .+ 1개(\(캐시\))?$/);
  await outline.getByTestId("slide-outline-feedback").fill("마지막 슬라이드에 실행 체크리스트를 넣어 줘.");
  await outline.getByTestId("slide-outline-continue").click();
  await expect(page.getByTestId("slide-outline-continued")).toBeVisible({ timeout: 15_000 });
  await page.getByTestId("run-detail-back").click();

  // 덱 run → 뷰어(최신 항목이 맨 위)
  await expect(runList.getByTestId("studio-run-review")).toHaveCount(0);
  const items = runList.locator('[data-testid^="studio-run-item-"]');
  await expect(items).toHaveCount(2, { timeout: 240_000 });
  await items.first().getByTestId("studio-run-title").click();
  const viewer = page.getByTestId("slide-deck-viewer");
  await expect(viewer).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("slide-deck-fallback")).toHaveCount(0);
  await expect(page.getByTestId("run-detail-breadcrumb")).toHaveText(/슬라이드 자료$/);
  const slides = viewer.getByRole("group", { name: /^슬라이드 \d+ \/ \d+/ });
  const total = await slides.count();
  expect(total).toBeGreaterThanOrEqual(3);
  await expect(viewer.getByText(`발표자 슬라이드 · 슬라이드 ${total}장`)).toBeVisible();
  // 반영 요약과 장마다 근거(#218) — 소스가 하나라 근거가 있는 장은 모두 그 소스를 가리킨다
  await expect(viewer.getByTestId("slide-coverage-summary")).toContainText("고른 소스 1개");
  // 「계속」으로 만든 덱 run은 개요 run이 만든 요약 캐시를 그대로 쓴다(#239)
  await expect(viewer.getByTestId("slide-coverage-digest").locator("summary")).toHaveText(/^요약: .+ 1개\(캐시\)$/);
  const evidence = viewer.getByTestId("slide-sources");
  expect(await evidence.count(), "장마다 근거 인용").toBeGreaterThanOrEqual(1);
  for (const line of await evidence.allTextContents()) expect(line).toBe("근거: LLM 위키");

  // 확대 보기 — 썸네일 레일과 키보드 넘김
  await viewer.getByRole("button", { name: "슬라이드 1 크게 보기" }).click();
  const expanded = page.getByTestId("slide-deck-expanded");
  await expect(expanded.getByText(`슬라이드 1 / ${total}`)).toBeVisible();
  await expect(expanded.getByRole("list", { name: "슬라이드 썸네일" }).getByRole("button")).toHaveCount(total);
  await page.keyboard.press("ArrowRight");
  await expect(expanded.getByText(`슬라이드 2 / ${total}`)).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(expanded).toHaveCount(0);

  // 발표 모드 — 전체 화면 무대, 키보드 넘김, N 노트, Esc 종료
  await viewer.getByRole("button", { name: "발표 시작" }).click();
  const stage = page.getByTestId("slide-presenter");
  await expect(stage).toBeVisible();
  await expect(stage.getByRole("group")).toHaveAccessibleName(new RegExp(`^슬라이드 1 / ${total}`));
  await page.keyboard.press("ArrowRight");
  await expect(stage.getByRole("group")).toHaveAccessibleName(new RegExp(`^슬라이드 2 / ${total}`));
  await page.keyboard.press("n");
  await expect(stage.getByTestId("slide-presenter-notes")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(stage).toHaveCount(0);

  // PPTX 다운로드 — 라이브러리를 이때 불러온다. 슬라이드 수가 같고 발표자 슬라이드라 노트가 있다
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    viewer.getByRole("button", { name: "PPTX 다운로드" }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/\.pptx$/);
  const zip = await JSZip.loadAsync(await readFile(await download.path()));
  const files = Object.keys(zip.files);
  expect(files.filter((n) => /^ppt\/slides\/slide\d+\.xml$/.test(n))).toHaveLength(total);
  expect(files.filter((n) => /^ppt\/notesSlides\/notesSlide\d+\.xml$/.test(n))).toHaveLength(total);
  // 노트 슬라이드의 첫 텍스트가 노트 본문이다(둘째는 쪽 번호) — 비어 있지 않은 장이 하나 이상
  const noteTexts = await Promise.all(
    Array.from({ length: total }, async (_, i) => {
      const xml = (await zip.file(`ppt/notesSlides/notesSlide${i + 1}.xml`)?.async("string")) ?? "";
      return xml.match(/<a:t>([^<]*)<\/a:t>/)?.[1].trim() ?? "";
    }),
  );
  expect(noteTexts.filter(Boolean).length, "발표자 슬라이드는 노트를 쓴다").toBeGreaterThan(0);

  // PDF 인쇄 — 대화상자 대신 호출만 센다. afterprint가 오지 않아 인쇄용 덱이 남아 page.pdf가 그것만 찍는다
  await page.evaluate(() => {
    const w = window as unknown as { __printCalls: number };
    w.__printCalls = 0;
    window.print = () => {
      w.__printCalls += 1;
    };
  });
  await viewer.getByRole("button", { name: "PDF로 인쇄" }).click();
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { __printCalls: number }).__printCalls))
    .toBe(1);
  await expect(page.locator("[data-slide-print-root] .slide-print-page")).toHaveCount(total);
  const pdf = (await page.pdf({ preferCSSPageSize: true, printBackground: true })).toString("latin1");
  const pages = pdf.match(/\/Type\s*\/Page(?!s)/g) ?? [];
  expect(pages.length, "슬라이드 하나가 한 쪽").toBe(total);
  expect(pdf).toMatch(/\/MediaBox\s*\[\s*0 0 960(\.\d+)? 540(\.\d+)?\s*\]/);
});

test("이미지 넣기를 켜면 덱 하나에 최대 4장 삽화가 붙고, 인쇄·PPTX에 같은 수가 들어간다", async ({ page }) => {
  test.setTimeout(420_000);
  await gotoDashboard(page);
  await createNotebookAndOpen(page, "PW 슬라이드 삽화");

  await page.getByTestId("add-source").click();
  await page.getByRole("tab", { name: "텍스트" }).click();
  await page.getByPlaceholder("제목 (선택)").fill("LLM 위키");
  await page.getByPlaceholder("텍스트를 붙여넣으세요").fill(SOURCE_TEXT);
  await page.getByRole("button", { name: "등록" }).click();
  await expect(page.getByRole("checkbox", { name: /LLM 위키 선택/ })).toBeEnabled({ timeout: 60_000 });

  await page.getByTestId("right-tab-agents").click();
  const card = page.getByTestId("studio-card-slide-deck");
  await expect(card).toBeEnabled({ timeout: 15_000 });
  await card.click();
  const dialog = page.getByTestId("slide-deck-dialog");
  await dialog.getByRole("radio", { name: "짧게" }).click();
  await dialog.getByTestId("slide-deck-description").click();
  await page.keyboard.type("개념을 그림으로 보여 주는 슬라이드를 골라 삽화를 꼭 넣어 줘");
  const toggle = dialog.getByRole("switch", { name: "이미지 넣기" });
  await expect(toggle).not.toHaveAttribute("aria-disabled", "true", { timeout: 15_000 });
  await toggle.click();
  await expect(toggle).toBeChecked();
  await dialog.getByTestId("slide-deck-generate").click();
  await expect(dialog).toHaveCount(0);

  // 스튜디오 항목은 성공한 run만 그린다 — run id는 노트북 run 목록에서 얻고, 끝날 때까지(삽화 loop 포함) 기다린다.
  // 산출물 목록이 정본이다
  const notebookId = /\/notebooks\/([0-9a-f-]{36})/.exec(page.url())?.[1] ?? "";
  type RunOut = { id: string; status: string; artifact_keys: { name: string; content_type: string }[] };
  const listRuns = async () =>
    (await (await page.request.get(`${API_BASE_URL}/api/v1/notebooks/${notebookId}/agent-runs`)).json()) as RunOut[];
  await expect.poll(async () => (await listRuns()).length, { timeout: 15_000 }).toBe(1);
  const [{ id: runId }] = await listRuns();
  const fetchRun = async () => (await (await page.request.get(`${API_BASE_URL}/api/v1/agent-runs/${runId}`)).json()) as RunOut;
  await expect.poll(async () => (await fetchRun()).status, { timeout: 300_000, intervals: [2_000] }).toBe("succeeded");
  const artifacts = (await fetchRun()).artifact_keys;
  const item = page.getByTestId("run-list").getByTestId(`studio-run-item-${runId}`);
  await expect(item).toBeVisible({ timeout: 30_000 });
  expect(artifacts[0].name).toBe("slide-deck.json");
  // 확장자는 코어가 이미지 바이트로 정한다 — 실제 Gemini 이미지 모델은 JPEG를 돌려준다(뷰어 `SLIDE_IMAGE_NAME`과 같다)
  const imageNames = artifacts.map((a) => a.name).filter((n) => /^slide-image-\d+\.(?:png|jpe?g)$/.test(n));
  expect(imageNames.length, "삽화는 1~4장").toBeGreaterThanOrEqual(1);
  expect(imageNames.length).toBeLessThanOrEqual(4);
  expect(
    artifacts
      .filter((a) => a.name.startsWith("slide-image-"))
      .every((a) => ["image/png", "image/jpeg"].includes(a.content_type)),
  ).toBe(true);

  await item.getByTestId("studio-run-title").click();
  const viewer = page.getByTestId("slide-deck-viewer");
  await expect(viewer).toBeVisible({ timeout: 15_000 });
  const slides = viewer.getByRole("group", { name: /^슬라이드 \d+ \/ \d+/ });
  const total = await slides.count();
  const expected = imageNames
    .map((n) => Number(/^slide-image-(\d+)\./.exec(n)?.[1]))
    .filter((n) => n >= 1 && n <= total)
    .sort((a, b) => a - b);
  expect(expected.length).toBeGreaterThanOrEqual(1);
  // 목록 — 산출물 번호의 장에만 그림이 있고, 오른쪽 40% 칸에 실제로 그려진다
  await expect(viewer.getByTestId("slide-image")).toHaveCount(expected.length, { timeout: 15_000 });
  for (const n of expected) {
    const slide = slides.nth(n - 1);
    await expect(slide).toHaveAttribute("data-image", "true");
    const img = slide.getByTestId("slide-image");
    await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth > 0)).toBe(true);
    const [box, frame] = await Promise.all([img.boundingBox(), slide.boundingBox()]);
    expect(box && frame && (box.x - frame.x) / frame.width).toBeCloseTo(0.6, 2);
  }

  // 발표 모드 — 첫 삽화 장에 그림이 있다
  await viewer.getByRole("button", { name: `슬라이드 ${expected[0]} 크게 보기` }).click();
  const expanded = page.getByTestId("slide-deck-expanded");
  await expect(expanded.getByTestId("slide-deck-expanded-main").getByTestId("slide-image")).toBeVisible();
  await expanded.getByRole("button", { name: "발표 시작" }).click();
  const stage = page.getByTestId("slide-presenter");
  await expect(stage.getByTestId("slide-image")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(stage).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(expanded).toHaveCount(0);

  // PPTX — 그림 수 = 삽화 수, 그림이 든 장 = 산출물 번호
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    viewer.getByRole("button", { name: "PPTX 다운로드" }).click(),
  ]);
  const zip = await JSZip.loadAsync(await readFile(await download.path()));
  expect(Object.keys(zip.files).filter((n) => /^ppt\/media\/./.test(n))).toHaveLength(expected.length);
  const withPic: number[] = [];
  for (let i = 1; i <= total; i++) {
    const xml = (await zip.file(`ppt/slides/slide${i}.xml`)?.async("string")) ?? "";
    if (xml.includes("<p:pic>")) withPic.push(i);
  }
  expect(withPic).toEqual(expected);
  console.log(`slide-deck-studio: 덱 ${total}장 · 삽화 ${expected.length}장(장 ${expected.join(",")}) · PPTX 그림 ${withPic.length}개`);

  // 인쇄용 덱 — 같은 장에 그림이 있다
  await page.evaluate(() => {
    window.print = () => undefined;
  });
  await viewer.getByRole("button", { name: "PDF로 인쇄" }).click();
  const pages = page.locator("[data-slide-print-root] .slide-print-page");
  await expect(pages).toHaveCount(total);
  await expect(page.locator("[data-slide-print-root] img")).toHaveCount(expected.length);
});
