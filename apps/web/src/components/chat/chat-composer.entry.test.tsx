import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ChatComposer, type EntrySendResult } from "@/components/chat/chat-composer";
import type { ChatEntry } from "@/lib/chat-types";

const toastMock = vi.hoisted(() => Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() }));
vi.mock("sonner", () => ({ toast: toastMock }));

const BRIEF: ChatEntry = {
  agent_id: "agent-brief",
  kind: "skill",
  slash_name: "brief",
  source: "own",
  display_name: "브리핑",
  description: "짧게 요약한다",
};
const QUIZ: ChatEntry = {
  agent_id: "agent-quiz",
  kind: "skill",
  slash_name: "quiz",
  source: "system",
  display_name: "퀴즈",
  description: "문제를 만든다",
};
const WEEKLY: ChatEntry = {
  agent_id: "agent-weekly",
  kind: "workflow",
  slash_name: null,
  source: "own",
  display_name: "주간 리포트",
  description: "매주 정리",
};

function renderComposer(
  props: Partial<React.ComponentProps<typeof ChatComposer>> = {},
  result: EntrySendResult = { status: "sent" },
) {
  const onSend = vi.fn();
  const onSendEntry = vi.fn(async () => result);
  render(
    <ChatComposer
      modelId={null}
      onModelChange={() => {}}
      modelOptions={[]}
      selectedCount={0}
      isStreaming={false}
      disabled={false}
      onSend={onSend}
      onStop={() => {}}
      entries={[BRIEF, QUIZ, WEEKLY]}
      onSendEntry={onSendEntry}
      {...props}
    />,
  );
  return { onSend, onSendEntry, input: screen.getByTestId("chat-input") as HTMLTextAreaElement };
}

const options = () => screen.queryAllByRole("option").map((o) => o.textContent ?? "");

beforeEach(() => {
  toastMock.mockClear();
  toastMock.error.mockClear();
});

describe("ChatComposer — `/` 팔레트 열림 조건(AC-10)", () => {
  it("첫 글자 `/`에서만 열고, 커서가 첫 토큰을 벗어나면 닫는다", async () => {
    const user = userEvent.setup();
    const { input } = renderComposer();

    await user.type(input, "/");
    expect(screen.getByRole("listbox", { name: "실행할 스킬·워크플로" })).toBeInTheDocument();
    expect(options()).toHaveLength(3);
    // source 라벨 — 본인 스킬·시스템, workflow는 종류
    expect(options()[0]).toContain("내 스킬");
    expect(options()[1]).toContain("시스템");
    expect(options()[2]).toContain("워크플로");

    await user.type(input, "brief ");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("`/`로 시작하지 않으면(`$`·중간의 `/`·앞 공백) 열지 않는다", async () => {
    const user = userEvent.setup();
    const { input } = renderComposer();
    for (const text of ["$brief", "a/b", " /b"]) {
      await user.clear(input);
      await user.type(input, text);
      expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    }
  });

  it("목록을 읽지 못하면(entries=null) 팔레트를 띄우지 않고 `/` 입력도 일반 메시지로 보낸다", async () => {
    const user = userEvent.setup();
    const { input, onSend, onSendEntry } = renderComposer({ entries: null });
    await user.type(input, "/brief 질문");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    await user.keyboard("{Enter}");
    expect(onSend).toHaveBeenCalledWith("/brief 질문");
    expect(onSendEntry).not.toHaveBeenCalled();
  });
});

describe("ChatComposer — 필터·키보드·칩(AC-10)", () => {
  it("slash_name·표시 이름·설명으로 거르고, 없으면 안내한다", async () => {
    const user = userEvent.setup();
    const { input } = renderComposer();
    await user.type(input, "/qu");
    expect(options()).toEqual([expect.stringContaining("퀴즈")]);
    await user.clear(input);
    await user.type(input, "/리포트");
    expect(options()).toEqual([expect.stringContaining("주간 리포트")]);
    await user.clear(input);
    await user.type(input, "/요약한");
    expect(options()).toEqual([expect.stringContaining("브리핑")]);
    await user.clear(input);
    await user.type(input, "/zzz");
    expect(screen.getByText("일치하는 스킬·워크플로가 없습니다")).toBeInTheDocument();
  });

  it("↑↓로 고르고 Enter로 칩을 만든다 — 남은 입력이 질문이다", async () => {
    const user = userEvent.setup();
    const { input } = renderComposer();
    await user.type(input, "/");
    expect(screen.getAllByRole("option")[0]).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowDown}");
    expect(screen.getAllByRole("option")[1]).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowUp}{ArrowUp}"); // 처음에서 위로 가면 끝으로 돈다
    expect(screen.getAllByRole("option")[2]).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{Enter}");

    expect(screen.getByTestId("entry-chip")).toHaveTextContent("주간 리포트"); // slash_name 없는 workflow
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(input).toHaveValue("");
    expect(input).toHaveAttribute("placeholder", "주간 리포트에 보낼 질문을 입력하세요…");
  });

  it("Tab으로도 고르고, 질문을 함께 써 둔 채 고르면 그 질문이 남는다", async () => {
    const user = userEvent.setup();
    const { input } = renderComposer();
    await user.type(input, "/bri 이번 주 요약");
    await user.click(input);
    // 커서를 첫 토큰 안으로 옮기면 다시 연다
    input.setSelectionRange(2, 2);
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    await user.keyboard("{Tab}");
    expect(screen.getByTestId("entry-chip")).toHaveTextContent("/brief");
    expect(input).toHaveValue("이번 주 요약");
  });

  it("Esc는 팔레트를 닫고, 빈 입력에서 Backspace는 칩을 지운다", async () => {
    const user = userEvent.setup();
    const { input } = renderComposer();
    await user.type(input, "/q");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    await user.type(input, "u");
    await user.keyboard("{Enter}");
    expect(screen.getByTestId("entry-chip")).toHaveTextContent("/quiz");
    await user.keyboard("{Backspace}");
    expect(screen.queryByTestId("entry-chip")).not.toBeInTheDocument();
  });

  it("마우스로 고를 수 있고, 칩의 X로 지운다", async () => {
    const user = userEvent.setup();
    const { input } = renderComposer();
    await user.type(input, "/");
    await user.click(screen.getByText("퀴즈"));
    expect(screen.getByTestId("entry-chip")).toHaveTextContent("/quiz");
    await user.click(screen.getByRole("button", { name: "퀴즈 실행 취소" }));
    expect(screen.queryByTestId("entry-chip")).not.toBeInTheDocument();
  });
});

