import { expect, test, type Page } from "@playwright/test";
import {
  API_BASE_URL,
  cleanupCreatedNotebooks,
  createNotebookAndOpen,
  fulfillJson,
  gotoDashboard,
} from "./helpers";

// #216(core-v0.25.0-upgrade AC-12·AC-14, D4) — 스튜디오 다이얼로그의 「모델」에서 고른 모델로 run이 돌고, 결과 상세
// 「실행 로그」에 노드별 사용 모델이 보인다. 실스택·실 LLM 전제(mindmap-studio와 같다). 고를 모델은 `E2E_RUN_MODEL_NAME`
// (표시 이름)이고, 없으면 기본이 아닌 첫 llm 모델, 그것도 없으면 기본 모델이다 — 어느 쪽이든 「기본 모델」 항목이
// 아니라 목록의 모델을 직접 고르므로 요청에 `model_id`가 실린다. 고른 모델이 폴백 없이 응답했는지 실행 로그 API로
// 확인한다(폴백이면 실패 — 환경의 모델 상태를 드러낸다). 만든 노트북(「PW 」 접두)은 생성 응답 id로 지운다.
// #234(core-v0.27.0-upgrade AC-11) — 「최근 결과」 항목 보조 줄에 실제로 응답한 「모델 X」가 보인다(위 실 LLM 실행).
// 「폴백」 배지·요청 모델 이름은 폴백을 실스택에서 일으킬 수 없어 목록 응답만 가로챈 run 하나로 본다(LLM 불필요).
cleanupCreatedNotebooks("studio-run-model");

const SOURCE_TEXT =
  "에스프레소는 9기압의 압력으로 25~30초간 추출한다. 핸드 드립은 92~96℃ 물을 원두에 부어 내린다. " +
  "콜드브루는 찬물에 12~24시간 우려낸다.";

interface ModelInfo {
  id: string;
  display_name: string;
  is_default: boolean;
}

interface LogRow {
  node_id: string;
  status: string;
  model: { id: string; display_name: string } | null;
  fallback_used: boolean | null;
}

interface ListedRun {
  id: string;
  used_models: { id: string; display_name: string }[];
  fallback_used: boolean;
}

async function listModels(page: Page): Promise<ModelInfo[]> {
  const res = await page.request.get(`${API_BASE_URL}/api/v1/models`);
  expect(res.status()).toBe(200);
  return (await res.json()) as ModelInfo[];
}

async function pickTargetModel(page: Page): Promise<ModelInfo> {
  const models = await listModels(page);
  expect(models.length, "고를 llm 모델이 하나 이상 있어야 한다").toBeGreaterThan(0);
  const wanted = process.env.E2E_RUN_MODEL_NAME;
  const target = wanted
    ? models.find((m) => m.display_name === wanted)
    : (models.find((m) => !m.is_default) ?? models[0]);
  expect(target, `E2E_RUN_MODEL_NAME=${wanted ?? ""} 모델이 목록에 없다`).toBeTruthy();
  console.log(`studio-run-model: 고른 모델 ${target!.display_name} (${target!.id})`);
  return target!;
}

test.describe.configure({ retries: 0 });

