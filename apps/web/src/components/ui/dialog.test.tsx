import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Dialog, DialogContent, DialogFooter, DialogTitle } from "./dialog";
import { Sheet, SheetContent, SheetTitle } from "./sheet";

// 공용 다이얼로그·시트의 닫기 버튼 이름(E-P5I-1, HCI-P5-8) — 화면 낭독기가 X를 영어 「Close」로 읽지 않게 한국어 「닫기」로
// 맞춘다(plan §6.3 용어). 실제 Base UI Dialog/Sheet로 렌더한다.

describe("Dialog·Sheet 닫기 버튼 이름 「닫기」(E-P5I-1)", () => {
  it("DialogContent의 X 버튼은 이름이 「닫기」이고 「Close」가 아니다", async () => {
    render(
      <Dialog open>
        <DialogContent>
          <DialogTitle>제목</DialogTitle>
        </DialogContent>
      </Dialog>,
    );
    const dialog = await screen.findByRole("dialog", { name: "제목" });
    const close = within(dialog).getByRole("button", { name: "닫기" });
    expect(close).toHaveAttribute("data-slot", "dialog-close");
    expect(within(dialog).queryByRole("button", { name: "Close" })).toBeNull();
  });

  it("showCloseButton={false}면 X가 없다 — 푸터에 「닫기」를 따로 두는 다이얼로그용", async () => {
    render(
      <Dialog open>
        <DialogContent showCloseButton={false}>
          <DialogTitle>제목</DialogTitle>
        </DialogContent>
      </Dialog>,
    );
    const dialog = await screen.findByRole("dialog", { name: "제목" });
    expect(within(dialog).queryByRole("button")).toBeNull();
  });

  it("DialogFooter showCloseButton의 버튼 글자도 「닫기」다", async () => {
    render(
      <Dialog open>
        <DialogContent showCloseButton={false}>
          <DialogTitle>제목</DialogTitle>
          <DialogFooter showCloseButton />
        </DialogContent>
      </Dialog>,
    );
    const dialog = await screen.findByRole("dialog", { name: "제목" });
    expect(within(dialog).getByRole("button", { name: "닫기" })).toHaveTextContent(/^닫기$/);
  });

  it("SheetContent의 X 버튼도 이름이 「닫기」다", async () => {
    render(
      <Sheet open>
        <SheetContent>
          <SheetTitle>시트</SheetTitle>
        </SheetContent>
      </Sheet>,
    );
    const sheet = await screen.findByRole("dialog", { name: "시트" });
    const close = within(sheet).getByRole("button", { name: "닫기" });
    expect(close).toHaveAttribute("data-slot", "sheet-close");
    expect(within(sheet).queryByRole("button", { name: "Close" })).toBeNull();
  });
});
