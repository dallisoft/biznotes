import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Notebook } from "@/lib/types";
import { isBulkSelectable, useNotebookSelection } from "./use-notebook-selection";

// plan §5.3 선택 모델(S-1~S-5) — 실제 훅 본문을 실행한다(mock 없음).

function nb(id: string, overrides: Partial<Notebook> = {}): Notebook {
  return {
    id,
    owner_id: "me",
    title: `노트북 ${id}`,
    emoji: "📓",
    description: "",
    visibility: "private",
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    source_count: 0,
    is_owner: true,
    permission: "owner",
    tags: [],
    is_favorite: false,
    ...overrides,
  };
}

const shared = (id: string) => nb(id, { owner_id: "other", is_owner: false, permission: "write" });

function setup(initial: Notebook[], filterKey = "all") {
  return renderHook(
    ({ notebooks, keyValue }: { notebooks: Notebook[]; keyValue: string }) =>
      useNotebookSelection(notebooks, keyValue),
    { initialProps: { notebooks: initial, keyValue: filterKey } },
  );
}

describe("isBulkSelectable", () => {
  it("실제 소유자만 자격이 있다 — admin 승격(permission만 owner)은 쓰지 않는다", () => {
    expect(isBulkSelectable(nb("a"))).toBe(true);
    expect(isBulkSelectable(shared("b"))).toBe(false);
    expect(isBulkSelectable(nb("c", { is_owner: false, permission: "owner" }))).toBe(false);
  });
});

