/**
 * 실패한 실행 숨기기(#220) — 항목 케밥 「숨기기」·「되돌리기」 토스트·「실패한 결과 모두 숨기기」, 숨긴 결과 보기(#221).
 * 실제 AgentPanel·훅·sonner Toaster로 돌리고 API 경계만 대역한다. 숨김 인가(`can_hide`)는 서버가 판정한다.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast, Toaster } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api-client";
import type { AgentRun } from "@/lib/agent-types";
import { AgentPanel } from "./agent-panel";
import { bulkHideableRuns } from "./run-hide-actions";

const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), getText: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/notebooks/nb-1",
  useSearchParams: () => new URLSearchParams(),
}));
// agent-panel → mindmap-viewer의 @xyflow 스타일시트만 비운다(agent-panel.test.tsx와 같은 처리, 로직 mock 아님)
vi.mock("@xyflow/react/dist/style.css", () => ({}));

const RUNS_PATH = "/api/v1/notebooks/nb-1/agent-runs";

function run(overrides: Partial<AgentRun>): AgentRun {
  return {
    id: "run-x",
    agent_id: "ag-1",
    notebook_id: "nb-1",
    params: {},
    trigger: "manual",
    status: "failed",
    progress: 0,
    result_summary: null,
    artifact_keys: [],
    token_usage: {},
    error: "LLM 오류",
    created_at: new Date().toISOString(),
    started_at: null,
    finished_at: null,
    agent_name: "보고서 생성",
    agent_icon: "🧩",
    can_cancel: true,
    can_hide: true,
    hidden: false,
    ...overrides,
  } as AgentRun;
}

const HIDDEN_PATH = `${RUNS_PATH}?hidden=true`;

/**
 * 서버 대역 — 목록은 숨기지 않은 run만, `?hidden=true`는 숨긴 run만(`hidden: true`) 돌려주고,
 * hide·unhide·hide-failed가 숨김 상태를 바꾼다. `initiallyHidden`은 처음부터 숨겨 둔 run id다.
 */
function serve(runs: AgentRun[], initiallyHidden: string[] = []) {
  const hidden = new Set<string>(initiallyHidden);
  api.get.mockImplementation((path: string) => {
    if (path === RUNS_PATH) return Promise.resolve(runs.filter((r) => !hidden.has(r.id)));
    if (path === HIDDEN_PATH) {
      return Promise.resolve(runs.filter((r) => hidden.has(r.id)).map((r) => ({ ...r, hidden: true })));
    }
    return Promise.resolve([]);
  });
  api.post.mockImplementation((path: string) => {
    const m = /^\/api\/v1\/agent-runs\/([^/]+)\/(hide|unhide)$/.exec(path);
    if (m) {
      if (m[2] === "hide") hidden.add(m[1]);
      else hidden.delete(m[1]);
      return Promise.resolve(runs.find((r) => r.id === m[1]));
    }
    if (path === `${RUNS_PATH}/hide-failed`) {
      const ids = bulkHideableRuns(runs.filter((r) => !hidden.has(r.id))).map((r) => r.id);
      ids.forEach((id) => hidden.add(id));
      return Promise.resolve({ hidden: ids.length, run_ids: ids });
    }
    return Promise.reject(new Error(`unexpected POST ${path}`));
  });
  return hidden;
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AgentPanel notebookId="nb-1" canWrite />
      <Toaster />
    </QueryClientProvider>,
  );
}

function postedPaths(): string[] {
  return api.post.mock.calls.map(([path]) => path as string);
}

beforeEach(() => {
  api.get.mockReset();
  api.getText.mockReset();
  api.post.mockReset();
});

afterEach(() => {
  act(() => {
    toast.dismiss();
  });
});

