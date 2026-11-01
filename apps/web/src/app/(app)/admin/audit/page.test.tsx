/**
 * 관리자 감사 로그 페이지(#236) — 「사용량·감사」에서 분리한 화면. ui-ux-redesign P5(AC-5.7, P-24, §6.2.5)의
 * 감사 로그 단언을 그대로 옮겼다.
 * 네트워크 경계(`api` 객체)만 대체하고 페이지·라벨 사전·포맷·필터·페이지네이션은 실제로 실행한다(§3.3-3).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import AdminAuditPage from "@/app/(app)/admin/audit/page";
import type { AuditLogRow } from "@/lib/admin-types";
import { AUDIT_ACTION_LABEL, auditActionLabel } from "@/lib/labels";
import { APP_QUERY_DEFAULTS } from "@/lib/query-defaults";

const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));

const AUDIT_PATH = "/api/v1/admin/audit-logs?limit=100";

/**
 * 감사 액션 코드 수집 결과(2026-10-15, 읽기 전용) — 보고서 `reports/phase-5/impl-ops.md` §4.
 * SOURCE: 코어 biznotes-core v0.15.0(`biznotes_core/**`)·`apps/api/app/**`의 감사 기록 호출 인자(AST 수집 70종).
 * DB: dev `audit_logs`의 `SELECT DISTINCT action`(31종).
 * 2026-10-27: `limits.settings_update`(apps/api 관리 › 사용 제한)를 더해 71종.
 * 2026-10-27: 코어 v0.26.0 「사용 제한」(`biznotes_core/api/v1/admin/limits.py`)의 `rate_limits.update`·
 *   `usage_defaults.update`·`source_upload.update`를 더해 74종.
 * biznotes: 외부 계정 커넥터(`connection.*` 4종)와 PDF 내보내기(`notebook.export`)를 빼서 69종,
 *   자동화(`agent_schedule.*` 3종·`agent_webhook.*` 4종)를 빼서 62종.
 */
const SOURCE_CODES = [
  "agent.create", "agent.delete",
  "agent.update", "group.create", "group.delete", "group.member_add", "group.member_remove", "llm.circuit_reset",
  "llm.model_create", "llm.model_delete", "llm.model_import", "llm.model_set_default", "llm.model_update",
  "limits.settings_update", "llm.provider_create", "llm.provider_delete", "llm.provider_update", "mcp.call",
  "mcp.call_result",
  "mcp.exposure_change", "mcp.server_create", "mcp.server_delete", "mcp.server_update", "mcp.tools_approve",
  "rag.reindex", "rag.settings_update", "rate_limits.update", "skill.clone", "skill.create",
  "skill.delete",
  "skill.demote", "skill.disable", "skill.enable", "skill.export", "skill.expose", "skill.freeze",
  "skill.frontmatter.update", "skill.import", "skill.publish", "skill.retract", "skill.review.approve",
  "skill.review.cancel", "skill.review.read", "skill.review.reject", "skill.review.request",
  "skill.review.withdraw", "skill.unexpose", "skill.update", "source_upload.update", "usage_defaults.update",
  "usage_limits.update", "user.create",
  "user.delete", "user.password_change", "user.password_reset", "user.update", "workflow.create",
  "workflow.delete", "workflow.duplicate", "workflow.publish", "workflow.unpublish", "workflow.update",
];
const DB_CODES = [
  "agent.create", "agent.delete", "agent.update", "llm.model_create", "llm.model_delete", "llm.model_import",
  "llm.model_set_default", "llm.model_update", "llm.provider_create", "llm.provider_delete",
  "llm.provider_update", "mcp.server_update", "mcp.tools_approve", "rag.reindex", "rag.settings_update",
  "skill.create", "skill.delete", "skill.expose", "skill.freeze", "skill.frontmatter.update", "skill.import",
  "skill.review.approve", "skill.review.request", "skill.update", "user.create", "user.delete",
  "user.password_change", "user.password_reset", "workflow.create", "workflow.delete", "workflow.publish",
];

