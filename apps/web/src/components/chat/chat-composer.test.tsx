import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ChatComposer } from "@/components/chat/chat-composer";

function renderComposer(props: Partial<React.ComponentProps<typeof ChatComposer>> = {}) {
  const onSend = vi.fn();
  const onStop = vi.fn();
  const utils = render(
    <ChatComposer
      modelId={null}
      onModelChange={() => {}}
      modelOptions={[]}
      selectedCount={0}
      isStreaming={false}
      disabled={false}
      onSend={onSend}
      onStop={onStop}
      {...props}
    />,
  );
  return { ...utils, onSend, onStop };
}

describe("ChatComposer — 답변 중지(ui-ux-improvements B3)", () => {
  it("생성 중에는 전송 대신 활성화된 「답변 중지」 버튼을 보이고, 누르면 onStop을 부른다", async () => {
    const user = userEvent.setup();
    const { onStop } = renderComposer({ isStreaming: true });
    expect(screen.queryByRole("button", { name: "전송" })).not.toBeInTheDocument();
    const stop = screen.getByRole("button", { name: "답변 중지" });
    expect(stop).toBeEnabled();
    await user.click(stop);
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("생성 중이 아니면 「답변 중지」가 없다", () => {
    renderComposer();
    expect(screen.queryByRole("button", { name: "답변 중지" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "전송" })).toBeInTheDocument();
  });

  it("restoredInput이 바뀌면 입력창에 그 문장을 되돌린다", () => {
    const { rerender } = renderComposer();
    const next = (restoredInput: { text: string; id: number } | null) => (
      <ChatComposer
        modelId={null}
        onModelChange={() => {}}
        modelOptions={[]}
        selectedCount={0}
        isStreaming={false}
        disabled={false}
        onSend={() => {}}
        onStop={() => {}}
        restoredInput={restoredInput}
      />
    );
    rerender(next({ text: "되돌린 질문", id: 1 }));
    expect(screen.getByTestId("chat-input")).toHaveValue("되돌린 질문");
  });
});

describe("ChatComposer — 입력 안내(ui-ux-improvements ⑧)", () => {
  it("placeholder에는 단축키를 넣지 않고, 단축키는 입력창 설명(aria-describedby)으로 둔다", () => {
    renderComposer();
    const input = screen.getByTestId("chat-input");
    expect(input).toHaveAttribute("placeholder", "소스에 대해 질문하세요…");
    expect(input).toHaveAccessibleDescription("Enter 전송 · Shift+Enter 줄바꿈");
  });
});