test("스튜디오 다이얼로그에서 고른 모델로 run이 돌고 결과 상세에 사용 모델이 보인다", async ({ page }) => {
  test.setTimeout(300_000);
  await gotoDashboard(page);
  const notebookId = await createNotebookAndOpen(page, `PW 실행 모델 ${Date.now()}`);

  await page.getByTestId("add-source").click();
  await page.getByRole("tab", { name: "텍스트" }).click();
  await page.getByPlaceholder("제목 (선택)").fill("커피 추출");
  await page.getByPlaceholder("텍스트를 붙여넣으세요").fill(SOURCE_TEXT);
  await page.getByRole("button", { name: "등록" }).click();
  await expect(page.getByRole("checkbox", { name: /커피 추출 선택/ })).toBeEnabled({ timeout: 60_000 });

  const target = await pickTargetModel(page);

  // 다이얼로그 — 기본값은 「기본 모델 (…)」, 목록에서 모델을 직접 고른다
  await page.getByTestId("right-tab-agents").click();
  const card = page.getByTestId("studio-card-mindmap");
  await expect(card).toBeEnabled({ timeout: 15_000 });
  await card.click();
  const dialog = page.getByRole("dialog");
  const picker = dialog.getByRole("combobox", { name: "모델" });
  await expect(picker).toHaveText(/^기본 모델/);
  await picker.click();
  await page.getByRole("option", { name: target.display_name, exact: true }).click();
  // 트리거 텍스트 끝에 펼침 아이콘 글자가 붙는다 — 앞부분이 고른 모델 이름인지만 본다(「기본 모델 (…)」과 구분)
  await expect(picker).toHaveText(new RegExp(`^${target.display_name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`));

  // 생성 요청에 model_id가 실리고 응답 run에도 남는다 — 대기는 클릭 전에 건다
  const createdRun = page.waitForResponse(
    (r) =>
      /^\/api\/v1\/notebooks\/[^/]+\/agent-runs$/.test(new URL(r.url()).pathname) &&
      r.request().method() === "POST",
  );
  await dialog.getByTestId("run-agent").click();
  const created = await createdRun;
  expect(created.request().postDataJSON()).toMatchObject({ model_id: target.id });
  expect(created.status(), await created.text()).toBe(202);
  const run = (await created.json()) as { id: string; model_id: string | null };
  expect(run.model_id).toBe(target.id);
  await expect(dialog).toBeHidden();

  // 실행이 끝날 때까지 — 끝난 뒤에 노트북을 지운다
  await expect
    .poll(
      async () => {
        const res = await page.request.get(`${API_BASE_URL}/api/v1/agent-runs/${run.id}`);
        return ((await res.json()) as { status: string }).status;
      },
      { timeout: 240_000, intervals: [2_000] },
    )
    .toMatch(/^(succeeded|failed|cancelled)$/);

  // 실행 로그(API) — llm 노드가 고른 모델로 폴백 없이 응답했다(AC-12 「실행 로그로 확인」)
  const logsRes = await page.request.get(`${API_BASE_URL}/api/v1/agent-runs/${run.id}/logs`);
  expect(logsRes.status()).toBe(200);
  const logs = (await logsRes.json()) as LogRow[];
  const answered = logs.filter((l) => l.model !== null);
  expect(answered.length, JSON.stringify(logs)).toBeGreaterThan(0);
  for (const l of answered) {
    expect(l.model!.id, `노드 ${l.node_id}`).toBe(target.id);
    expect(l.fallback_used, `노드 ${l.node_id}`).toBe(false);
  }

  // 목록 응답(#234) — 실행 로그와 같은 규칙으로 실제로 응답한 모델을 싣는다
  const listRes = await page.request.get(`${API_BASE_URL}/api/v1/notebooks/${notebookId}/agent-runs`);
  expect(listRes.status()).toBe(200);
  const listed = ((await listRes.json()) as ListedRun[]).find((r) => r.id === run.id);
  expect(listed, "목록에 이번 run이 있다").toBeTruthy();
  expect(listed!.used_models).toEqual([{ id: target.id, display_name: target.display_name }]);
  expect(listed!.fallback_used).toBe(false);

  // 「최근 결과」 항목 보조 줄 — 「모델 X」, 폴백 배지 없음(#234 AC-11)
  const item = page.getByTestId(`studio-run-item-${run.id}`);
  await expect(item).toBeVisible({ timeout: 15_000 });
  await expect(item.getByTestId("studio-run-model")).toHaveText(`· 모델 ${target.display_name}`, {
    timeout: 15_000,
  });
  await expect(item.getByTestId("studio-run-fallback")).toHaveCount(0);

  // 결과 상세 — 머리 요약과 「실행 로그」 탭의 노드 행에 사용 모델(AC-14)
  await item.getByTestId("studio-run-title").click();
  const detail = page.getByTestId("run-detail-view");
  await expect(detail).toBeVisible({ timeout: 15_000 });
  await expect(detail.getByTestId("run-model-summary")).toContainText(`사용 모델 ${target.display_name}`);
  await detail.getByRole("tab", { name: "실행 로그" }).click();
  const row = detail.getByTestId(new RegExp(`^execution-log-`)).filter({ hasText: answered[0].node_id });
  await expect(row.getByTestId("node-model")).toContainText(target.display_name);
  await expect(row.getByTestId("node-fallback")).toHaveCount(0);
  await page.screenshot({ path: "test-results/studio-run-model.png", fullPage: true });
});

