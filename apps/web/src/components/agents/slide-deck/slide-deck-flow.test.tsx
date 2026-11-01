/**
 * 스튜디오 「슬라이드 자료」 흐름(docs/plans/slide-deck-studio.md AC-1~5·7) — 실제 AgentPanel·훅으로 돌리고 API 경계만 대역한다.
 * 다이얼로그 → (개요 run → 「검토」 → 의견 → 「계속」) → 덱 run → 덱 뷰어.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AgentPanel } from "@/components/agents/agent-panel";
import type { Agent, AgentRun } from "@/lib/agent-types";
import { useSourceSelection } from "@/stores/source-selection";

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

function workflow(id: string, name: string): Agent {
  return {
    id,
    name,
    description: `${name} 설명`,
    icon: "📽️",
    kind: "workflow",
    scope: "system",
    enabled: true,
    version: 1,
    params_schema: { type: "object", properties: {} },
  } as unknown as Agent;
}

const DECK = workflow("ag-deck", "슬라이드 자료");
const OUTLINE = workflow("ag-outline", "슬라이드 자료 개요");

const SOURCES = [
  { id: "s1", title: "카파시 LLM 위키", status: "ready", type: "text" },
  { id: "s2", title: "OKF 표준", status: "ready", type: "text" },
];

const OUTLINE_BODY = JSON.stringify({
  title: "카르파티 LLM 위키 구축하기",
  format: "presenter",
  slides: [
    { title: "안드레이 카파시의 LLM 위키", summary: "에이전틱 지식 베이스를 선언하는 오프닝" },
    { title: "지식 베이스 붕괴의 법칙", summary: "인간 중심 관리의 한계" },
  ],
});

const DECK_BODY = JSON.stringify({
  title: "LLM 위키",
  format: "detailed",
  slides: [
    { layout: "title", heading: "안드레이 카파시의 LLM 위키", subheading: "설계와 구현" },
    { layout: "bullets", heading: "붕괴의 법칙", bullets: ["수집", "구조화", "유지보수"] },
  ],
});

function run(overrides: Partial<AgentRun>): AgentRun {
  return {
    id: "run-x",
    agent_id: "ag-deck",
    notebook_id: "nb-1",
    params: {},
    input_source_ids: null,
    trigger: "manual",
    status: "succeeded",
    progress: 100,
    result_summary: null,
    artifact_keys: [],
    token_usage: {},
    error: null,
    created_at: new Date().toISOString(),
    started_at: null,
    finished_at: null,
    agent_name: "슬라이드 자료",
    agent_icon: "📽️",
    can_cancel: true,
    ...overrides,
  } as unknown as AgentRun;
}

const OUTLINE_RUN = run({
  id: "run-outline",
  agent_id: "ag-outline",
  agent_name: "슬라이드 자료 개요",
  params: { format: "presenter", length: "short", language: "English", description: "초보자용" },
  input_source_ids: ["s2"],
  artifact_keys: [
    { key: "run-outline/slide-outline.json", name: "slide-outline.json", content_type: "application/json" },
  ],
});

const DECK_RUN = run({
  id: "run-deck",
  artifact_keys: [{ key: "run-deck/slide-deck.json", name: "slide-deck.json", content_type: "application/json" }],
});

const IMAGE_MODEL = { id: "m-img", display_name: "Mock Image", provider_name: "mock", provider_type: "mock", is_default: true };

function setup({
  runs = [] as AgentRun[],
  workflows = [DECK, OUTLINE],
  imageModels = [] as unknown[],
  models = [] as unknown[],
} = {}) {
  api.get.mockImplementation((path: string) => {
    if (path === "/api/v1/agents?kind=workflow") return Promise.resolve(workflows);
    if (path === "/api/v1/models") return Promise.resolve(models);
    if (path === "/api/v1/models?modality=image") return Promise.resolve(imageModels);
    if (path === "/api/v1/notebooks/nb-1/agent-runs") return Promise.resolve(runs);
    if (path === "/api/v1/notebooks/nb-1/sources") return Promise.resolve(SOURCES);
    return Promise.resolve([]);
  });
  api.getText.mockImplementation((path: string) =>
    Promise.resolve(path.includes("slide-outline.json") ? OUTLINE_BODY : DECK_BODY),
  );
  api.post.mockResolvedValue(run({ status: "queued" }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <AgentPanel notebookId="nb-1" canWrite />
    </QueryClientProvider>,
  );
}

/**
 * jsdom에서는 패널 분할 핸들(react-resizable-panels)이 문서 pointerdown을 핸들 위로 판정해 기본 동작(포커스 이동)을
 * 막는다(모든 사각형이 0×0). 입력란은 포커스를 직접 옮긴 뒤 타이핑한다 — 실제 브라우저 경로는 e2e가 본다.
 */