describe("항목 케밥 「숨기기」 (#220 AC-1)", () => {
  it("실패 run을 숨기면 「최근 결과」에서 사라지고, 「되돌리기」를 누르면 다시 보인다(확인 대화상자 없음)", async () => {
    const user = userEvent.setup();
    serve([run({ id: "run-f" }), run({ id: "run-ok", status: "succeeded", error: null, can_hide: false })]);
    renderPanel();
    const item = await screen.findByTestId("run-item-run-f");

    await user.click(within(item).getByRole("button", { name: "보고서 생성 메뉴" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["숨기기"]);
    await user.click(within(menu).getByRole("menuitem", { name: "숨기기" }));

    await waitFor(() => expect(screen.queryByTestId("run-item-run-f")).toBeNull());
    expect(postedPaths()).toEqual(["/api/v1/agent-runs/run-f/hide"]);
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(await screen.findByText("결과를 숨겼습니다")).toBeInTheDocument();
    expect(screen.getByTestId("run-item-run-ok")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "되돌리기" }));

    expect(await screen.findByTestId("run-item-run-f")).toBeInTheDocument();
    expect(postedPaths()).toEqual([
      "/api/v1/agent-runs/run-f/hide",
      "/api/v1/agent-runs/run-f/unhide",
    ]);
  });

  it("취소한 run도 can_hide면 케밥 「숨기기」가 있다", async () => {
    serve([run({ id: "run-c", status: "cancelled", error: null })]);
    renderPanel();
    const item = await screen.findByTestId("run-item-run-c");
    expect(within(item).getByTestId("run-item-menu")).toBeInTheDocument();
  });

  it("숨길 권한이 없거나(can_hide=false) 성공한 run에는 케밥이 없다", async () => {
    serve([
      run({ id: "run-other", can_hide: false }),
      run({ id: "run-ok", status: "succeeded", error: null, can_hide: false }),
      run({ id: "run-mine" }),
    ]);
    renderPanel();
    // 「없음」 단언이 목록 응답 전에 공허하게 통과하지 않도록 케밥이 있는 목격자 항목을 먼저 기다린다
    expect(within(await screen.findByTestId("run-item-run-mine")).getByTestId("run-item-menu")).toBeInTheDocument();
    expect(within(screen.getByTestId("run-item-run-other")).queryByTestId("run-item-menu")).toBeNull();
    expect(within(screen.getByTestId("run-item-run-ok")).queryByTestId("run-item-menu")).toBeNull();
  });

  it("숨기기에 실패하면 실패 토스트를 보이고 항목은 그대로다", async () => {
    const user = userEvent.setup();
    serve([run({ id: "run-f" })]);
    api.post.mockRejectedValue(new ApiError(409, "진행 중인 실행은 숨길 수 없습니다"));
    renderPanel();
    const item = await screen.findByTestId("run-item-run-f");
    await user.click(within(item).getByTestId("run-item-menu"));
    await user.click(await screen.findByRole("menuitem", { name: "숨기기" }));

    expect(await screen.findByText("결과를 숨김 처리하지 못했습니다")).toBeInTheDocument();
    expect(screen.getByTestId("run-item-run-f")).toBeInTheDocument();
  });
});

