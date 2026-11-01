import { useRef, useState } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { ConfirmDialog, focusFallbacks, isFocusable, watchFocusLoss } from "./confirm-dialog";

// 실제 ConfirmDialog·Base UI AlertDialog로 포커스 계약(plan §6.5, AC-1.7)을 검사한다. 포커스 코드는 mock하지 않는다.

function Harness({
  destructive,
  onConfirm = () => {},
  returnAsFunction = false,
  awaitConfirm,
  pendingLabel,
}: {
  destructive?: boolean;
  onConfirm?: () => void | Promise<unknown>;
  returnAsFunction?: boolean;
  awaitConfirm?: boolean;
  pendingLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const openerRef = useRef<HTMLButtonElement>(null);
  return (
    <>
      <button ref={openerRef} onClick={() => setOpen(true)}>
        열기
      </button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title="‘gemini’ 프로바이더를 삭제할까요?"
        description="삭제하면 되돌릴 수 없습니다."
        actionLabel="삭제"
        destructive={destructive}
        onConfirm={onConfirm}
        returnFocusTo={returnAsFunction ? () => openerRef.current : openerRef}
        awaitConfirm={awaitConfirm}
        pendingLabel={pendingLabel}
      />
    </>
  );
}

function deferred<T = unknown>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** 열고 초기 포커스(「취소」)가 자리 잡을 때까지 기다린 뒤 다이얼로그를 돌려준다. */
async function openReady(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "열기" }));
  const dialog = await screen.findByRole("alertdialog");
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "취소" })).toHaveFocus());
  return dialog;
}

describe("ConfirmDialog", () => {
  it("질문형 제목·설명·동사 확인 버튼을 그리고 초기 포커스는 「취소」다", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "열기" }));
    const dialog = await screen.findByRole("alertdialog", { name: "‘gemini’ 프로바이더를 삭제할까요?" });
    expect(dialog).toHaveAccessibleDescription("삭제하면 되돌릴 수 없습니다.");
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "취소" })).toHaveFocus());
  });

  it("위험 확인 버튼은 destructive 채움 + destructive-foreground 글자 토큰을 쓴다(text-white 아님)", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "열기" }));
    const confirm = within(await screen.findByRole("alertdialog")).getByRole("button", { name: "삭제" });
    expect(confirm).toHaveClass("bg-destructive", "text-destructive-foreground", "hover:bg-destructive-hover");
    expect(confirm.className).not.toMatch(/text-white/);
  });

  it("위험 동작이 아니면(destructive=false) 빨간 버튼을 쓰지 않는다", async () => {
    const user = userEvent.setup();
    render(<Harness destructive={false} />);
    await user.click(screen.getByRole("button", { name: "열기" }));
    const confirm = within(await screen.findByRole("alertdialog")).getByRole("button", { name: "삭제" });
    expect(confirm).not.toHaveClass("bg-destructive");
  });

  it.each([
    ["ref", false],
    ["함수", true],
  ])("취소·Esc로 닫으면 returnFocusTo(%s)로 포커스가 돌아가고 onConfirm은 호출되지 않는다", async (_, asFn) => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(<Harness onConfirm={onConfirm} returnAsFunction={asFn} />);
    const opener = screen.getByRole("button", { name: "열기" });

    await user.click(opener);
    let dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "취소" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    await waitFor(() => expect(opener).toHaveFocus());

    act(() => document.body.focus());
    await user.click(opener);
    dialog = await screen.findByRole("alertdialog");
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "취소" })).toHaveFocus());
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    await waitFor(() => expect(opener).toHaveFocus());
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("확인하면 onConfirm 1회 후 닫히고 returnFocusTo로 돌아간다", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(<Harness onConfirm={onConfirm} />);
    const opener = screen.getByRole("button", { name: "열기" });
    await user.click(opener);
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "삭제" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("계약 ④: 확인 콜백이 같은 틱에 결과 다이얼로그를 열어도 확인 다이얼로그가 포커스를 뺏지 않는다", async () => {
    function Chain() {
      const [confirmOpen, setConfirmOpen] = useState(false);
      const [resultOpen, setResultOpen] = useState(false);
      const openerRef = useRef<HTMLButtonElement>(null);
      const copyRef = useRef<HTMLButtonElement>(null);
      return (
        <>
          <button ref={openerRef} onClick={() => setConfirmOpen(true)}>
            초기화 열기
          </button>
          <ConfirmDialog
            open={confirmOpen}
            onOpenChange={setConfirmOpen}
            title="비밀번호를 초기화할까요?"
            actionLabel="초기화"
            onConfirm={() => setResultOpen(true)}
            returnFocusTo={openerRef}
          />
          <Dialog open={resultOpen} onOpenChange={setResultOpen}>
            <DialogContent initialFocus={copyRef} finalFocus={openerRef}>
              <DialogTitle>임시 비밀번호</DialogTitle>
              <Button ref={copyRef}>복사</Button>
            </DialogContent>
          </Dialog>
        </>
      );
    }
    const user = userEvent.setup();
    render(<Chain />);
    await user.click(screen.getByRole("button", { name: "초기화 열기" }));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "초기화" }));
    const result = await screen.findByRole("dialog");
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    await waitFor(() => expect(within(result).getByRole("button", { name: "복사" })).toHaveFocus());
    // 다음 프레임 뒤에도 그대로다(닫힌 확인 다이얼로그의 복귀가 늦게 끼어들지 않음)
    await new Promise((r) => setTimeout(r, 50));
    expect(within(result).getByRole("button", { name: "복사" })).toHaveFocus();
  });
});

