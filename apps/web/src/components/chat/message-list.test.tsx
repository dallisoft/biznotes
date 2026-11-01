import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MessageList, STREAMING_ID, type DisplayMessage } from "@/components/chat/message-list";
import autonomousDone from "@/lib/__fixtures__/sse-done/autonomous.json";
import legacyDone from "@/lib/__fixtures__/sse-done/legacy.json";
import type { ChatEvidence, ChatMessage, ChatStreamEvent } from "@/lib/chat-types";
import { applyChatEvent, INITIAL_STREAM, selectStreamingEvidence } from "@/lib/sse";

// mermaid는 ESM/캔버스 의존이 무거워 단위 테스트에서 목 처리(message-bubble.test.tsx와 동일 사유)
vi.mock("@/components/chat/mermaid-diagram", () => ({
  MermaidDiagram: () => <div data-testid="mermaid" />,
}));

// jsdom에는 scrollIntoView 구현이 없다(source-panel.test.tsx와 동일 처리).
Element.prototype.scrollIntoView = vi.fn();

const doneEvent = (fixture: unknown): ChatStreamEvent => fixture as ChatStreamEvent;

/**
 * [P4-04] **라이브 경로**: SSE `done` → `applyChatEvent` 리듀서 → `chat-panel.tsx`가 쓰는
 * `selectStreamingEvidence`. 테스트 안에서 배열을 그대로 복사하지 않고 실제 코드 경로로
 * 유도한다.
 */
function liveEvidence(): ChatEvidence[] {
  return selectStreamingEvidence(applyChatEvent(INITIAL_STREAM, doneEvent(autonomousDone)));
}

/**
 * [P4-04] **재조회 경로**: `GET /chat/sessions/{id}/messages`가 돌려주는
 * `ChatMessageOut` 모양에서 `chat-panel.tsx:136`과 같은 식(`m.provenance?.items`)으로 유도한다.
 * 백엔드가 두 경로를 dict 동등으로 보장하므로(`test_sse_done_fixture_autonomous_…`)
 * 같은 fixture의 `provenance`를 응답처럼 감싸되 **깊은 복사**해 참조 공유를 끊는다.
 */
function refetchEvidence(): ChatEvidence[] | undefined {
  const payload = JSON.parse(JSON.stringify(autonomousDone)) as { provenance: unknown };
  const message = {
    id: "msg-1",
    role: "assistant",
    content: "재조회 답변",
    citations: [],
    provenance: payload.provenance,
  } as unknown as ChatMessage;
  return message.provenance?.items;
}

function badgeSnapshot(container: HTMLElement) {
  return Array.from(container.querySelectorAll('[data-testid^="evidence-badge-"]')).map((el) => ({
    testId: el.getAttribute("data-testid"),
    label: el.textContent,
    className: el.className,
  }));
}

function renderList(messages: DisplayMessage[]) {
  return render(
    <MessageList
      messages={messages}
      canWrite
      streamingContent=""
      onAddSource={() => {}}
      onSaveNote={() => {}}
    />,
  );
}

describe("MessageList — 라이브 vs 재조회 배지 동일성 (BL-22, AC-15 · P4-04)", () => {
  it("서로 다른 두 경로에서 유도한 evidence가 같은 배지를 만든다", () => {
    const live = liveEvidence();
    const refetched = refetchEvidence();
    // 동어반복 방지: 두 입력은 **다른 참조**여야 한다(같은 배열을 두 번 넘기는 게 아니다).
    expect(refetched).not.toBe(live);
    expect(refetched).toEqual(live);

    renderList([
      { id: "__streaming__", role: "assistant", content: "라이브 답변", citations: [], evidence: live },
      { id: "msg-1", role: "assistant", content: "재조회 답변", citations: [], evidence: refetched },
    ]);

    const containers = screen.getAllByTestId("agent-evidence");
    expect(containers).toHaveLength(2);
    const [liveBox, refetchedBox] = containers;
    expect(badgeSnapshot(liveBox)).toEqual(badgeSnapshot(refetchedBox));
    expect(badgeSnapshot(liveBox)).not.toHaveLength(0);
    expect(within(liveBox).getByTestId("evidence-badge-tool")).toHaveTextContent("도구 rag_search");
  });

  it("provenance가 없는 메시지(legacy 행)는 근거 영역을 그리지 않는다", () => {
    renderList([{ id: "msg-0", role: "assistant", content: "과거 답변", citations: [] }]);
    expect(screen.queryByTestId("agent-evidence")).not.toBeInTheDocument();
  });
});

