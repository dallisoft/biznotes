import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MessageBubble } from "@/components/chat/message-bubble";
import type { ChatEvidence } from "@/lib/chat-types";

// mermaid는 ESM/캔버스 의존이 무거워 단위 테스트에서 목 처리(citation-text.test.tsx와 동일 사유)
vi.mock("@/components/chat/mermaid-diagram", () => ({
  MermaidDiagram: () => <div data-testid="mermaid" />,
}));

describe("MessageBubble — evidence 배지(note-provenance Phase 3, AC-13)", () => {
  it("kind=skill — 이름과 버전을 배지에 싣는다", () => {
    const evidence: ChatEvidence[] = [
      { kind: "skill", status: "succeeded", name: "건축물 용도변경 검토 보고서", version: 3 },
    ];
    render(
      <MessageBubble role="assistant" content="답변" citations={[]} evidence={evidence} />,
    );
    expect(screen.getByTestId("evidence-badge-skill")).toHaveTextContent(
      "스킬 건축물 용도변경 검토 보고서 v3",
    );
  });

  it("실패한 mcp evidence — '실패' 배지를 별도 스타일로 렌더한다", () => {
    const evidence: ChatEvidence[] = [
      { kind: "mcp", status: "failed", server: "archhub", tool: "building_profile" },
    ];
    render(
      <MessageBubble role="assistant" content="답변" citations={[]} evidence={evidence} />,
    );
    const badge = screen.getByTestId("evidence-badge-mcp");
    expect(badge).toHaveTextContent("실패");
    expect(badge.className).toContain("destructive");
  });

  it("unknown 상태 — '상태 미상' 접미사를 붙인다", () => {
    const evidence: ChatEvidence[] = [{ kind: "tool", status: "unknown", tool: "rag_search" }];
    render(
      <MessageBubble role="assistant" content="답변" citations={[]} evidence={evidence} />,
    );
    expect(screen.getByTestId("evidence-badge-tool")).toHaveTextContent("상태 미상");
  });

  // [BL-22] 구 도구-호출 prop 케이스를 kind="tool" evidence 케이스로 치환했다 — 실패한
  // 도구 호출은 이제 provenance items의 kind="tool" 항목으로만 들어온다.
  it("도구 호출 실패 — kind=tool evidence 배지를 '실패'로 표시하고 강조 스타일을 준다", () => {
    const evidence: ChatEvidence[] = [{ kind: "tool", status: "failed", tool: "run_skill" }];
    render(
      <MessageBubble role="assistant" content="답변" citations={[]} evidence={evidence} />,
    );
    const badge = screen.getByTestId("evidence-badge-tool");
    expect(badge).toHaveTextContent("도구 run_skill");
    expect(badge).toHaveTextContent("실패");
    expect(badge.className).toContain("destructive");
  });

  it("evidence가 비어 있으면 근거 영역을 렌더하지 않는다", () => {
    render(<MessageBubble role="assistant" content="답변" citations={[]} />);
    expect(screen.queryByTestId("agent-evidence")).not.toBeInTheDocument();
  });

  it("BL-17 — run_skill의 kind=tool 중복 항목은 배지로 렌더하지 않는다", () => {
    const evidence: ChatEvidence[] = [
      { kind: "skill", status: "succeeded", name: "용도변경 검토", version: 1 },
      { kind: "tool", status: "succeeded", tool: "run_skill" },
    ];
    render(
      <MessageBubble role="assistant" content="답변" citations={[]} evidence={evidence} />,
    );
    expect(screen.getByTestId("evidence-badge-skill")).toBeInTheDocument();
    expect(screen.queryByTestId("evidence-badge-tool")).not.toBeInTheDocument();
  });
});