describe("ChatComposer — 전송·이름 풀기(AC-10)", () => {
  it("칩이 있으면 agent_id로 진입 실행하고, 보내면 칩과 입력을 비운다", async () => {
    const user = userEvent.setup();
    const { input, onSend, onSendEntry } = renderComposer();
    await user.type(input, "/bri");
    await user.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: "전송" })).toBeDisabled(); // 질문 없이 보내지 않는다
    await user.type(input, "이번 주 요약{Enter}");

    expect(onSendEntry).toHaveBeenCalledWith({ agent_id: "agent-brief" }, "이번 주 요약");
    expect(onSend).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByTestId("entry-chip")).not.toBeInTheDocument());
    expect(input).toHaveValue("");
  });

  it("칩 없는 `/<이름> 질문`은 name으로 보낸다(one)", async () => {
    const user = userEvent.setup();
    const { input, onSendEntry } = renderComposer();
    await user.type(input, "/brief 이번 주 요약{Enter}");
    expect(onSendEntry).toHaveBeenCalledWith({ name: "brief" }, "이번 주 요약");
    await waitFor(() => expect(input).toHaveValue(""));
  });

  it("none이면 입력을 그대로 둔다(알림은 호출측)", async () => {
    const user = userEvent.setup();
    const { input, onSendEntry } = renderComposer({}, { status: "none" });
    await user.type(input, "/nope 질문{Enter}");
    expect(onSendEntry).toHaveBeenCalledWith({ name: "nope" }, "질문");
    await waitFor(() => expect(screen.getByRole("button", { name: "전송" })).toBeEnabled());
    expect(input).toHaveValue("/nope 질문");
  });

  it("many면 그 후보로 팔레트를 열고, 고르면 칩과 질문이 된다", async () => {
    const user = userEvent.setup();
    const { input } = renderComposer({}, { status: "many", candidates: [BRIEF, QUIZ] });
    await user.type(input, "/dup 질문{Enter}");

    const listbox = await screen.findByRole("listbox");
    expect(listbox).toBeInTheDocument();
    expect(options()).toEqual([expect.stringContaining("브리핑"), expect.stringContaining("퀴즈")]);
    await user.keyboard("{ArrowDown}{Enter}");
    expect(screen.getByTestId("entry-chip")).toHaveTextContent("/quiz");
    expect(input).toHaveValue("질문");
  });

  it("이름만 있고 질문이 없으면 실행하지 않고 안내한다", async () => {
    const user = userEvent.setup();
    const { input, onSendEntry } = renderComposer();
    await user.type(input, "/brief");
    await user.keyboard("{Escape}{Enter}");
    expect(onSendEntry).not.toHaveBeenCalled();
    expect(toastMock).toHaveBeenCalledWith("/brief 뒤에 질문을 함께 입력하세요");
    expect(input).toHaveValue("/brief");
  });

  it("`$`로 시작하는 입력은 일반 메시지다", async () => {
    const user = userEvent.setup();
    const { input, onSend, onSendEntry } = renderComposer();
    await user.type(input, "$brief 질문{Enter}");
    expect(onSend).toHaveBeenCalledWith("$brief 질문");
    expect(onSendEntry).not.toHaveBeenCalled();
  });

  it("진입 요청 중에는 입력을 바꿀 수 없고, 끝나면 보낸 입력을 비운다(리뷰 Medium)", async () => {
    const user = userEvent.setup();
    let resolve!: (r: EntrySendResult) => void;
    const onSendEntry = vi.fn(() => new Promise<EntrySendResult>((r) => (resolve = r)));
    const { input } = renderComposer({ onSendEntry });
    await user.type(input, "/brief 질문{Enter}");
    expect(onSendEntry).toHaveBeenCalledTimes(1);
    expect(input).toHaveAttribute("readonly");
    await user.type(input, " 고친 내용");
    await user.keyboard("{Enter}");
    expect(input).toHaveValue("/brief 질문");
    expect(onSendEntry).toHaveBeenCalledTimes(1); // 요청 중 Enter는 다시 보내지 않는다
    resolve({ status: "sent" });
    await waitFor(() => expect(input).toHaveValue(""));
    expect(input).not.toHaveAttribute("readonly");
  });

  it("진입 run이 끝나기 전에는 전송을 잠근다(D3)", async () => {
    const user = userEvent.setup();
    const { input, onSend } = renderComposer({ entryLocked: true });
    await user.type(input, "다음 질문{Enter}");
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "전송" })).toBeDisabled();
    expect(screen.getByTestId("entry-locked-hint")).toHaveTextContent("실행이 끝나면 보낼 수 있습니다");
  });
});
