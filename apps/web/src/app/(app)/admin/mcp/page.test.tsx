/**
 * 관리자 MCP 서버 페이지 — ui-ux-redesign P5(AC-5.1·5.3·5.6, P-23).
 * 네트워크 경계(`api` 객체)와 토스트(sonner)만 대체하고 페이지·useCrudResource·다이얼로그·라벨 사전은 실제로 실행한다(§3.3-3).
 * 실제 MCP 서버를 켜거나 상태 확인하지 않는다 — 응답은 모두 더미다.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import AdminMCPPage from "@/app/(app)/admin/mcp/page";
import { APP_QUERY_DEFAULTS } from "@/lib/query-defaults";
import { stubNarrowViewport } from "@/hooks/use-narrow-viewport.test-kit";

const { toast } = vi.hoisted(() => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("sonner", () => ({ toast }));
const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));

const LIST = "/api/v1/admin/mcp-servers";

const server = (
  id: string,
  name: string,
  last_health_ok: boolean | null,
  extra: Record<string, unknown> = {},
) => ({
  id,
  name,
  transport: "stdio",
  endpoint_or_command: `python -m demo_${id}`,
  has_env: false,
  enabled: true,
  last_health_check: last_health_ok === null ? null : "2026-10-14T14:50:40Z",
  last_health_ok,
  ...extra,
});

const OK = server("m1", "demo-ok", true);
const BAD = server("m2", "demo-bad", false, { transport: "sse", endpoint_or_command: "https://mcp.example.com/sse" });
const NEW = server("m3", "demo-new", null, { transport: "streamable_http", enabled: false });

function renderPage({ servers = [OK, BAD, NEW] as unknown[], error = false } = {}) {
  api.get.mockImplementation(async (path: string) => {
    if (path === LIST) {
      if (error) throw new Error("boom");
      return servers;
    }
    throw new Error(`unexpected GET ${path}`);
  });
  const client = new QueryClient({ defaultOptions: { queries: { ...APP_QUERY_DEFAULTS, retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AdminMCPPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
  toast.success.mockReset();
  toast.error.mockReset();
});

const rowOf = (name: string) => screen.getByText(name).closest("tr")!;

describe("AdminMCPPage 헤더 (AC-5.1)", () => {
  it("브레드크럼 「관리 / MCP 서버」, h1 하나, 주 동작 「MCP 서버 추가」(Plus + 글자) 하나", async () => {
    renderPage();
    await screen.findByText("demo-ok");
    const crumb = screen.getByRole("navigation", { name: "위치" });
    expect(within(crumb).getByRole("link", { name: "관리" })).toHaveAttribute("href", "/admin");
    expect(within(crumb).getByText("MCP 서버")).toHaveAttribute("aria-current", "page");
    expect(screen.getAllByRole("heading", { level: 1 }).map((h) => h.textContent)).toEqual(["MCP 서버"]);
    // 「+ 텍스트」 금지(§6.3.3) — 아이콘 + 「MCP 서버 추가」
    const add = screen.getByRole("button", { name: "MCP 서버 추가" });
    expect(add.querySelector("svg")).not.toBeNull();
    expect(screen.queryByRole("button", { name: /^\+/ })).not.toBeInTheDocument();
  });
});

describe("AdminMCPPage 표 (AC-5.3 · AC-5.6, P-23)", () => {
  it("머리글은 한국어(Transport 없음)이고 상태는 글자 있는 StatusBadge(이모지 0)", async () => {
    const { container } = renderPage();
    await screen.findByText("demo-ok");
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "이름",
      "전송 방식",
      "엔드포인트/명령",
      "상태",
      "활성",
      "동작",
    ]);
    expect(screen.queryByText("Transport")).not.toBeInTheDocument();
    expect(container.textContent).not.toMatch(/🟢|🔴/);

    const ok = within(rowOf("demo-ok")).getByText("정상");
    expect(ok).toHaveAttribute("data-slot", "status-badge");
    expect(ok).toHaveAttribute("data-tone", "success");
    expect(within(rowOf("demo-bad")).getByText("오류")).toHaveAttribute("data-tone", "danger");
    expect(within(rowOf("demo-new")).getByText("미확인")).toHaveAttribute("data-tone", "neutral");
    // 마지막 확인 시각은 lib/format으로(「—」 대신 미확인 행에는 없음)
    expect(within(rowOf("demo-ok")).getByText(/2026\. 10\. 14\..*확인$/)).toBeInTheDocument();
    expect(within(rowOf("demo-new")).queryByText(/\d 확인$/)).not.toBeInTheDocument();
  });

  it("전송 방식은 화면 사전으로 보인다(stdio·SSE·Streamable HTTP)", async () => {
    renderPage();
    await screen.findByText("demo-ok");
    expect(within(rowOf("demo-ok")).getByText("stdio(명령 실행)")).toBeInTheDocument();
    expect(within(rowOf("demo-bad")).getByText("SSE")).toBeInTheDocument();
    expect(within(rowOf("demo-new")).getByText("Streamable HTTP")).toBeInTheDocument();
    expect(screen.queryByText("streamable_http")).not.toBeInTheDocument();
  });

  it("행 스위치는 「{이름} 활성」 이름을 갖고 enabled를 PATCH한다", async () => {
    const user = userEvent.setup();
    api.patch.mockResolvedValue({});
    renderPage();
    const sw = await screen.findByRole("switch", { name: "demo-new 활성" });
    expect(sw).not.toBeChecked();
    expect(screen.getByRole("switch", { name: "demo-ok 활성" })).toBeChecked();
    await user.click(sw);
    await waitFor(() => expect(api.patch).toHaveBeenCalledWith(`${LIST}/m3`, { enabled: true }));
  });

  it("활성 전환 실패는 토스트 한 번(「…하지 못했습니다」)", async () => {
    const user = userEvent.setup();
    api.patch.mockRejectedValue(new Error("boom"));
    renderPage();
    await user.click(await screen.findByRole("switch", { name: "demo-ok 활성" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(1));
    expect(toast.error.mock.calls[0][0]).toBe("‘demo-ok’ 서버를 비활성화하지 못했습니다");
  });

  it("목록이 비면 표·페이지네이션 대신 EmptyState", async () => {
    renderPage({ servers: [] });
    expect(await screen.findByText("아직 MCP 서버가 없습니다")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("페이지당 개수")).not.toBeInTheDocument();
  });

  it("목록을 못 불러오면 ErrorState(「다시 시도」), 성공하면 포커스가 브레드크럼이 아니라 표 안으로 간다", async () => {
    renderPage({ error: true });
    expect(await screen.findByText("MCP 서버 목록을 불러오지 못했습니다")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "다시 시도" })).toBeInTheDocument();
    api.get.mockImplementation(async (path: string) => (path === LIST ? [OK, BAD, NEW] : []));
    await userEvent.setup().click(screen.getByRole("button", { name: "다시 시도" }));
    await screen.findByText("demo-ok");
    // 성공하면 포커스는 페이지 맨 위(브레드크럼)가 아니라 다시 그려진 표 안으로 간다(HCI-P2-2)
    await waitFor(() => expect(screen.getByRole("switch", { name: "demo-ok 활성" })).toHaveFocus());
  });
});

describe("AdminMCPPage 상태 확인 (P-23 동작)", () => {
  it("「상태 확인」은 health-check를 POST하고, 성공하면 도구 목록 다이얼로그와 성공 토스트", async () => {
    const user = userEvent.setup();
    api.post.mockResolvedValue({
      ok: true,
      tools: [{ name: "echo", description: "에코", spec: { status: "ok", reason: null } }],
    });
    renderPage();
    await screen.findByText("demo-ok");
    await user.click(within(rowOf("demo-ok")).getByRole("button", { name: "상태 확인" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith(`${LIST}/m1/health-check`));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("제공 도구 — demo-ok");
    expect(within(dialog).getByText("echo")).toBeInTheDocument();
    expect(toast.success).toHaveBeenCalledWith("‘demo-ok’ 서버 상태를 확인했습니다", {
      id: undefined,
      duration: 4000,
      description: "정상 — 도구 1개",
    });
  });

  it("연결 실패 응답(ok=false)은 다이얼로그 없이 실패 토스트(서버 사유는 설명 둘째 줄)", async () => {
    const user = userEvent.setup();
    api.post.mockResolvedValue({ ok: false, error: "connection refused" });
    renderPage();
    await screen.findByText("demo-bad");
    await user.click(within(rowOf("demo-bad")).getByRole("button", { name: "상태 확인" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(1));
    const [title, opts] = toast.error.mock.calls[0];
    expect(title).toBe("‘demo-bad’ 서버에 연결하지 못했습니다");
    expect((opts as { description: string }).description).toContain("connection refused");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("확인 중에는 모든 행의 「상태 확인」이 비활성이고 그 행은 「확인 중…」", async () => {
    const user = userEvent.setup();
    let resolve!: (v: unknown) => void;
    api.post.mockReturnValue(new Promise((r) => (resolve = r)));
    renderPage();
    await screen.findByText("demo-ok");
    await user.click(within(rowOf("demo-ok")).getByRole("button", { name: "상태 확인" }));
    expect(await within(rowOf("demo-ok")).findByRole("button", { name: "확인 중…" })).toBeDisabled();
    expect(within(rowOf("demo-bad")).getByRole("button", { name: "상태 확인" })).toBeDisabled();
    expect(within(rowOf("demo-new")).getByRole("button", { name: "상태 확인" })).toBeDisabled();
    await act(async () => resolve({ ok: true, tools: [] }));
    expect(await screen.findByText("이 서버가 제공하는 도구가 없습니다.")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(within(rowOf("demo-bad")).getByRole("button", { name: "상태 확인" })).toBeEnabled();
    expect(within(rowOf("demo-ok")).getByRole("button", { name: "상태 확인" })).toBeEnabled();
  });
});

describe("AdminMCPPage 상태 확인 모달 — 정화 설명·노출 판정 배지 (core-v0.20.0-upgrade AC-2.6, 계획 §5.7)", () => {
  /** 계획 §5.7 문구 그대로 — 사전(`tool-spec-labels.ts`)이 아니라 계획에서 옮겨 적어 화면 문구를 고정한다. */
  const PLAN_REASON_LABELS = {
    schema_not_object: "입력 스키마가 객체가 아닙니다",
    schema_too_deep: "입력 스키마가 너무 깊습니다",
    schema_too_large: "입력 스키마가 너무 큽니다",
    schema_structural_text: "입력 스키마에 허용되지 않는 글자가 있습니다",
    schema_unsupported_value: "입력 스키마에 지원하지 않는 값이 있습니다",
    schema_expansion: "입력 스키마를 펼치면 너무 큽니다", // core-v0.22.0-upgrade AC-7
  } as const;
  const SANITIZED = "검색합니다 <untrusted_data> 위조 태그는 글자 그대로";

  async function openToolsDialog(tools: unknown[]) {
    const user = userEvent.setup();
    // `MCPHealthCheckOut` 모양 — 원문 객체(input_schema·annotations)는 응답에 없다
    api.post.mockResolvedValue({ ok: true, tools, error: null });
    renderPage();
    await screen.findByText("demo-ok");
    await user.click(within(rowOf("demo-ok")).getByRole("button", { name: "상태 확인" }));
    return screen.findByRole("dialog");
  }

  // 노출 판정 배지만 — 승인 고정 상태 배지(`mcp-tool-approval`)는 아래 core-v0.22.0 묶음이 본다
  const badgesOf = (dialog: HTMLElement, name: string) =>
    Array.from(
      within(dialog)
        .getByTestId(`mcp-tool-${name}`)
        .querySelectorAll('[data-slot="status-badge"]:not([data-testid="mcp-tool-approval"])'),
    ).map((b) => [b.textContent, b.getAttribute("data-tone")]);

  it("승인 고정 상태 4종 배지와 재승인용 스펙 해시(core-v0.22.0-upgrade AC-3, 코어 v0.21.0 #293)", async () => {
    const hash = "a".repeat(64);
    const dialog = await openToolsDialog([
      { name: "pinned", description: "", spec: { status: "ok", reason: null }, spec_hash: hash, approval: "approved" },
      { name: "legacy", description: "", spec: { status: "ok", reason: null }, spec_hash: hash, approval: "unpinned" },
      { name: "drift", description: "", spec: { status: "ok", reason: null }, spec_hash: hash, approval: "changed" },
      {
        name: "denied",
        description: "",
        spec: { status: "rejected", reason: "schema_expansion" },
        spec_hash: null,
        approval: "not_approved",
      },
      // 구 API 응답(필드 없음)은 「승인 없음」
      { name: "old_api", description: "", spec: { status: "ok", reason: null } },
    ]);
    const approvalOf = (name: string) => {
      const badge = within(within(dialog).getByTestId(`mcp-tool-${name}`)).getByTestId("mcp-tool-approval");
      return [badge.textContent, badge.getAttribute("data-tone")];
    };
    expect(approvalOf("pinned")).toEqual(["승인됨", "success"]);
    expect(approvalOf("legacy")).toEqual(["미고정 — 재승인 필요", "warning"]);
    expect(approvalOf("drift")).toEqual(["승인 뒤 변경됨 — 재승인 필요", "warning"]);
    expect(approvalOf("denied")).toEqual(["승인 없음", "neutral"]);
    expect(approvalOf("old_api")).toEqual(["승인 없음", "neutral"]);
    expect(within(within(dialog).getByTestId("mcp-tool-legacy")).getByTestId("mcp-tool-spec-hash")).toHaveTextContent(hash);
    expect(within(within(dialog).getByTestId("mcp-tool-denied")).queryByTestId("mcp-tool-spec-hash")).toBeNull();
    expect(badgesOf(dialog, "denied")).toEqual([["노출 불가: 입력 스키마를 펼치면 너무 큽니다", "danger"]]);
  });

  it("ok는 정화 설명만(배지 없음), sanitized는 「설명 정화됨」 배지와 정화 설명을 그린다", async () => {
    const dialog = await openToolsDialog([
      { name: "word_count", description: "단어 수를 셉니다", spec: { status: "ok", reason: null } },
      { name: "search", description: SANITIZED, spec: { status: "sanitized", reason: null } },
    ]);
    const ok = within(dialog).getByTestId("mcp-tool-word_count");
    expect(within(ok).getByTestId("mcp-tool-description")).toHaveTextContent("단어 수를 셉니다");
    expect(badgesOf(dialog, "word_count")).toEqual([]);
    expect(ok).toHaveAttribute("data-spec-status", "ok");

    const sanitized = within(dialog).getByTestId("mcp-tool-search");
    expect(badgesOf(dialog, "search")).toEqual([["설명 정화됨", "warning"]]);
    // 정화된 설명도 외부 문자열 — 태그 모양은 텍스트 그대로(요소로 만들지 않는다)
    expect(within(sanitized).getByTestId("mcp-tool-description")).toHaveTextContent(SANITIZED);
    expect(sanitized.querySelector("untrusted_data")).toBeNull();
    expect(dialog).toHaveTextContent("이 서버가 제공하는 도구 2개입니다.");
  });

  it("rejected는 사유마다 「노출 불가: <사유 라벨>」(danger) 배지이고 설명 줄이 없다", async () => {
    const reasons = Object.keys(PLAN_REASON_LABELS) as (keyof typeof PLAN_REASON_LABELS)[];
    const dialog = await openToolsDialog(
      reasons.map((reason) => ({ name: `t_${reason}`, description: "", spec: { status: "rejected", reason } })),
    );
    for (const reason of reasons) {
      expect(badgesOf(dialog, `t_${reason}`)).toEqual([[`노출 불가: ${PLAN_REASON_LABELS[reason]}`, "danger"]]);
      const item = within(dialog).getByTestId(`mcp-tool-t_${reason}`);
      expect(within(item).queryByTestId("mcp-tool-description")).toBeNull();
      // 사유 코드 원문은 이름 칸(t_<사유>) 밖에 보이지 않는다
      expect(item.textContent?.replace(`t_${reason}`, "")).not.toContain(reason);
    }
  });

  it("세 판정이 한 응답에 섞여도 도구마다 따로 그리고, 모르는 사유는 원문·사유 없음은 「노출 불가」만 보인다", async () => {
    const dialog = await openToolsDialog([
      { name: "ok_tool", description: "정상", spec: { status: "ok", reason: null } },
      { name: "deep", description: "", spec: { status: "rejected", reason: "schema_too_deep" } },
      { name: "clean", description: "정화됨", spec: { status: "sanitized", reason: null } },
      // 배포 순서가 어긋나 웹이 모르는 사유(생성 타입 밖) — 원문으로 보인다
      { name: "future", description: "", spec: { status: "rejected", reason: "schema_future_rule" } },
      { name: "no_reason", description: "", spec: { status: "rejected", reason: null } },
    ]);
    expect(within(dialog).getAllByRole("listitem")).toHaveLength(5);
    expect(badgesOf(dialog, "ok_tool")).toEqual([]);
    expect(badgesOf(dialog, "deep")).toEqual([["노출 불가: 입력 스키마가 너무 깊습니다", "danger"]]);
    expect(badgesOf(dialog, "clean")).toEqual([["설명 정화됨", "warning"]]);
    expect(badgesOf(dialog, "future")).toEqual([["노출 불가: schema_future_rule", "danger"]]);
    expect(badgesOf(dialog, "no_reason")).toEqual([["노출 불가", "danger"]]);
    expect(toast.success).toHaveBeenCalledWith("‘demo-ok’ 서버 상태를 확인했습니다", {
      id: undefined,
      duration: 4000,
      description: "정상 — 도구 5개",
    });
  });

  it("128자로 잘려 이름이 겹친 도구도 둘 다 그린다(키 충돌 없음)", async () => {
    const name = "x".repeat(128);
    const dialog = await openToolsDialog([
      { name, description: "첫째", spec: { status: "ok", reason: null } },
      { name, description: "둘째", spec: { status: "sanitized", reason: null } },
    ]);
    const items = within(dialog).getAllByTestId(`mcp-tool-${name}`);
    expect(items.map((i) => i.getAttribute("data-spec-status"))).toEqual(["ok", "sanitized"]);
    expect(dialog).toHaveTextContent("첫째");
    expect(dialog).toHaveTextContent("둘째");
  });
});

