import { render, renderHook, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PAGE_SIZE_OPTIONS, usePagination } from "@/hooks/use-pagination";
import { TablePagination } from "./table-pagination";

function paginationFor(total: number, pageSize?: number) {
  const items = Array.from({ length: total }, (_, i) => i);
  const { result } = renderHook(() => usePagination(items, pageSize));
  return result.current;
}

describe("TablePagination", () => {
  it.each([0, 1, PAGE_SIZE_OPTIONS[0]])(
    "전체 %i건(최소 페이지 크기 이하)이면 아무것도 그리지 않는다",
    (total) => {
      const { container } = render(<TablePagination pagination={paginationFor(total)} />);
      expect(container).toBeEmptyDOMElement();
    },
  );

  it("최소 페이지 크기를 넘으면 페이지당·이전/다음 컨트롤을 그린다", () => {
    render(<TablePagination pagination={paginationFor(PAGE_SIZE_OPTIONS[0] + 1)} />);
    expect(screen.getByLabelText("페이지당 개수")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "이전 페이지" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "다음 페이지" })).toBeEnabled();
    expect(screen.getByText("1–10 / 11")).toBeInTheDocument();
  });

  it("큰 페이지 크기를 골라 1페이지가 되어도 되돌릴 수 있게 컨트롤을 유지한다", () => {
    render(<TablePagination pagination={paginationFor(30, 50)} />);
    expect(screen.getByLabelText("페이지당 개수")).toBeInTheDocument();
    expect(screen.getByText("1 / 1")).toBeInTheDocument();
  });
});
