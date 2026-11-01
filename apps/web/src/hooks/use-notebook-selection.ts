"use client";

import { useCallback, useMemo, useState } from "react";
import type { Notebook } from "@/lib/types";

export type SelectAllState = "checked" | "mixed" | "unchecked";

export interface NotebookSelection {
  /** 유효 선택(S-2) = 선택 ∩ 보이는 소유 노트북. 개수·모두 선택·일괄 실행은 이 값만 쓴다. */
  selectedIds: ReadonlySet<string>;
  count: number;
  selectableCount: number;
  selectAllState: SelectAllState;
  isSelected: (id: string) => boolean;
  setSelected: (id: string, selected: boolean) => void;
  toggleAll: () => void;
  /** 일괄 작업이 끝난 항목을 선택에서 뺀다(S-7). 지금 보이지 않는 id도 뺀다(삭제돼 목록에서 사라진 항목). */
  deselect: (ids: readonly string[]) => void;
  clear: () => void;
}

const EMPTY: ReadonlySet<string> = new Set();

/** 일괄 작업 자격(D-02) — 실제 소유자만. admin 승격은 UI에서 쓰지 않는다. */
export function isBulkSelectable(notebook: Notebook): boolean {
  return notebook.is_owner === true;
}

/**
 * 대시보드 선택 모델(plan §5.3). `visibleNotebooks`는 필터를 적용한 표시 목록이고,
 * `filterKey`는 탭·즐겨찾기만·태그 필터를 합친 값이다 — 바뀌면 선택을 비운다(S-3).
 * 뷰 모드는 `filterKey`에 넣지 않으므로 카드 ↔ 테이블 전환은 선택을 유지한다.
 */
export function useNotebookSelection(
  visibleNotebooks: readonly Notebook[],
  filterKey: string,
): NotebookSelection {
  const [selected, setSelectedIds] = useState<ReadonlySet<string>>(EMPTY);
  const [selectionFilterKey, setSelectionFilterKey] = useState(filterKey);
  // 렌더 중에 이전 필터와 비교해 바로 비운다 — effect로 비우면 새 필터 화면에 옛 선택이 한 번 그려진다.
  if (selectionFilterKey !== filterKey) {
    setSelectionFilterKey(filterKey);
    setSelectedIds(EMPTY);
  }

  const selectableIds = useMemo(
    () => visibleNotebooks.filter(isBulkSelectable).map((nb) => nb.id),
    [visibleNotebooks],
  );
  const selectableSet = useMemo(() => new Set(selectableIds), [selectableIds]);
  const selectedIds = useMemo<ReadonlySet<string>>(
    () => new Set(selectableIds.filter((id) => selected.has(id))),
    [selectableIds, selected],
  );

  const count = selectedIds.size;
  const selectableCount = selectableIds.length;
  const selectAllState: SelectAllState =
    selectableCount > 0 && count === selectableCount
      ? "checked"
      : count > 0
        ? "mixed"
        : "unchecked";

  const isSelected = useCallback((id: string) => selectedIds.has(id), [selectedIds]);

  const setSelected = useCallback(
    (id: string, on: boolean) => {
      if (!selectableSet.has(id)) return;
      setSelectedIds((prev) => {
        // 선택을 바꿀 때 지금 보이지 않는 id를 함께 걷어낸다. 걷어내기 전에 항목이 refetch로 사라졌다가
        // 다시 나타나면 다시 선택된 상태로 보인다 — 화면 값은 매 렌더 `선택 ∩ 보이는 소유 항목`으로
        // 파생하므로(S-2) 사라져 있는 동안에는 개수·일괄 실행 입력에 들어가지 않는다.
        const next = new Set([...prev].filter((sid) => selectableSet.has(sid)));
        if (on) next.add(id);
        else next.delete(id);
        return next;
      });
    },
    [selectableSet],
  );

  const toggleAll = useCallback(() => {
    if (selectableCount === 0) return;
    setSelectedIds(selectAllState === "checked" ? EMPTY : new Set(selectableIds));
  }, [selectAllState, selectableCount, selectableIds]);

  const deselect = useCallback((ids: readonly string[]) => {
    if (ids.length === 0) return;
    const drop = new Set(ids);
    setSelectedIds((prev) => new Set([...prev].filter((sid) => !drop.has(sid))));
  }, []);

  const clear = useCallback(() => setSelectedIds(EMPTY), []);

  return {
    selectedIds,
    count,
    selectableCount,
    selectAllState,
    isSelected,
    setSelected,
    toggleAll,
    deselect,
    clear,
  };
}
