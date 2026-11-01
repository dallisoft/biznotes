import { expect, test } from "@playwright/test";
import { gotoDashboard } from "./helpers";

// 브라우저가 쓰는 공개 API 주소(compose NEXT_PUBLIC_API_BASE_URL 기본값과 같다)
const API_BASE_URL = process.env.E2E_API_BASE_URL ?? "http://localhost:28000";

// #260 PR-2 — `save_sources` 노드(「소스로 저장」)는 팔레트에 없지만 기존 정의의 노드는 렌더된다.
// 속성 패널은 items만 편집 칸이고, 이름공간·허용 호스트·노트북 규칙은 읽기 전용 값으로 보인다.
// 정의는 API로 만든다(user 정의 — requires_approval 기본값이라 저장 검증을 통과한다).
test("save_sources 노드 — 팔레트에 없고, 속성 패널은 items만 편집한다", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await gotoDashboard(page);

  const created = await page.request.post(`${API_BASE_URL}/api/v1/workflows`, {
    data: {
      name: `e2e 소스로 저장 ${Date.now()}`,
      params_schema: {},
      definition: {
        nodes: [
          { id: "list", type: "constant", value: { text: "[]" } },
          {
            id: "save",
            type: "save_sources",
            items: "{list.text}",
            namespace: "e2e-save",
            allowed_hosts: ["stock.pstatic.net"],
            notebook_rule: { title_template: "e2e 저장 {month}", timezone: "Asia/Seoul" },
          },
        ],
        edges: [["list", "save"]],
      },
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  const workflowId = ((await created.json()) as { id: string }).id;

  try {
    await page.goto(`/workflows/${workflowId}/edit`);
    // 팔레트가 렌더된 뒤에 판정한다(빈 화면에서의 "없음"은 증거가 아니다)
    await expect(page.getByTestId("palette-llm")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("palette-save_sources")).toHaveCount(0);

    const node = page.getByTestId("wf-node-save");
    await expect(node).toContainText("소스로 저장");
    await expect(node).toContainText("e2e 저장 {month}");
    await node.click();

    const panel = page.getByTestId("property-panel");
    await expect(panel).toContainText("소스로 저장");
    await expect(panel.getByTestId("readonly-namespace")).toHaveText("e2e-save");
    await expect(panel.getByTestId("readonly-allowed_hosts")).toContainText("stock.pstatic.net");
    await expect(panel.getByTestId("readonly-notebook_rule")).toContainText("e2e 저장 {month}");
    // 편집 칸은 items 하나뿐이다
    const editors = panel.locator("input, textarea");
    await expect(editors).toHaveCount(1);
    await expect(editors.first()).toHaveValue("{list.text}");

    await page.screenshot({ path: "test-results/save-sources-property-panel.png", fullPage: true });
  } finally {
    // 실행마다 admin 소유 draft가 쌓이지 않게 지운다. finally에서 던지면 본문의 원래 실패를 덮으므로
    // 요청 예외도 값으로 받고 soft로 단언한다(workflow-editor-surface.spec.ts와 같은 방식).
    const status = await page.request
      .delete(`${API_BASE_URL}/api/v1/workflows/${workflowId}`)
      .then((res) => res.status(), (err: unknown) => `요청 실패: ${String(err)}`);
    expect.soft(status, `draft ${workflowId} 정리`).toBe(204);
  }
});
