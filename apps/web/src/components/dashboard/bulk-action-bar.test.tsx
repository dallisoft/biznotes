import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { useNotebookSelection } from "@/hooks/use-notebook-selection";
import type { Notebook } from "@/lib/types";
import { BulkActionBar } from "./bulk-action-bar";
import { NotebookSelectCheckbox } from "./selection-checkboxes";

// AC-1.6(모두 선택 mixed) · AC-1.7(N개 선택됨·선택 해제·0개면 일괄 버튼 비활성) — 실제 선택 훅으로 렌더한다.

function nb(id: string, isOwner = true): Notebook {
  return {
    id,
    owner_id: isOwner ? "me" : "other",
    title: `노트북 ${id}`,
    emoji: "📓",
    description: "",
    visibility: "private",
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    source_count: 0,
    is_owner: isOwner,
    permission: isOwner ? "owner" : "read",
    tags: [],
    is_favorite: false,
  };
}

function Harness({ notebooks, showSelectAll = true }: { notebooks: Notebook[]; showSelectAll?: boolean }) {
  const selection = useNotebookSelection(notebooks, "all");
  return (
    <>
      <BulkActionBar
        selection={selection}
        showSelectAll={showSelectAll}
        actions={({ disabled }) => (
          <button type="button" disabled={disabled}>
            일괄 동작
          </button>
        )}
      />
      <ul>
        {notebooks.map((n) => (
          <li key={n.id}>
            <NotebookSelectCheckbox notebook={n} selection={selection} />
          </li>
        ))}
      </ul>
    </>
  );
}

const selectAll = () => screen.getByRole("checkbox", { name: "표시된 노트북 모두 선택" });

describe("BulkActionBar", () => {
  it("AC-1.6 선택 가능 3개 — 0개 unchecked, 1~2개 mixed(가로줄 아이콘), 3개 checked", async () => {
    const user = userEvent.setup();
    render(<Harness notebooks={[nb("a"), nb("b"), nb("c"), nb("s", false)]} />);
    expect(selectAll()).toHaveAttribute("aria-checked", "false");

    await user.click(screen.getByRole("checkbox", { name: "노트북 a 선택" }));
    expect(selectAll()).toHaveAttribute("aria-checked", "mixed");
    expect(selectAll().querySelector('[data-icon="indeterminate"]')).not.toBeNull();

    await user.click(screen.getByRole("checkbox", { name: "노트북 b 선택" }));
    expect(selectAll()).toHaveAttribute("aria-checked", "mixed");

    await user.click(screen.getByRole("checkbox", { name: "노트북 c 선택" }));
    expect(selectAll()).toHaveAttribute("aria-checked", "true");
  });

  it("AC-1.6 mixed에서 클릭하면 3개 선택, checked에서 클릭하면 0개", async () => {
    const user = userEvent.setup();
    render(<Harness notebooks={[nb("a"), nb("b"), nb("c")]} />);
    await user.click(screen.getByRole("checkbox", { name: "노트북 b 선택" }));
    await user.click(selectAll());
    expect(screen.getByTestId("bulk-selected-count")).toHaveTextContent("3개 선택됨");
    for (const id of ["a", "b", "c"]) {
      expect(screen.getByRole("checkbox", { name: `노트북 ${id} 선택` })).toHaveAttribute(
        "aria-checked",
        "true",
      );
    }
    await user.click(selectAll());
    expect(screen.queryByTestId("bulk-selected-count")).not.toBeInTheDocument();
    expect(selectAll()).toHaveAttribute("aria-checked", "false");
  });

  it("AC-1.6 unchecked에서 툴바의 라벨 글자를 눌러도 한 번만 토글돼 3개 선택", async () => {
    const user = userEvent.setup();
    render(<Harness notebooks={[nb("a"), nb("b"), nb("c")]} />);
    await user.click(within(screen.getByTestId("bulk-action-bar")).getByText("표시된 노트북 모두 선택"));
    expect(screen.getByTestId("bulk-selected-count")).toHaveTextContent("3개 선택됨");
  });

  it("AC-1.6·AC-1.3 선택 가능 항목이 0개면 모두 선택은 비활성이고 눌러도 선택되지 않는다", async () => {
    const user = userEvent.setup();
    render(<Harness notebooks={[nb("s1", false), nb("s2", false)]} />);
    expect(selectAll()).toHaveAttribute("data-disabled");
    expect(selectAll()).toHaveAttribute("aria-disabled", "true");
    await user.click(selectAll());
    expect(screen.queryByTestId("bulk-selected-count")).not.toBeInTheDocument();
    expect(selectAll()).toHaveAttribute("aria-checked", "false");
  });

  it("AC-1.7 0개면 개수·선택 해제가 없고 일괄 버튼이 비활성, 1개 이상이면 「N개 선택됨」·「선택 해제」", async () => {
    const user = userEvent.setup();
    render(<Harness notebooks={[nb("a"), nb("b")]} />);
    expect(screen.queryByText(/개 선택됨/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "선택 해제" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "일괄 동작" })).toBeDisabled();

    await user.click(screen.getByRole("checkbox", { name: "노트북 a 선택" }));
    expect(screen.getByTestId("bulk-selected-count")).toHaveTextContent("1개 선택됨");
    expect(screen.getByRole("button", { name: "일괄 동작" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "선택 해제" }));
    expect(screen.queryByTestId("bulk-selected-count")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "일괄 동작" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "노트북 a 선택" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
  });

  it("showSelectAll=false(테이블 뷰)면 툴바에 모두 선택을 그리지 않는다", () => {
    render(<Harness notebooks={[nb("a")]} showSelectAll={false} />);
    expect(screen.queryByRole("checkbox", { name: "표시된 노트북 모두 선택" })).not.toBeInTheDocument();
  });
});
