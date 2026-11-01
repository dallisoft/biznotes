import { expect, test } from "@playwright/test";
import { gotoDashboard } from "./helpers";

// 브라우저가 쓰는 공개 API 주소(compose NEXT_PUBLIC_API_BASE_URL 기본값과 같다)
const API_BASE_URL = process.env.E2E_API_BASE_URL ?? "http://localhost:28000";

// 계획 AC-30 (c) — 일반 워크플로우 에디터의 숨김은 편집 표면 기준이다(로그인 role이 아니다).
// 이 스위트의 storageState는 admin 계정이므로, admin으로 일반 editor를 열어도 mcp_tool 노드가
// 팔레트에 없음을 실증한다. 관리자 빌더(/admin/agents)는 phase13.spec.ts의 빌더 스펙이 다룬다.
test("일반 워크플로우 에디터 — admin 계정으로 열어도 팔레트에 mcp_tool이 없다", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await gotoDashboard(page);
  await page.goto("/workflows");
  // 「새 워크플로우」는 이름 입력 다이얼로그를 거친다(ui-ux-redesign P4 AC-4.2 — create-workflow testid 유지)
  await page.getByTestId("create-workflow").click();
  const dialog = page.getByRole("dialog", { name: "새 워크플로우" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "만들기" }), "이름이 비면 만들기 비활성").toBeDisabled();
  await dialog.getByLabel("이름").fill(`e2e 편집 표면 ${Date.now()}`);
  const created = page.waitForResponse(
    (r) => new URL(r.url()).pathname === "/api/v1/workflows" && r.request().method() === "POST",
  );
  await dialog.getByRole("button", { name: "만들기" }).click();
  // 정리용 ID는 응답에서 먼저 잡는다(편집 화면 이동이 늦어도 finally가 지울 수 있게)
  const createdResponse = await created;
  expect(createdResponse.status()).toBe(201);
  const workflowId = ((await createdResponse.json()) as { id: string }).id;

  try {
    await page.waitForURL(new RegExp(`/workflows/${workflowId}/edit$`));
    // 팔레트가 렌더된 뒤에 판정한다(빈 화면에서의 "없음"은 증거가 아니다)
    await expect(page.getByTestId("palette-llm")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("palette-react_agent")).toBeVisible();
    await expect(page.getByTestId("palette-http")).toBeVisible();
    await expect(page.getByTestId("palette-mcp_tool")).toHaveCount(0);

    // react_agent의 허용 도구에서도 mcp_call(“MCP 도구(단건 지정)”)을 내지 않는다
    await page.getByTestId("palette-react_agent").click();
    const panel = page.getByTestId("property-panel");
    await expect(panel.getByText("허용 도구")).toBeVisible();
    await expect(panel.getByLabel("서브에이전트 호출")).toBeVisible();
    await expect(panel.getByLabel("MCP 도구(단건 지정)")).toHaveCount(0);

    await page.screenshot({ path: "test-results/ac30-admin-user-editor-palette.png", fullPage: true });
  } finally {
    // 실행마다 admin 소유 draft가 쌓이면 /admin/agents 목록 스펙이 페이지 밖으로 밀린다(리뷰 P1-06).
    // 브라우저 컨텍스트의 인증 쿠키로 제품 API(DELETE /api/v1/workflows/{id})를 호출해 지운다.
    // finally에서 던지면 try 본문의 원래 실패를 덮어쓴다(리뷰 P1-11) — 요청 예외도 값으로 받고
    // soft로 단언한다. 본문이 통과해도 soft 실패는 테스트를 실패로 기록한다.
    const status = await page.request
      .delete(`${API_BASE_URL}/api/v1/workflows/${workflowId}`)
      .then((res) => res.status(), (err: unknown) => `요청 실패: ${String(err)}`);
    expect.soft(status, `draft ${workflowId} 정리`).toBe(204);
  }
});