async function typeInto(user: ReturnType<typeof userEvent.setup>, el: HTMLElement, text: string) {
  act(() => el.focus());
  await user.keyboard(text);
}

async function openDialog(user: ReturnType<typeof userEvent.setup>) {
  const card = await screen.findByTestId("studio-card-slide-deck");
  await waitFor(() => expect(card).toBeEnabled());
  await user.click(card);
  const dialog = await screen.findByTestId("slide-deck-dialog");
  // Base UI는 첫 포커스(선택된 형식 카드)를 비동기로 옮긴다 — 그 뒤에 입력해야 포커스를 빼앗기지 않는다
  await waitFor(() => expect(within(dialog).getByRole("radio", { name: /자세한 자료/ })).toHaveFocus());
  return dialog;
}

beforeEach(() => {
  api.get.mockReset();
  api.getText.mockReset();
  api.post.mockReset();
  useSourceSelection.setState({ selected: {} });
});

describe("슬라이드 자료 맞춤설정 다이얼로그(AC-2·3·4)", () => {
  it("기본값(자세한 자료·한국어·기본값·전체 소스·검토 끔)으로 생성하면 덱 run 하나 — source_ids 없음", async () => {
    const user = userEvent.setup();
    setup();
    const dialog = await openDialog(user);
    expect(within(dialog).getByRole("radio", { name: /자세한 자료/ })).toBeChecked();
    expect(within(dialog).getByRole("radio", { name: "기본값" })).toBeChecked();
    expect(within(dialog).getByRole("radio", { name: "한국어" })).toBeChecked();
    expect(within(dialog).getByRole("switch", { name: "슬라이드 개요 검토" })).not.toBeChecked();
    await within(dialog).findByText("전체 소스 2개");
    await user.click(within(dialog).getByTestId("slide-deck-generate"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/nb-1/agent-runs", {
      agent_id: "ag-deck",
      params: { format: "detailed", length: "default", language: "한국어", description: "", image_count: 0 },
    });
  });

  it("형식·언어·길이·소스(하위 화면)·설명을 바꾸고 검토를 켜면 개요 run을 만든다", async () => {
    const user = userEvent.setup();
    setup();
    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("radio", { name: /발표자 슬라이드/ }));
    await user.click(within(dialog).getByRole("radio", { name: "English" }));
    await user.click(within(dialog).getByRole("radio", { name: "짧게" }));
    await typeInto(user, within(dialog).getByTestId("slide-deck-description"), "  초보자용  ");

    await user.click(within(dialog).getByTestId("slide-deck-sources"));
    await within(dialog).findByTestId("source-picker-list");
    await user.click(within(dialog).getByTestId("source-option-s2"));
    await user.click(within(dialog).getByTestId("slide-deck-sources-done"));
    expect(within(dialog).getByTestId("slide-deck-sources")).toHaveTextContent("소스 1개");

    await user.click(within(dialog).getByRole("switch", { name: "슬라이드 개요 검토" }));
    expect(within(dialog).getByTestId("slide-deck-generate")).toHaveTextContent("개요 만들기");
    await user.click(within(dialog).getByTestId("slide-deck-generate"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/nb-1/agent-runs", {
      agent_id: "ag-outline",
      params: { format: "presenter", length: "short", language: "English", description: "초보자용", image_count: 0 },
      source_ids: ["s2"],
    });
    await waitFor(() => expect(screen.queryByTestId("slide-deck-dialog")).toBeNull());
  });

  it("두 워크플로우는 띠의 카드로만 연다 — 워크플로우 탭에 없다(AC-1)", async () => {
    const user = userEvent.setup();
    setup({
      workflows: [
        DECK,
        OUTLINE,
        workflow("ag-image", "슬라이드 삽화"),
        workflow("ag-write", "슬라이드 장 작성"),
        workflow("ag-sample", "[샘플] 타임라인"),
      ],
    });
    await waitFor(() => expect(screen.getByTestId("studio-card-slide-deck")).toBeEnabled());
    await user.click(screen.getByRole("tab", { name: "워크플로우" }));
    const grid = await screen.findByTestId("agent-grid");
    await within(grid).findByText("[샘플] 타임라인 설명");
    expect(within(grid).queryByText("슬라이드 자료 설명")).toBeNull();
    expect(within(grid).queryByText("슬라이드 자료 개요 설명")).toBeNull();
    // loop 본문(숨김 카드) — 삽화(core-v0.23.0-upgrade PR-2)·장 작성(#218)도 탭에 없다
    expect(within(grid).queryByText("슬라이드 삽화 설명")).toBeNull();
    expect(within(grid).queryByText("슬라이드 장 작성 설명")).toBeNull();
  });

  it("개요 워크플로우가 없으면 검토를 켤 때 안내하고 생성 버튼을 끈다", async () => {
    const user = userEvent.setup();
    setup({ workflows: [DECK] });
    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("switch", { name: "슬라이드 개요 검토" }));
    expect(within(dialog).getByText("슬라이드 자료 개요 에이전트를 찾을 수 없습니다")).toBeInTheDocument();
    expect(within(dialog).getByTestId("slide-deck-generate")).toBeDisabled();
  });
});