/** 받은 100건: admin 60건(LLM 프로바이더 삭제 40 + 재인덱싱 20), demo 30건(비밀번호 변경), 사용자 없음 9건, 미지 코드 1건 */
function auditFixture(): AuditLogRow[] {
  const rows: AuditLogRow[] = [];
  const push = (user_email: string | null, action: string, resource = "") =>
    rows.push({
      id: `l${rows.length}`,
      user_email,
      action,
      resource,
      detail: {},
      created_at: new Date(Date.UTC(2026, 9, 14, 14, 50, 40) - rows.length * 1000).toISOString(),
    });
  for (let i = 0; i < 40; i += 1) push("admin@biznotes.dev", "llm.provider_delete", `e2e-gate-${i}`);
  for (let i = 0; i < 20; i += 1) push("admin@biznotes.dev", "rag.reindex");
  for (let i = 0; i < 30; i += 1) push("demo@biznotes.dev", "user.password_change", "demo@biznotes.dev");
  for (let i = 0; i < 9; i += 1) push(null, "skill.expose", "seed-skill");
  push("admin@biznotes.dev", "future.unknown_action", "x");
  return rows;
}

function renderPage({ audit = auditFixture() as AuditLogRow[] | Error } = {}) {
  api.get.mockImplementation(async (path: string) => {
    if (path === AUDIT_PATH) {
      if (audit instanceof Error) throw audit;
      return audit;
    }
    throw new Error(`unexpected GET ${path}`);
  });
  const client = new QueryClient({ defaultOptions: { queries: { ...APP_QUERY_DEFAULTS, retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AdminAuditPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
});

const auditRegion = () => screen.getByTestId("audit-log");
const auditRows = () => within(auditRegion()).getAllByRole("row").slice(1); // 머리글 제외

describe("AdminAuditPage 헤더 (#236)", () => {
  it("브레드크럼 「관리 / 감사 로그」, h1 하나, 범위 설명, 감사 로그 요청만 1건", async () => {
    renderPage();
    const crumb = screen.getByRole("navigation", { name: "위치" });
    expect(within(crumb).getByRole("link", { name: "관리" })).toHaveAttribute("href", "/admin");
    expect(within(crumb).getByText("감사 로그")).toHaveAttribute("aria-current", "page");
    expect(screen.getAllByRole("heading", { level: 1 }).map((h) => h.textContent)).toEqual(["감사 로그"]);
    expect(
      screen.getByText("최근 100건의 관리 작업 기록입니다. 사용자·액션 필터는 받은 기록 안에서만 적용됩니다"),
    ).toBeInTheDocument();
    await within(auditRegion()).findAllByText("LLM 프로바이더 삭제");
    // 사용량 화면의 요청(사용량 표·일별 그래프·사용자 목록)은 부르지 않는다
    expect(api.get.mock.calls.map((c) => c[0])).toEqual([AUDIT_PATH]);
  });
});

describe("AdminAuditPage 감사 로그 (AC-5.7, P-24 ①②)", () => {
  it("액션은 라벨 + 원시 코드(작은 모노)로, 미지 코드는 원시 코드만, 시각은 초 포함 formatDateTime", async () => {
    renderPage();
    await within(auditRegion()).findAllByText("LLM 프로바이더 삭제");
    const first = auditRows()[0];
    expect(within(first).getByText("LLM 프로바이더 삭제")).toBeInTheDocument();
    const code = within(first).getByText("llm.provider_delete");
    expect(code.tagName).toBe("CODE");
    expect(code).toHaveClass("font-mono", "text-xs");
    // 2026-10-14T14:50:40Z — 초까지 보인다(시간대와 무관하게 「:40」으로 끝난다)
    expect(within(first).getAllByRole("cell")[0].textContent).toMatch(/^2026\. 10\. 1[45]\. 오[전후] \d{1,2}:50:40$/);
    expect(api.get).toHaveBeenCalledWith(AUDIT_PATH);
  });

  it("미지 코드는 원시 코드를 그대로 보인다(라벨 중복 없음)", async () => {
    const user = userEvent.setup();
    renderPage();
    await within(auditRegion()).findAllByText("LLM 프로바이더 삭제");
    await user.click(screen.getByRole("combobox", { name: "액션 필터" }));
    await user.click(await screen.findByRole("option", { name: "future.unknown_action" }));
    const rows = auditRows();
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getAllByText("future.unknown_action")).toHaveLength(1);
  });

  it("사용자 필터는 받은 100건 안에서만 동작한다(추가 요청 0) — 선택지는 실제로 있는 사용자", async () => {
    const user = userEvent.setup();
    renderPage();
    await within(auditRegion()).findAllByText("LLM 프로바이더 삭제");
    const calls = api.get.mock.calls.length;
    expect(screen.getByTestId("audit-count")).toHaveTextContent("100건");

    await user.click(screen.getByRole("combobox", { name: "사용자 필터" }));
    const options = (await screen.findAllByRole("option")).map((o) => o.textContent);
    expect(options).toEqual(["모든 사용자", "admin@biznotes.dev", "demo@biznotes.dev", "사용자 정보 없음"]);
    await user.click(screen.getByRole("option", { name: "demo@biznotes.dev" }));

    expect(screen.getByTestId("audit-count")).toHaveTextContent("받은 100건 중 30건");
    for (const r of auditRows()) expect(r).toHaveTextContent("demo@biznotes.dev");
    expect(auditRows()).toHaveLength(10); // 30건 → 10건씩 페이지
    expect(within(auditRegion()).getByText("1–10 / 30")).toBeInTheDocument();
    expect(api.get.mock.calls.length).toBe(calls); // 클라이언트 필터 — 서버 요청 없음
  });

  it("「사용자 정보 없음」은 user_email이 없는 기록만 고른다", async () => {
    const user = userEvent.setup();
    renderPage();
    await within(auditRegion()).findAllByText("LLM 프로바이더 삭제");
    await user.click(screen.getByRole("combobox", { name: "사용자 필터" }));
    await user.click(await screen.findByRole("option", { name: "사용자 정보 없음" }));
    expect(screen.getByTestId("audit-count")).toHaveTextContent("받은 100건 중 9건");
    for (const r of auditRows()) expect(r).toHaveTextContent("스킬 공개");
  });

  it("액션 필터 선택지는 받은 기록의 액션을 라벨로 보이고, 고르면 그 액션만 남는다", async () => {
    const user = userEvent.setup();
    renderPage();
    await within(auditRegion()).findAllByText("LLM 프로바이더 삭제");
    await user.click(screen.getByRole("combobox", { name: "액션 필터" }));
    const options = (await screen.findAllByRole("option")).map((o) => o.textContent);
    expect(options[0]).toBe("모든 액션");
    expect(new Set(options.slice(1))).toEqual(
      new Set(["LLM 프로바이더 삭제", "재인덱싱 요청", "비밀번호 변경", "스킬 공개", "future.unknown_action"]),
    );
    await user.click(screen.getByRole("option", { name: "재인덱싱 요청" }));
    expect(screen.getByTestId("audit-count")).toHaveTextContent("받은 100건 중 20건");
    for (const r of auditRows()) expect(r).toHaveTextContent("rag.reindex");
  });

  it("두 필터는 함께 적용되고, 결과가 없으면 「필터 지우기」로 전체로 돌아간다", async () => {
    const user = userEvent.setup();
    renderPage();
    await within(auditRegion()).findAllByText("LLM 프로바이더 삭제");
    await user.click(screen.getByRole("combobox", { name: "사용자 필터" }));
    await user.click(await screen.findByRole("option", { name: "demo@biznotes.dev" }));
    await user.click(screen.getByRole("combobox", { name: "액션 필터" }));
    await user.click(await screen.findByRole("option", { name: "재인덱싱 요청" }));

    expect(screen.getByText("조건에 맞는 항목이 없습니다")).toBeInTheDocument();
    expect(within(auditRegion()).queryByRole("table")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "필터 지우기" }));
    expect(screen.getByTestId("audit-count")).toHaveTextContent("100건");
    expect(screen.getByRole("combobox", { name: "사용자 필터" })).toHaveTextContent("모든 사용자");
    expect(screen.getByRole("combobox", { name: "액션 필터" })).toHaveTextContent("모든 액션");
    expect(auditRows()).toHaveLength(10);
  });

  it("필터를 바꾸면 1페이지로 돌아간다", async () => {
    const user = userEvent.setup();
    renderPage();
    await within(auditRegion()).findAllByText("LLM 프로바이더 삭제");
    const next = within(auditRegion()).getByRole("button", { name: "다음 페이지" });
    await user.click(next);
    await user.click(next);
    expect(within(auditRegion()).getByText("21–30 / 100")).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "사용자 필터" }));
    await user.click(await screen.findByRole("option", { name: "admin@biznotes.dev" }));
    expect(within(auditRegion()).getByText("1–10 / 61")).toBeInTheDocument();
  });

  it("감사 기록이 없으면 EmptyState(필터·표 없음), 못 불러오면 ErrorState", async () => {
    const { unmount } = renderPage({ audit: [] });
    expect(await screen.findByText("아직 감사 기록이 없습니다")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "사용자 필터" })).not.toBeInTheDocument();
    unmount();
    renderPage({ audit: new Error("boom") });
    expect(await screen.findByText("감사 로그를 불러오지 못했습니다")).toBeInTheDocument();
  });
});