test("폴백한 결과 항목에 「폴백」 배지와 요청 모델 이름이 보인다", async ({ page }) => {
  await gotoDashboard(page);
  const notebookId = await createNotebookAndOpen(page, `PW 결과 폴백 ${Date.now()}`);

  // 마인드맵 카드는 시드 시스템 스킬 `mindmap`의 wrapper 에이전트에 묶인다 — 가짜 run도 그 에이전트로 둔다
  const skillsRes = await page.request.get(`${API_BASE_URL}/api/v1/skills`);
  expect(skillsRes.status()).toBe(200);
  const skills = (await skillsRes.json()) as { slug: string; visibility: string; wrapper_agent_id: string | null }[];
  const mindmapAgentId = skills.find((s) => s.slug === "mindmap" && s.visibility === "system")?.wrapper_agent_id;
  expect(mindmapAgentId, "시드 마인드맵 스킬").toBeTruthy();
  // 요청 모델 이름은 웹이 켜진 모델 목록에서 찾는다 — 목록의 첫 모델을 요청 모델로 둔다
  const models = await listModels(page);
  expect(models.length, "llm 모델이 하나 이상 있어야 한다").toBeGreaterThan(0);
  const requested = models[0];

  const runId = "00000000-0000-4000-8000-000000000234";
  const now = new Date().toISOString();
  const run = {
    id: runId,
    agent_id: mindmapAgentId,
    notebook_id: notebookId,
    params: { topic: "커피 추출" },
    input_source_ids: null,
    trigger: "manual",
    status: "succeeded",
    progress: 100,
    result_summary: null,
    artifact_keys: [{ key: `${runId}/mindmap.json`, name: "mindmap.json", content_type: "application/json" }],
    token_usage: {},
    error: null,
    created_at: now,
    started_at: now,
    finished_at: now,
    skill_version_id: null,
    model_id: requested.id,
    agent_name: "마인드맵",
    agent_icon: "🧠",
    can_cancel: false,
    can_hide: false,
    hidden: false,
    used_models: [{ id: "00000000-0000-4000-8000-0000000002a4", display_name: "E2E 폴백 모델" }],
    fallback_used: true,
  };
  await page.route(`**/api/v1/notebooks/${notebookId}/agent-runs`, (route) =>
    route.request().method() === "GET" || route.request().method() === "OPTIONS"
      ? fulfillJson(route, 200, [run])
      : route.fallback(),
  );
  await page.route(`**/api/v1/agent-runs/${runId}/artifacts/mindmap.json`, (route) =>
    fulfillJson(route, 200, { version: 1, root: { id: "r", label: "커피 추출 마인드맵", children: [] } }),
  );
  // 패널은 노트북을 열 때 빈 목록을 이미 캐시했다 — 가로채기를 건 뒤 새로 고친다(run-hide와 같은 처리)
  await page.reload();
  await page.getByTestId("right-tab-agents").click();

  const item = page.getByTestId(`studio-run-item-${runId}`);
  await expect(item).toBeVisible({ timeout: 15_000 });
  await expect(item.getByTestId("studio-run-title")).toHaveText("커피 추출 마인드맵");
  await expect(item.getByTestId("studio-run-model")).toHaveText("· 모델 E2E 폴백 모델");
  await expect(item.getByTestId("studio-run-fallback")).toHaveText("폴백");
  await expect(item.getByTestId("studio-run-requested-model")).toHaveText(`요청 ${requested.display_name}`);
  await page.screenshot({ path: "test-results/studio-run-model-fallback.png", fullPage: true });
});
