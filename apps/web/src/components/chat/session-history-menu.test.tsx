/**
 * 대화 바(plan §6.6.2, AC-3.3): 왼쪽 「대화: {제목} ▾」 드롭다운(대화 기록·고정), 오른쪽 「새 대화」 하나.
 * 실제 컴포넌트·Base UI 메뉴로 검사한다(콜백만 관찰).
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SessionHistoryMenu, sessionTriggerLabel } from "@/components/chat/session-history-menu";
import type { ChatSession } from "@/lib/chat-types";

function session(id: string, title: string, pinned = false): ChatSession {
  return {
    id,
    notebook_id: "nb1",
    title,
    pinned,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
  } as ChatSession;
}

function renderMenu(overrides: Partial<Parameters<typeof SessionHistoryMenu>[0]> = {}) {
  const props = {
    sessions: [session("s1", "첫 대화"), session("s2", "커피 질문", true)],
    sessionId: "s1",
    currentTitle: "첫 대화",
    isStreaming: false,
    creating: false,
    onSessionChange: vi.fn(),
    onTogglePin: vi.fn(),
    onNewSession: vi.fn(),
    ...overrides,
  };
  render(<SessionHistoryMenu {...props} />);
  return props;
}

describe("SessionHistoryMenu — 대화 바(AC-3.3)", () => {
  it("대화 기록 트리거 라벨은 「대화: {제목}」이고, 현재 대화를 아직 모르면 「대화 기록」이다", () => {
    expect(sessionTriggerLabel("첫 대화")).toBe("대화: 첫 대화");
    expect(sessionTriggerLabel(undefined)).toBe("대화 기록");
    expect(sessionTriggerLabel("")).toBe("대화 기록");

    renderMenu();
    expect(screen.getByTestId("session-history")).toHaveAccessibleName("대화: 첫 대화");
  });

  it("「새 대화」 실행 컨트롤은 하나이고 outline + Plus 아이콘이며, 누르면 onNewSession을 부른다", async () => {
    const user = userEvent.setup();
    const props = renderMenu();
    const buttons = screen.getAllByRole("button", { name: "새 대화" });
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveAttribute("data-testid", "new-session");
    expect(buttons[0].className).toContain("border-border"); // outline 변형
    expect(buttons[0].querySelector("svg.lucide-plus")).not.toBeNull();
    await user.click(buttons[0]);
    expect(props.onNewSession).toHaveBeenCalledTimes(1);
  });

  it("좁은 화면에서는 「새 대화」 글자를 시각적으로만 숨긴다(접근 가능한 이름 유지)", () => {
    renderMenu();
    const label = within(screen.getByRole("button", { name: "새 대화" })).getByText("새 대화");
    expect(label).toHaveClass("max-sm:sr-only");
  });

  it("응답 중이거나 대화를 만드는 중에는 「새 대화」가 비활성이다", () => {
    renderMenu({ isStreaming: true });
    expect(screen.getByRole("button", { name: "새 대화" })).toBeDisabled();
  });

  it("만드는 중(creating)에도 비활성이다", () => {
    renderMenu({ creating: true });
    expect(screen.getByRole("button", { name: "새 대화" })).toBeDisabled();
  });

  it("드롭다운에서 대화를 고르면 onSessionChange, 고정 버튼은 onTogglePin(대화 선택은 하지 않음)", async () => {
    const user = userEvent.setup();
    const props = renderMenu();
    await user.click(screen.getByTestId("session-history"));
    const list = await screen.findByTestId("session-list");
    expect(within(list).getByText("대화 기록")).toBeInTheDocument();
    const items = within(list).getAllByTestId("session-item");
    expect(items.map((i) => within(i).getByText(/대화|질문/).textContent)).toEqual(["첫 대화", "커피 질문"]);

    await user.click(within(items[1]).getByRole("button", { name: "고정 해제" }));
    expect(props.onTogglePin).toHaveBeenCalledWith("s2", false);
    expect(props.onSessionChange).not.toHaveBeenCalled();

    await user.click(within(items[1]).getByText("커피 질문"));
    expect(props.onSessionChange).toHaveBeenCalledWith("s2");
  });
});
