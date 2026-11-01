import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentRun } from "@/lib/agent-types";
import { AgentPanel } from "./agent-panel";

// API 경계만 mock — 패널의 조회 훅(에이전트·스킬·실행)은 실제 코드로 돈다.
const { api } = vi.hoisted(() => ({
  api: {
    get: vi.fn(),
    getText: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
// 프레임워크 경계: App Router 대역(하위 컴포넌트의 useRouter용)
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/notebooks/nb-1",
  useSearchParams: () => new URLSearchParams(),
}));
// agent-panel → mindmap-viewer가 불러오는 @xyflow 스타일시트는 Vitest PostCSS 변환과 충돌한다 — 스타일시트만 비운다
// (mindmap-viewer.test.tsx·status-badges.test.tsx와 같은 처리, 로직 mock 아님).
vi.mock("@xyflow/react/dist/style.css", () => ({}));

beforeEach(() => {
  api.get.mockReset();
  api.get.mockResolvedValue([]);
  api.getText.mockReset();
  api.post.mockReset();
});

function renderPanel(canWrite = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AgentPanel notebookId="nb-1" canWrite={canWrite} />
    </QueryClientProvider>,
  );
}

describe("AgentPanel 유형 탭 — 기본 2개 + 고급(ui-ux-improvements ④)", () => {
  it("기본은 「스킬·워크플로우」 탭만 보이고, 「고급」을 펼치면 플러그인·도구·MCP가 나온다", async () => {
    const user = userEvent.setup();
    renderPanel();
    const tablist = await screen.findByRole("tablist");
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["스킬", "워크플로우"]);
    const advanced = screen.getByRole("button", { name: "고급" });
    expect(advanced).toHaveAttribute("aria-expanded", "false");

    await user.click(advanced);
    expect(advanced).toHaveAttribute("aria-expanded", "true");
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual([
      "스킬",
      "워크플로우",
      "플러그인",
      "도구",
      "MCP",
    ]);
    // 좁은 패널에서 라벨이 겹치지 않는다(P1 통합): grid가 아닌 가로 스크롤 줄, 12px 라벨
    expect(tablist.className).not.toMatch(/\bgrid\b|grid-cols-/);
    expect(tablist.className).toMatch(/\boverflow-x-auto\b/);
    for (const tab of screen.getAllByRole("tab")) {
      expect(tab.className).toMatch(/\bwhitespace-nowrap\b/);
      expect(tab.className).toMatch(/\btext-xs\b/);
    }
  });

  it("최상위 구역은 「만들기」와 「최근 결과」 두 개다", async () => {
    renderPanel();
    expect(await screen.findByRole("heading", { name: "만들기" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "최근 결과" })).toBeInTheDocument();
  });
});

describe("AgentPanel 최근 결과 — 날짜별 묶음(ui-ux-improvements ④)", () => {
  it("실행 결과를 날짜(오늘·어제·그 밖의 날짜)로 묶어 머리글을 단다", async () => {
    vi.useFakeTimers({ toFake: ["Date"], now: new Date("2026-10-22T03:00:00Z") });
    const base = {
      agent_id: "ag",
      notebook_id: "nb-1",
      params: {},
      trigger: "manual",
      status: "succeeded",
      progress: 100,
      result_summary: null,
      artifact_keys: [],
      token_usage: {},
      error: null,
      started_at: null,
      finished_at: null,
      agent_icon: "🧩",
    };
    const runs = [
      { ...base, id: "r1", agent_name: "오늘 실행", created_at: "2026-10-22T01:00:00Z" },
      { ...base, id: "r2", agent_name: "어제 실행", created_at: "2026-10-21T01:00:00Z" },
      { ...base, id: "r3", agent_name: "예전 실행", created_at: "2026-10-10T01:00:00Z" },
    ] as unknown as AgentRun[];
    api.get.mockImplementation((path: string) =>
      Promise.resolve(path === "/api/v1/notebooks/nb-1/agent-runs" ? runs : []),
    );
    renderPanel();
    const list = await screen.findByTestId("run-list");
    const groups = within(list).getAllByRole("heading", { level: 4 });
    expect(groups.map((g) => g.textContent)).toEqual(["오늘", "어제", "2026. 10. 10."]);
    // 묶음 안 항목은 날짜를 되풀이하지 않는다
    expect(within(screen.getByTestId("run-item-r3")).queryByText("2026. 10. 10.")).toBeNull();
    vi.useRealTimers();
  });
});

