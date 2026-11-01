import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { STICKY_FIRST_COLUMN_CLASS, Table, TableBody, TableCell, TableRow, TableScrollFrame } from "./table";

// 표 가로 스크롤 단서(plan §6.7 「표(< 768)」, P3 리뷰 P3-03) — `scrollCue`를 준 표만. 스크롤 치수는 jsdom이 계산하지
// 않으므로 컨테이너 요소의 치수 속성을 정해 두고 실제 scroll 이벤트를 보낸다(컴포넌트 코드는 그대로 실행).

function setScroll(el: HTMLElement, { left, client, scroll }: { left: number; client: number; scroll: number }) {
  Object.defineProperty(el, "scrollLeft", { configurable: true, get: () => left });
  Object.defineProperty(el, "clientWidth", { configurable: true, get: () => client });
  Object.defineProperty(el, "scrollWidth", { configurable: true, get: () => scroll });
}

function Sample({ scrollCue }: { scrollCue?: boolean }) {
  return (
    <Table data-testid="t" scrollCue={scrollCue}>
      <TableBody>
        <TableRow>
          <TableCell>칸</TableCell>
        </TableRow>
      </TableBody>
    </Table>
  );
}

describe("Table scrollCue", () => {
  it("오른쪽에 더 볼 열이 남아 있으면 단서를 보이고, 끝까지 스크롤하면 숨긴다(좁은 화면 전용 — md 이상 숨김)", () => {
    render(<Sample scrollCue />);
    const container = screen.getByTestId("t").parentElement as HTMLElement;
    expect(container).toHaveAttribute("data-slot", "table-container");
    const cue = document.querySelector<HTMLElement>("[data-slot=table-scroll-cue]")!;
    expect(cue).toHaveAttribute("aria-hidden", "true");
    expect(cue).toHaveClass("md:hidden", "pointer-events-none", "bg-linear-to-l", "from-background");

    setScroll(container, { left: 0, client: 300, scroll: 800 });
    act(() => {
      fireEvent.scroll(container);
    });
    expect(cue).toHaveAttribute("data-visible", "true");

    setScroll(container, { left: 500, client: 300, scroll: 800 });
    act(() => {
      fireEvent.scroll(container);
    });
    expect(cue).toHaveAttribute("data-visible", "false");

    // 넘칠 열이 없으면 단서 없음
    setScroll(container, { left: 0, client: 800, scroll: 800 });
    act(() => {
      fireEvent.scroll(container);
    });
    expect(cue).toHaveAttribute("data-visible", "false");
  });

  it("scrollCue를 주지 않으면 기존 구조 그대로다(컨테이너 하나, 단서 없음)", () => {
    const { container } = render(<Sample />);
    expect(container.querySelector("[data-slot=table-scroll-cue]")).toBeNull();
    expect(container.querySelector("[data-slot=table-scroll]")).toBeNull();
    expect(screen.getByTestId("t").parentElement).toHaveAttribute("data-slot", "table-container");
    expect(container.firstElementChild).toHaveAttribute("data-slot", "table-container");
  });
});

// P6 정리 — P3 `Table scrollCue`·P5 `AdminTableFrame`·P4 실행 기록 지역 단서를 공용 `TableScrollFrame` 하나로 합쳤다.
describe("TableScrollFrame (좁은 화면 표 규칙의 단일 구현)", () => {
  it("첫 열 고정은 opt-in이고, 기본은 끈다", () => {
    const { rerender } = render(
      <TableScrollFrame data-testid="f">
        <Table>
          <TableBody>
            <TableRow>
              <TableCell>칸</TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </TableScrollFrame>,
    );
    expect(screen.getByTestId("f")).not.toHaveClass("max-md:[&_tr>*:first-child]:sticky");
    rerender(
      <TableScrollFrame data-testid="f" stickyFirstColumn>
        <Table>
          <TableBody>
            <TableRow>
              <TableCell>칸</TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </TableScrollFrame>,
    );
    for (const cls of STICKY_FIRST_COLUMN_CLASS.split(" ")) expect(screen.getByTestId("f")).toHaveClass(cls);
  });

  it("Table scrollCue의 바탕을 card로 바꿀 수 있고(카드 안 표), 창 크기가 바뀌어도 단서를 다시 잰다", () => {
    render(
      <Table data-testid="t" scrollCue scrollSurface="card">
        <TableBody>
          <TableRow>
            <TableCell>칸</TableCell>
          </TableRow>
        </TableBody>
      </Table>,
    );
    const container = screen.getByTestId("t").parentElement as HTMLElement;
    const frame = container.parentElement as HTMLElement;
    const cue = frame.querySelector<HTMLElement>("[data-slot=table-scroll-cue]")!;
    expect(cue).toHaveClass("from-card");
    expect(cue).not.toHaveClass("from-background");
    expect(frame).toHaveAttribute("data-scroll-cue", "hidden");

    setScroll(container, { left: 0, client: 358, scroll: 900 });
    act(() => {
      fireEvent(window, new Event("resize"));
    });
    expect(cue).toHaveAttribute("data-visible", "true");
    expect(cue).toHaveClass("opacity-100");
    expect(frame).toHaveAttribute("data-scroll-cue", "visible");
  });
});