describe("이미지 넣기(core-v0.23.0-upgrade AC-8·AC-11)", () => {
  it("이미지 생성 모델이 없으면 스위치를 끄고 안내한다 — 장수 0으로 만든다", async () => {
    const user = userEvent.setup();
    setup();
    const dialog = await openDialog(user);
    const toggle = within(dialog).getByRole("switch", { name: "이미지 넣기" });
    await waitFor(() =>
      expect(toggle).toHaveAccessibleDescription(
        "이미지 생성 모델이 없어 켤 수 없습니다. 관리자가 이미지 모델을 등록하면 쓸 수 있습니다.",
      ),
    );
    // Base UI 스위치는 네이티브 버튼이 아니라 aria-disabled로 끈다 — 눌러도 켜지지 않는다
    expect(toggle).toHaveAttribute("aria-disabled", "true");
    await user.click(toggle);
    expect(toggle).not.toBeChecked();
    expect(api.get).toHaveBeenCalledWith("/api/v1/models?modality=image");
    await user.click(within(dialog).getByTestId("slide-deck-generate"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post.mock.calls[0][1].params.image_count).toBe(0);
  });

  it("모델이 있으면 기본은 끔이고, 켜면 덱 run에 장수 4를 보낸다", async () => {
    const user = userEvent.setup();
    setup({ imageModels: [IMAGE_MODEL] });
    const dialog = await openDialog(user);
    const toggle = within(dialog).getByRole("switch", { name: "이미지 넣기" });
    await waitFor(() => expect(toggle).not.toHaveAttribute("aria-disabled", "true"));
    expect(toggle).not.toBeChecked();
    expect(toggle).toHaveAccessibleDescription(
      "그림이 도움이 되는 슬라이드에 AI 삽화를 최대 4장 넣습니다. 만드는 시간이 늘어납니다.",
    );
    await user.click(toggle);
    expect(toggle).toBeChecked();
    await user.click(within(dialog).getByTestId("slide-deck-generate"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/nb-1/agent-runs", {
      agent_id: "ag-deck",
      params: { format: "detailed", length: "default", language: "한국어", description: "", image_count: 4 },
    });
  });

  it("검토를 켜면 개요 run이 장수를 받아 두고, 「계속」이 덱 run에 그대로 넘긴다", async () => {
    const user = userEvent.setup();
    setup({ imageModels: [IMAGE_MODEL] });
    const dialog = await openDialog(user);
    const toggle = within(dialog).getByRole("switch", { name: "이미지 넣기" });
    await waitFor(() => expect(toggle).not.toHaveAttribute("aria-disabled", "true"));
    await user.click(toggle);
    await user.click(within(dialog).getByRole("switch", { name: "슬라이드 개요 검토" }));
    await user.click(within(dialog).getByTestId("slide-deck-generate"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post.mock.calls[0][1]).toMatchObject({ agent_id: "ag-outline", params: { image_count: 4 } });
  });

  it("장수 4로 만든 개요를 계속하면 덱 run도 4다", async () => {
    const user = userEvent.setup();
    setup({ runs: [{ ...OUTLINE_RUN, params: { ...OUTLINE_RUN.params, image_count: 4 } } as AgentRun] });
    const item = await screen.findByTestId("studio-run-item-run-outline");
    await user.click(await within(item).findByTestId("studio-run-review"));
    await user.click(await screen.findByTestId("slide-outline-continue"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post.mock.calls[0][1]).toMatchObject({ agent_id: "ag-deck", params: { image_count: 4 } });
  });
});

describe("고른 모델로 요약 다시 만들기(#239, core-v0.27.0-upgrade AC-4·D3)", () => {
  it("기본은 끔이고 키를 보내지 않는다 — 켜면 덱 run에 refresh_digest: true를 보낸다", async () => {
    const user = userEvent.setup();
    setup();
    const dialog = await openDialog(user);
    const toggle = within(dialog).getByRole("switch", { name: "고른 모델로 요약 다시 만들기" });
    expect(toggle).not.toBeChecked();
    expect(toggle).toHaveAccessibleDescription(/시간과 토큰이 더 듭니다/);
    await user.click(toggle);
    await user.click(within(dialog).getByTestId("slide-deck-generate"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/nb-1/agent-runs", {
      agent_id: "ag-deck",
      params: {
        format: "detailed",
        length: "default",
        language: "한국어",
        description: "",
        image_count: 0,
        refresh_digest: true,
      },
    });
  });

  it("검토를 켜면 개요 run이 받고, 「계속」은 덱 run에 넘기지 않는다(개요 run이 이미 다시 만들었다)", async () => {
    const user = userEvent.setup();
    setup();
    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("switch", { name: "고른 모델로 요약 다시 만들기" }));
    await user.click(within(dialog).getByRole("switch", { name: "슬라이드 개요 검토" }));
    await user.click(within(dialog).getByTestId("slide-deck-generate"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post.mock.calls[0][1]).toMatchObject({ agent_id: "ag-outline", params: { refresh_digest: true } });
  });

  it("다시 만들기로 만든 개요를 계속해도 덱 run params에 refresh_digest가 없다", async () => {
    const user = userEvent.setup();
    setup({ runs: [{ ...OUTLINE_RUN, params: { ...OUTLINE_RUN.params, refresh_digest: true } } as AgentRun] });
    const item = await screen.findByTestId("studio-run-item-run-outline");
    await user.click(await within(item).findByTestId("studio-run-review"));
    await user.click(await screen.findByTestId("slide-outline-continue"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post.mock.calls[0][1].params).not.toHaveProperty("refresh_digest");
  });

  it("개요 검토 화면에 소스 요약 모델 줄을 보인다", async () => {
    const user = userEvent.setup();
    setup({ runs: [OUTLINE_RUN] });
    api.getText.mockResolvedValue(
      JSON.stringify({
        title: "커피 입문",
        format: "detailed",
        slides: [{ title: "표지", summary: "오프닝", source_ids: ["s1", "s2"] }],
        sources: [
          { id: "s1", title: "원두", cached: true, model_id: "m1", model_name: "gemini-3.8-flash" },
          { id: "s2", title: "추출", cached: false, model_id: "m2", model_name: "gpt-oss-120b" },
        ],
        degraded: [],
      }),
    );
    const item = await screen.findByTestId("studio-run-item-run-outline");
    await user.click(await within(item).findByTestId("studio-run-review"));
    const review = await screen.findByTestId("slide-outline-review");
    expect(within(review).getByTestId("slide-outline-digest").querySelector("summary")).toHaveTextContent(
      "요약: gemini-3.8-flash 1개(캐시) · gpt-oss-120b 1개",
    );
  });
});

describe("개요 검토 → 계속(AC-4·5)", () => {
  it("성공한 개요 run은 JSON 제목 항목과 「검토」 버튼 — 검토에서 의견을 넣고 계속하면 같은 소스·설정으로 덱 run", async () => {
    const user = userEvent.setup();
    setup({ runs: [OUTLINE_RUN] });
    const item = await screen.findByTestId("studio-run-item-run-outline");
    await within(item).findByText("카르파티 LLM 위키 구축하기");
    await user.click(within(item).getByTestId("studio-run-review"));

    const review = await screen.findByTestId("slide-outline-review");
    expect(screen.getByTestId("run-detail-breadcrumb")).toHaveTextContent("슬라이드 자료 개요");
    const items = within(review).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("1.안드레이 카파시의 LLM 위키: 에이전틱 지식 베이스를 선언하는 오프닝");
    await typeInto(user, within(review).getByTestId("slide-outline-feedback"), "결론을 더 짧게");
    await user.click(within(review).getByTestId("slide-outline-continue"));

    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    const [path, body] = api.post.mock.calls[0];
    expect(path).toBe("/api/v1/notebooks/nb-1/agent-runs");
    expect(body).toEqual({
      agent_id: "ag-deck",
      params: {
        format: "presenter",
        length: "short",
        language: "English",
        description: "초보자용",
        image_count: 0, // 장수가 없는 옛 개요 run은 이미지 없이 계속한다
        // 장마다 개요 run의 형식·언어를 싣는다(#218 독립 리뷰 M — 옛 개요의 장에는 없다)
        outline: JSON.stringify({
          ...JSON.parse(OUTLINE_BODY),
          slides: JSON.parse(OUTLINE_BODY).slides.map((s: object) => ({ ...s, format: "presenter", language: "English" })),
        }),
        feedback: "결론을 더 짧게",
        outline_run_id: "run-outline",
      },
      source_ids: ["s2"],
    });
  });

  it("이미 계속한 개요는 「검토」가 없고 검토 화면은 입력 대신 안내를 보인다 — 실패한 덱 run은 세지 않는다", async () => {
    const user = userEvent.setup();
    const continued = run({ id: "run-next", status: "queued", params: { outline_run_id: "run-outline" } });
    setup({ runs: [continued, OUTLINE_RUN] });
    const item = await screen.findByTestId("studio-run-item-run-outline");
    await within(item).findByText("카르파티 LLM 위키 구축하기");
    expect(within(item).queryByTestId("studio-run-review")).toBeNull();
    await user.click(within(item).getByTestId("studio-run-title"));
    await screen.findByTestId("slide-outline-continued");
    expect(screen.queryByTestId("slide-outline-continue")).toBeNull();
  });

  it("개요가 서버 상한보다 길면 「계속」을 끄고 안내한다", async () => {
    const user = userEvent.setup();
    // 따옴표는 JSON에서 두 글자다 — 정규화 상한(장 20·제목 120·요약 300·검색어 200·근거 20개)을 지켜도 넘는다
    const quoted = JSON.stringify({
      title: "긴 개요",
      slides: Array.from({ length: 20 }, () => ({
        title: '"'.repeat(120),
        summary: '"'.repeat(300),
        query: '"'.repeat(200),
        source_ids: Array.from({ length: 10 }, (_, i) => `${i}${'"'.repeat(60)}`),
      })),
    });
    setup({ runs: [OUTLINE_RUN] });
    api.getText.mockResolvedValue(quoted);
    const item = await screen.findByTestId("studio-run-item-run-outline");
    await user.click(await within(item).findByTestId("studio-run-review"));
    await screen.findByTestId("slide-outline-too-long");
    expect(screen.getByTestId("slide-outline-continue")).toBeDisabled();
  });

  it("개요가 읽은 소스(#218) — 대체 본문·배정되지 않은 소스와 장마다 근거를 보이고, 「계속」은 장의 검색어·근거를 넘긴다", async () => {
    const user = userEvent.setup();
    const outline = {
      title: "LLM 위키 입문",
      format: "detailed",
      slides: [
        { title: "표지", summary: "오프닝", layout: "title", query: "LLM 위키", source_ids: ["s1"], format: "detailed", language: "한국어" },
        { title: "표준", summary: "OKF", layout: "bullets", query: "OKF", source_ids: ["s2", "s9"], format: "detailed", language: "한국어" },
      ],
      sources: [
        { id: "s1", title: "카파시 LLM 위키", cached: true },
        { id: "s2", title: "OKF 표준", cached: false },
        { id: "s3", title: "빈 메모", cached: false },
      ],
      degraded: [{ id: "s3", title: "빈 메모", reason: "no_content", fallback: "none" }],
    };
    setup({ runs: [OUTLINE_RUN] });
    api.getText.mockResolvedValue(JSON.stringify(outline));
    const item = await screen.findByTestId("studio-run-item-run-outline");
    await user.click(await within(item).findByTestId("studio-run-review"));

    const review = await screen.findByTestId("slide-outline-review");
    const sources = within(review).getByTestId("slide-outline-sources");
    expect(sources).toHaveTextContent("고른 소스 3개를 모두 요약으로 읽고 개요를 만들었습니다.");
    expect(within(sources).getByTestId("slide-outline-degraded")).toHaveTextContent("요약 대신 원문 일부로 읽은 소스 1개");
    expect(within(sources).getByTestId("slide-outline-degraded")).toHaveTextContent("빈 메모 (본문 없음)");
    expect(within(sources).getByTestId("slide-outline-unassigned")).toHaveTextContent("어느 장에도 배정되지 않은 소스 1개");
    expect(within(sources).getByTestId("slide-outline-unassigned")).toHaveTextContent("빈 메모");
    // 개요가 읽지 않은 id(s9)는 근거 제목으로 그리지 않는다
    expect(within(review).getAllByTestId("slide-outline-item-sources").map((n) => n.textContent)).toEqual([
      "근거: 카파시 LLM 위키",
      "근거: OKF 표준",
    ]);

    await user.click(within(review).getByTestId("slide-outline-continue"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    const sent = JSON.parse(api.post.mock.calls[0][1].params.outline);
    // 형식·언어는 개요 run(발표자·English) 설정으로 덮어쓰고 검색어·근거는 그대로다
    expect(sent).toEqual({
      title: outline.title,
      format: "presenter",
      slides: outline.slides.map((s) => ({ ...s, format: "presenter", language: "English" })),
    });
    expect(api.post.mock.calls[0][1].params.feedback).toBe("");
  });

  it("덱 run이 실패했으면 다시 「검토」할 수 있다", async () => {
    const failed = run({ id: "run-failed", status: "failed", params: { outline_run_id: "run-outline" } });
    setup({ runs: [failed, OUTLINE_RUN] });
    const item = await screen.findByTestId("studio-run-item-run-outline");
    await within(item).findByTestId("studio-run-review");
  });
});

describe("덱 결과 항목(AC-7)", () => {
  it("제목은 덱 JSON title, 열면 「스튜디오 › 슬라이드 자료」 머리와 덱 뷰어", async () => {
    const user = userEvent.setup();
    setup({ runs: [DECK_RUN] });
    const item = await screen.findByTestId("studio-run-item-run-deck");
    await within(item).findByText("LLM 위키");
    expect(within(item).queryByTestId("studio-run-review")).toBeNull();
    await user.click(within(item).getByTestId("studio-run-title"));
    const viewer = await screen.findByTestId("slide-deck-viewer");
    expect(screen.getByTestId("run-detail-breadcrumb")).toHaveTextContent("스튜디오 › 슬라이드 자료");
    expect(within(viewer).getByText("자세한 자료 · 슬라이드 2장")).toBeInTheDocument();
    expect(within(viewer).getAllByRole("group", { name: /^슬라이드 \d+ \/ 2/ })).toHaveLength(2);
  });
});

describe("실행 모델(#216, core-v0.25.0-upgrade AC-12)", () => {
  const MODELS = [
    { id: "m-default", display_name: "gpt-oss-120b", provider_name: "mock", provider_type: "mock", is_default: true },
    { id: "m-flash", display_name: "gemini 3.8 flash", provider_name: "mock", provider_type: "mock", is_default: false },
  ];

  /**
   * 패널 분할 핸들이 문서 pointerdown을 가로채 jsdom에서는 항목 클릭이 확정되지 않는다(위 `typeInto` 주석) — 목록을
   * 연 뒤 키보드로 고른다. 실제 브라우저의 클릭 경로는 e2e(`studio-run-model.spec.ts`)가 본다.
   */
  async function pickModel(user: ReturnType<typeof userEvent.setup>, dialog: HTMLElement, name: string) {
    const trigger = within(dialog).getByRole("combobox", { name: "모델" });
    await waitFor(() => expect(trigger).toHaveTextContent("기본 모델 (gpt-oss-120b)"));
    await user.click(trigger);
    const options = await screen.findAllByRole("option");
    const steps = options.findIndex((o) => o.textContent === name);
    expect(steps).toBeGreaterThan(0);
    await user.keyboard("{ArrowDown}".repeat(steps) + "{Enter}");
    await waitFor(() => expect(trigger).toHaveTextContent(name));
  }

  it("다이얼로그에서 모델을 고르면 덱 run에 model_id를 보낸다(기본값이면 보내지 않는다 — 위 기본값 사례)", async () => {
    const user = userEvent.setup();
    setup({ models: MODELS });
    const dialog = await openDialog(user);
    await pickModel(user, dialog, "gemini 3.8 flash");
    await user.click(within(dialog).getByTestId("slide-deck-generate"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/nb-1/agent-runs", {
      agent_id: "ag-deck",
      params: { format: "detailed", length: "default", language: "한국어", description: "", image_count: 0 },
      model_id: "m-flash",
    });
  });

  it("검토를 켜면 개요 run이 고른 모델을 받는다", async () => {
    const user = userEvent.setup();
    setup({ models: MODELS });
    const dialog = await openDialog(user);
    await pickModel(user, dialog, "gemini 3.8 flash");
    await user.click(within(dialog).getByRole("switch", { name: "슬라이드 개요 검토" }));
    await user.click(within(dialog).getByTestId("slide-deck-generate"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post.mock.calls[0][1]).toMatchObject({ agent_id: "ag-outline", model_id: "m-flash" });
  });

  it("개요 run의 모델로 「계속」하면 덱 run도 같은 모델이다 — 기본 모델 개요는 model_id 없이 계속한다(위 계속 사례)", async () => {
    const user = userEvent.setup();
    setup({ runs: [{ ...OUTLINE_RUN, model_id: "m-flash" } as AgentRun], models: MODELS });
    const item = await screen.findByTestId("studio-run-item-run-outline");
    await user.click(await within(item).findByTestId("studio-run-review"));
    await user.click(await screen.findByTestId("slide-outline-continue"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post.mock.calls[0][1]).toMatchObject({
      agent_id: "ag-deck",
      params: { outline_run_id: "run-outline" },
      source_ids: ["s2"],
      model_id: "m-flash",
    });
  });
});