describe("「실패한 결과 모두 숨기기」 (#220 AC-5)", () => {
  it("숨길 수 있는 실패 run이 2개 이상이면 보이고, 누르면 그 run들만 숨긴 뒤 「되돌리기」로 모두 되돌린다", async () => {
    const user = userEvent.setup();
    serve([
      run({ id: "run-f1" }),
      run({ id: "run-f2" }),
      run({ id: "run-other", can_hide: false }),
      run({ id: "run-ok", status: "succeeded", error: null, can_hide: false }),
    ]);
    renderPanel();
    const button = await screen.findByRole("button", { name: "실패한 결과 모두 숨기기" });

    await user.click(button);

    await waitFor(() => expect(screen.queryByTestId("run-item-run-f1")).toBeNull());
    expect(screen.queryByTestId("run-item-run-f2")).toBeNull();
    expect(screen.getByTestId("run-item-run-other")).toBeInTheDocument();
    expect(screen.getByTestId("run-item-run-ok")).toBeInTheDocument();
    expect(postedPaths()).toEqual([`${RUNS_PATH}/hide-failed`]);
    expect(await screen.findByText("실패한 결과 2개를 숨겼습니다")).toBeInTheDocument();
    // 숨길 수 있는 실패 run이 남지 않으면 버튼도 사라진다
    expect(screen.queryByRole("button", { name: "실패한 결과 모두 숨기기" })).toBeNull();

    await user.click(screen.getByRole("button", { name: "되돌리기" }));

    expect(await screen.findByTestId("run-item-run-f1")).toBeInTheDocument();
    expect(screen.getByTestId("run-item-run-f2")).toBeInTheDocument();
    expect(postedPaths().slice(1).sort()).toEqual([
      "/api/v1/agent-runs/run-f1/unhide",
      "/api/v1/agent-runs/run-f2/unhide",
    ]);
  });

  it("일괄 「되돌리기」는 요청이 모두 끝난 뒤 목록을 한 번 다시 읽는다 — 먼저 실패한 요청이 늦게 성공한 되돌리기를 가리지 않는다", async () => {
    const user = userEvent.setup();
    const hidden = serve([run({ id: "run-f1" }), run({ id: "run-f2" })]);
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "실패한 결과 모두 숨기기" }));
    await screen.findByText("실패한 결과 2개를 숨겼습니다");
    await waitFor(() => expect(screen.queryByTestId("run-item-run-f1")).toBeNull());

    // run-f1 되돌리기는 곧바로 403, run-f2는 늦게 성공한다(서버 상태는 응답 시점에 바뀐다)
    let releaseF2 = () => {};
    const serveDefault = api.post.getMockImplementation();
    api.post.mockImplementation((path: string) => {
      if (path === "/api/v1/agent-runs/run-f1/unhide") {
        return Promise.reject(new ApiError(403, "이 실행을 숨길 권한이 없습니다"));
      }
      if (path === "/api/v1/agent-runs/run-f2/unhide") {
        return new Promise((resolve) => {
          releaseF2 = () => {
            hidden.delete("run-f2");
            resolve(undefined);
          };
        });
      }
      return serveDefault?.(path);
    });
    const listGets = () => api.get.mock.calls.filter(([path]) => path === RUNS_PATH).length;
    const getsBeforeUndo = listGets();

    await user.click(screen.getByRole("button", { name: "되돌리기" }));
    await waitFor(() => expect(postedPaths()).toContain("/api/v1/agent-runs/run-f2/unhide"));
    // 먼저 실패한 요청만으로는 목록을 다시 읽지 않는다
    await act(() => new Promise((r) => setTimeout(r, 50)));
    expect(listGets()).toBe(getsBeforeUndo);
    act(() => releaseF2());

    expect(await screen.findByTestId("run-item-run-f2")).toBeInTheDocument();
    expect(screen.queryByTestId("run-item-run-f1")).toBeNull();
    expect(await screen.findByText("결과 1개의 숨김을 해제하지 못했습니다")).toBeInTheDocument();
    expect(listGets()).toBe(getsBeforeUndo + 1);
  });

  it("숨길 수 있는 실패 run이 1개면 보이지 않는다 — 권한 없는 실패·취소 run은 세지 않는다", async () => {
    serve([
      run({ id: "run-f1" }),
      run({ id: "run-other", can_hide: false }),
      run({ id: "run-c", status: "cancelled", error: null }),
    ]);
    renderPanel();
    await screen.findByTestId("run-item-run-f1");
    expect(screen.getByRole("heading", { name: "최근 결과" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "실패한 결과 모두 숨기기" })).toBeNull();
  });
});

