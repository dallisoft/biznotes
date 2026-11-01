import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { NotePanel } from "@/components/notes/note-panel";
import type { Note } from "@/lib/note-types";

// API 경계만 대체한다 — useNotes·useUpdateNote 등 훅과 react-query 무효화는 실제 코드로 돈다(§3.3-3 R6).
const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));

const NOTES_PATH = "/api/v1/notebooks/nb1/notes";

function note(overrides: Partial<Note> = {}): Note {
  return {
    id: "n1",
    notebook_id: "nb1",
    author_id: "u1",
    title: "브리핑 문서 — briefing.md",
    content: "## 핵심 요약\n\nAI 에이전트는 행동하는 지능형 행위자로 진화하고 있다.",
    origin: "agent",
    origin_ref: "run-1",
    citations: [],
    provenance: null,
    content_intact: null,
    created_at: "2026-10-16T00:00:00Z",
    updated_at: "2026-10-16T00:00:00Z",
    ...overrides,
  };
}

// 서버 상태 — 변경 요청이 목록에 반영되어야 「저장 후 목록」 흐름을 실제로 확인할 수 있다.
let server: Note[] = [];

beforeEach(() => {
  server = [note()];
  api.get.mockReset();
  api.post.mockReset();
  api.patch.mockReset();
  api.delete.mockReset();
  api.get.mockImplementation((path: string) =>
    Promise.resolve(path === NOTES_PATH ? server.map((n) => ({ ...n })) : []),
  );
  api.patch.mockImplementation((path: string, body: Partial<Note>) => {
    server = server.map((n) => (path === `/api/v1/notes/${n.id}` ? { ...n, ...body } : n));
    return Promise.resolve(server.find((n) => path.endsWith(n.id)));
  });
  api.post.mockImplementation((path: string, body: Partial<Note>) => {
    if (path === NOTES_PATH) {
      const created = note({ id: "n2", origin: "manual", origin_ref: null, ...body });
      server = [created, ...server];
      return Promise.resolve(created);
    }
    return Promise.resolve({});
  });
  api.delete.mockImplementation((path: string) => {
    server = server.filter((n) => path !== `/api/v1/notes/${n.id}`);
    return Promise.resolve(undefined);
  });
});

function renderPanel(canWrite = true) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <NotePanel notebookId="nb1" canWrite={canWrite} />
    </QueryClientProvider>,
  );
}

const noteButton = (title: string) =>
  within(screen.getByTestId("note-list")).getByRole("button", { name: new RegExp(title) });

describe("NotePanel — 메모 보기·편집은 모달이 아니라 우측 패널 인라인", () => {
  it("메모를 누르면 대화상자 없이 패널이 메모 보기로 바뀌고, 「목록」으로 돌아오면 그 메모로 포커스가 돌아온다", async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: /브리핑 문서 — briefing\.md/ }));

    const view = await screen.findByTestId("note-detail-view");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByTestId("note-list")).toBeNull();
    const heading = within(view).getByRole("heading", { level: 2, name: "브리핑 문서 — briefing.md" });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(view).toHaveTextContent("에이전트 결과");
    expect(within(view).getByTestId("note-viewer")).toHaveTextContent("AI 에이전트는 행동하는 지능형 행위자로");
    expect(within(view).getByTestId("note-provenance")).toBeInTheDocument();

    await user.click(within(view).getByTestId("note-detail-back"));
    await waitFor(() => expect(noteButton("브리핑 문서")).toHaveFocus());
  });

  it("「편집」은 같은 패널에서 편집으로 바뀌고, 「취소」는 원래 내용의 보기로, 「저장」은 PATCH 뒤 목록으로 돌아간다", async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: /브리핑 문서 — briefing\.md/ }));
    const view = await screen.findByTestId("note-detail-view");

    await user.click(within(view).getByTestId("note-edit"));
    const titleInput = within(view).getByTestId("note-title-input");
    await waitFor(() => expect(titleInput).toHaveFocus());
    expect(within(view).getByRole("heading", { level: 2 })).toHaveTextContent("메모 편집");
    await user.clear(titleInput);
    await user.type(titleInput, "버려질 제목");
    await user.click(within(view).getByTestId("note-cancel"));
    // 보기로 돌아오면 본문 마크다운의 ## 머리글도 h2라 이름으로 찾는다
    expect(
      within(view).getByRole("heading", { level: 2, name: "브리핑 문서 — briefing.md" }),
    ).toBeInTheDocument();
    expect(within(view).getByTestId("note-viewer")).toHaveTextContent("AI 에이전트는");
    expect(api.patch).not.toHaveBeenCalled();

    await user.click(within(view).getByTestId("note-edit"));
    await user.clear(within(view).getByTestId("note-title-input"));
    await user.type(within(view).getByTestId("note-title-input"), "임원 브리핑 (수정)");
    await user.click(within(view).getByTestId("note-save"));

    await waitFor(() =>
      expect(api.patch).toHaveBeenCalledWith("/api/v1/notes/n1", {
        title: "임원 브리핑 (수정)",
        content: note().content,
      }),
    );
    await waitFor(() => expect(noteButton("임원 브리핑 \\(수정\\)")).toHaveFocus());
    expect(screen.queryByTestId("note-detail-view")).toBeNull();
  });

  it("「새 메모」는 패널 안에서 바로 편집으로 열리고, 저장하면 목록으로 돌아와 「새 메모」로 포커스가 간다", async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByTestId("new-note"));
    const view = await screen.findByTestId("note-detail-view");
    expect(within(view).getByRole("heading", { level: 2 })).toHaveTextContent("새 메모");
    await waitFor(() => expect(within(view).getByTestId("note-title-input")).toHaveFocus());

    await user.type(within(view).getByTestId("note-title-input"), "회의 정리");
    await user.type(within(view).getByTestId("note-content-input"), "결정사항: 초안 작성");
    await user.click(within(view).getByTestId("note-save"));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith(NOTES_PATH, { title: "회의 정리", content: "결정사항: 초안 작성" }),
    );
    expect(await screen.findByRole("button", { name: /회의 정리/ })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("new-note")).toHaveFocus());
  });

  it("삭제하면 목록으로 돌아오고, 연 메모가 사라졌으므로 패널 머리글로 포커스가 간다", async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: /브리핑 문서 — briefing\.md/ }));
    const view = await screen.findByTestId("note-detail-view");
    await user.click(within(view).getByRole("button", { name: "삭제" }));

    await waitFor(() => expect(api.delete).toHaveBeenCalledWith("/api/v1/notes/n1"));
    expect(await screen.findByText("아직 메모가 없습니다.", { exact: false })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("heading", { level: 2, name: "메모" })).toHaveFocus());
  });

  it("읽기 전용이면 패널 안에서 보기만 하고 편집·삭제·소스 변환 동작이 없다", async () => {
    const user = userEvent.setup();
    renderPanel(false);
    await user.click(await screen.findByRole("button", { name: /브리핑 문서 — briefing\.md/ }));
    const view = await screen.findByTestId("note-detail-view");
    expect(within(view).getByTestId("note-viewer")).toBeInTheDocument();
    expect(within(view).queryByTestId("note-edit")).toBeNull();
    expect(within(view).queryByRole("button", { name: "삭제" })).toBeNull();
    expect(within(view).queryByRole("button", { name: "소스로 변환" })).toBeNull();
  });
});
