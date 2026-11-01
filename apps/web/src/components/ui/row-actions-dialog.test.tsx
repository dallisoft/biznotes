import { useState } from "react";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { RowActions } from "./row-actions";

// P3 리뷰 수정의 RowActions 추가 동작 — 위험 항목 `confirm.awaitConfirm`(P3-01)이 ConfirmDialog까지 전달되는지, 다이얼로그를
// 여는 일반 항목을 키보드로 골랐을 때 포커스가 다이얼로그 안에 남는지. 실제 RowActions·ConfirmDialog·Base UI
// Menu/AlertDialog/Dialog로 검사한다(별도 파일: P5가 같은 컴포넌트의 기존 테스트 파일을 확장 중이라 병합 충돌을 피한다).
// P6 정리: P3 `opensDialog`(닫힘 뒤 실행)와 P5-02(복귀 끔 + 사라졌을 때만 ⋯)를 불리언 메뉴 복귀 하나로 합쳤다 — 이 파일의
// 단언은 그대로이고, 표시(prop) 없이 같은 동작을 검사한다(`reports/phase-6/impl-clean.md` ③).

function deferred() {
  let resolve!: () => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<void>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function chooseDelete(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "알파 동작 더보기" }));
  await user.click(await screen.findByRole("menuitem", { name: "삭제" }));
  const dialog = await screen.findByRole("alertdialog", { name: "‘알파’를 삭제할까요?" });
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "취소" })).toHaveFocus());
  return dialog;
}

describe("RowActions — 위험 항목 confirm.awaitConfirm", () => {
  it("awaitConfirm이면 onConfirm의 Promise가 끝날 때까지 확인 다이얼로그가 대기 문구로 열려 있고, 거부하면 남고 이행하면 닫힌다", async () => {
    const user = userEvent.setup();
    const attempts: ReturnType<typeof deferred>[] = [];
    const onConfirm = vi.fn(() => {
      const d = deferred();
      attempts.push(d);
      return d.promise;
    });
    render(
      <RowActions
        rowLabel="알파"
        items={[
          {
            label: "삭제",
            destructive: true,
            confirm: { title: "‘알파’를 삭제할까요?", actionLabel: "삭제", awaitConfirm: true, pendingLabel: "삭제하는 중…" },
            onConfirm,
          },
        ]}
      />,
    );
    const dialog = await chooseDelete(user);
    await user.click(within(dialog).getByRole("button", { name: "삭제" }));
    expect(await within(dialog).findByRole("button", { name: "삭제하는 중…" })).toHaveAttribute("aria-disabled", "true");

    await act(async () => attempts[0].reject(new Error("500")));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 250));
    });
    expect(screen.getByRole("alertdialog")).toBe(dialog);
    await user.click(within(dialog).getByRole("button", { name: "삭제" }));
    expect(onConfirm).toHaveBeenCalledTimes(2);
    await act(async () => attempts[1].resolve());
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    await waitFor(() => expect(screen.getByRole("button", { name: "알파 동작 더보기" })).toHaveFocus());
  });

  it("awaitConfirm이 없으면 기존처럼 확인 즉시 닫는다(하위 호환)", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn(() => new Promise<void>(() => {}));
    render(
      <RowActions
        rowLabel="알파"
        items={[{ label: "삭제", destructive: true, confirm: { title: "‘알파’를 삭제할까요?", actionLabel: "삭제" }, onConfirm }]}
      />,
    );
    const dialog = await chooseDelete(user);
    await user.click(within(dialog).getByRole("button", { name: "삭제" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  });
});

/** 다이얼로그 쪽에 `finalFocus`가 없다(P3 노트북 다이얼로그와 같다) — 닫으면 연 시점의 포커스 기록(⋯)으로 돌아와야 한다. */
function WithDialog() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <RowActions rowLabel="알파" items={[{ label: "이름 변경", onSelect: () => setOpen(true) }]} />
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogTitle>이름 변경</DialogTitle>
          <Input aria-label="이름" defaultValue="알파" />
        </DialogContent>
      </Dialog>
    </>
  );
}

describe("RowActions — 다이얼로그를 여는 일반 항목", () => {
  it("키보드로 고르면 다이얼로그를 열어, 포커스가 다이얼로그 안에 남고(닫히는 메뉴가 덮지 않음) 닫으면 ⋯로 돌아온다", async () => {
    const user = userEvent.setup();
    render(<WithDialog />);
    const more = screen.getByRole("button", { name: "알파 동작 더보기" });
    act(() => more.focus());
    await user.keyboard("{Enter}");
    const item = await screen.findByRole("menuitem", { name: "이름 변경" });
    act(() => item.focus());
    await user.keyboard("{Enter}");
    const dialog = await screen.findByRole("dialog", { name: "이름 변경" });
    await waitFor(() => expect(within(dialog).getByLabelText("이름")).toHaveFocus());
    await act(async () => {
      await new Promise((r) => setTimeout(r, 100));
    });
    expect(within(dialog).getByLabelText("이름")).toHaveFocus();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(more).toHaveFocus());
  });

  it("일반 항목은 고르는 즉시 onSelect를 부른다(하위 호환)", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<RowActions rowLabel="알파" items={[{ label: "복제", onSelect }]} />);
    await user.click(screen.getByRole("button", { name: "알파 동작 더보기" }));
    await user.click(await screen.findByRole("menuitem", { name: "복제" }));
    expect(onSelect).toHaveBeenCalledTimes(1);
  });
});

// P6 정리 — 메뉴 닫힘 포커스 규칙 하나(row-actions.tsx 설명): 일반 항목은 즉시 실행하고, 메뉴는 Base UI 기본(불리언) 복귀로
// 「포커스가 아직 메뉴 안이거나 사라졌을 때만」 ⋯로 돌린다. 함수형 finalFocus로 되돌리면 아래 테스트와 위 키보드 테스트가
// 실패한다(명시 복귀는 모달로 옮겨진 포커스를 ⋯로 덮는다 — P3·P5가 따로 우회하던 원인).
describe("RowActions — 메뉴 닫힘 포커스 규칙(P6 통합)", () => {
  it("마우스로 고른 일반 항목도 다이얼로그 안에 포커스가 남고, 닫으면 ⋯로 돌아온다", async () => {
    const user = userEvent.setup();
    render(<WithDialog />);
    const more = screen.getByRole("button", { name: "알파 동작 더보기" });
    await user.click(more);
    await user.click(await screen.findByRole("menuitem", { name: "이름 변경" }));
    const dialog = await screen.findByRole("dialog", { name: "이름 변경" });
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 100));
    });
    expect(dialog.contains(document.activeElement), document.activeElement?.outerHTML).toBe(true);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(more).toHaveFocus());
  });
});