describe("숨긴 결과 보기·되돌리기 (#221)", () => {
  it("숨긴 run이 있으면 「숨긴 결과 N개 보기」가 보이고, 펼치면 숨긴 항목만 보인다", async () => {
    const user = userEvent.setup();
    serve([run({ id: "run-h1" }), run({ id: "run-h2", status: "cancelled", error: null }), run({ id: "run-v" })], [
      "run-h1",
      "run-h2",
    ]);
    renderPanel();
    await screen.findByTestId("run-item-run-v");
    const toggle = await screen.findByRole("button", { name: "숨긴 결과 2개 보기" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("hidden-run-list")).toBeNull();

    await user.click(toggle);

    const list = screen.getByRole("list", { name: "숨긴 결과" });
    expect(within(list).getAllByRole("listitem").map((li) => li.dataset.testid)).toEqual([
      "hidden-run-item-run-h1",
      "hidden-run-item-run-h2",
    ]);
    expect(within(list).getByText("실패")).toBeInTheDocument();
    expect(within(list).getByText("취소됨")).toBeInTheDocument();
    // 숨긴 항목은 「최근 결과」에 섞이지 않는다
    expect(screen.queryByTestId("run-item-run-h1")).toBeNull();
    expect(screen.getByRole("button", { name: "숨긴 결과 접기" })).toHaveAttribute("aria-expanded", "true");
  });

  it("「다시 보이기」를 누르면 unhide 후 「최근 결과」로 돌아오고, 숨긴 결과가 없으면 토글이 사라진다", async () => {
    const user = userEvent.setup();
    serve([run({ id: "run-h" })], ["run-h"]);
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "숨긴 결과 1개 보기" }));

    await user.click(screen.getByRole("button", { name: "보고서 생성 다시 보이기" }));

    expect(await screen.findByTestId("run-item-run-h")).toBeInTheDocument();
    expect(postedPaths()).toEqual(["/api/v1/agent-runs/run-h/unhide"]);
    await waitFor(() => expect(screen.queryByTestId("hidden-run-toggle")).toBeNull());
  });

  it("되돌릴 권한이 없는 숨긴 run(can_hide=false)에는 「다시 보이기」가 없다", async () => {
    const user = userEvent.setup();
    serve([run({ id: "run-mine" }), run({ id: "run-other", can_hide: false })], ["run-mine", "run-other"]);
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "숨긴 결과 2개 보기" }));

    expect(
      within(screen.getByTestId("hidden-run-item-run-mine")).getByTestId("hidden-run-unhide"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("hidden-run-item-run-other")).queryByTestId("hidden-run-unhide"),
    ).toBeNull();
  });

  it("되돌리기에 실패하면 실패 토스트를 보이고 항목은 숨긴 결과에 남는다", async () => {
    const user = userEvent.setup();
    serve([run({ id: "run-h" })], ["run-h"]);
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "숨긴 결과 1개 보기" }));
    api.post.mockRejectedValue(new ApiError(403, "이 실행을 숨길 권한이 없습니다"));

    await user.click(screen.getByRole("button", { name: "보고서 생성 다시 보이기" }));

    expect(await screen.findByText("숨김을 해제하지 못했습니다")).toBeInTheDocument();
    expect(screen.getByTestId("hidden-run-item-run-h")).toBeInTheDocument();
    expect(screen.queryByTestId("run-item-run-h")).toBeNull();
  });

  it("항목 케밥으로 숨기면 숨긴 결과 수가 늘어난다", async () => {
    const user = userEvent.setup();
    serve([run({ id: "run-f" }), run({ id: "run-ok", status: "succeeded", error: null, can_hide: false })]);
    renderPanel();
    const item = await screen.findByTestId("run-item-run-f");
    expect(screen.queryByTestId("hidden-run-toggle")).toBeNull();

    await user.click(within(item).getByTestId("run-item-menu"));
    await user.click(await screen.findByRole("menuitem", { name: "숨기기" }));

    expect(await screen.findByRole("button", { name: "숨긴 결과 1개 보기" })).toBeInTheDocument();
  });
});