describe("useNotebookSelection", () => {
  it("AC-1.3 비소유 항목은 개별 선택도, 모두 선택도 되지 않는다", () => {
    const { result } = setup([nb("a"), shared("s1"), nb("b")]);
    act(() => result.current.setSelected("s1", true));
    expect(result.current.count).toBe(0);
    expect(result.current.isSelected("s1")).toBe(false);

    act(() => result.current.toggleAll());
    expect([...result.current.selectedIds]).toEqual(["a", "b"]);
    expect(result.current.isSelected("s1")).toBe(false);
    expect(result.current.selectAllState).toBe("checked");
  });

  it("AC-1.3·AC-1.6 선택 가능 항목이 0개면 모두 선택은 아무것도 하지 않는다", () => {
    const { result } = setup([shared("s1"), shared("s2")]);
    expect(result.current.selectableCount).toBe(0);
    act(() => result.current.toggleAll());
    expect(result.current.count).toBe(0);
    expect(result.current.selectAllState).toBe("unchecked");
  });

  it("AC-1.6 3개 기준 — 0개 unchecked, 1~2개 mixed, 3개 checked", () => {
    const { result } = setup([nb("a"), nb("b"), shared("s"), nb("c")]);
    expect(result.current.selectableCount).toBe(3);
    expect(result.current.selectAllState).toBe("unchecked");

    act(() => result.current.setSelected("a", true));
    expect(result.current.selectAllState).toBe("mixed");
    act(() => result.current.setSelected("b", true));
    expect(result.current.selectAllState).toBe("mixed");
    act(() => result.current.setSelected("c", true));
    expect(result.current.selectAllState).toBe("checked");
    expect(result.current.count).toBe(3);
  });

  it("AC-1.6 mixed·unchecked에서 모두 선택은 3개, checked에서는 0개", () => {
    const { result } = setup([nb("a"), nb("b"), nb("c")]);
    act(() => result.current.toggleAll()); // unchecked → 전체
    expect(result.current.count).toBe(3);
    act(() => result.current.toggleAll()); // checked → 0
    expect(result.current.count).toBe(0);

    act(() => result.current.setSelected("b", true));
    expect(result.current.selectAllState).toBe("mixed");
    act(() => result.current.toggleAll()); // mixed → 전체
    expect(result.current.count).toBe(3);
    expect(result.current.selectAllState).toBe("checked");
  });

  it("개별 해제는 그 항목만 뺀다", () => {
    const { result } = setup([nb("a"), nb("b")]);
    act(() => result.current.toggleAll());
    act(() => result.current.setSelected("a", false));
    expect([...result.current.selectedIds]).toEqual(["b"]);
  });

  it("AC-1.4 refetch로 사라진 항목은 개수·모두 선택 상태에서 즉시 빠진다", () => {
    const { result, rerender } = setup([nb("a"), nb("b"), nb("c")]);
    act(() => result.current.setSelected("a", true));
    act(() => result.current.setSelected("b", true));
    expect(result.current.count).toBe(2);
    expect(result.current.selectAllState).toBe("mixed");

    // c가 사라지면 선택(a,b)이 남은 전체와 같아져 checked
    rerender({ notebooks: [nb("a"), nb("b")], keyValue: "all" });
    expect(result.current.selectAllState).toBe("checked");

    // b가 사라지면 개수 1
    rerender({ notebooks: [nb("a")], keyValue: "all" });
    expect(result.current.count).toBe(1);
    expect([...result.current.selectedIds]).toEqual(["a"]);
  });

  it("AC-1.4 소유권을 잃은 항목(is_owner=false로 바뀜)도 즉시 빠진다", () => {
    const { result, rerender } = setup([nb("a"), nb("b")]);
    act(() => result.current.toggleAll());
    rerender({ notebooks: [nb("a"), shared("b")], keyValue: "all" });
    expect([...result.current.selectedIds]).toEqual(["a"]);
    expect(result.current.selectAllState).toBe("checked");
  });

  it("사라졌던 항목은 다음 선택 변경 때 걷어내 다시 나타나도 선택되지 않는다", () => {
    const { result, rerender } = setup([nb("a"), nb("b")]);
    act(() => result.current.toggleAll());
    rerender({ notebooks: [nb("a")], keyValue: "all" });
    act(() => result.current.setSelected("a", false));
    rerender({ notebooks: [nb("a"), nb("b")], keyValue: "all" });
    expect(result.current.count).toBe(0);
  });

  it("AC-1.5 필터 키가 바뀌면 같은 항목이 남아 있어도 선택을 비운다", () => {
    const list = [nb("a"), nb("b")];
    const { result, rerender } = setup(list, "all");
    act(() => result.current.toggleAll());
    expect(result.current.count).toBe(2);

    rerender({ notebooks: list, keyValue: "mine" });
    expect(result.current.count).toBe(0);

    // 원래 필터로 돌아와도 옛 선택이 되살아나지 않는다
    rerender({ notebooks: list, keyValue: "all" });
    expect(result.current.count).toBe(0);
  });

  it("AC-1.5 필터 키가 같으면 목록 객체가 바뀌어도 선택을 유지한다", () => {
    const { result, rerender } = setup([nb("a"), nb("b")], "k");
    act(() => result.current.setSelected("a", true));
    rerender({ notebooks: [nb("a"), nb("b")], keyValue: "k" });
    expect(result.current.count).toBe(1);
  });

  it("S-7 deselect는 지정한 항목만 빼고, 이미 목록에서 사라진 항목도 빼서 다시 나타나도 선택되지 않는다", () => {
    const list = [nb("a"), nb("b"), nb("c")];
    const { result, rerender } = setup(list);
    act(() => result.current.toggleAll());
    // a는 삭제돼 목록에서 사라졌다(refetch가 deselect보다 먼저 와도 같은 결과)
    rerender({ notebooks: [nb("b"), nb("c")], keyValue: "all" });
    act(() => result.current.deselect(["a", "b"]));
    expect([...result.current.selectedIds]).toEqual(["c"]);
    rerender({ notebooks: list, keyValue: "all" });
    expect([...result.current.selectedIds]).toEqual(["c"]);
    expect(result.current.selectAllState).toBe("mixed");
    act(() => result.current.deselect([]));
    expect(result.current.count).toBe(1);
  });

  it("clear는 선택을 모두 비운다", () => {
    const { result } = setup([nb("a"), nb("b")]);
    act(() => result.current.toggleAll());
    act(() => result.current.clear());
    expect(result.current.count).toBe(0);
    expect(result.current.selectAllState).toBe("unchecked");
  });
});
