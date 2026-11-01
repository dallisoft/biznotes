import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState } from "react";
import { Toaster } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Button } from "@/components/ui/button";
import {
  INITIAL_FOCUS_SETTLE_MS,
  SECRET_REVEAL_WARNING,
  type SecretRevealContent,
  SecretRevealDialog,
} from "./secret-reveal-dialog";

// 결과 다이얼로그(plan §6.5, D-11·D-22) — 실제 Base UI Dialog·포커스 코드로 검사한다(mock 없음). 비밀값은 더미다.

const SECRET = "dummy-temp-pass-7Qx9";
const CONTENT: SecretRevealContent = {
  title: "임시 비밀번호를 발급했습니다",
  description: "‘demo@example.com’ 계정의 임시 비밀번호입니다.",
  fields: [{ label: "임시 비밀번호", value: SECRET, secret: true, testId: "temporary-password" }],
  notice: "첫 로그인 때 비밀번호를 바꿔야 합니다",
};
const WEBHOOK: SecretRevealContent = {
  title: "웹훅을 발급했습니다",
  fields: [
    { label: "URL", value: "http://localhost/api/v1/webhooks/generic/tok" },
    { label: "시크릿", value: SECRET, secret: true, testId: "webhook-secret" },
  ],
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function Harness({ initial = CONTENT, onClose = vi.fn() }: { initial?: SecretRevealContent; onClose?: () => void }) {
  const [content, setContent] = useState<SecretRevealContent | null>(null);
  const opener = useRef<HTMLButtonElement>(null);
  return (
    <>
      <Toaster />
      <Button ref={opener} onClick={() => setContent(initial)}>
        열기
      </Button>
      <Button onClick={() => setContent(null)}>밖에서 지우기</Button>
      <SecretRevealDialog
        content={content}
        onClose={() => {
          onClose();
          setContent(null);
        }}
        returnFocusTo={opener}
      />
    </>
  );
}

async function open(user: ReturnType<typeof userEvent.setup>) {
  const opener = screen.getByRole("button", { name: "열기" });
  await user.click(opener);
  const dialog = await screen.findByRole("dialog");
  return { opener, dialog };
}

describe("SecretRevealDialog", () => {
  it("열린 직후 다른 팝업의 늦은 포커스 복귀가 밖으로 포커스를 옮겨도 「복사」로 되돌린다(secure R-1)", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const { opener, dialog } = await open(user);
    const copy = within(dialog).getByRole("button", { name: "임시 비밀번호 복사" });
    await waitFor(() => expect(copy).toHaveFocus());
    // 막 닫힌 메뉴가 자기 트리거로 포커스를 돌리는 상황을 흉내 낸다(다이얼로그 밖 요소로 프로그램 포커스)
    act(() => opener.focus());
    expect(copy).toHaveFocus();
  });

  it("보호 시간이 지나면 밖으로 옮긴 포커스를 되돌리지 않는다(영구 포커스 강제 아님)", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const { opener, dialog } = await open(user);
    const copy = within(dialog).getByRole("button", { name: "임시 비밀번호 복사" });
    await waitFor(() => expect(copy).toHaveFocus());
    const now = Date.now();
    vi.spyOn(Date, "now").mockReturnValue(now + INITIAL_FOCUS_SETTLE_MS + 1);
    act(() => opener.focus());
    expect(opener).toHaveFocus();
  });

  it("제목·경고·비밀값·안내를 보이고 초기 포커스는 비밀값의 「복사」다", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const { dialog } = await open(user);
    expect(within(dialog).getByRole("heading", { name: CONTENT.title })).toBeInTheDocument();
    expect(dialog).toHaveTextContent(SECRET_REVEAL_WARNING);
    expect(within(dialog).getByTestId("temporary-password")).toHaveTextContent(SECRET);
    expect(dialog).toHaveTextContent(CONTENT.notice!);
    await waitFor(() =>
      expect(within(dialog).getByRole("button", { name: "임시 비밀번호 복사" })).toHaveFocus(),
    );
  });

  it("값이 여러 개면 첫 비밀값(시크릿)의 「복사」가 초기 포커스다", async () => {
    const user = userEvent.setup();
    render(<Harness initial={WEBHOOK} />);
    const { dialog } = await open(user);
    expect(within(dialog).getByTestId("webhook-secret")).toHaveTextContent(SECRET);
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "시크릿 복사" })).toHaveFocus());
    expect(within(dialog).getByRole("button", { name: "URL 복사" })).toBeEnabled();
  });

  it("「복사」는 클립보드에 쓰고, 토스트에는 비밀값을 넣지 않는다", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    render(<Harness />);
    const { dialog } = await open(user);
    await user.click(within(dialog).getByRole("button", { name: "임시 비밀번호 복사" }));
    expect(writeText).toHaveBeenCalledWith(SECRET);
    const toast = await screen.findByText("임시 비밀번호를 복사했습니다");
    expect(toast.closest("[data-sonner-toast]")?.textContent ?? "").not.toContain(SECRET);
  });

  it("클립보드를 쓸 수 없으면 실패 토스트(비밀값 없음)를 보이고 다이얼로그는 그대로다", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("navigator", { ...navigator, clipboard: undefined });
    render(<Harness />);
    const { dialog } = await open(user);
    await user.click(within(dialog).getByRole("button", { name: "임시 비밀번호 복사" }));
    const toast = await screen.findByText("임시 비밀번호를 복사하지 못했습니다");
    expect(toast.closest("[data-sonner-toast]")?.textContent ?? "").not.toContain(SECRET);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it.each([
    ["「닫기」 버튼", async (user: ReturnType<typeof userEvent.setup>, dialog: HTMLElement) => {
      await user.click(within(dialog).getByRole("button", { name: "닫기" }));
    }],
    ["Esc", async (user: ReturnType<typeof userEvent.setup>) => {
      await user.keyboard("{Escape}");
    }],
    ["바깥 클릭", async (user: ReturnType<typeof userEvent.setup>) => {
      const backdrop = document.querySelector<HTMLElement>("[data-slot='dialog-overlay']");
      expect(backdrop).not.toBeNull();
      await user.click(backdrop!);
    }],
  ])("%s로 닫으면 onClose 1회, 비밀값이 DOM에서 사라지고 포커스는 연 곳으로 돌아간다", async (_n, close) => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    const { opener, dialog } = await open(user);
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "임시 비밀번호 복사" })).toHaveFocus());
    await close(user, dialog);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).not.toContain(SECRET);
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("content가 null이 되면(로그아웃·경로 이탈) 닫히고, 닫히는 동안에도 비밀값을 그리지 않는다", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    await open(user);
    // 다이얼로그 밖의 상태 변경 — onOpenChange를 거치지 않는다
    act(() => {
      screen.getByText("밖에서 지우기").click();
    });
    expect(document.body.textContent).not.toContain(SECRET);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(onClose).not.toHaveBeenCalled();
  });

  it("값이 비어 있으면 「—」을 보이고 그 값의 「복사」는 비활성이다", async () => {
    const user = userEvent.setup();
    render(
      <Harness
        initial={{ title: "웹훅을 발급했습니다", fields: [{ label: "시크릿", value: "", secret: true }] }}
      />,
    );
    const { dialog } = await open(user);
    expect(dialog).toHaveTextContent("—");
    expect(within(dialog).getByRole("button", { name: "시크릿 복사" })).toBeDisabled();
  });
});
