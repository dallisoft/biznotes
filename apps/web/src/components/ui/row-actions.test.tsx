import { useRef, useState } from "react";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { stubNarrowViewport } from "@/hooks/use-narrow-viewport.test-kit";
import { useReturnFocus } from "./confirm-dialog";
import { RowActions } from "./row-actions";

// 포커스 계약(AC-1.7, plan §6.5 r1 PR-12)은 실제 RowActions·ConfirmDialog·Base UI Menu/AlertDialog로 검사한다.
// 포커스 관리 코드는 mock하지 않는다. 삭제 요청은 실제 화면처럼 비동기로 끝난다(테스트가 resolve 시점을 정한다).

type Deferred = { name: string; resolve: () => void };

function DeleteTable({
  initial,
  withHeading = true,
  withMain = true,
  onDeleteRequested,
}: {
  initial: string[];
  withHeading?: boolean;
  withMain?: boolean;
  onDeleteRequested: (d: Deferred) => void;
}) {
  const [rows, setRows] = useState(initial);
  const table = (
    <section>
      {withHeading && (
        <h2 tabIndex={-1} data-focus-fallback="">
          항목 목록
        </h2>
      )}
      <table>
        <tbody>
          {rows.map((name) => (
            <tr key={name}>
              <td>{name}</td>
              <td>
                <RowActions
                  rowLabel={name}
                  primary={{ label: "수정", onClick: () => {} }}
                  items={[
                    { label: "복제", onSelect: () => {} },
                    {
                      label: "삭제",
                      destructive: true,
                      confirm: { title: `‘${name}’ 항목을 삭제할까요?`, actionLabel: "삭제" },
                      onConfirm: () =>
                        onDeleteRequested({
                          name,
                          resolve: () => setRows((r) => r.filter((x) => x !== name)),
                        }),
                    },
                  ]}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
  return withMain ? (
    <main id="main-content" tabIndex={-1}>
      {table}
    </main>
  ) : (
    table
  );
}

const moreButton = (name: string) => screen.getByRole("button", { name: `${name} 동작 더보기` });

/** 키보드로 행의 ⋯ 메뉴를 열고(Enter 또는 ↓) 「삭제」를 Enter로 선택해 확인 다이얼로그를 띄운다. */
async function openDeleteDialogByKeyboard(
  user: ReturnType<typeof userEvent.setup>,
  name: string,
  openKey: "{Enter}" | "{ArrowDown}" = "{Enter}",
) {
  const trigger = moreButton(name);
  act(() => trigger.focus());
  await user.keyboard(openKey);
  const menu = await screen.findByRole("menu");
  // 메뉴의 첫 포커스는 다음 프레임이다 — 항목에 포커스가 생긴 뒤 움직여야 늦은 포커스 사이로 지나치지 않는다
  await waitFor(() => expect(document.activeElement?.getAttribute("role")).toBe("menuitem"));
  // 첫 항목에서 시작하지 않을 수 있으므로 「삭제」에 닿을 때까지 ↓
  for (let i = 0; i < 5; i += 1) {
    if (document.activeElement?.textContent === "삭제") break;
    await user.keyboard("{ArrowDown}");
  }
  expect(document.activeElement).toBe(within(menu).getByRole("menuitem", { name: "삭제" }));
  await user.keyboard("{Enter}");
  return screen.findByRole("alertdialog");
}

describe("RowActions", () => {
  it("주 동작 버튼과 「{행 이름} 동작 더보기」 버튼을 그리고, 메뉴는 일반 동작 → 구분선 → 위험 동작 순이다", async () => {
    const user = userEvent.setup();
    const onEdit = vi.fn();
    render(
      <RowActions
        rowLabel="gemini"
        primary={{ label: "수정", onClick: onEdit }}
        items={[
          {
            label: "삭제",
            destructive: true,
            confirm: { title: "삭제할까요?", actionLabel: "삭제" },
            onConfirm: vi.fn(),
          },
          { label: "복제", onSelect: vi.fn() },
        ]}
      />,
    );
    await user.click(screen.getByRole("button", { name: "수정" }));
    expect(onEdit).toHaveBeenCalledTimes(1);

    const trigger = screen.getByRole("button", { name: "gemini 동작 더보기" });
    expect(trigger).toHaveAttribute("data-row-actions-trigger");
    await user.click(trigger);
    const menu = await screen.findByRole("menu");
    const items = within(menu).getAllByRole("menuitem");
    expect(items.map((i) => i.textContent)).toEqual(["복제", "삭제"]);
    expect(items[1]).toHaveAttribute("data-variant", "destructive");
    expect(within(menu).getByRole("separator")).toBeInTheDocument();
  });

  it("주 동작이 없으면 더보기만, 위험 동작이 없으면 구분선이 없다", async () => {
    const user = userEvent.setup();
    const onCopy = vi.fn();
    render(<RowActions rowLabel="a" items={[{ label: "복제", onSelect: onCopy }]} />);
    expect(screen.getAllByRole("button")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "a 동작 더보기" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).queryByRole("separator")).toBeNull();
    await user.click(within(menu).getByRole("menuitem", { name: "복제" }));
    expect(onCopy).toHaveBeenCalledTimes(1);
  });

  it("비활성 항목은 사유를 함께 보이고 선택해도 실행되지 않는다", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(
      <RowActions
        rowLabel="나"
        items={[
          {
            label: "비활성화",
            destructive: true,
            disabled: true,
            disabledReason: "자기 계정은 비활성화할 수 없습니다",
            confirm: { title: "비활성화할까요?", actionLabel: "비활성화" },
            onConfirm,
          },
        ]}
      />,
    );
    await user.click(screen.getByRole("button", { name: "나 동작 더보기" }));
    const item = await screen.findByRole("menuitem", { name: /비활성화/ });
    expect(item).toHaveTextContent("자기 계정은 비활성화할 수 없습니다");
    expect(item).toHaveAttribute("aria-disabled", "true");
    await user.click(item);
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("위험 항목 선택만으로는 콜백 0회, 다이얼로그에서 확인해야 1회 호출된다", async () => {
    const user = userEvent.setup();
    const requested = vi.fn();
    render(<DeleteTable initial={["alpha"]} onDeleteRequested={requested} />);
    await user.click(moreButton("alpha"));
    await user.click(await screen.findByRole("menuitem", { name: "삭제" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("‘alpha’ 항목을 삭제할까요?");
    expect(requested).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "삭제" }));
    expect(requested).toHaveBeenCalledTimes(1);
    expect(requested.mock.calls[0][0].name).toBe("alpha");
  });

  describe("확인을 받는 일반 항목(confirm — §6.6.2 사용자 행 「비밀번호 초기화…」, P5)", () => {
    function ResetAndDeactivate({ onReset, onDeactivate }: { onReset: () => void; onDeactivate: () => void }) {
      return (
        <table>
          <tbody>
            <tr>
              <td>kim</td>
              <td>
                <RowActions
                  rowLabel="kim"
                  primary={{ label: "사용 한도", onClick: () => {} }}
                  items={[
                    {
                      label: "비활성화…",
                      destructive: true,
                      confirm: { title: "‘kim’ 계정을 비활성화할까요?", actionLabel: "비활성화" },
                      onConfirm: onDeactivate,
                    },
                    {
                      label: "비밀번호 초기화…",
                      confirm: { title: "‘kim’ 계정의 비밀번호를 초기화할까요?", actionLabel: "초기화" },
                      onSelect: onReset,
                    },
                  ]}
                />
              </td>
            </tr>
          </tbody>
        </table>
      );
    }

    it("일반 구획(구분선 위)에 남고, 선택만으로는 0회 · 확인해야 1회 실행되며 확인 버튼은 빨강이 아니다", async () => {
      const user = userEvent.setup();
      const onReset = vi.fn();
      render(<ResetAndDeactivate onReset={onReset} onDeactivate={vi.fn()} />);
      await user.click(moreButton("kim"));
      const menu = await screen.findByRole("menu");
      const items = within(menu).getAllByRole("menuitem");
      expect(items.map((i) => i.textContent)).toEqual(["비밀번호 초기화…", "비활성화…"]);
      expect(items[0]).not.toHaveAttribute("data-variant", "destructive");
      expect(within(menu).getByRole("separator")).toBeInTheDocument();

      await user.click(items[0]);
      const dialog = await screen.findByRole("alertdialog");
      expect(dialog).toHaveTextContent("‘kim’ 계정의 비밀번호를 초기화할까요?");
      expect(onReset).not.toHaveBeenCalled();
      const confirm = within(dialog).getByRole("button", { name: "초기화" });
      expect(confirm.className).not.toMatch(/bg-destructive/);
      await user.click(confirm);
      expect(onReset).toHaveBeenCalledTimes(1);
    });

    it("키보드(Enter → ↓ → Enter)로 고르면 초기 포커스는 「취소」이고, 취소하면 0회 · 그 행 ⋯로 복귀한다", async () => {
      const user = userEvent.setup();
      const onReset = vi.fn();
      render(<ResetAndDeactivate onReset={onReset} onDeactivate={vi.fn()} />);
      const trigger = moreButton("kim");
      act(() => trigger.focus());
      await user.keyboard("{Enter}");
      const menu = await screen.findByRole("menu");
      const target = within(menu).getByRole("menuitem", { name: "비밀번호 초기화…" });
      // 메뉴가 첫 항목에 포커스를 두는 것은 다음 프레임이다 — 항목에 포커스가 생긴 뒤 이동한다
      await waitFor(() => expect(document.activeElement?.getAttribute("role")).toBe("menuitem"));
      for (let i = 0; i < 5 && document.activeElement !== target; i += 1) {
        await user.keyboard("{ArrowDown}");
      }
      expect(target).toHaveFocus();
      await user.keyboard("{Enter}");
      const dialog = await screen.findByRole("alertdialog");
      const cancel = within(dialog).getByRole("button", { name: "취소" });
      await waitFor(() => expect(cancel).toHaveFocus());
      // 닫히는 메뉴의 뒤늦은 포커스 복귀가 「취소」를 빼앗지 않는다
      await act(() => new Promise<void>((r) => setTimeout(r, 0)));
      await act(() => new Promise<void>((r) => requestAnimationFrame(() => r())));
      expect(cancel).toHaveFocus();
      await user.tab();
      await waitFor(() => expect(within(dialog).getByRole("button", { name: "초기화" })).toHaveFocus());

      await user.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
      await waitFor(() => expect(trigger).toHaveFocus());
      expect(onReset).not.toHaveBeenCalled();
    });

    it("위험 항목은 그대로 빨간 확인 버튼을 쓴다(같은 행에서 일반 확인 뒤에 열어도 섞이지 않는다)", async () => {
      const user = userEvent.setup();
      const onReset = vi.fn();
      const onDeactivate = vi.fn();
      render(<ResetAndDeactivate onReset={onReset} onDeactivate={onDeactivate} />);
      await user.click(moreButton("kim"));
      await user.click(await screen.findByRole("menuitem", { name: "비밀번호 초기화…" }));
      await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "취소" }));
      await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());

      await user.click(moreButton("kim"));
      await user.click(await screen.findByRole("menuitem", { name: "비활성화…" }));
      const dialog = await screen.findByRole("alertdialog");
      expect(dialog).toHaveTextContent("‘kim’ 계정을 비활성화할까요?");
      const confirm = within(dialog).getByRole("button", { name: "비활성화" });
      expect(confirm.className).toMatch(/bg-destructive/);
      await user.click(confirm);
      expect(onDeactivate).toHaveBeenCalledTimes(1);
      expect(onReset).not.toHaveBeenCalled();
    });
  });

  describe("포커스 계약 (키보드)", () => {
    it("Enter로 열기 → 위험 항목 Enter → 초기 포커스 「취소」 → Tab·Shift+Tab 순환 → Esc로 그 행 ⋯ 복귀", async () => {
      const user = userEvent.setup();
      const requested = vi.fn();
      render(<DeleteTable initial={["alpha", "beta", "gamma"]} onDeleteRequested={requested} />);

      const dialog = await openDeleteDialogByKeyboard(user, "beta", "{Enter}");
      const cancel = within(dialog).getByRole("button", { name: "취소" });
      const confirm = within(dialog).getByRole("button", { name: "삭제" });
      await waitFor(() => expect(cancel).toHaveFocus());

      // Tab·Shift+Tab은 다이얼로그 안에서만 순환한다. 끝에서 넘어가면 Base UI 포커스 가드가 반대쪽 끝으로 되돌린다
      // (가드의 되돌림은 다음 프레임에 일어나므로 waitFor로 기다린다).
      await user.tab();
      await waitFor(() => expect(confirm).toHaveFocus());
      await user.tab();
      await waitFor(() => expect(cancel).toHaveFocus());
      await user.tab({ shift: true });
      await waitFor(() => expect(confirm).toHaveFocus());
      await user.tab({ shift: true });
      await waitFor(() => expect(cancel).toHaveFocus());
      // 순환하는 동안 다이얼로그 밖(다른 행 ⋯ 등)으로 나가지 않았다
      expect(dialog.contains(document.activeElement)).toBe(true);

      await user.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
      await waitFor(() => expect(moreButton("beta")).toHaveFocus());
      expect(requested).not.toHaveBeenCalled();
    });

    it("↓로 열기 → 위험 항목 선택 → 「취소」(Enter)로 닫으면 그 행 ⋯로 복귀", async () => {
      const user = userEvent.setup();
      render(<DeleteTable initial={["alpha", "beta"]} onDeleteRequested={vi.fn()} />);
      const dialog = await openDeleteDialogByKeyboard(user, "alpha", "{ArrowDown}");
      const cancel = within(dialog).getByRole("button", { name: "취소" });
      await waitFor(() => expect(cancel).toHaveFocus());
      await user.keyboard("{Enter}");
      await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
      await waitFor(() => expect(moreButton("alpha")).toHaveFocus());
    });

    it("바깥 클릭으로는 닫히지 않는다(AlertDialog) — 이후 Esc로 닫으면 그 행 ⋯로 복귀", async () => {
      const user = userEvent.setup();
      const requested = vi.fn();
      render(<DeleteTable initial={["alpha"]} onDeleteRequested={requested} />);
      const dialog = await openDeleteDialogByKeyboard(user, "alpha");
      const cancel = within(dialog).getByRole("button", { name: "취소" });
      await waitFor(() => expect(cancel).toHaveFocus());
      await user.click(document.body);
      expect(screen.getByRole("alertdialog")).toBe(dialog);
      expect(requested).not.toHaveBeenCalled();
      await user.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
      await waitFor(() => expect(moreButton("alpha")).toHaveFocus());
    });

    it("확인 → (삭제 성공으로) 행이 사라지면 다음 행 ⋯로 이동한다", async () => {
      const user = userEvent.setup();
      let pending: Deferred | undefined;
      render(
        <DeleteTable initial={["alpha", "beta", "gamma"]} onDeleteRequested={(d) => (pending = d)} />,
      );
      const dialog = await openDeleteDialogByKeyboard(user, "beta");
      await user.click(within(dialog).getByRole("button", { name: "삭제" }));
      await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
      // 요청이 끝나기 전에는 그 행 ⋯에 있다
      await waitFor(() => expect(moreButton("beta")).toHaveFocus());
      expect(pending?.name).toBe("beta");

      act(() => pending?.resolve());
      expect(screen.queryByRole("button", { name: "beta 동작 더보기" })).toBeNull();
      await waitFor(() => expect(moreButton("gamma")).toHaveFocus());
    });

    it("마지막 행이 사라지면 이전 행 ⋯로 이동한다", async () => {
      const user = userEvent.setup();
      let pending: Deferred | undefined;
      render(<DeleteTable initial={["alpha", "beta"]} onDeleteRequested={(d) => (pending = d)} />);
      const dialog = await openDeleteDialogByKeyboard(user, "beta");
      await waitFor(() => expect(within(dialog).getByRole("button", { name: "취소" })).toHaveFocus());
      await user.tab();
      await waitFor(() => expect(within(dialog).getByRole("button", { name: "삭제" })).toHaveFocus());
      await user.keyboard("{Enter}");
      await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
      expect(dialog.isConnected).toBe(false);
      act(() => pending?.resolve());
      await waitFor(() => expect(moreButton("alpha")).toHaveFocus());
    });

    it("남은 행이 없으면 표 섹션 제목으로 이동한다", async () => {
      const user = userEvent.setup();
      let pending: Deferred | undefined;
      render(<DeleteTable initial={["alpha"]} onDeleteRequested={(d) => (pending = d)} />);
      const dialog = await openDeleteDialogByKeyboard(user, "alpha");
      await user.click(within(dialog).getByRole("button", { name: "삭제" }));
      await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
      act(() => pending?.resolve());
      await waitFor(() => expect(screen.getByRole("heading", { name: "항목 목록" })).toHaveFocus());
    });

    it("섹션 제목도 없으면 #main-content로 이동한다", async () => {
      const user = userEvent.setup();
      let pending: Deferred | undefined;
      render(
        <DeleteTable
          initial={["alpha"]}
          withHeading={false}
          onDeleteRequested={(d) => (pending = d)}
        />,
      );
      const dialog = await openDeleteDialogByKeyboard(user, "alpha");
      await user.click(within(dialog).getByRole("button", { name: "삭제" }));
      await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
      act(() => pending?.resolve());
      await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
      expect(document.activeElement?.id).toBe("main-content");
    });

    it("확인 콜백이 같은 틱에 행을 지워도(낙관적 갱신) 다음 행 ⋯로 이동한다", async () => {
      const user = userEvent.setup();
      render(
        <DeleteTable initial={["alpha", "beta", "gamma"]} onDeleteRequested={(d) => d.resolve()} />,
      );
      const dialog = await openDeleteDialogByKeyboard(user, "alpha");
      await user.click(within(dialog).getByRole("button", { name: "삭제" }));
      await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
      await waitFor(() => expect(moreButton("beta")).toHaveFocus());
    });
  });

  describe("포커스 계약 ④ — 확인 → 결과 다이얼로그", () => {
    function ResetRow({ name }: { name: string }) {
      const triggerRef = useRef<HTMLButtonElement>(null);
      const copyRef = useRef<HTMLButtonElement>(null);
      const [result, setResult] = useState<string | null>(null);
      // 결과 다이얼로그도 ConfirmDialog와 같은 복귀 규칙(계약 ②·③)을 쓴다
      const { resolve } = useReturnFocus(triggerRef);
      return (
        <table>
          <tbody>
            <tr>
              <td>{name}</td>
              <td>
                <RowActions
                  rowLabel={name}
                  triggerRef={triggerRef}
                  items={[
                    {
                      label: "비밀번호 초기화",
                      destructive: true,
                      confirm: { title: `${name}의 비밀번호를 초기화할까요?`, actionLabel: "초기화" },
                      // 실제 화면처럼 응답이 비동기로 도착한 뒤 결과 다이얼로그를 연다
                      onConfirm: () => {
                        void Promise.resolve().then(() => setResult("결과"));
                      },
                    },
                  ]}
                />
                <Dialog open={result !== null} onOpenChange={(o) => !o && setResult(null)}>
                  <DialogContent initialFocus={copyRef} finalFocus={() => resolve()}>
                    <DialogTitle>비밀번호를 초기화했습니다</DialogTitle>
                    <Button ref={copyRef}>복사</Button>
                  </DialogContent>
                </Dialog>
              </td>
            </tr>
          </tbody>
        </table>
      );
    }

    it("확인하면 확인 다이얼로그가 닫히고 결과 「복사」에 포커스, 결과를 닫으면 그 행 ⋯로 복귀", async () => {
      const user = userEvent.setup();
      render(<ResetRow name="kim" />);
      const trigger = moreButton("kim");
      await user.click(trigger);
      await user.click(await screen.findByRole("menuitem", { name: "비밀번호 초기화" }));
      const confirmDialog = await screen.findByRole("alertdialog");
      await user.click(within(confirmDialog).getByRole("button", { name: "초기화" }));

      const resultDialog = await screen.findByRole("dialog");
      await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
      await waitFor(() =>
        expect(within(resultDialog).getByRole("button", { name: "복사" })).toHaveFocus(),
      );

      await user.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      await waitFor(() => expect(trigger).toHaveFocus());
    });
  });

  describe("일반 항목이 다른 모달을 열면 메뉴는 포커스를 되돌리지 않는다(P5 리뷰 P5-02)", () => {
    function OpenDialogRow() {
      const triggerRef = useRef<HTMLButtonElement>(null);
      const [open, setOpen] = useState(false);
      return (
        <>
          <RowActions
            rowLabel="gemini"
            triggerRef={triggerRef}
            items={[
              { label: "모델 가져오기", onSelect: () => setOpen(true) },
              { label: "연결 테스트", onSelect: () => {} },
            ]}
          />
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogContent finalFocus={triggerRef}>
              <DialogTitle>모델 가져오기</DialogTitle>
              <Button>전체</Button>
            </DialogContent>
          </Dialog>
        </>
      );
    }

    async function choose(user: ReturnType<typeof userEvent.setup>, name: string) {
      const trigger = moreButton("gemini");
      act(() => trigger.focus());
      await user.keyboard("{Enter}");
      const menu = await screen.findByRole("menu");
      const item = within(menu).getByRole("menuitem", { name });
      // 메뉴가 첫 항목에 포커스를 두는 것은 다음 프레임이다 — 그 전에 ↓를 누르면 늦게 따라오는 포커스 사이로 목표를
      // 지나쳐 다른 항목을 고른다(부하에서 간헐 실패 — reports/phase-6/merge-report.md §4). 항목에 포커스가 생긴 뒤 이동한다.
      await waitFor(() => expect(document.activeElement?.getAttribute("role")).toBe("menuitem"));
      for (let i = 0; i < 4 && document.activeElement !== item; i += 1) await user.keyboard("{ArrowDown}");
      await user.keyboard("{Enter}");
      return trigger;
    }

    it("모달을 연 일반 항목: 메뉴가 다 닫힌 뒤에도 포커스는 모달 안이고, 모달을 닫으면 ⋯로 돌아온다", async () => {
      const user = userEvent.setup();
      render(<OpenDialogRow />);
      const trigger = await choose(user, "모델 가져오기");
      const dialog = await screen.findByRole("dialog");
      await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
      await act(() => new Promise((r) => setTimeout(r, 100)));
      expect(dialog.contains(document.activeElement), document.activeElement?.outerHTML).toBe(true);
      await user.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      await waitFor(() => expect(trigger).toHaveFocus());
    });

    it("모달을 열지 않는 일반 항목: 메뉴가 닫히면 ⋯로 포커스가 돌아온다(기존 동작 유지)", async () => {
      const user = userEvent.setup();
      render(<OpenDialogRow />);
      const trigger = await choose(user, "연결 테스트");
      await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
      await waitFor(() => expect(trigger).toHaveFocus());
    });
  });

  describe("좁은 화면에서 주 동작을 더보기로 접기(foldPrimaryOnNarrow — plan §6.7, P5 리뷰 P5-04)", () => {

    afterEach(() => {
      vi.unstubAllGlobals();
    });

    function Row({ fold = true, onEdit = vi.fn(), items = true }: { fold?: boolean; onEdit?: () => void; items?: boolean }) {
      return (
        <RowActions
          rowLabel="gemini"
          foldPrimaryOnNarrow={fold}
          primary={{ label: "수정", onClick: onEdit }}
          items={
            items
              ? [
                  { label: "복제", onSelect: vi.fn() },
                  {
                    label: "삭제",
                    destructive: true,
                    confirm: { title: "삭제할까요?", actionLabel: "삭제" },
                    onConfirm: vi.fn(),
                  },
                ]
              : []
          }
        />
      );
    }

    it("< 768: 주 동작 버튼 없이 ⋯ 하나 — 메뉴 첫 항목이 주 동작이고 선택하면 실행된다", async () => {
      stubNarrowViewport(true);
      const user = userEvent.setup();
      const onEdit = vi.fn();
      render(<Row onEdit={onEdit} />);
      await waitFor(() => expect(screen.queryByRole("button", { name: "수정" })).toBeNull());
      expect(screen.getAllByRole("button")).toHaveLength(1);
      await user.click(moreButton("gemini"));
      const menu = await screen.findByRole("menu");
      expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["수정", "복제", "삭제"]);
      await user.click(within(menu).getByRole("menuitem", { name: "수정" }));
      expect(onEdit).toHaveBeenCalledTimes(1);
    });

    it("주 동작만 있는 행도 < 768에서는 ⋯ 메뉴 하나가 된다", async () => {
      stubNarrowViewport(true);
      const user = userEvent.setup();
      render(<Row items={false} />);
      await waitFor(() => expect(screen.queryByRole("button", { name: "수정" })).toBeNull());
      await user.click(moreButton("gemini"));
      const menu = await screen.findByRole("menu");
      expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["수정"]);
      expect(within(menu).queryByRole("separator")).toBeNull();
    });

    it("≥ 768이거나 opt-in이 아니면 지금처럼 주 동작 버튼 + ⋯(하위 호환)", async () => {
      stubNarrowViewport(false);
      const user = userEvent.setup();
      const { unmount } = render(<Row />);
      expect(screen.getByRole("button", { name: "수정" })).toBeInTheDocument();
      await user.click(moreButton("gemini"));
      expect((await screen.findAllByRole("menuitem")).map((i) => i.textContent)).toEqual(["복제", "삭제"]);
      await user.keyboard("{Escape}");
      unmount();

      stubNarrowViewport(true);
      render(<Row fold={false} />);
      await act(() => new Promise((r) => setTimeout(r, 0)));
      expect(screen.getByRole("button", { name: "수정" })).toBeInTheDocument();
    });
  });
});
