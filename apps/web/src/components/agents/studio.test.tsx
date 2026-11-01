/**
 * 스튜디오 띠·결과 항목(mindmap-agent-skill AC-1.9·AC-1.10, P4 AC-4.4 · #180) — 실제 AgentPanel·훅으로 돌리고 API 경계만 대역한다.
 * 카드는 시스템 스킬(스킬 목록 API, wrapper 에이전트로 실행) 또는 시드 시스템 에이전트(에이전트 목록 API)에 묶인다.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Agent, AgentRun } from "@/lib/agent-types";
import type { LLMModelInfo } from "@/lib/chat-types";
import type { Skill } from "@/lib/skill-types";
import { useSourceSelection } from "@/stores/source-selection";
import { AgentPanel } from "./agent-panel";
import {
  jsonTitle,
  markdownFirstHeading,
  requestedModelName,
  studioModelLabel,
  studioRunTitle,
  studioSourceCountLabel,
} from "./studio-run-item";
import { STUDIO_CARDS } from "@/lib/studio-registry";

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
vi.mock("@xyflow/react/dist/style.css", () => ({}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const MINDMAP_SKILL = {
  id: "sk-mm",
  slug: "mindmap",
  name: "마인드맵",
  description: "소스 내용을 인터랙티브 마인드맵 트리(JSON)로 구조화합니다",
  visibility: "system",
  enabled: true,
  can_run: true,
  wrapper_agent_id: "ag-mm",
  params_schema: {
    type: "object",
    properties: { topic: { type: "string", default: "선택한 소스 전체의 주제와 하위 개념 구조" } },
    required: ["topic"],
  },
  input: { mode: "rag", scope: "selected", max_sources: 12 },
} as unknown as Skill;

// 스킬 탭(기본 탭)의 레거시 skill 에이전트 — 기본값만으로 즉시 실행된다
const OTHER_AGENT = {
  id: "ag-other",
  name: "요약",
  description: "요약 설명",
  icon: "📝",
  kind: "skill",
  scope: "system",
  enabled: true,
  version: 1,
  params_schema: {
    type: "object",
    properties: { topic: { type: "string", default: "전체" } },
    required: ["topic"],
  },
} as unknown as Agent;

// 보고서는 모든 소스를 반영하는 시드 워크플로우다(#282 — 옛 스킬 report-writer는 은퇴)
const REPORT = {
  ...OTHER_AGENT,
  id: "ag-report",
  name: "보고서 생성",
  kind: "workflow",
  icon: "📊",
  description: "선택한 소스를 바탕으로 구조화된 보고서를 작성합니다",
} as unknown as Agent;

// 사용자가 만든 스킬 — 산출물 이름이 퀴즈와 같아도 스튜디오 항목이 아니다
const USER_SKILL = {
  ...MINDMAP_SKILL,
  id: "sk-user",
  slug: "my-quiz",
  name: "내 퀴즈",
  description: "내가 만든 퀴즈 스킬",
  visibility: "private",
  wrapper_agent_id: "ag-user",
} as unknown as Skill;

function systemAgent(id: string, name: string, kind: "skill" | "workflow", icon: string): Agent {
  return {
    ...OTHER_AGENT,
    id,
    name,
    kind,
    icon,
    description: `${name} 설명`,
    params_schema: {
      type: "object",
      properties: { topic: { type: "string", default: "기본" } },
      required: ["topic"],
    },
  } as unknown as Agent;
}

const STUDY = systemAgent("ag-study", "스터디 가이드", "workflow", "📖");
const QUIZ = systemAgent("ag-quiz", "퀴즈 생성", "workflow", "❓");
const FLASH = systemAgent("ag-flash", "플래시카드 생성", "workflow", "🗂️");
const SAMPLE_WORKFLOW = systemAgent("ag-sample", "[샘플] 타임라인", "workflow", "🕒");

const SOURCES = [
  { id: "s1", title: "커피 교재", status: "ready", type: "text" },
  { id: "s2", title: "차 교재", status: "ready", type: "text" },
  { id: "s-pending", title: "처리 중", status: "embedding", type: "text" },
];

function mindmapRun(overrides: Partial<AgentRun> = {}): AgentRun {
  return {
    id: "run-mm",
    agent_id: "ag-mm",
    notebook_id: "nb-1",
    params: { topic: "기본 주제" },
    input_source_ids: ["s1"],
    trigger: "manual",
    status: "succeeded",
    progress: 100,
    result_summary: null,
    artifact_keys: [{ key: "run-mm/mindmap.json", name: "mindmap.json", content_type: "application/json" }],
    token_usage: {},
    error: null,
    created_at: new Date().toISOString(),
    started_at: null,
    finished_at: null,
    agent_name: "마인드맵",
    agent_icon: "🧠",
    used_models: [],
    fallback_used: false,
    ...overrides,
  } as unknown as AgentRun;
}

const DOC = JSON.stringify({ version: 1, root: { id: "r", label: "커피 마인드맵", children: [] } });

function setup({
  skills = [MINDMAP_SKILL],
  agents = [OTHER_AGENT],
  workflows = [] as Agent[],
  runs = [] as AgentRun[],
  body = DOC as string | Error,
  models = [] as LLMModelInfo[],
} = {}) {
  api.get.mockImplementation((path: string) => {
    if (path === "/api/v1/models") return Promise.resolve(models);
    if (path === "/api/v1/skills") return Promise.resolve(skills);
    if (path === "/api/v1/agents?kind=skill") return Promise.resolve(agents);
    if (path === "/api/v1/agents?kind=workflow") return Promise.resolve(workflows);
    if (path === "/api/v1/notebooks/nb-1/agent-runs") return Promise.resolve(runs);
    if (path === "/api/v1/notebooks/nb-1/sources") return Promise.resolve(SOURCES);
    return Promise.resolve([]);
  });
  api.getText.mockImplementation(() =>
    body instanceof Error ? Promise.reject(body) : Promise.resolve(body),
  );
  api.post.mockResolvedValue(mindmapRun({ status: "queued" }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={client}>
      <AgentPanel notebookId="nb-1" canWrite />
    </QueryClientProvider>,
  );
  return { client, ...utils };
}

beforeEach(() => {
  api.get.mockReset();
  api.getText.mockReset();
  api.post.mockReset();
  useSourceSelection.setState({ selected: {} });
});

describe("스튜디오 진입(AC-1.9)", () => {
  it("마인드맵 카드 클릭은 항상 다이얼로그를 연다 — createRun 0회, 소스 선택기 기본값 = 소스 패널 선택(준비된 것만)", async () => {
    useSourceSelection.setState({ selected: { "nb-1": ["s1", "s-pending"] } });
    const user = userEvent.setup();
    setup();
    const card = await screen.findByTestId("studio-card-mindmap");
    await waitFor(() => expect(card).toBeEnabled());
    await user.click(card);
    const dialog = await screen.findByRole("dialog");
    expect(api.post).not.toHaveBeenCalled();
    expect(within(dialog).getByText("마인드맵")).toBeInTheDocument();
    await within(dialog).findByTestId("source-picker-list");
    expect(within(dialog).getByText("1개 소스 선택됨")).toBeInTheDocument();
    await user.click(within(dialog).getByTestId("run-agent"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/nb-1/agent-runs", {
      agent_id: "ag-mm",
      params: { topic: "선택한 소스 전체의 주제와 하위 개념 구조" },
      source_ids: ["s1"],
    });
  });

  it("소스 패널 선택이 0개면 「노트북 전체」 — source_ids를 보내지 않는다", async () => {
    const user = userEvent.setup();
    setup();
    const card = await screen.findByTestId("studio-card-mindmap");
    await waitFor(() => expect(card).toBeEnabled());
    await user.click(card);
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("선택 없음 → 노트 전체 대상");
    await user.click(within(dialog).getByTestId("run-agent"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post.mock.calls[0][1]).not.toHaveProperty("source_ids");
  });

  it("스튜디오에 묶인 에이전트는 스킬·워크플로우 탭에 없다 — 사용자·샘플·그 밖의 시스템 항목은 남는다(#180 결정 2)", async () => {
    const user = userEvent.setup();
    setup({
      skills: [MINDMAP_SKILL, USER_SKILL],
      agents: [OTHER_AGENT],
      workflows: [QUIZ, FLASH, SAMPLE_WORKFLOW, REPORT, STUDY],
    });
    await waitFor(() => expect(screen.getByTestId("studio-card-quiz")).toBeEnabled());
    const grid = await screen.findByTestId("agent-grid");
    await within(grid).findByText("요약 설명");
    expect(within(grid).getByText("내가 만든 퀴즈 스킬")).toBeInTheDocument();
    expect(within(grid).queryByText(MINDMAP_SKILL.description)).toBeNull();
    await user.click(screen.getByRole("tab", { name: "워크플로우" }));
    await within(screen.getByTestId("agent-grid")).findByText("[샘플] 타임라인 설명");
    for (const hidden of ["퀴즈 생성 설명", "플래시카드 생성 설명", REPORT.description, "스터디 가이드 설명"]) {
      expect(screen.queryByText(hidden)).toBeNull();
    }
  });

  it("탭의 다른 카드는 기본값으로 즉시 실행한다(canInstantRun 회귀)", async () => {
    const user = userEvent.setup();
    setup();
    const grid = await screen.findByTestId("agent-grid");
    await user.click(await within(grid).findByText("요약 설명"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post.mock.calls[0][1]).toMatchObject({ agent_id: "ag-other" });
  });

  it("스튜디오 띠는 7개 카드(숨김 개요 카드 제외) — 묶인 대상이 있으면 켜지고 없으면 카드별 안내 문구로 꺼진다", async () => {
    setup({ skills: [MINDMAP_SKILL], workflows: [QUIZ, REPORT] });
    const strip = await screen.findByTestId("studio-strip");
    await waitFor(() => expect(within(strip).getByTestId("studio-card-quiz")).toBeEnabled());
    expect(within(strip).getAllByRole("button")).toHaveLength(7);
    expect(within(strip).queryByTestId("studio-card-slide-deck-outline")).toBeNull();
    for (const id of ["mindmap", "report", "quiz"]) {
      expect(within(strip).getByTestId(`studio-card-${id}`)).toBeEnabled();
    }
    // 컴팩트 타일: 이름만 보이고 설명은 툴팁 — 갤러리 높이를 빼앗지 않는다
    const quiz = within(strip).getByTestId("studio-card-quiz");
    expect(quiz).toHaveTextContent(/^❓퀴즈$/);
    expect(quiz).toHaveAttribute("title", "객관식 문제를 풀고 채점합니다");
    for (const [id, label] of [
      ["briefing", "브리핑 문서"],
      ["study-guide", "스터디 가이드"],
      ["flashcards", "플래시카드"],
      ["slide-deck", "슬라이드 자료"],
    ]) {
      const card = within(strip).getByTestId(`studio-card-${id}`);
      expect(card).toBeDisabled();
      expect(card).toHaveAttribute("title", `${label} 에이전트를 찾을 수 없습니다`);
      expect(card).toHaveAccessibleDescription(`${label} 에이전트를 찾을 수 없습니다`);
    }
  });

  it("워크플로우 카드(퀴즈)도 다이얼로그를 열고 카드 아이콘·소스 선택기를 보인 뒤 그 에이전트로 실행한다", async () => {
    useSourceSelection.setState({ selected: { "nb-1": ["s2"] } });
    const user = userEvent.setup();
    setup({ workflows: [QUIZ] });
    const card = await screen.findByTestId("studio-card-quiz");
    await waitFor(() => expect(card).toBeEnabled());
    await user.click(card);
    const dialog = await screen.findByRole("dialog");
    expect(api.post).not.toHaveBeenCalled();
    expect(within(dialog).getByRole("heading", { name: /퀴즈 생성/ })).toHaveTextContent("❓");
    expect(within(dialog).getByText("객관식 문제를 풀고 채점합니다")).toBeInTheDocument();
    await within(dialog).findByTestId("source-picker-list");
    await user.click(within(dialog).getByTestId("run-agent"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post.mock.calls[0][1]).toMatchObject({ agent_id: "ag-quiz", source_ids: ["s2"] });
  });

  it.each([
    ["스킬 없음", [] as Skill[]],
    ["실행 불가", [{ ...MINDMAP_SKILL, can_run: false } as Skill]],
    ["시스템이 아닌 같은 slug", [{ ...MINDMAP_SKILL, visibility: "private" } as Skill]],
  ])("바인딩 대상이 없으면(%s) 카드 비활성 + 「마인드맵 에이전트를 찾을 수 없습니다」", async (_label, skills) => {
    setup({ skills });
    const card = await screen.findByTestId("studio-card-mindmap");
    await waitFor(() => expect(card).toHaveAttribute("title", "마인드맵 에이전트를 찾을 수 없습니다"));
    expect(card).toBeDisabled();
  });

  it("사용자 scope의 동명 워크플로우는 카드에 묶이지 않는다", async () => {
    setup({ workflows: [{ ...QUIZ, scope: "user" } as Agent] });
    const card = await screen.findByTestId("studio-card-quiz");
    await waitFor(() => expect(card).toHaveAttribute("title", "퀴즈 에이전트를 찾을 수 없습니다"));
    expect(card).toBeDisabled();
  });
});

describe("결과 항목(AC-1.10)", () => {
  it("성공 run은 「제목 · 소스 N개 · 상대 시각」 항목 — 제목은 산출물 root.label", async () => {
    setup({ runs: [mindmapRun()] });
    const item = await screen.findByTestId("studio-run-item-run-mm");
    await waitFor(() => expect(within(item).getByTestId("studio-run-title")).toHaveTextContent("커피 마인드맵"));
    expect(item).toHaveTextContent("소스 1개 · 방금 전");
    expect(api.getText).toHaveBeenCalledWith("/api/v1/agent-runs/run-mm/artifacts/mindmap.json");
  });

  it("목록을 다시 불러와도 본문을 다시 조회하지 않는다(요청 1회), 실패는 재시도 0 · 제목은 params.topic", async () => {
    const { client } = setup({ runs: [mindmapRun()], body: new Error("boom") });
    const item = await screen.findByTestId("studio-run-item-run-mm");
    await waitFor(() => expect(api.getText).toHaveBeenCalledTimes(1));
    expect(within(item).getByTestId("studio-run-title")).toHaveTextContent("기본 주제");
    for (let i = 0; i < 3; i += 1) {
      await act(() => client.invalidateQueries({ queryKey: ["agent-runs", "nb-1"] }));
    }
    await new Promise((r) => setTimeout(r, 20));
    expect(api.getText).toHaveBeenCalledTimes(1);
  });

  it("성공 본문도 목록 재조회 때 다시 읽지 않는다", async () => {
    const { client } = setup({ runs: [mindmapRun()] });
    await screen.findByText("커피 마인드맵");
    for (let i = 0; i < 3; i += 1) {
      await act(() => client.invalidateQueries({ queryKey: ["agent-runs", "nb-1"] }));
    }
    expect(api.getText).toHaveBeenCalledTimes(1);
  });

  it("케밥은 「열기」·「JSON 다운로드」 두 항목(메모 저장 없음)", async () => {
    const user = userEvent.setup();
    setup({ runs: [mindmapRun()] });
    const item = await screen.findByTestId("studio-run-item-run-mm");
    await within(item).findByText("커피 마인드맵");
    await user.click(within(item).getByTestId("studio-run-menu"));
    const menu = await screen.findByRole("menu");
    const items = within(menu).getAllByRole("menuitem");
    expect(items.map((i) => i.textContent)).toEqual(["열기", "JSON 다운로드"]);
    expect(within(menu).queryByText(/메모/)).toBeNull();
  });

  it("항목 클릭 → 결과 탭 뷰어 + 머리 「스튜디오 › 마인드맵」, 뒤로 가면 포커스가 항목으로 돌아온다", async () => {
    const user = userEvent.setup();
    setup({ runs: [mindmapRun()] });
    const title = await screen.findByText("커피 마인드맵");
    await user.click(title);
    const view = await screen.findByTestId("run-detail-view");
    expect(within(view).getByTestId("run-detail-breadcrumb")).toHaveTextContent("스튜디오 › 마인드맵");
    expect(await within(view).findByTestId("mindmap-canvas")).toBeInTheDocument();
    await user.click(within(view).getByTestId("run-detail-back"));
    await waitFor(() => expect(screen.getByTestId("studio-run-title")).toHaveFocus());
  });

  it("실행 중·실패 run은 기존 행(상태 배지·오류) 그대로", async () => {
    setup({
      runs: [
        mindmapRun({ id: "run-a", status: "running", artifact_keys: [] }),
        mindmapRun({ id: "run-b", status: "failed", error: "LLM 오류", artifact_keys: [] }),
      ],
    });
    expect(await screen.findByTestId("run-item-run-a")).toBeInTheDocument();
    expect(screen.getByTestId("run-item-run-b")).toHaveTextContent("LLM 오류");
    expect(screen.queryByTestId("studio-run-item-run-a")).toBeNull();
  });
});

describe("결과 항목 — 1차 6종(#180)", () => {
  const md = (name: string) => ({ key: `run-x/${name}`, name, content_type: "text/markdown" });
  const json = (name: string) => ({ key: `run-x/${name}`, name, content_type: "application/json" });

  it("퀴즈 run은 본문을 읽지 않고 제목 = params.topic, 아이콘 = 카드 아이콘, 케밥은 「열기」만", async () => {
    const user = userEvent.setup();
    setup({
      workflows: [QUIZ],
      runs: [
        mindmapRun({
          id: "run-q",
          agent_id: "ag-quiz",
          agent_name: "퀴즈 생성",
          params: { topic: "커피 추출" },
          artifact_keys: [json("quiz.json")],
        }),
      ],
    });
    const item = await screen.findByTestId("studio-run-item-run-q");
    expect(within(item).getByTestId("studio-run-title")).toHaveTextContent("커피 추출");
    expect(item).toHaveTextContent("❓");
    await user.click(within(item).getByTestId("studio-run-menu"));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["열기"]);
    expect(api.getText).not.toHaveBeenCalled();
  });

  it("보고서 run의 제목은 마크다운 첫 제목 — 본문은 한 번만 읽는다", async () => {
    const { client } = setup({
      workflows: [REPORT],
      runs: [
        mindmapRun({ id: "run-r", agent_id: "ag-report", artifact_keys: [md("report.md")] }),
      ],
      body: "머리말\n\n# 커피 산업 보고서 #\n\n본문",
    });
    const item = await screen.findByTestId("studio-run-item-run-r");
    await waitFor(() =>
      expect(within(item).getByTestId("studio-run-title")).toHaveTextContent("커피 산업 보고서"),
    );
    expect(api.getText).toHaveBeenCalledWith("/api/v1/agent-runs/run-r/artifacts/report.md");
    for (let i = 0; i < 3; i += 1) {
      await act(() => client.invalidateQueries({ queryKey: ["agent-runs", "nb-1"] }));
    }
    expect(api.getText).toHaveBeenCalledTimes(1);
  });

  it("사용자 스킬이 quiz.json을 내도 스튜디오 항목이 아니라 일반 행이다", async () => {
    setup({
      skills: [MINDMAP_SKILL, USER_SKILL],
      workflows: [QUIZ],
      runs: [
        mindmapRun({
          id: "run-u",
          agent_id: "ag-user",
          agent_name: "내 퀴즈",
          agent_icon: "🧩",
          artifact_keys: [json("quiz.json")],
        }),
      ],
    });
    expect(await screen.findByTestId("run-item-run-u")).toBeInTheDocument();
    expect(screen.queryByTestId("studio-run-item-run-u")).toBeNull();
  });

  it("은퇴한 워크플로우 「마인드맵」의 옛 run은 일반 행(산출물 칩 유지)", async () => {
    setup({ runs: [mindmapRun({ id: "run-old", agent_id: "ag-retired" })] });
    const row = await screen.findByTestId("run-item-run-old");
    expect(within(row).getByRole("button", { name: /mindmap\.json/ })).toBeInTheDocument();
  });

  it("퀴즈 항목을 열면 대표 산출물(quiz.json)이 먼저 선택되고 머리는 「스튜디오 › 퀴즈」", async () => {
    const user = userEvent.setup();
    setup({
      workflows: [QUIZ],
      runs: [
        mindmapRun({
          id: "run-a",
          agent_id: "ag-quiz",
          params: { topic: "커피 이야기" },
          artifact_keys: [json("notes.json"), json("quiz.json")],
        }),
      ],
    });
    await user.click(await screen.findByText("커피 이야기"));
    const view = await screen.findByTestId("run-detail-view");
    expect(within(view).getByTestId("run-detail-breadcrumb")).toHaveTextContent("스튜디오 › 퀴즈");
    expect(within(view).getByRole("button", { name: /quiz\.json/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await user.click(within(view).getByTestId("run-detail-back"));
    await waitFor(() => expect(screen.getByTestId("studio-run-title")).toHaveFocus());
  });
});

describe("제목·소스 수 순수 함수(D-6)", () => {
  const card = STUDIO_CARDS[0];
  it("본문 제목 → params.topic → 카드 이름", () => {
    expect(studioRunTitle(mindmapRun(), "본문 제목", card)).toBe("본문 제목");
    expect(studioRunTitle(mindmapRun(), null, card)).toBe("기본 주제");
    expect(studioRunTitle(mindmapRun({ params: {} }), null, card)).toBe("마인드맵");
    expect(studioRunTitle(mindmapRun({ params: { topic: " \n " } }), null, card)).toBe("마인드맵");
  });
  it("마크다운 첫 제목: ATX 제목만, 강조 기호·닫는 #은 빼고, 없거나 비면 null", () => {
    expect(markdownFirstHeading("# 보고서")).toBe("보고서");
    expect(markdownFirstHeading("서론\n## **커피** 개요 ##\n# 둘째")).toBe("커피 개요");
    expect(markdownFirstHeading("#해시태그\n본문")).toBeNull();
    expect(markdownFirstHeading("제목 없음")).toBeNull();
    expect(markdownFirstHeading("#   \n")).toBeNull();
    expect(markdownFirstHeading(`# ${"가".repeat(200)}`)).toHaveLength(80);
    expect(markdownFirstHeading(`${"x".repeat(5000)}\n# 늦은 제목`)).toBeNull();
  });
  it("JSON 제목(슬라이드 자료): 최상위 title만, 펜스 허용, 없거나 비거나 배열이면 null", () => {
    expect(jsonTitle('{"title": "LLM 위키", "slides": []}')).toBe("LLM 위키");
    expect(jsonTitle('```json\n{"title": " 개요 "}\n```')).toBe("개요");
    expect(jsonTitle('{"title": "  "}')).toBeNull();
    expect(jsonTitle('{"name": "x"}')).toBeNull();
    expect(jsonTitle('[{"title": "x"}]')).toBeNull();
    expect(jsonTitle("제목 아님")).toBeNull();
    expect(jsonTitle(JSON.stringify({ title: "가".repeat(200) }))).toHaveLength(80);
  });
  it("소스 수: 목록 길이, null이면 「전체 소스」", () => {
    expect(studioSourceCountLabel(mindmapRun({ input_source_ids: ["a", "b"] }))).toBe("소스 2개");
    expect(studioSourceCountLabel(mindmapRun({ input_source_ids: null }))).toBe("전체 소스");
  });
});

describe("결과 항목 — 사용 모델·폴백(#234)", () => {
  const ref = (id: string, display_name: string) => ({ id, display_name });
  const CHOSEN: LLMModelInfo = {
    id: "m-chosen",
    display_name: "선택 모델",
    provider_name: "mock",
    provider_type: "mock",
    is_default: false,
  };

  it("모델을 고른 run과 고르지 않은 run 모두 보조 줄에 실제로 응답한 「모델 X」를 보인다", async () => {
    setup({
      runs: [
        mindmapRun({ id: "run-chosen", model_id: "m-chosen", used_models: [ref("m-chosen", "선택 모델")] }),
        mindmapRun({ id: "run-default", model_id: null, used_models: [ref("m-default", "기본 모델 A")] }),
      ],
    });
    const chosen = await screen.findByTestId("studio-run-item-run-chosen");
    const unspecified = await screen.findByTestId("studio-run-item-run-default");
    expect(within(chosen).getByTestId("studio-run-model")).toHaveTextContent("모델 선택 모델");
    expect(within(unspecified).getByTestId("studio-run-model")).toHaveTextContent("모델 기본 모델 A");
    expect(chosen).toHaveTextContent("소스 1개 · 방금 전 · 모델 선택 모델");
    expect(screen.queryByTestId("studio-run-fallback")).toBeNull();
    // 폴백이 없으면 요청 모델 이름을 찾지 않는다 — 모델 목록을 부르지 않는다
    expect(api.get).not.toHaveBeenCalledWith("/api/v1/models");
  });

  it("폴백한 run은 「폴백」 배지와 요청 모델 이름을 붙인다", async () => {
    setup({
      models: [CHOSEN],
      runs: [
        mindmapRun({
          id: "run-fb",
          model_id: "m-chosen",
          used_models: [ref("m-default", "기본 모델 A")],
          fallback_used: true,
        }),
      ],
    });
    const item = await screen.findByTestId("studio-run-item-run-fb");
    expect(within(item).getByTestId("studio-run-model")).toHaveTextContent("모델 기본 모델 A");
    expect(within(item).getByTestId("studio-run-fallback")).toHaveTextContent("폴백");
    expect(await within(item).findByTestId("studio-run-requested-model")).toHaveTextContent("요청 선택 모델");
  });

  it("요청 모델을 목록에서 찾지 못하거나 지정하지 않았으면 이름 없이 「폴백」만 붙인다", async () => {
    setup({
      models: [CHOSEN],
      runs: [
        mindmapRun({ id: "run-gone", model_id: "m-gone", used_models: [ref("m-a", "모델 A")], fallback_used: true }),
        mindmapRun({ id: "run-none", model_id: null, used_models: [ref("m-b", "모델 B")], fallback_used: true }),
      ],
    });
    const gone = await screen.findByTestId("studio-run-item-run-gone");
    const none = await screen.findByTestId("studio-run-item-run-none");
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/api/v1/models"));
    for (const item of [gone, none]) {
      expect(within(item).getByTestId("studio-run-fallback")).toHaveTextContent("폴백");
      expect(within(item).queryByTestId("studio-run-requested-model")).toBeNull();
    }
  });

  it("응답한 모델이 없으면(옛 run·필드 없는 응답) 모델 줄을 그리지 않는다", async () => {
    setup({
      runs: [
        mindmapRun({ id: "run-empty", used_models: [], fallback_used: false }),
        mindmapRun({ id: "run-old", used_models: undefined, fallback_used: undefined }),
      ],
    });
    const empty = await screen.findByTestId("studio-run-item-run-empty");
    const old = await screen.findByTestId("studio-run-item-run-old");
    for (const item of [empty, old]) {
      expect(item).toHaveTextContent("소스 1개 · 방금 전");
      expect(within(item).queryByTestId("studio-run-model")).toBeNull();
      expect(within(item).queryByTestId("studio-run-fallback")).toBeNull();
    }
  });

  it("모델 이름은 둘까지 쉼표로 잇고, 셋 이상이면 첫 이름 「외 N개」 — 없으면 null", () => {
    const names = (...n: string[]) => mindmapRun({ used_models: n.map((d, i) => ref(`m${i}`, d)) });
    expect(studioModelLabel(names())).toBeNull();
    expect(studioModelLabel(mindmapRun({ used_models: undefined }))).toBeNull();
    expect(studioModelLabel(names("A"))).toBe("모델 A");
    expect(studioModelLabel(names("A", "B"))).toBe("모델 A, B");
    expect(studioModelLabel(names("A", "B", "C"))).toBe("모델 A 외 2개");
    // 표시 이름이 같은 다른 모델(id가 다르다)은 한 번만 — 상세 「사용 모델」과 같다
    expect(studioModelLabel(names("Same", "Same"))).toBe("모델 Same");
    expect(studioModelLabel(names("A", "Same", "Same", "B"))).toBe("모델 A 외 2개");
  });

  it("요청 모델 이름은 run의 `model_id`로 모델 목록에서 찾는다", () => {
    expect(requestedModelName("m-chosen", [CHOSEN])).toBe("선택 모델");
    expect(requestedModelName("m-gone", [CHOSEN])).toBeNull();
    expect(requestedModelName(null, [CHOSEN])).toBeNull();
    expect(requestedModelName("m-chosen", undefined)).toBeNull();
  });
});
