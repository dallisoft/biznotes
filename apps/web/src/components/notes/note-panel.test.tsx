import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Note } from "@/lib/note-types";

const { notesData } = vi.hoisted(() => ({ notesData: { current: [] as Note[] } }));

vi.mock("@/hooks/use-notes", () => ({
  useNotes: () => ({ data: notesData.current, isPending: false }),
  useCreateNote: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateNote: () => ({ mutate: vi.fn(), isPending: false }),
  useDeleteNote: () => ({ mutate: vi.fn(), isPending: false }),
  useConvertNoteToSource: () => ({ mutate: vi.fn(), isPending: false }),
}));

const { NotePanel } = await import("@/components/notes/note-panel");

function baseNote(overrides: Partial<Note>): Note {
  return {
    id: "n1",
    notebook_id: "nb1",
    author_id: "u1",
    title: "제목",
    content: "본문",
    origin: "chat",
    origin_ref: "m1",
    citations: [],
    provenance: null,
    content_intact: null,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    ...overrides,
  };
}

async function openNoteViewer(note: Note) {
  notesData.current = [note];
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <NotePanel notebookId="nb1" canWrite={true} />
    </QueryClientProvider>,
  );
  const user = userEvent.setup();
  await user.click(await screen.findByText(note.title));
  await screen.findByTestId("note-viewer");
}

describe("NotePanel — 생성 근거 섹션 5상태(AC-12)", () => {
  it("origin=manual — 섹션 자체가 렌더되지 않는다", async () => {
    await openNoteViewer(baseNote({ origin: "manual", origin_ref: null, provenance: null }));
    expect(screen.queryByTestId("note-provenance")).not.toBeInTheDocument();
  });

  it("(i) provenance=null(비-manual) — '생성 근거 기록 없음'", async () => {
    await openNoteViewer(baseNote({ provenance: null }));
    const section = await screen.findByTestId("note-provenance");
    expect(within(section).getByText("생성 근거 기록 없음")).toBeInTheDocument();
  });

  it("(ii) items 존재 — 모드 라벨 + 스킬/실패 배지", async () => {
    await openNoteViewer(
      baseNote({
        provenance: {
          v: 1,
          mode: "autonomous",
          recorded_at: "2026-09-01T00:00:00Z",
          run_id: null,
          artifact_name: null,
          items: [
            { kind: "skill", status: "succeeded", name: "용도변경 검토", version: 3 },
            { kind: "mcp", status: "failed", server: "archhub", tool: "building_profile" },
          ],
        },
        content_intact: true,
      }),
    );
    const section = await screen.findByTestId("note-provenance");
    expect(within(section).getByText("자율 채팅")).toBeInTheDocument();
    expect(within(section).getByText("스킬 용도변경 검토 v3")).toBeInTheDocument();
    expect(within(section).getByText(/MCP archhub\/building_profile.*실패/)).toBeInTheDocument();
  });

  it("BL-17 — run_skill의 kind=tool 중복 항목은 접혀서 스킬 배지 하나만 남는다", async () => {
    await openNoteViewer(
      baseNote({
        provenance: {
          v: 1,
          mode: "autonomous",
          recorded_at: "2026-09-01T00:00:00Z",
          run_id: null,
          artifact_name: null,
          items: [
            { kind: "skill", status: "succeeded", name: "용도변경 검토", version: 1 },
            { kind: "tool", status: "succeeded", tool: "run_skill" },
          ],
        },
        content_intact: true,
      }),
    );
    const section = await screen.findByTestId("note-provenance");
    expect(within(section).getByText("스킬 용도변경 검토 v1")).toBeInTheDocument();
    expect(within(section).queryByText(/도구 run_skill/)).not.toBeInTheDocument();
  });

  it("(iii) items=[] — '사용 기록된 스킬·MCP·도구·소스 없음'", async () => {
    await openNoteViewer(
      baseNote({
        provenance: {
          v: 1,
          mode: "static",
          recorded_at: "2026-09-01T00:00:00Z",
          run_id: null,
          artifact_name: null,
          items: [],
        },
        content_intact: true,
      }),
    );
    const section = await screen.findByTestId("note-provenance");
    expect(within(section).getByText("사용 기록된 스킬·MCP·도구·소스 없음")).toBeInTheDocument();
  });

  it("(iv) content_intact=false — '생성 후 편집됨' 안내가 함께 뜬다", async () => {
    await openNoteViewer(
      baseNote({
        provenance: {
          v: 1,
          mode: "static",
          recorded_at: "2026-09-01T00:00:00Z",
          run_id: null,
          artifact_name: null,
          items: [],
        },
        content_intact: false,
      }),
    );
    const section = await screen.findByTestId("note-provenance");
    expect(
      within(section).getByText("생성 후 편집됨 — 아래 근거는 생성 시점 기준"),
    ).toBeInTheDocument();
  });

  it("(v) mode=unrecorded — 미기록 안내 문구", async () => {
    await openNoteViewer(
      baseNote({
        origin: "chat",
        provenance: {
          v: 1,
          mode: "unrecorded",
          recorded_at: "2026-09-03T00:00:00Z",
          run_id: null,
          artifact_name: null,
          items: [],
        },
        content_intact: true,
      }),
    );
    const section = await screen.findByTestId("note-provenance");
    expect(within(section).getByText(/생성 근거 미기록/)).toBeInTheDocument();
    expect(within(section).getByText(/본문은 채팅 원문에서 서버가 복사했습니다/)).toBeInTheDocument();
  });
});

describe("NotePanel 머리글 동작(ui-ux-redesign P3 — 패널 머리글 동작은 outline + Plus)", () => {
  function renderPanel(canWrite: boolean) {
    notesData.current = [];
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <NotePanel notebookId="nb1" canWrite={canWrite} />
      </QueryClientProvider>,
    );
  }

  it("「새 메모」는 outline + Plus 아이콘이고 testid `new-note`를 유지한다", () => {
    renderPanel(true);
    const button = screen.getByTestId("new-note");
    expect(button).toHaveAccessibleName("새 메모");
    expect(button.className).toContain("border-border");
    expect(button.querySelector("svg.lucide-plus")).not.toBeNull();
    expect(screen.getByRole("heading", { level: 2, name: "메모" })).toBeInTheDocument();
  });

  it("쓰기 권한이 없으면 「새 메모」가 없다", () => {
    renderPanel(false);
    expect(screen.queryByTestId("new-note")).toBeNull();
  });
});
