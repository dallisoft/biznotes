import { expect, test } from "@playwright/test";
import { fulfillJson } from "./helpers";

// core-v0.20.0-upgrade AC-2.11(계획 §5.7 — AC-2.6 화면): MCP 서버 「상태 확인」 모달이 도구마다 정화 설명을 그리고,
// `rejected`면 「노출 불가: <사유 라벨>」, `sanitized`면 「설명 정화됨」 배지를 보인다(`ok`는 배지 없음).
// 서버 쓰기 없음 — 헬스체크 POST 응답과 모달을 여는 데 필요한 서버 목록 GET만 `page.route`로 바꾼다(계획 §9
// 허용 대체 ⑥ + 발주 「헬스체크 POST 응답만 mock」). 그 밖의 MCP 서버 관리 요청은 실서버로 보내지 않고 막아
// 센다(0이어야 한다). 실제 dev MCP 서버의 헬스체크·모달 스크린샷은 라이브 L2-4가 맡는다.

const SERVER_ID = "00000000-0000-4000-8000-0000000002e6";
const SERVER = {
  id: SERVER_ID,
  name: "PW mock MCP",
  transport: "stdio",
  endpoint_or_command: "python -m pw_mock_mcp",
  has_env: false,
  enabled: true,
  last_health_check: null,
  last_health_ok: null,
};

const OK_DESCRIPTION = "입력 문장의 단어 수를 셉니다.";
/** 정화된 설명도 외부 문자열이다 — 태그 모양은 텍스트 그대로 보여야 한다. */
const SANITIZED_DESCRIPTION = "문서를 요약합니다 <b>굵게</b>는 글자 그대로";

/** `MCPHealthCheckOut` 모양(계획 §5.7) — 원문 객체(input_schema·annotations)는 응답에 없다. */
const HEALTH = {
  ok: true,
  error: null,
  tools: [
    { name: "word_count", description: OK_DESCRIPTION, spec: { status: "ok", reason: null } },
    { name: "summarize", description: SANITIZED_DESCRIPTION, spec: { status: "sanitized", reason: null } },
    { name: "deep_schema", description: "", spec: { status: "rejected", reason: "schema_too_deep" } },
    { name: "list_schema", description: "", spec: { status: "rejected", reason: "schema_not_object" } },
    { name: "nan_default", description: "", spec: { status: "rejected", reason: "schema_unsupported_value" } },
  ],
};

test("MCP 상태 확인 모달 — 노출 불가 사유·설명 정화됨 배지와 정화 설명(헬스체크 POST mock, 서버 쓰기 없음)", async ({
  page,
}) => {
  const blocked: string[] = [];
  let healthPosts = 0;
  await page.route("**/api/v1/admin/mcp-servers**", async (route) => {
    const request = route.request();
    const { pathname } = new URL(request.url());
    if (request.method() === "OPTIONS") {
      await fulfillJson(route, 204, null);
    } else if (request.method() === "GET" && pathname === "/api/v1/admin/mcp-servers") {
      await fulfillJson(route, 200, [SERVER]);
    } else if (request.method() === "POST" && pathname === `/api/v1/admin/mcp-servers/${SERVER_ID}/health-check`) {
      healthPosts += 1;
      await fulfillJson(route, 200, HEALTH);
    } else {
      // 목록 GET·헬스체크 POST 밖의 MCP 서버 요청은 실서버에 닿지 않게 막는다
      blocked.push(`${request.method()} ${pathname}`);
      await route.abort("blockedbyclient");
    }
  });

  await page.goto("/admin/mcp");
  await expect(page.getByRole("heading", { level: 1, name: "MCP 서버" })).toBeVisible({ timeout: 15_000 });
  const row = page.getByRole("row").filter({ hasText: SERVER.name });
  await expect(row).toBeVisible();

  // 응답 대기는 클릭 전에 건다
  const healthChecked = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === `/api/v1/admin/mcp-servers/${SERVER_ID}/health-check` &&
      r.request().method() === "POST",
  );
  await row.getByRole("button", { name: "상태 확인" }).click();
  expect((await healthChecked).status()).toBe(200);

  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText(`제공 도구 — ${SERVER.name}`);
  const tool = (name: string) => dialog.getByTestId(`mcp-tool-${name}`);
  // 노출 판정 배지 — 승인 고정 상태 배지(core-v0.22.0-upgrade AC-3, 모든 행에 붙는다)는 뺀다
  const specBadge = (name: string) =>
    tool(name).locator('[data-slot="status-badge"]:not([data-testid="mcp-tool-approval"])');

  // ok — 정화 설명 그대로, 노출 판정 배지 없음(행이 렌더된 뒤 부재를 본다)
  await expect(tool("word_count")).toContainText(OK_DESCRIPTION);
  // mock 응답에 approval이 없으면 「승인 없음」
  await expect(tool("word_count").getByTestId("mcp-tool-approval")).toHaveText("승인 없음");
  await expect(specBadge("word_count")).toHaveCount(0);

  // sanitized — 「설명 정화됨」 배지 + 정화 설명(텍스트로만 렌더)
  await expect(specBadge("summarize")).toHaveText("설명 정화됨");
  await expect(tool("summarize")).toContainText(SANITIZED_DESCRIPTION);
  await expect(tool("summarize").locator("b")).toHaveCount(0);

  // rejected — 「노출 불가: <사유 라벨>」(계획 §5.7 문구), 설명 없음
  await expect(specBadge("deep_schema")).toHaveText(
    "노출 불가: 입력 스키마가 너무 깊습니다",
  );
  await expect(specBadge("list_schema")).toHaveText(
    "노출 불가: 입력 스키마가 객체가 아닙니다",
  );
  await expect(specBadge("nan_default")).toHaveText(
    "노출 불가: 입력 스키마에 지원하지 않는 값이 있습니다",
  );
  await expect(tool("deep_schema").getByTestId("mcp-tool-description")).toHaveCount(0);

  await page.screenshot({ path: "test-results/p2-admin-mcp-health.png", fullPage: true });
  expect(healthPosts, "헬스체크 POST는 mock 응답으로만 받았다").toBe(1);
  expect(blocked, "목록 GET·헬스체크 POST 밖의 MCP 서버 요청 0").toEqual([]);
});