const BRIEFING_MD = "## 핵심 요약\n\n콜드브루는 상온에서 12~24시간 우려낸다.";

const BRIEFING_RUN = {
  id: "run-1",
  agent_id: "ag-brief",
  notebook_id: "nb-1",
  params: {},
  trigger: "manual",
  status: "succeeded",
  progress: 100,
  result_summary: null,
  artifact_keys: [
    { key: "run-1/briefing.md", name: "briefing.md", content_type: "text/markdown" },
    { key: "run-1/sources.json", name: "sources.json", content_type: "application/json" },
  ],
  token_usage: {},
  error: null,
  created_at: "2026-10-16T00:00:00Z",
  started_at: null,
  finished_at: null,
  agent_name: "브리핑 문서",
  agent_icon: "🧩",
} as unknown as AgentRun;

const LOGS = [
  { id: "log-1", node_id: "generate", node_type: "llm", status: "succeeded", duration_ms: 17947 },
];

function withRuns(runs: AgentRun[]) {
  api.get.mockImplementation((path: string) => {
    if (path === "/api/v1/notebooks/nb-1/agent-runs") return Promise.resolve(runs);
    if (path === "/api/v1/agent-runs/run-1/logs") return Promise.resolve(LOGS);
    return Promise.resolve([]);
  });
  api.getText.mockImplementation((path: string) =>
    Promise.resolve(path.endsWith("/briefing.md") ? BRIEFING_MD : '{"sources":["자료"]}'),
  );
}

async function openRunItem(user: ReturnType<typeof userEvent.setup>) {
  // 화면 그대로 실행 결과 항목(행)을 누른다 — 산출물 칩이 아니라(이번 보고 경로)
  await user.click(await screen.findByTestId("run-item-run-1"));
  return screen.findByTestId("run-detail-view");
}

