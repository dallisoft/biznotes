/**
 * 실행 상세 「실행 로그」 탭의 노드 행 — 실패 노드에만 코드별 공개 문구와 `error_message`
 * (core-v0.20.0-upgrade AC-2.3 화면, 계획 §5.5).
 * API 경계(`api` 객체)만 대체하고 RunDetailView·조회 훅·문구 사전(`lib/node-error-labels`)은 실제로 실행한다(§3.3-3).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentRun, ExecutionLog } from "@/lib/agent-types";
import { RunDetailView } from "./run-detail-view";

const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), getText: vi.fn(), post: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
// run-detail-view → artifact-content → mindmap-viewer가 불러오는 @xyflow 스타일시트는 Vitest PostCSS 변환과
// 충돌한다 — 스타일시트만 비운다(run-detail-view.test.ts·agent-panel.test.tsx와 같은 처리, 로직 mock 아님).
vi.mock("@xyflow/react/dist/style.css", () => ({}));

const RUN = {
  id: "run-1",
  agent_id: "ag-1",
  notebook_id: "nb-1",
  trigger: "manual",
  status: "failed",
  progress: 100,
  result_summary: null,
  artifact_keys: [],
  token_usage: {},
  error: "노드 실행에 실패했습니다",
  created_at: "2026-10-20T00:00:00Z",
  started_at: null,
  finished_at: null,
  agent_name: "로그 확인",
  agent_icon: "🧪",
} as unknown as AgentRun;

function log(overrides: Partial<ExecutionLog> & Pick<ExecutionLog, "id">): ExecutionLog {
  return {
    run_id: "run-1",
    node_id: overrides.id,
    node_type: "llm",
    status: "failed",
    error_message: null,
    error_code: null,
    token_usage: null,
    cost_usd: null,
    duration_ms: 12,
    started_at: null,
    finished_at: null,
    model: null,
    fallback_used: null,
    model_attempts: null,
    ...overrides,
  };
}

/** 계획 §5.5 문구 그대로 — 사전(`lib/node-error-labels.ts`)이 아니라 계획에서 옮겨 적어 화면 문구를 고정한다. */
const PLAN_LABELS: Record<string, string> = {
  rerank_unavailable: "리랭커를 사용할 수 없어 검색을 진행하지 못했습니다",
  model_call_failed: "모델 호출에 실패했습니다",
  gateway_error: "모델 설정이나 입력을 확인해야 합니다",
  ssrf_blocked: "내부 주소로의 요청이 차단되었습니다",
  tool_unavailable: "도구를 사용할 수 없습니다",
  usage_limit_exceeded: "사용량 한도를 넘었습니다",
};
const FALLBACK = "노드 실행에 실패했습니다";