describe("포커스 보조 함수", () => {
  it("isFocusable: 분리된·비활성·inert·포커스 불가 요소는 거른다", () => {
    const { container } = render(
      <div>
        <button data-testid="ok">ok</button>
        <button data-testid="disabled" disabled>
          x
        </button>
        <div data-testid="plain">plain</div>
        <div data-testid="tabbable" tabIndex={-1}>
          t
        </div>
        <div inert>
          <button data-testid="inert">i</button>
        </div>
      </div>,
    );
    const get = (id: string) => container.querySelector<HTMLElement>(`[data-testid="${id}"]`);
    expect(isFocusable(get("ok"))).toBe(true);
    expect(isFocusable(get("tabbable"))).toBe(true);
    expect(isFocusable(get("disabled"))).toBe(false);
    expect(isFocusable(get("plain"))).toBe(false);
    expect(isFocusable(get("inert"))).toBe(false);
    expect(isFocusable(document.createElement("button"))).toBe(false);
    expect(isFocusable(null)).toBe(false);
  });

  it("focusFallbacks: 다음 행 ⋯ → 이전 행 ⋯ → 섹션 제목 → #main-content 순서", () => {
    render(
      <main id="main-content" tabIndex={-1}>
        <section>
          <h2 tabIndex={-1} data-focus-fallback="">
            제목
          </h2>
          <ul>
            <li>
              <button data-row-actions-trigger="">a</button>
            </li>
            <li>
              <span>행 동작 없음</span>
            </li>
            <li>
              <button data-testid="origin">b</button>
            </li>
            <li>
              <span>행 동작 없음</span>
            </li>
            <li>
              <button data-row-actions-trigger="">c</button>
            </li>
          </ul>
        </section>
      </main>,
    );
    const origin = screen.getByTestId("origin");
    expect(focusFallbacks(origin).map((el) => (el.tagName === "MAIN" ? "MAIN" : el.textContent))).toEqual([
      "c",
      "a",
      "제목",
      "MAIN",
    ]);
  });

  it("watchFocusLoss: 사용자가 다른 곳으로 포커스를 옮기면 감시를 끝내고, 제한 시간이 지나도 끝난다", async () => {
    vi.useFakeTimers();
    try {
      const { container } = render(
        <div>
          <button data-testid="target">t</button>
          <button data-testid="other">o</button>
          <button data-testid="fallback">f</button>
        </div>,
      );
      const get = (id: string) => container.querySelector<HTMLElement>(`[data-testid="${id}"]`)!;
      // 1) 사용자가 다른 버튼으로 옮긴 뒤 대상이 사라지면 대체 후보로 옮기지 않는다
      get("target").focus();
      watchFocusLoss(get("target"), [get("fallback")]);
      get("other").focus();
      container.append(document.createElement("span")); // 변이 → 감시 종료
      await vi.waitFor(() => Promise.resolve());
      get("target").remove();
      await vi.waitFor(() => Promise.resolve());
      expect(get("other")).toHaveFocus();

      // 2) 제한 시간 뒤에는 대상이 사라져도 아무것도 하지 않는다
      const t2 = document.createElement("button");
      container.prepend(t2);
      t2.focus();
      watchFocusLoss(t2, [get("fallback")], { timeoutMs: 100 });
      vi.advanceTimersByTime(101);
      t2.remove();
      await vi.waitFor(() => Promise.resolve());
      expect(get("fallback")).not.toHaveFocus();
    } finally {
      vi.useRealTimers();
    }
  });

  it("watchFocusLoss: 포커스가 있던 대상이 비활성이 되면 대체 후보로 옮긴다(BL-150 P1R-01 ③)", async () => {
    const { container } = render(
      <div>
        <button data-testid="target">t</button>
        <button data-testid="fallback">f</button>
      </div>,
    );
    const target = container.querySelector<HTMLButtonElement>('[data-testid="target"]')!;
    const fallback = container.querySelector<HTMLButtonElement>('[data-testid="fallback"]')!;
    target.focus();
    watchFocusLoss(target, [fallback]);
    target.disabled = true;
    await waitFor(() => expect(fallback).toHaveFocus());
  });
});