describe("감사 액션 라벨 사전 — 수집한 코드 전부 (AC-5.7, §6.3.2)", () => {
  it("코어·apps/api에서 수집한 62종이 사전 키와 정확히 같다(누락·여분 0)", () => {
    expect(new Set(SOURCE_CODES).size).toBe(62);
    expect(Object.keys(AUDIT_ACTION_LABEL).sort()).toEqual([...SOURCE_CODES].sort());
  });

  it("dev 감사 로그의 31종은 모두 수집 목록 안에 있고 라벨로 보인다(원시 코드 그대로 0)", () => {
    for (const code of DB_CODES) {
      expect(SOURCE_CODES, code).toContain(code);
      expect(auditActionLabel(code), code).not.toBe(code);
    }
  });

  it("라벨은 서로 다르다(같은 라벨로 서로 다른 작업을 가리지 않는다)", () => {
    const labels = Object.values(AUDIT_ACTION_LABEL);
    expect(new Set(labels).size).toBe(labels.length);
  });
});

describe("AdminAuditPage 좁은 화면 표 규칙 (plan §6.7, P5 리뷰 P5-04)", () => {
  it("감사 로그 표는 좁은 화면 규칙 래퍼(첫 열 고정·오른쪽 단서) 안에 있다", async () => {
    renderPage();
    await within(auditRegion()).findAllByRole("row");
    expect(within(auditRegion()).getByRole("table").closest('[data-slot="admin-table-frame"]')).not.toBeNull();
  });
});

describe("AdminAuditPage 다시 시도 (HCI-P2-2)", () => {
  it("못 불러온 뒤 「다시 시도」가 성공하면 표를 그리고 포커스는 감사 로그 영역 안(사용자 필터)에 남는다", async () => {
    renderPage({ audit: new Error("boom") });
    const retry = await within(auditRegion()).findByRole("button", { name: "다시 시도" });
    const rows = auditFixture();
    api.get.mockImplementation(async (path: string) => (path === AUDIT_PATH ? rows : []));
    await userEvent.setup().click(retry);
    await within(auditRegion()).findByRole("table");
    await waitFor(() =>
      expect(within(auditRegion()).getByRole("combobox", { name: "사용자 필터" })).toHaveFocus(),
    );
  });
});