async function openLogs(logs: ExecutionLog[]) {
  api.get.mockImplementation((path: string) => {
    if (path === "/api/v1/agent-runs/run-1/logs") return Promise.resolve(logs);
    if (path === "/api/v1/agent-runs/run-1/approvals") return Promise.resolve([]);
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={client}>
      <RunDetailView run={RUN} notebookId="nb-1" canWrite onBack={() => {}} />
    </QueryClientProvider>,
  );
  const view = await screen.findByTestId("run-detail-view");
  await user.click(within(view).getByRole("tab", { name: "실행 로그" }));
  // 첫 행이 그려질 때까지 기다린다(「없음」 단언은 대상이 렌더된 뒤에만)
  await within(view).findByTestId(`execution-log-${logs[0].id}`);
  return view;
}

const row = (view: HTMLElement, id: string) => within(view).getByTestId(`execution-log-${id}`);
const labelOf = (view: HTMLElement, id: string) =>
  within(row(view, id)).queryByTestId("node-error-label");
const messageOf = (view: HTMLElement, id: string) =>
  within(row(view, id)).queryByTestId("node-error-message");

beforeEach(() => {
  api.get.mockReset();
  api.getText.mockReset();
  api.post.mockReset();
});

describe("RunDetailView 실행 로그 — 실패 노드 문구 (AC-2.3, 계획 §5.5)", () => {
  it("알려진 코드 6종은 실패 행에 계획 §5.5 문구와 error_message를 보인다", async () => {
    const codes = Object.keys(PLAN_LABELS);
    const view = await openLogs(
      codes.map((code, i) =>
        log({ id: `log-${code}`, node_id: `node${i}`, error_code: code, error_message: `노드 [node${i}] 실행 실패` }),
      ),
    );
    for (const [i, code] of codes.entries()) {
      expect(labelOf(view, `log-${code}`)).toHaveTextContent(PLAN_LABELS[code]);
      expect(messageOf(view, `log-${code}`)).toHaveTextContent(`노드 [node${i}] 실행 실패`);
      // 코드 원문은 화면에 내지 않는다
      expect(row(view, `log-${code}`)).not.toHaveTextContent(code);
    }
  });

  it("모르는 코드·null 코드는 일반 문구 「노드 실행에 실패했습니다」, error_message가 없으면 그 줄이 없다", async () => {
    const view = await openLogs([
      log({ id: "log-future", node_id: "future", error_code: "future_node_error", error_message: "새 부류의 오류" }),
      log({ id: "log-null", node_id: "legacy", error_code: null, error_message: null }),
      log({ id: "log-proto", node_id: "proto", error_code: "constructor", error_message: "x" }),
    ]);
    expect(labelOf(view, "log-future")).toHaveTextContent(FALLBACK);
    expect(messageOf(view, "log-future")).toHaveTextContent("새 부류의 오류");
    expect(row(view, "log-future")).not.toHaveTextContent("future_node_error");
    expect(labelOf(view, "log-null")).toHaveTextContent(FALLBACK);
    expect(messageOf(view, "log-null")).toBeNull();
    // 프로토타입 키가 문구로 새지 않는다(자체 속성만 본다)
    expect(labelOf(view, "log-proto")).toHaveTextContent(FALLBACK);
  });

  it("성공·실행 중·대기 행에는 문구와 error_message가 없다 — 코드·메시지가 실려 와도 상태로 가른다", async () => {
    const view = await openLogs([
      log({ id: "log-ok", node_id: "search", status: "succeeded" }),
      // 상태가 실패가 아니면 코드·메시지가 있어도 보이지 않는다(문구는 실패 노드에만)
      log({ id: "log-ok-coded", node_id: "retry", status: "succeeded", error_code: "model_call_failed", error_message: "재시도 전 오류" }),
      log({ id: "log-running", node_id: "summary", status: "running", duration_ms: null }),
      log({ id: "log-queued", node_id: "wait", status: "queued", duration_ms: null }),
      log({ id: "log-failed", node_id: "fetch", node_type: "http", error_code: "ssrf_blocked" }),
    ]);
    for (const id of ["log-ok", "log-ok-coded", "log-running", "log-queued"]) {
      expect(row(view, id)).toBeInTheDocument();
      expect(labelOf(view, id)).toBeNull();
      expect(messageOf(view, id)).toBeNull();
    }
    expect(row(view, "log-ok-coded")).not.toHaveTextContent("모델 호출에 실패했습니다");
    expect(row(view, "log-ok-coded")).not.toHaveTextContent("재시도 전 오류");
    // 같은 목록의 실패 행은 문구를 보인다(부재 단언이 빈 화면이 아님을 보인다)
    expect(labelOf(view, "log-failed")).toHaveTextContent(PLAN_LABELS.ssrf_blocked);
    expect(within(view).getAllByTestId("node-error-label")).toHaveLength(1);
  });

  it("react_agent 도구 호출 보조행이 실패해도 같은 규칙으로 문구를 보인다", async () => {
    const view = await openLogs([
      log({ id: "log-react", node_id: "agent", node_type: "react_agent", status: "failed", error_code: "tool_unavailable" }),
      log({ id: "log-tool", node_id: "agent:tool:0", node_type: "react_tool_call", status: "failed", error_code: "tool_unavailable", error_message: "도구 호출 실패" }),
      log({ id: "log-tool-ok", node_id: "agent:tool:1", node_type: "react_tool_call", status: "succeeded" }),
    ]);
    expect(labelOf(view, "log-tool")).toHaveTextContent(PLAN_LABELS.tool_unavailable);
    expect(messageOf(view, "log-tool")).toHaveTextContent("도구 호출 실패");
    expect(row(view, "log-tool").className).toMatch(/\bpl-6\b/);
    expect(labelOf(view, "log-tool-ok")).toBeNull();
  });

  it("error_message는 외부 문자열 — 태그 모양도 텍스트로만 그린다", async () => {
    const view = await openLogs([
      log({ id: "log-html", node_id: "fetch", error_code: "ssrf_blocked", error_message: "<b>굵게</b> 그대로" }),
    ]);
    const message = messageOf(view, "log-html")!;
    expect(message).toHaveTextContent("<b>굵게</b> 그대로");
    expect(message.querySelector("b")).toBeNull();
  });
});

describe("RunDetailView 실행 로그 — 사용 모델·폴백·시도한 모델 (#216, core-v0.25.0-upgrade AC-14)", () => {
  const CHOSEN = { id: "m-chosen", display_name: "선택 모델" };
  const DEFAULT = { id: "m-default", display_name: "기본 모델 A" };
  const modelOf = (view: HTMLElement, id: string) => within(row(view, id)).queryByTestId("node-model");

  it("응답한 모델이 있는 행은 모델 이름을, 폴백 모델이 답한 행은 「폴백」 배지를 함께 보인다", async () => {
    const view = await openLogs([
      log({ id: "log-gen", node_id: "gen", status: "succeeded", model: CHOSEN, fallback_used: false }),
      log({ id: "log-sum", node_id: "sum", status: "succeeded", model: DEFAULT, fallback_used: true }),
      log({ id: "log-search", node_id: "search", node_type: "rag_search", status: "succeeded" }),
    ]);
    expect(modelOf(view, "log-gen")).toHaveTextContent("선택 모델");
    expect(within(row(view, "log-gen")).queryByTestId("node-fallback")).toBeNull();
    expect(modelOf(view, "log-sum")).toHaveTextContent("기본 모델 A");
    expect(within(row(view, "log-sum")).getByTestId("node-fallback")).toHaveTextContent("폴백");
    // 모델이 없는 노드(검색 등)에는 모델 줄이 없다
    expect(modelOf(view, "log-search")).toBeNull();
    // 머리 요약 — 응답한 모델(중복 없이 처음 나온 순서)과 폴백 여부. 결과 탭에서도 보인다
    expect(within(view).getByTestId("run-model-summary")).toHaveTextContent(
      "사용 모델 선택 모델, 기본 모델 A (폴백)",
    );
  });

  it("모든 후보가 실패한 행은 시도한 모델과 각 실패 문구를 순서대로 보인다 — 코드 원문은 내지 않는다", async () => {
    const view = await openLogs([
      log({
        id: "log-gen",
        node_id: "gen",
        error_code: "model_call_failed",
        error_message: "모델 호출에 실패했습니다",
        model_attempts: [
          { ...CHOSEN, error_code: "context_length_exceeded" },
          { ...DEFAULT, error_code: "circuit_open" },
          { id: "m-x", display_name: "모델 X", error_code: "future_code" },
        ],
      }),
    ]);
    const attempts = within(row(view, "log-gen")).getByTestId("node-model-attempts");
    const items = within(attempts).getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual([
      "선택 모델— 입력이 모델 컨텍스트 한도를 넘었습니다",
      "기본 모델 A— 연속 실패로 잠시 건너뛰었습니다",
      "모델 X— 노드 실행에 실패했습니다",
    ]);
    expect(attempts).not.toHaveTextContent("circuit_open");
    expect(attempts).not.toHaveTextContent("future_code");
    expect(within(view).queryByTestId("run-model-summary")).toBeNull();
  });

  it("시도한 모델은 실패 행에만 보이고, 모델 이름은 외부 문자열이라 텍스트로만 그린다", async () => {
    const view = await openLogs([
      log({
        id: "log-ok",
        node_id: "ok",
        status: "succeeded",
        model: { id: "m-html", display_name: "<i>모델</i>" },
        fallback_used: false,
        model_attempts: [{ ...CHOSEN, error_code: "model_call_failed" }],
      }),
    ]);
    expect(within(row(view, "log-ok")).queryByTestId("node-model-attempts")).toBeNull();
    expect(modelOf(view, "log-ok")).toHaveTextContent("<i>모델</i>");
    expect(modelOf(view, "log-ok")!.querySelector("i")).toBeNull();
  });
});