describe("MessageList — 구 API legacy fallback 렌더 (P4-02, HCI-P2-5)", () => {
  it("provenance 키가 없는 done이면 evidence fallback으로 MCP 배지를 그린다", () => {
    // chat-panel.tsx의 스트리밍 메시지와 **같은 코드 경로**로 evidence를 만든다.
    const evidence = selectStreamingEvidence(
      applyChatEvent(INITIAL_STREAM, doneEvent(legacyDone)),
    );
    renderList([
      { id: "__streaming__", role: "assistant", content: "구 API 답변", citations: [], evidence },
    ]);
    const badge = screen.getByTestId("evidence-badge-mcp");
    expect(badge).toHaveTextContent("MCP archhub/find_region");
  });
});

describe("MessageList — 답변 동작·접근성(ui-ux-improvements B2·③)", () => {
  const answer: DisplayMessage = { id: "a1", role: "assistant", content: "**답변** 본문", citations: [] };

  function renderWith(props: { canWrite: boolean; messages?: DisplayMessage[]; streamingContent?: string }) {
    const onSaveNote = vi.fn();
    render(
      <MessageList
        messages={props.messages ?? [answer]}
        canWrite={props.canWrite}
        streamingContent={props.streamingContent ?? ""}
        onAddSource={() => {}}
        onSaveNote={onSaveNote}
      />,
    );
    return { onSaveNote };
  }

  it("읽기 전용 노트북에서는 「메모에 저장」을 숨기고 복사는 남긴다", () => {
    renderWith({ canWrite: false });
    expect(screen.queryByRole("button", { name: "메모에 저장" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "답변 복사" })).toBeInTheDocument();
  });

  it("쓰기 권한이 있으면 「메모에 저장」이 보이고 누르면 해당 메시지를 넘긴다", () => {
    const { onSaveNote } = renderWith({ canWrite: true });
    screen.getByRole("button", { name: "메모에 저장" }).click();
    expect(onSaveNote).toHaveBeenCalledWith(answer);
  });

  it("복사하면 답변 원문(markdown)을 클립보드에 쓴다", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    renderWith({ canWrite: true });
    screen.getByRole("button", { name: "답변 복사" }).click();
    expect(writeText).toHaveBeenCalledWith("**답변** 본문");
    vi.unstubAllGlobals();
  });

  it("스트리밍 중인 답변에는 동작 바가 없다", () => {
    renderWith({
      canWrite: true,
      messages: [{ id: STREAMING_ID, role: "assistant", content: "생성 중", citations: [] }],
      streamingContent: "생성 중",
    });
    expect(screen.queryByRole("group", { name: "답변 동작" })).not.toBeInTheDocument();
  });

  it("메시지 목록은 role=log 라이브 영역이고, 스트리밍 중에는 aria-busy다", () => {
    renderWith({ canWrite: true, streamingContent: "생성 중" });
    const log = screen.getByRole("log", { name: "대화 내용" });
    expect(log).toHaveAttribute("aria-live", "polite");
    expect(log).toHaveAttribute("aria-busy", "true");
  });
});

describe("MessageList — 다시 생성은 마지막 답변에만(ui-ux-improvements ③)", () => {
  it("마지막 assistant 메시지에만 「다시 생성」을 두고, 누르면 그 메시지를 넘긴다", () => {
    const onRegenerate = vi.fn();
    const msgs: DisplayMessage[] = [
      { id: "u1", role: "user", content: "q1", citations: [] },
      { id: "a1", role: "assistant", content: "a1", citations: [] },
      { id: "u2", role: "user", content: "q2", citations: [] },
      { id: "a2", role: "assistant", content: "a2", citations: [] },
    ];
    render(
      <MessageList
        messages={msgs}
        canWrite
        streamingContent=""
        onAddSource={() => {}}
        onSaveNote={() => {}}
        onRegenerate={onRegenerate}
      />,
    );
    const buttons = screen.getAllByRole("button", { name: "다시 생성" });
    expect(buttons).toHaveLength(1);
    buttons[0].click();
    expect(onRegenerate).toHaveBeenCalledWith(msgs[3]);
  });
});