describe("ConfirmDialog — awaitConfirm(확인 뒤 결과를 기다림, P3 리뷰 P3-01)", () => {
  it("대기 중에는 「{동사}하는 중…」·aria-disabled이고 취소·Esc로 닫히지 않으며, 이행하면 그때 닫고 returnFocusTo로 돌아간다", async () => {
    const user = userEvent.setup();
    const job = deferred();
    const onConfirm = vi.fn(() => job.promise);
    render(<Harness awaitConfirm onConfirm={onConfirm} />);
    const dialog = await openReady(user);
    await user.tab();
    await user.keyboard("{Enter}");

    const busy = await within(dialog).findByRole("button", { name: "삭제하는 중…" });
    expect(busy).toHaveAttribute("aria-disabled", "true");
    expect(busy).toHaveAttribute("aria-busy", "true");
    expect(busy).toHaveFocus();
    expect(within(dialog).getByRole("button", { name: "취소" })).toBeDisabled();
    await user.keyboard("{Escape}");
    await user.click(busy);
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(screen.getByRole("alertdialog")).toBe(dialog);
    expect(onConfirm).toHaveBeenCalledTimes(1);

    await act(async () => job.resolve(undefined));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    await waitFor(() => expect(screen.getByRole("button", { name: "열기" })).toHaveFocus());
  });

  it("거부하면 다이얼로그를 연 채 대기만 풀어 다시 시도할 수 있고, 다시 시도가 이행하면 닫힌다", async () => {
    const user = userEvent.setup();
    let attempt = 0;
    const onConfirm = vi.fn(() => (++attempt === 1 ? Promise.reject(new Error("boom")) : Promise.resolve()));
    render(<Harness awaitConfirm onConfirm={onConfirm} />);
    const dialog = await openReady(user);
    await user.click(within(dialog).getByRole("button", { name: "삭제" }));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 250));
    });
    expect(screen.getByRole("alertdialog")).toBe(dialog);
    const retry = within(dialog).getByRole("button", { name: "삭제" });
    expect(retry).not.toHaveAttribute("aria-disabled");
    expect(within(dialog).getByRole("button", { name: "취소" })).toBeEnabled();

    await user.click(retry);
    expect(onConfirm).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  });

  it("같은 틱에 확인이 두 번 들어와도(대기 표시가 그려지기 전) onConfirm은 한 번이다", async () => {
    const user = userEvent.setup();
    const job = deferred();
    const onConfirm = vi.fn(() => job.promise);
    render(<Harness awaitConfirm onConfirm={onConfirm} />);
    const dialog = await openReady(user);
    const action = within(dialog).getByRole("button", { name: "삭제" });
    act(() => {
      fireEvent.click(action);
      fireEvent.click(action);
    });
    expect(onConfirm).toHaveBeenCalledTimes(1);
    await act(async () => job.resolve(undefined));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  });

  it("pendingLabel을 주면 대기 중 문구로 쓴다", async () => {
    const user = userEvent.setup();
    render(<Harness awaitConfirm pendingLabel="지우는 중…" onConfirm={() => new Promise(() => {})} />);
    const dialog = await openReady(user);
    await user.click(within(dialog).getByRole("button", { name: "삭제" }));
    expect(await within(dialog).findByRole("button", { name: "지우는 중…" })).toHaveAttribute("aria-disabled", "true");
  });

  it("awaitConfirm을 주지 않으면 Promise를 돌려주는 onConfirm이어도 기존처럼 확인 즉시 닫는다(하위 호환)", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn(() => new Promise(() => {}));
    render(<Harness onConfirm={onConfirm} />);
    const dialog = await openReady(user);
    await user.click(within(dialog).getByRole("button", { name: "삭제" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(screen.queryByRole("button", { name: "삭제하는 중…" })).toBeNull();
  });
});