describe("MessageBubble — 답변 동작 바(ui-ux-improvements B2·③)", () => {
  it("동작 바는 hover 없이도 보인다(opacity-0으로 숨기지 않는다)", () => {
    render(
      <MessageBubble role="assistant" content="답변" citations={[]} onSaveNote={() => {}} onCopy={() => {}} />,
    );
    const bar = screen.getByRole("group", { name: "답변 동작" });
    expect(bar.className).not.toContain("opacity-0");
    expect(screen.getByRole("button", { name: "답변 복사" })).toBeVisible();
    expect(screen.getByRole("button", { name: "메모에 저장" })).toBeVisible();
  });

  it("복사 버튼을 누르면 onCopy를 부른다", async () => {
    const onCopy = vi.fn();
    render(<MessageBubble role="assistant" content="답변" citations={[]} onCopy={onCopy} />);
    screen.getByRole("button", { name: "답변 복사" }).click();
    expect(onCopy).toHaveBeenCalledTimes(1);
  });

  it("동작이 하나도 없으면 동작 바를 렌더하지 않는다", () => {
    render(<MessageBubble role="assistant" content="답변" citations={[]} />);
    expect(screen.queryByRole("group", { name: "답변 동작" })).not.toBeInTheDocument();
  });
});

describe("MessageBubble — 소스 근거 배지로 원문 열기(ui-ux-improvements ②)", () => {
  it("kind=source 배지는 버튼이고 누르면 그 소스의 상세(읽기 모드)를 요청한다", async () => {
    const { useSourceFocus } = await import("@/stores/source-focus");
    useSourceFocus.getState().clear();
    const evidence: ChatEvidence[] = [
      { kind: "source", status: "succeeded", source_id: "s9", title: "취업규칙" },
    ];
    render(<MessageBubble role="assistant" content="답변" citations={[]} evidence={evidence} />);
    const badge = screen.getByRole("button", { name: "소스 취업규칙 원문 보기" });
    badge.click();
    expect(useSourceFocus.getState().requestedSourceId).toBe("s9");
    useSourceFocus.getState().clear();
  });

  it("source_id가 없는 소스 배지는 버튼이 아니다", () => {
    const evidence: ChatEvidence[] = [{ kind: "source", status: "succeeded", title: "제목만" }];
    render(<MessageBubble role="assistant" content="답변" citations={[]} evidence={evidence} />);
    expect(screen.queryByRole("button", { name: /원문 보기/ })).not.toBeInTheDocument();
    expect(screen.getByTestId("evidence-badge-source")).toHaveTextContent("소스 제목만");
  });
});

describe("MessageBubble — 다시 생성(ui-ux-improvements ③)", () => {
  it("onRegenerate가 있으면 동작 바에 「다시 생성」이 있고 누르면 부른다", () => {
    const onRegenerate = vi.fn();
    render(<MessageBubble role="assistant" content="답변" citations={[]} onRegenerate={onRegenerate} />);
    screen.getByRole("button", { name: "다시 생성" }).click();
    expect(onRegenerate).toHaveBeenCalledTimes(1);
  });
});

describe("MessageBubble — 마인드맵 질문·대기 행(mindmap-agent-skill AC-2.6·AC-2.12)", () => {
  it("mindmap_prompt_badge: 마인드맵 질문 user 메시지에 「마인드맵 질문」을 표시한다(일반 질문에는 없음)", () => {
    const { rerender } = render(<MessageBubble role="user" content="질문" citations={[]} mindmapPrompt />);
    expect(screen.getByTestId("mindmap-prompt-badge")).toHaveTextContent("마인드맵 질문");
    rerender(<MessageBubble role="user" content="질문" citations={[]} />);
    expect(screen.queryByTestId("mindmap-prompt-badge")).toBeNull();
  });

  it("대기 상태 문구: not_saved 「전송되지 않음」, uncertain 「응답 중단 — 전송 결과 확인 필요」, sending·confirmed는 문구 없음", () => {
    const onDismiss = vi.fn();
    const { rerender } = render(
      <MessageBubble role="user" content="q" citations={[]} pendingState="not_saved" onDismiss={onDismiss} />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("전송되지 않음");
    screen.getByTestId("pending-dismiss").click();
    expect(onDismiss).toHaveBeenCalledTimes(1);
    rerender(<MessageBubble role="user" content="q" citations={[]} pendingState="uncertain" onDismiss={onDismiss} />);
    expect(screen.getByRole("status")).toHaveTextContent("응답 중단 — 전송 결과 확인 필요");
    rerender(<MessageBubble role="user" content="q" citations={[]} pendingState="sending" />);
    expect(screen.queryByRole("status")).toBeNull();
    rerender(<MessageBubble role="user" content="q" citations={[]} pendingState="confirmed" />);
    expect(screen.queryByRole("status")).toBeNull();
  });
});
