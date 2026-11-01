import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { AdminTableFrame, hasMoreToRight, STICKY_FIRST_COLUMN_CLASS } from "./admin-table-frame";

// 관리 표의 좁은 화면 규칙(plan §6.7 「표(< 768)」 — P5 리뷰 P5-04): 가로 스크롤 유지 + 오른쪽 그라데이션 단서 + 첫 열 고정.
// jsdom은 레이아웃을 계산하지 않으므로 스크롤 크기는 표 컨테이너에 직접 정의해 단서 판정을 검사한다.

function Sample() {
  return (
    <AdminTableFrame>
      <Table aria-label="사용자">
        <TableHeader>
          <TableRow>
            <TableHead>이메일</TableHead>
            <TableHead>역할</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow>
            <TableCell>admin@example.com</TableCell>
            <TableCell>관리자</TableCell>
          </TableRow>
        </TableBody>
      </Table>
    </AdminTableFrame>
  );
}

function setScroll(el: HTMLElement, { scrollWidth, clientWidth, scrollLeft }: Record<string, number>) {
  Object.defineProperty(el, "scrollWidth", { configurable: true, value: scrollWidth });
  Object.defineProperty(el, "clientWidth", { configurable: true, value: clientWidth });
  Object.defineProperty(el, "scrollLeft", { configurable: true, writable: true, value: scrollLeft });
}

describe("hasMoreToRight", () => {
  it("오른쪽에 남은 내용이 1px보다 많을 때만 참", () => {
    expect(hasMoreToRight({ scrollWidth: 800, clientWidth: 358, scrollLeft: 0 })).toBe(true);
    expect(hasMoreToRight({ scrollWidth: 800, clientWidth: 358, scrollLeft: 442 })).toBe(false);
    expect(hasMoreToRight({ scrollWidth: 800, clientWidth: 358, scrollLeft: 441 })).toBe(false); // 1px 오차
    expect(hasMoreToRight({ scrollWidth: 800, clientWidth: 358, scrollLeft: 440 })).toBe(true);
    expect(hasMoreToRight({ scrollWidth: 358, clientWidth: 358, scrollLeft: 0 })).toBe(false);
  });
});

describe("AdminTableFrame", () => {
  it("표를 감싸고 좁은 화면 첫 열 고정(sticky·left-0·불투명 배경·경계)을 입힌다", () => {
    render(<Sample />);
    const table = screen.getByRole("table", { name: "사용자" });
    const frame = table.closest('[data-slot="admin-table-frame"]') as HTMLElement;
    expect(frame).not.toBeNull();
    for (const cls of STICKY_FIRST_COLUMN_CLASS.split(" ")) expect(frame).toHaveClass(cls);
    expect(STICKY_FIRST_COLUMN_CLASS).toContain("max-md:[&_tr>*:first-child]:sticky");
    expect(STICKY_FIRST_COLUMN_CLASS).toContain("max-md:[&_tr>*:first-child]:bg-background");
    // 표 자체의 가로 스크롤 컨테이너는 공용 Table 그대로다
    expect(table.parentElement).toHaveAttribute("data-slot", "table-container");
  });

  it("오른쪽으로 더 스크롤할 내용이 있으면 단서를 보이고, 끝까지 스크롤하면 숨긴다(장식 — aria-hidden)", () => {
    render(<Sample />);
    const frame = screen.getByRole("table").closest('[data-slot="admin-table-frame"]') as HTMLElement;
    const scroller = frame.querySelector<HTMLElement>('[data-slot="table-container"]')!;
    // 단서 요소는 공용 TableScrollFrame 것이다(P6 정리 — 이전 이름 admin-table-scroll-cue)
    const cue = frame.querySelector('[data-slot="table-scroll-cue"]')!;
    expect(cue).toHaveAttribute("aria-hidden", "true");
    expect(cue).toHaveClass("md:hidden");
    expect(frame).toHaveAttribute("data-scroll-cue", "hidden"); // jsdom 기본 — 넘침 없음

    setScroll(scroller, { scrollWidth: 800, clientWidth: 358, scrollLeft: 0 });
    fireEvent.scroll(scroller);
    expect(frame).toHaveAttribute("data-scroll-cue", "visible");
    expect(cue).toHaveClass("opacity-100");

    setScroll(scroller, { scrollWidth: 800, clientWidth: 358, scrollLeft: 442 });
    fireEvent.scroll(scroller);
    expect(frame).toHaveAttribute("data-scroll-cue", "hidden");
    expect(cue).toHaveClass("opacity-0");

    // 창 크기가 바뀌어 넘침이 생기면 다시 보인다
    setScroll(scroller, { scrollWidth: 900, clientWidth: 358, scrollLeft: 0 });
    fireEvent(window, new Event("resize"));
    expect(frame).toHaveAttribute("data-scroll-cue", "visible");
  });
});