describe("AdminMCPPage 추가 다이얼로그 (X-04b, §6.3.3)", () => {
  it("이름·전송 방식·실행 명령을 입력해 POST하고, 성공하면 닫고 토스트를 띄운다", async () => {
    const user = userEvent.setup();
    api.post.mockResolvedValue(server("m9", "새 서버", null));
    renderPage();
    await screen.findByText("demo-ok");
    await user.click(screen.getByRole("button", { name: "MCP 서버 추가" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "MCP 서버 추가" })).toBeInTheDocument();
    expect(within(dialog).queryByText("Transport")).not.toBeInTheDocument();
    expect(within(dialog).getByRole("combobox", { name: "전송 방식" })).toHaveTextContent("stdio(명령 실행)");

    await user.type(within(dialog).getByLabelText("이름"), "새 서버");
    await user.type(within(dialog).getByLabelText("실행 명령"), "python -m x");
    await user.click(within(dialog).getByRole("button", { name: "추가" }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith(LIST, {
        name: "새 서버",
        transport: "stdio",
        endpoint_or_command: "python -m x",
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(toast.success).toHaveBeenCalledWith("MCP 서버를 추가했습니다");
  });

  it("전송 방식을 SSE로 바꾸면 입력 이름이 「엔드포인트 URL」로 바뀐다", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("demo-ok");
    await user.click(screen.getByRole("button", { name: "MCP 서버 추가" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("combobox", { name: "전송 방식" }));
    await user.click(await screen.findByRole("option", { name: "SSE" }));
    expect(within(dialog).getByLabelText("엔드포인트 URL")).toBeInTheDocument();
  });

  it("추가에 실패하면 다이얼로그를 유지하고 안내 문구 + 실패 토스트 한 번", async () => {
    const user = userEvent.setup();
    api.post.mockRejectedValue(new Error("boom"));
    renderPage();
    await screen.findByText("demo-ok");
    await user.click(screen.getByRole("button", { name: "MCP 서버 추가" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("이름"), "x");
    await user.type(within(dialog).getByLabelText("실행 명령"), "y");
    await user.click(within(dialog).getByRole("button", { name: "추가" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(1));
    expect(toast.error.mock.calls[0][0]).toBe("MCP 서버를 추가하지 못했습니다");
    expect(screen.getByRole("dialog")).toHaveTextContent("MCP 서버를 추가하지 못했습니다. 입력값을 확인해 주세요.");
  });

  it("다이얼로그를 Esc로 닫으면 포커스가 「MCP 서버 추가」로 돌아오고, 다시 열면 입력이 비어 있다", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("demo-ok");
    const add = screen.getByRole("button", { name: "MCP 서버 추가" });
    await user.click(add);
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("이름"), "남은 값");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(add).toHaveFocus());
    await user.click(add);
    expect(within(await screen.findByRole("dialog")).getByLabelText("이름")).toHaveValue("");
  });
});

describe("AdminMCPPage 좁은 화면 표 규칙 (plan §6.7, P5 리뷰 P5-04)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("표는 좁은 화면 규칙 래퍼(첫 열 고정·오른쪽 단서) 안에 있다", async () => {
    renderPage();
    const table = (await screen.findByText("demo-ok")).closest("table")!;
    expect(table.closest('[data-slot="admin-table-frame"]')).not.toBeNull();
  });

  it("< 768: 「상태 확인」은 ⋯ 메뉴의 항목이 되고, 고르면 health-check를 POST한다", async () => {
    stubNarrowViewport();
    const user = userEvent.setup();
    api.post.mockResolvedValue({ ok: true, tools: [] });
    renderPage();
    await screen.findByText("demo-ok");
    await waitFor(() => expect(within(rowOf("demo-ok")).queryByRole("button", { name: "상태 확인" })).toBeNull());
    await user.click(within(rowOf("demo-ok")).getByRole("button", { name: "demo-ok 동작 더보기" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["상태 확인"]);
    await user.click(within(menu).getByRole("menuitem", { name: "상태 확인" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith(`${LIST}/m1/health-check`));
  });
});
