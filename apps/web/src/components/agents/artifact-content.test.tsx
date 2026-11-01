import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { AgentRun, ArtifactRef } from "@/lib/agent-types";

const { createNoteMutate } = vi.hoisted(() => ({ createNoteMutate: vi.fn() }));

// @xyflow/react의 CSS import는 이 프로젝트의 Vite/Vitest 조합에서 PostCSS(Tailwind v4
// 플러그인)와 충돌해 트랜스폼이 실패한다(Next.js 빌드 파이프라인에서는 문제 없음) —
// mindmap.json 분기가 이를 렌더하므로 jsdom 테스트에서는 목 처리한다.
vi.mock("@xyflow/react/dist/style.css", () => ({}));

const mindmapDoc = JSON.stringify({
  version: 1,
  root: { id: "root", label: "루트 주제", children: [] },
  truncated: false,
});
const flashcardsDoc = JSON.stringify([
  { id: "c1", question: "질문1", answer: "답1" },
]);
const quizDoc = JSON.stringify([
  { id: "q1", question: "문항1", choices: ["a", "b", "c", "d"], answer_index: 0 },
]);

const infographicDoc = JSON.stringify({
  title: "인포그래픽 제목",
  key_stats: [{ label: "규모", value: "10억" }],
  sections: [],
});

const artifactContent: Record<string, string> = {
  "mindmap.json": mindmapDoc,
  "flashcards.json": flashcardsDoc,
  "quiz.json": quizDoc,
  "infographic.json": infographicDoc,
};

vi.mock("@/hooks/use-agents", () => ({
  fetchArtifact: vi.fn(
    async (_runId: string, name: string) => artifactContent[name] ?? mindmapDoc,
  ),
  fetchArtifactObjectUrl: vi.fn(async () => "blob:mock"),
}));
vi.mock("@/hooks/use-notes", () => ({
  useCreateNote: () => ({ mutate: createNoteMutate, isPending: false }),
}));
vi.mock("@/hooks/use-skills", () => ({
  useCreateSkillFromArtifact: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

const { ArtifactContent } = await import("@/components/agents/artifact-content");

const run: AgentRun = {
  id: "run1",
  agent_id: "agent1",
  notebook_id: "nb1",
  params: {},
  trigger: "manual",
  status: "succeeded",
  progress: 100,
  result_summary: null,
  artifact_keys: [],
  token_usage: {},
  error: null,
  created_at: "2026-01-01T00:00:00Z",
  started_at: null,
  finished_at: null,
  agent_name: "마인드맵",
  agent_icon: "🧠",
  can_cancel: false,
  can_hide: false,
  hidden: false,
  used_models: [],
  fallback_used: false,
};

function renderView(artifact: ArtifactRef, canWrite = true) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ArtifactContent run={run} artifact={artifact} notebookId="nb1" canWrite={canWrite} />
    </QueryClientProvider>,
  );
}

describe("ArtifactContent — mindmap.json 분기 (phase-21-design.md §1.4)", () => {
  it("mindmap.json 아티팩트는 MindmapViewer 캔버스로 렌더한다", async () => {
    renderView({ key: "k", name: "mindmap.json", content_type: "application/json" });
    expect(await screen.findByTestId("mindmap-canvas")).toBeInTheDocument();
    expect(screen.getAllByText("루트 주제").length).toBeGreaterThan(0);
  });

  it("mindmap-agent-skill AC-1.8: 깊이 초과 마인드맵 본문은 JSON.parse 없이 원본 미리보기만 보인다", async () => {
    const deep = "[".repeat(80) + "]".repeat(80);
    const original = artifactContent["mindmap.json"];
    artifactContent["mindmap.json"] = deep;
    try {
      const spy = vi.spyOn(JSON, "parse");
      renderView({ key: "k", name: "mindmap.json", content_type: "application/json" });
      fireEvent.click(await screen.findByTestId("mindmap-show-raw"));
      expect(screen.getByTestId("mindmap-raw")).toHaveTextContent(deep);
      expect(spy).not.toHaveBeenCalled();
      spy.mockRestore();
    } finally {
      artifactContent["mindmap.json"] = original;
    }
  });

  it("다른 JSON 아티팩트는 기존 pretty-print 경로를 그대로 사용한다", async () => {
    renderView({ key: "k", name: "other.json", content_type: "application/json" });
    expect(await screen.findByText(/"root"/)).toBeInTheDocument();
    expect(screen.queryByTestId("mindmap-canvas")).not.toBeInTheDocument();
  });
});

describe("ArtifactContent — flashcards.json/quiz.json 분기 (phase-21-design.md §2.5)", () => {
  it("flashcards.json 아티팩트는 FlashcardViewer로 렌더한다", async () => {
    renderView({ key: "k", name: "flashcards.json", content_type: "application/json" });
    expect(await screen.findByTestId("flashcard-viewer")).toBeInTheDocument();
    expect(screen.getByText("질문1")).toBeInTheDocument();
  });

  it("quiz.json 아티팩트는 QuizViewer로 렌더한다", async () => {
    renderView({ key: "k", name: "quiz.json", content_type: "application/json" });
    expect(await screen.findByTestId("quiz-viewer")).toBeInTheDocument();
    expect(screen.getByText(/문항1/)).toBeInTheDocument();
  });
});

describe("ArtifactContent — infographic.json 분기 (#181)", () => {
  it("infographic.json 아티팩트는 InfographicViewer로 렌더한다", async () => {
    renderView({ key: "k", name: "infographic.json", content_type: "application/json" });
    expect(await screen.findByTestId("infographic-viewer")).toBeInTheDocument();
    expect(screen.getByText("인포그래픽 제목")).toBeInTheDocument();
    expect(screen.getByText("10억")).toBeInTheDocument();
  });
});

describe("ArtifactContent — 메모로 저장 (note-provenance PLAN.md note-create-v2)", () => {
  it("클릭 시 origin=agent + artifact_name을 함께 보낸다(content는 기존대로 유지)", async () => {
    createNoteMutate.mockClear();
    renderView({ key: "k", name: "other.json", content_type: "application/json" });
    await screen.findByText(/"root"/);

    fireEvent.click(screen.getByTestId("artifact-save-to-note"));

    expect(createNoteMutate).toHaveBeenCalledTimes(1);
    const [body] = createNoteMutate.mock.calls[0];
    expect(body).toMatchObject({
      origin: "agent",
      origin_ref: "run1",
      artifact_name: "other.json",
    });
    expect(typeof body.content).toBe("string");
  });
});

describe("ArtifactContent — 「메모로 저장」 권한 표시 (#174)", () => {
  it.each([
    [false, 0],
    [true, 1],
  ])("canWrite=%s이면 「메모로 저장」 버튼이 %i개다", async (canWrite, count) => {
    renderView({ key: "k", name: "result.json", content_type: "application/json" }, canWrite);
    await screen.findByText(/"root"/);
    expect(screen.queryAllByTestId("artifact-save-to-note")).toHaveLength(count);
  });
});
