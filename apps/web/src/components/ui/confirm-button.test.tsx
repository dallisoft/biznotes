import { useRef, useState } from "react";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ConfirmButton } from "./confirm-button";

describe("ConfirmButton", () => {
  it("확인 버튼을 누르면 onConfirm을 1회 호출하고 다이얼로그를 닫는다", async () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmButton title="전체 재인덱싱" actionLabel="재인덱싱" onConfirm={onConfirm}>
        열기
      </ConfirmButton>,
    );
    await userEvent.click(screen.getByRole("button", { name: "열기" }));
    const dialog = await screen.findByRole("alertdialog");
    // BL-150 P1R-03: 새 prop(confirmDisabled·confirmDisabledReason) 미지정이면 다이얼로그 DOM이 바뀌지 않는다
    const confirm = within(dialog).getByRole("button", { name: "재인덱싱" });
    expect(within(dialog).queryByRole("status")).toBeNull();
    expect(confirm).not.toHaveAttribute("aria-describedby");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    // 바깥 트리거가 다시 접근 가능(모달 inert 해제)
    expect(screen.getByRole("button", { name: "열기" })).toBeVisible();
  });

  it("취소 버튼은 onConfirm 없이 다이얼로그를 닫는다", async () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmButton title="삭제" onConfirm={onConfirm}>
        열기
      </ConfirmButton>,
    );
    await userEvent.click(screen.getByRole("button", { name: "열기" }));
    const dialog = await screen.findByRole("alertdialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "취소" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("disabled면 트리거가 비활성이라 다이얼로그가 열리지 않는다", async () => {
    render(
      <ConfirmButton title="전체 재인덱싱" onConfirm={vi.fn()} disabled>
        열기
      </ConfirmButton>,
    );
    const trigger = screen.getByRole("button", { name: "열기" });
    expect(trigger).toBeDisabled();
    await userEvent.click(trigger);
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  function WithFocusTarget({ onConfirm }: { onConfirm: () => void }) {
    const targetRef = useRef<HTMLDivElement>(null);
    return (
      <>
        <ConfirmButton title="전체 재인덱싱" actionLabel="재인덱싱" onConfirm={onConfirm} focusAfterConfirm={targetRef}>
          열기
        </ConfirmButton>
        <div ref={targetRef} tabIndex={-1} data-testid="focus-target">
          상태
        </div>
      </>
    );
  }

  it("focusAfterConfirm 지정 시 확인으로 닫히면 그 요소로, 취소로 닫히면 트리거로 포커스가 간다", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(<WithFocusTarget onConfirm={onConfirm} />);
    const trigger = screen.getByRole("button", { name: "열기" });

    await user.click(trigger);
    let dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "취소" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());

    await user.click(trigger);
    dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "재인덱싱" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId("focus-target")).toHaveFocus());
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("focusAfterConfirm 미지정이면 확인 후 포커스는 기본 동작대로 트리거로 돌아간다", async () => {
    const user = userEvent.setup();
    render(
      <ConfirmButton title="삭제" onConfirm={vi.fn()}>
        열기
      </ConfirmButton>,
    );
    const trigger = screen.getByRole("button", { name: "열기" });
    await user.click(trigger);
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "삭제" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("열린 동안 confirmDisabled가 true가 되면 확인 버튼이 비활성·사유 연결되고 onConfirm이 호출되지 않는다", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const view = (confirmDisabled: boolean) => (
      <ConfirmButton
        title="전체 재인덱싱"
        actionLabel="재인덱싱"
        onConfirm={onConfirm}
        confirmDisabled={confirmDisabled}
        confirmDisabledReason={confirmDisabled ? "재인덱싱 1건이 진행 중" : ""}
      >
        열기
      </ConfirmButton>
    );
    const { rerender } = render(view(false));
    await user.click(screen.getByRole("button", { name: "열기" }));
    const dialog = await screen.findByRole("alertdialog");
    const confirm = within(dialog).getByRole("button", { name: "재인덱싱" });
    expect(confirm).toBeEnabled();
    const reason = within(dialog).getByRole("status");
    expect(reason).toHaveTextContent(/^$/);

    rerender(view(true));
    expect(confirm).toBeDisabled();
    // 같은 라이브 영역 노드에서 문구만 바뀐다
    expect(within(dialog).getByRole("status")).toBe(reason);
    expect(reason).toHaveTextContent("재인덱싱 1건이 진행 중");
    expect(confirm).toHaveAccessibleDescription("재인덱싱 1건이 진행 중");
    await user.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  });

  // ── BL-150 (plan AC-1.8) ──────────────────────────────────────────────

  it("BL-150 P1R-03: 새 prop 미지정이면 다이얼로그 자식 구조가 제목·버튼뿐이다(라이브 영역·설명 연결 없음)", async () => {
    const user = userEvent.setup();
    render(
      <ConfirmButton title="삭제" onConfirm={vi.fn()}>
        열기
      </ConfirmButton>,
    );
    await user.click(screen.getByRole("button", { name: "열기" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(Array.from(dialog.children).map((c) => c.getAttribute("data-slot"))).toEqual([
      "alert-dialog-header",
      "alert-dialog-footer",
    ]);
    expect(dialog.querySelector("[role='status']")).toBeNull();
    expect(dialog.querySelector("[aria-describedby]")).toBeNull();
    expect(dialog.querySelector("[disabled]")).toBeNull();
  });

  function BusyPage({ busy, withTarget = true }: { busy: boolean; withTarget?: boolean }) {
    const targetRef = useRef<HTMLDivElement>(null);
    return (
      <main id="main-content" tabIndex={-1}>
        <ConfirmButton
          title="전체 재인덱싱"
          actionLabel="재인덱싱"
          onConfirm={vi.fn()}
          disabled={busy}
          confirmDisabled={busy}
          confirmDisabledReason={busy ? "재인덱싱 1건이 진행 중" : ""}
          focusAfterConfirm={withTarget ? targetRef : undefined}
        >
          열기
        </ConfirmButton>
        {withTarget && (
          <div ref={targetRef} tabIndex={-1} data-testid="busy-reason">
            사유
          </div>
        )}
      </main>
    );
  }

  it.each([
    ["취소", "click"],
    ["Esc", "escape"],
  ])(
    "BL-150 P1R-01: 열린 중 busy가 되어 트리거가 비활성이면 %s로 닫을 때 focusAfterConfirm 대상으로 간다(body 유실 없음)",
    async (_, how) => {
      const user = userEvent.setup();
      const { rerender } = render(<BusyPage busy={false} />);
      const trigger = screen.getByRole("button", { name: "열기" });
      await user.click(trigger);
      const dialog = await screen.findByRole("alertdialog");
      await waitFor(() => expect(within(dialog).getByRole("button", { name: "취소" })).toHaveFocus());

      rerender(<BusyPage busy={true} />);
      expect(trigger).toBeDisabled();
      if (how === "click") await user.click(within(dialog).getByRole("button", { name: "취소" }));
      else await user.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
      await waitFor(() => expect(screen.getByTestId("busy-reason")).toHaveFocus());
      expect(document.activeElement).not.toBe(document.body);
    },
  );

  it("BL-150 P1R-01: focusAfterConfirm도 없으면 비활성 트리거 대신 #main-content로 간다", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<BusyPage busy={false} withTarget={false} />);
    await user.click(screen.getByRole("button", { name: "열기" }));
    const dialog = await screen.findByRole("alertdialog");
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "취소" })).toHaveFocus());
    rerender(<BusyPage busy={true} withTarget={false} />);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
  });

  it("BL-150 P1R-01 ③: 확인 뒤 트리거가 요청 대기 중 비활성이 되면 포커스를 대체 대상으로 옮긴다", async () => {
    function PendingPage() {
      const [pending, setPending] = useState(false);
      return (
        <main id="main-content" tabIndex={-1}>
          <ConfirmButton title="철회" actionLabel="공개 철회" onConfirm={() => setPending(true)} disabled={pending}>
            열기
          </ConfirmButton>
        </main>
      );
    }
    const user = userEvent.setup();
    render(<PendingPage />);
    await user.click(screen.getByRole("button", { name: "열기" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "공개 철회" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "열기" })).toBeDisabled();
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
  });

  it("BL-150 P1R-02: 확인 직후 닫히는(퇴장 중인) 다이얼로그의 사유 영역에는 문구를 넣지 않는다", async () => {
    function ClosingPage() {
      const [busy, setBusy] = useState(false);
      return (
        <ConfirmButton
          title="전체 재인덱싱"
          actionLabel="재인덱싱"
          // 확인과 같은 틱에 요청이 시작되어 confirmDisabled가 true가 된다(admin/rag 경로)
          onConfirm={() => setBusy(true)}
          confirmDisabled={busy}
          confirmDisabledReason={busy ? "재인덱싱 요청을 등록하는 중입니다" : ""}
        >
          열기
        </ConfirmButton>
      );
    }
    // 실브라우저의 퇴장 애니메이션(data-closed:animate-out)을 재현한다: jsdom에는 Web Animations가 없어
    // (vitest.setup이 getAnimations를 빈 배열로 둔다) 팝업이 바로 언마운트되므로, 닫히는 다이얼로그 요소에만
    // 끝나지 않은 애니메이션을 돌려줘 Base UI가 언마운트를 기다리게 한다. 포커스·사유 로직은 그대로 실행된다.
    let finishExit = () => {};
    const exitFinished = new Promise<void>((resolve) => (finishExit = resolve));
    const spy = vi
      .spyOn(Element.prototype, "getAnimations")
      .mockImplementation(function (this: Element) {
        const closing =
          this.hasAttribute("data-closed") &&
          this.closest("[data-slot='alert-dialog-portal']") !== null;
        return closing ? ([{ finished: exitFinished }] as unknown as Animation[]) : [];
      });
    try {
      const user = userEvent.setup();
      render(<ClosingPage />);
      await user.click(screen.getByRole("button", { name: "열기" }));
      const dialog = await screen.findByRole("alertdialog");
      const reason = within(dialog).getByRole("status");
      const confirm = within(dialog).getByRole("button", { name: "재인덱싱" });
      await user.click(confirm);
      // 닫힘이 시작됐고(busy가 같은 틱에 true) 팝업은 퇴장 애니메이션 동안 마운트돼 있다
      await waitFor(() => expect(dialog).toHaveAttribute("data-closed"));
      await new Promise((r) => setTimeout(r, 50));
      expect(dialog.isConnected).toBe(true);
      expect(reason).toHaveTextContent(/^$/);
      expect(confirm).not.toHaveAttribute("aria-describedby");
      act(() => finishExit());
      await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    } finally {
      spy.mockRestore();
    }
  });
});
