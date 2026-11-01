import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentRun } from "@/lib/agent-types";
import { useChatPrompt } from "@/stores/chat-prompt";
import { useSourceFocus } from "@/stores/source-focus";

// @xyflow/react의 CSS import는 이 프로젝트의 Vite/Vitest 조합에서 PostCSS(Tailwind v4
// 플러그인)와 충돌해 트랜스폼이 실패한다(Next.js 빌드 파이프라인에서는 문제 없음) —
// jsdom 렌더 검증에는 스타일시트가 필요 없으므로 목 처리한다.
vi.mock("@xyflow/react/dist/style.css", () => ({}));

// 뷰포트 함수만 감시한다(§12 R-3) — 레이아웃·노드 컴포넌트·키보드 처리기는 실제로 돈다.
const { flowSpies } = vi.hoisted(() => ({
  flowSpies: {
    zoomIn: vi.fn(),
    zoomOut: vi.fn(),
    fitView: vi.fn(),
    setCenter: vi.fn(),
    getViewport: vi.fn(() => ({ x: 0, y: 0, zoom: 1 })),
  },
}));
vi.mock("@xyflow/react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@xyflow/react")>();
  return { ...actual, useReactFlow: () => flowSpies };
});

// API 경계만 대역 — 소스 목록 조회
const { api } = vi.hoisted(() => ({ api: { get: vi.fn() } }));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));

const { MindmapViewer, nodeAriaLabel, DISCUSS_DEBOUNCE_MS } = await import(
  "@/components/agents/mindmap-viewer"
);

const DOC = JSON.stringify({
  version: 1,
  root: {
    id: "root",
    label: "커피 추출",
    children: [
      {
        id: "n1",
        label: "에스프레소",
        source_ref: { source_id: "s1", chunk_id: "c1" },
        children: [{ id: "n1a", label: "9기압", children: [{ id: "n1a1", label: "크레마" }] }],
      },
      { id: "n2", label: "드립", source_ref: { source_id: "gone" }, children: [] },
    ],
  },
  truncated: false,
});

const RUN = {
  id: "run1",
  notebook_id: "nb1",
  status: "succeeded",
  input_source_ids: ["s1", "s-deleted"],
  artifact_keys: [],
} as unknown as AgentRun;

function renderViewer(content = DOC, run: AgentRun = RUN) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MindmapViewer content={content} run={run} notebookId="nb1" />
    </QueryClientProvider>,
  );
}

const nodeButton = (label: string) =>
  screen.getAllByRole("button").find((b) => b.dataset.mindmapNodeId && b.textContent === label) as HTMLElement;