describe("AgentPanel 실행 결과 — 모달이 아니라 우측 패널, 결과(문서)를 먼저", () => {
  it("실행 결과 항목을 누르면 상세의 「결과」 탭에 첫 산출물 본문을 보인다(실행 로그가 아니다)", async () => {
    withRuns([BRIEFING_RUN]);
    const user = userEvent.setup();
    renderPanel();
    const view = await openRunItem(user);

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByTestId("run-list")).toBeNull();
    expect(within(view).getByRole("tab", { name: "결과" })).toHaveAttribute("aria-selected", "true");
    expect(await within(view).findByRole("heading", { name: "핵심 요약" })).toBeInTheDocument();
    expect(view).toHaveTextContent("briefing.md · text/markdown");
    expect(within(view).queryByText("노드 실행 기록")).toBeNull();
    expect(api.getText).toHaveBeenCalledWith("/api/v1/agent-runs/run-1/artifacts/briefing.md");
    // 화면이 바뀐 것을 알리도록 제목으로 포커스를 옮긴다
    await waitFor(() =>
      expect(within(view).getByRole("heading", { level: 2, name: "브리핑 문서" })).toHaveFocus(),
    );
  });

  it("「실행 로그」 탭으로 바꾸면 노드 실행 기록과 승인 이력이 보인다", async () => {
    withRuns([BRIEFING_RUN]);
    const user = userEvent.setup();
    renderPanel();
    const view = await openRunItem(user);
    await user.click(within(view).getByRole("tab", { name: "실행 로그" }));

    expect(await within(view).findByTestId("execution-log-log-1")).toHaveTextContent("generate");
    expect(within(view).getByText("승인 요청 이력이 없습니다")).toBeInTheDocument();
    // 이전 탭 패널은 Base UI가 비동기로 내린다 — 내려갈 때까지 기다린다
    await waitFor(() =>
      expect(within(view).queryByRole("heading", { name: "핵심 요약" })).toBeNull(),
    );
  });

  it("산출물 칩으로 열면 그 산출물을 먼저 보이고, 여럿이면 골라 바꿀 수 있다", async () => {
    withRuns([BRIEFING_RUN]);
    const user = userEvent.setup();
    renderPanel();
    const runList = await screen.findByTestId("run-list");
    await user.click(within(runList).getByRole("button", { name: /sources\.json/ }));

    const view = await screen.findByTestId("run-detail-view");
    const picker = within(view).getByRole("group", { name: "산출물 선택" });
    expect(within(picker).getByRole("button", { name: /sources\.json/ })).toHaveAttribute("aria-pressed", "true");
    expect(await within(view).findByText(/"sources"/)).toBeInTheDocument();

    await user.click(within(picker).getByRole("button", { name: /briefing\.md/ }));
    expect(await within(view).findByRole("heading", { name: "핵심 요약" })).toBeInTheDocument();
    expect(within(picker).getByRole("button", { name: /briefing\.md/ })).toHaveAttribute("aria-pressed", "true");
  });

  it("「목록」으로 돌아가면 상세를 연 요소(실행 이름 버튼·산출물 칩)로 포커스가 돌아온다", async () => {
    withRuns([BRIEFING_RUN]);
    const user = userEvent.setup();
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "브리핑 문서 실행 결과 보기" }));
    await user.click(await screen.findByTestId("run-detail-back"));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "브리핑 문서 실행 결과 보기" })).toHaveFocus(),
    );

    await user.click(within(screen.getByTestId("run-list")).getByRole("button", { name: /briefing\.md/ }));
    await user.click(await screen.findByTestId("run-detail-back"));
    await waitFor(() =>
      expect(
        within(screen.getByTestId("run-list")).getByRole("button", { name: /briefing\.md/ }),
      ).toHaveFocus(),
    );
  });

  it("실행 중인 run은 결과 탭에 완료 뒤 결과가 나온다고 알린다", async () => {
    withRuns([{ ...BRIEFING_RUN, status: "running", progress: 40, artifact_keys: [] } as AgentRun]);
    const user = userEvent.setup();
    renderPanel();
    const view = await openRunItem(user);
    expect(within(view).getByText("실행 중입니다. 완료되면 결과가 여기에 표시됩니다.")).toBeInTheDocument();
  });

  it("결과 탭의 「메모로 저장」은 origin=agent·artifact_name과 본문을 그대로 보낸다", async () => {
    withRuns([BRIEFING_RUN]);
    api.post.mockResolvedValue({ id: "note-1" });
    const user = userEvent.setup();
    renderPanel();
    const view = await openRunItem(user);
    await within(view).findByRole("heading", { name: "핵심 요약" });
    await user.click(within(view).getByTestId("artifact-save-to-note"));

    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/nb-1/notes", {
      title: "브리핑 문서 — briefing.md",
      content: BRIEFING_MD,
      origin: "agent",
      origin_ref: "run-1",
      artifact_name: "briefing.md",
    });
  });
});

describe("AgentPanel 실행 취소·메모 저장 권한 표시 (#173·#174)", () => {
  it.each([
    [true, 1],
    [false, 0],
  ])("진행 중 run의 「취소」 버튼은 can_cancel=%s일 때 %i개다", async (canCancel, count) => {
    withRuns([
      { ...BRIEFING_RUN, status: "running", progress: 40, can_cancel: canCancel } as AgentRun,
    ]);
    renderPanel();
    const item = await screen.findByTestId("run-item-run-1");
    expect(within(item).queryAllByRole("button", { name: "취소" })).toHaveLength(count);
  });

  it("종료된 run은 can_cancel=true여도 「취소」 버튼이 없다", async () => {
    withRuns([{ ...BRIEFING_RUN, can_cancel: true } as AgentRun]);
    renderPanel();
    const item = await screen.findByTestId("run-item-run-1");
    expect(within(item).queryByRole("button", { name: "취소" })).not.toBeInTheDocument();
  });

  it("read 공유자(canWrite=false)는 결과 탭에 「메모로 저장」이 없다", async () => {
    withRuns([BRIEFING_RUN]);
    const user = userEvent.setup();
    renderPanel(false);
    const view = await openRunItem(user);
    await within(view).findByRole("heading", { name: "핵심 요약" });
    expect(within(view).queryByTestId("artifact-save-to-note")).not.toBeInTheDocument();
  });
});