beforeEach(() => {
  api.get.mockReset();
  api.get.mockResolvedValue([{ id: "s1", title: "커피 교재", status: "ready" }]);
  Object.values(flowSpies).forEach((f) => f.mockClear());
  useSourceFocus.setState({ requestedSourceId: null });
  useChatPrompt.setState({ requests: {}, busyNonce: null });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("MindmapViewer — 해석 실패(AC-1.8)", () => {
  it("JSON이 아니면 오류 문구와 「원본 보기」(원문)를 보인다", () => {
    renderViewer("이것은 JSON이 아닙니다");
    expect(screen.getByText(/해석하지 못했습니다/)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("mindmap-show-raw"));
    expect(screen.getByTestId("mindmap-raw")).toHaveTextContent("이것은 JSON이 아닙니다");
  });

  it("형태가 틀린 JSON은 pretty JSON으로 원본을 보인다", () => {
    renderViewer('{"root":{"id":"","label":"x"}}');
    fireEvent.click(screen.getByTestId("mindmap-show-raw"));
    expect(screen.getByTestId("mindmap-raw").textContent).toContain('\n  "root"');
  });

  it("깊이 초과 본문은 JSON.parse 없이 미리보기만 보인다", () => {
    const spy = vi.spyOn(JSON, "parse");
    const deep = "[".repeat(70) + "]".repeat(70);
    renderViewer(deep);
    expect(screen.getByText(/너무 커서 열지 않았습니다/)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("mindmap-show-raw"));
    expect(screen.getByTestId("mindmap-raw")).toHaveTextContent(deep);
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });
});

describe("MindmapViewer — 뷰어 동작(AC-1.3)", () => {
  it("코어 v0.21.0 #303 최상위 refs_resolved·refs_dropped가 있어도 그대로 렌더한다(core-v0.22.0-upgrade)", () => {
    renderViewer(JSON.stringify({ ...JSON.parse(DOC), refs_resolved: 1, refs_dropped: 2 }));
    expect(screen.getByTestId("mindmap-canvas")).toBeInTheDocument();
    expect(nodeButton("에스프레소")).toBeInTheDocument();
    expect(screen.queryByText(/해석하지 못했습니다/)).toBeNull();
  });

  it("처음에는 루트와 1단계만 보인다(2단계 이상 접힘)", () => {
    renderViewer();
    expect(screen.getByTestId("mindmap-canvas")).toBeInTheDocument();
    expect(nodeButton("커피 추출")).toBeInTheDocument();
    expect(nodeButton("에스프레소")).toBeInTheDocument();
    expect(nodeButton("드립")).toBeInTheDocument();
    expect(screen.queryByText("9기압")).not.toBeInTheDocument();
  });

  it("토글은 >(접힘)·<(펼침) 상태를 aria-expanded·aria-label로 보이고 채팅 요청을 하지 않는다", () => {
    renderViewer();
    const toggles = screen.getAllByTestId("mindmap-node-toggle");
    // 루트(펼침)와 에스프레소(접힘)
    expect(toggles[0]).toHaveAttribute("aria-expanded", "true");
    expect(toggles[0]).toHaveAccessibleName("접기");
    const espresso = toggles[1];
    expect(espresso).toHaveAttribute("aria-expanded", "false");
    expect(espresso).toHaveAccessibleName("펼치기");
    fireEvent.click(espresso);
    expect(screen.getByText("9기압")).toBeInTheDocument();
    fireEvent.click(screen.getAllByTestId("mindmap-node-toggle")[1]);
    expect(screen.queryByText("9기압")).not.toBeInTheDocument();
    expect(Object.keys(useChatPrompt.getState().requests)).toHaveLength(0);
  });

  it("전체 펼침은 모든 노드를, 전체 접힘은 루트만 보인다", () => {
    renderViewer();
    fireEvent.click(screen.getByTestId("mindmap-expand-all"));
    expect(screen.getByText("크레마")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("mindmap-collapse-all"));
    expect(screen.getAllByTestId("mindmap-node")).toHaveLength(1);
    expect(nodeButton("커피 추출")).toBeInTheDocument();
  });

  it("줌 +·−·화면 맞춤 버튼이 React Flow zoomIn·zoomOut·fitView를 부른다", () => {
    renderViewer();
    fireEvent.click(screen.getByTestId("mindmap-zoom-in"));
    fireEvent.click(screen.getByTestId("mindmap-zoom-out"));
    fireEvent.click(screen.getByTestId("mindmap-fit"));
    expect(flowSpies.zoomIn).toHaveBeenCalledTimes(1);
    expect(flowSpies.zoomOut).toHaveBeenCalledTimes(1);
    expect(flowSpies.fitView).toHaveBeenCalledTimes(1);
  });

  it("노드는 깊이별 색 토큰을 쓰고, 비대화형 노드 래퍼(pointer-events:none) 안에서도 클릭을 받는다", () => {
    renderViewer();
    const [root, child] = screen.getAllByTestId("mindmap-node");
    expect(root.className).toContain("pointer-events-auto");
    expect(root.className).toContain("nopan");
    expect(root.className).toContain("bg-[var(--mindmap-depth-0)]");
    expect(child.className).toContain("bg-[var(--mindmap-depth-1)]");
  });

  it("truncated=true면 절단 경고 배지를 표시한다", () => {
    renderViewer(JSON.stringify({ version: 1, root: { id: "r", label: "r" }, truncated: true }));
    expect(screen.getByText(/일부 하위 항목이 생략/)).toBeInTheDocument();
  });
});

describe("MindmapViewer — 소스(AC-1.6·AC-2.3)", () => {
  it("「소스 N개 보기」는 run의 입력 소스 수를 보이고, 목록 항목 클릭 → 소스 포커스, 없는 소스는 「삭제된 소스」", async () => {
    renderViewer();
    const trigger = screen.getByTestId("mindmap-sources");
    expect(trigger).toHaveTextContent("소스 2개 보기");
    fireEvent.click(trigger);
    const list = await screen.findByTestId("mindmap-sources-list");
    await within(list).findByText("커피 교재");
    expect(within(list).getByText("삭제된 소스")).toBeInTheDocument();
    fireEvent.click(within(list).getByText("커피 교재"));
    expect(useSourceFocus.getState().requestedSourceId).toBe("s1");
  });

  it("input_source_ids가 null이면 「전체 소스」", () => {
    renderViewer(DOC, { ...RUN, input_source_ids: null } as AgentRun);
    expect(screen.getByTestId("mindmap-sources")).toHaveTextContent("전체 소스");
  });

  it("「소스로 이동」 아이콘은 목록에 있는 소스에만 보이고, 누르면 소스 포커스(라벨 클릭은 포커스 0)", async () => {
    renderViewer();
    const icons = await screen.findAllByTestId("mindmap-node-source");
    expect(icons).toHaveLength(1); // 「드립」의 source_ref(gone)는 목록에 없다
    fireEvent.click(nodeButton("드립"));
    expect(useSourceFocus.getState().requestedSourceId).toBeNull();
    fireEvent.click(icons[0]);
    expect(useSourceFocus.getState().requestedSourceId).toBe("s1");
  });
});

describe("MindmapViewer — 키보드·스크린리더(AC-1.7)", () => {
  it("노드 aria-label = 「{라벨}, {d}단계, 하위 {m}개, 펼쳐짐|접힘」, 캔버스는 role=group·마인드맵", () => {
    renderViewer();
    expect(nodeButton("커피 추출")).toHaveAccessibleName("커피 추출, 1단계, 하위 2개, 펼쳐짐");
    expect(nodeButton("에스프레소")).toHaveAccessibleName("에스프레소, 2단계, 하위 1개, 접힘");
    expect(nodeButton("드립")).toHaveAccessibleName("드립, 2단계, 하위 0개");
    const canvas = screen.getByTestId("mindmap-canvas");
    expect(canvas).toHaveAttribute("role", "group");
    expect(canvas).toHaveAttribute("aria-roledescription", "마인드맵");
    expect(canvas.getAttribute("aria-describedby")).toBeTruthy();
    expect(typeof nodeAriaLabel).toBe("function");
  });

  it("로빙 tabindex — Tab 진입은 한 곳(처음엔 루트)", () => {
    renderViewer();
    const nodes = screen.getAllByRole("button").filter((b) => b.dataset.mindmapNodeId);
    expect(nodes.filter((b) => b.tabIndex === 0)).toHaveLength(1);
    expect(nodeButton("커피 추출").tabIndex).toBe(0);
  });

  it("↓/↑ 이동, → 펼침 후 첫 자식, ← 접힘 후 부모, Home/End, Space 토글", async () => {
    renderViewer();
    const root = nodeButton("커피 추출");
    root.focus();
    fireEvent.keyDown(root, { key: "ArrowDown" });
    await waitFor(() => expect(document.activeElement).toBe(nodeButton("에스프레소")));
    expect(nodeButton("에스프레소").tabIndex).toBe(0);
    expect(root.tabIndex).toBe(-1);

    fireEvent.keyDown(nodeButton("에스프레소"), { key: "ArrowRight" });
    expect(screen.getByText("9기압")).toBeInTheDocument();
    fireEvent.keyDown(nodeButton("에스프레소"), { key: "ArrowRight" });
    await waitFor(() => expect(document.activeElement).toBe(nodeButton("9기압")));

    fireEvent.keyDown(nodeButton("9기압"), { key: "ArrowLeft" });
    await waitFor(() => expect(document.activeElement).toBe(nodeButton("에스프레소")));
    fireEvent.keyDown(nodeButton("에스프레소"), { key: "ArrowLeft" });
    expect(screen.queryByText("9기압")).not.toBeInTheDocument();

    fireEvent.keyDown(nodeButton("에스프레소"), { key: "End" });
    await waitFor(() => expect(document.activeElement).toBe(nodeButton("드립")));
    fireEvent.keyDown(nodeButton("드립"), { key: "ArrowUp" });
    await waitFor(() => expect(document.activeElement).toBe(nodeButton("에스프레소")));
    fireEvent.keyDown(nodeButton("에스프레소"), { key: " " });
    expect(screen.getByText("9기압")).toBeInTheDocument();
    fireEvent.keyDown(nodeButton("에스프레소"), { key: "Home" });
    await waitFor(() => expect(document.activeElement).toBe(nodeButton("커피 추출")));
    // jsdom 캔버스 크기 0 → 포커스 노드가 화면 밖으로 판정되어 가운데로 데려온다
    expect(flowSpies.setCenter).toHaveBeenCalled();
  });

  it("접혀 사라진 노드에 있던 포커스는 보이는 조상으로 옮겨진다", () => {
    renderViewer();
    fireEvent.click(screen.getByTestId("mindmap-expand-all"));
    fireEvent.focus(nodeButton("크레마"));
    fireEvent.click(screen.getByTestId("mindmap-collapse-all"));
    expect(nodeButton("커피 추출").tabIndex).toBe(0);
  });
});

describe("MindmapViewer — 노드 클릭 → 채팅 질문(AC-2.3)", () => {
  it("discuss_label_click: 접힌 노드 라벨 클릭 → 펼침 + 요청 1회(run 소스 범위·origin=mindmap)", () => {
    renderViewer();
    fireEvent.click(nodeButton("에스프레소"));
    expect(screen.getByText("9기압")).toBeInTheDocument();
    const reqs = Object.values(useChatPrompt.getState().requests);
    expect(reqs).toHaveLength(1);
    expect(reqs[0]).toMatchObject({
      notebookId: "nb1",
      sourceIds: ["s1", "s-deleted"],
      origin: "mindmap",
      text: "이 소스들이 「에스프레소」에 대해 무엇을 말하는지 「커피 추출」의 맥락에서 설명해 주세요.",
    });
  });

  it("discuss_root: 루트는 맥락 절 없는 질문, 이미 펼친 노드는 접지 않는다", () => {
    renderViewer();
    fireEvent.click(nodeButton("커피 추출"));
    expect(nodeButton("에스프레소")).toBeInTheDocument();
    const [req] = Object.values(useChatPrompt.getState().requests);
    expect(req.text).toBe("이 소스들이 「커피 추출」에 대해 무엇을 말하는지 설명해 주세요.");
  });

  it("discuss_enter: Enter도 같은 동작", () => {
    renderViewer();
    fireEvent.keyDown(nodeButton("드립"), { key: "Enter" });
    expect(Object.values(useChatPrompt.getState().requests)).toHaveLength(1);
  });

  it("discuss_debounce: 300ms 안 재클릭은 요청 0(펼침만)", () => {
    renderViewer();
    fireEvent.click(nodeButton("드립"));
    const first = Object.keys(useChatPrompt.getState().requests);
    act(() => useChatPrompt.getState().consume(first[0]));
    fireEvent.click(nodeButton("드립"));
    expect(Object.keys(useChatPrompt.getState().requests)).toHaveLength(0);
    expect(DISCUSS_DEBOUNCE_MS).toBe(300);
  });

  it("discuss_busy: busy 중·같은 노트북 미소비 요청이 있으면 요청 0 + aria-live 안내", () => {
    renderViewer();
    act(() => useChatPrompt.setState({ busyNonce: "other" }));
    fireEvent.click(nodeButton("에스프레소"));
    expect(screen.getByText("9기압")).toBeInTheDocument();
    expect(Object.keys(useChatPrompt.getState().requests)).toHaveLength(0);
    expect(screen.getByTestId("mindmap-announce")).toHaveTextContent("답변이 끝난 뒤 다시 눌러 주세요");

    act(() => useChatPrompt.setState({ busyNonce: null }));
    fireEvent.click(nodeButton("드립"));
    expect(Object.keys(useChatPrompt.getState().requests)).toHaveLength(1);
    fireEvent.click(nodeButton("커피 추출"));
    expect(Object.keys(useChatPrompt.getState().requests)).toHaveLength(1);
  });
});

describe("MindmapViewer — 전체화면(AC-1.5·AC-2.7)", () => {
  it("requestFullscreen을 부르고, fullscreenchange로 해제되면 포커스가 버튼으로 돌아온다", async () => {
    const request = vi.fn(async function (this: HTMLElement) {
      Object.defineProperty(document, "fullscreenElement", { configurable: true, value: this });
    });
    HTMLElement.prototype.requestFullscreen = request as unknown as HTMLElement["requestFullscreen"];
    renderViewer();
    const button = screen.getByTestId("mindmap-fullscreen");
    await act(async () => fireEvent.click(button));
    expect(request).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("mindmap-viewer")).toHaveAttribute("data-fullscreen", "native");
    Object.defineProperty(document, "fullscreenElement", { configurable: true, value: null });
    act(() => {
      document.dispatchEvent(new Event("fullscreenchange"));
    });
    expect(screen.getByTestId("mindmap-viewer")).toHaveAttribute("data-fullscreen", "none");
    expect(document.activeElement).toBe(screen.getByTestId("mindmap-fullscreen"));
    delete (HTMLElement.prototype as Partial<HTMLElement>).requestFullscreen;
  });

  it("미지원이면 고정 오버레이로 대체하고 Esc로 해제, 포커스 복귀", async () => {
    delete (HTMLElement.prototype as Partial<HTMLElement>).requestFullscreen;
    renderViewer();
    await act(async () => fireEvent.click(screen.getByTestId("mindmap-fullscreen")));
    const viewer = screen.getByTestId("mindmap-viewer");
    expect(viewer).toHaveAttribute("data-fullscreen", "overlay");
    expect(viewer.className).toContain("fixed");
    act(() => {
      fireEvent.keyDown(window, { key: "Escape" });
    });
    expect(viewer).toHaveAttribute("data-fullscreen", "none");
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId("mindmap-fullscreen")));
  });

  it("거부되면 오버레이로 대체한다", async () => {
    HTMLElement.prototype.requestFullscreen = vi.fn(async () => {
      throw new Error("denied");
    }) as unknown as HTMLElement["requestFullscreen"];
    renderViewer();
    await act(async () => fireEvent.click(screen.getByTestId("mindmap-fullscreen")));
    expect(screen.getByTestId("mindmap-viewer")).toHaveAttribute("data-fullscreen", "overlay");
    delete (HTMLElement.prototype as Partial<HTMLElement>).requestFullscreen;
  });

  it("fullscreen_discuss: 전체화면 중 노드 클릭은 전체화면을 해제한 뒤 요청한다", async () => {
    renderViewer();
    await act(async () => fireEvent.click(screen.getByTestId("mindmap-fullscreen")));
    expect(screen.getByTestId("mindmap-viewer")).toHaveAttribute("data-fullscreen", "overlay");
    fireEvent.click(nodeButton("드립"));
    expect(screen.getByTestId("mindmap-viewer")).toHaveAttribute("data-fullscreen", "none");
    expect(Object.keys(useChatPrompt.getState().requests)).toHaveLength(1);
  });
});
