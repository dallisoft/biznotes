"use client";

import type { ReactNode } from "react";
import {
  SELECT_ALL_LABEL,
  SelectAllCheckbox,
} from "@/components/dashboard/selection-checkboxes";
import { Button } from "@/components/ui/button";
import type { NotebookSelection } from "@/hooks/use-notebook-selection";

/**
 * 선택 바(S-4·S-5, plan §6.6.2 노트북 홈). 「모두 선택」은 카드 뷰에서만 여기에 두고, 테이블 뷰에서는 헤더
 * 체크박스가 맡는다(`showSelectAll`). 0개면 「표시된 노트북 모두 선택」과 비활성 일괄 버튼만, 1개 이상이면
 * 「N개 선택됨 · 선택 해제」가 함께 보인다. 일괄 동작 버튼은 `actions`가 그리며 선택이 0개면 `disabled`를 받는다.
 * 화살표 키 이동을 관리하지 않으므로 `role="toolbar"`가 아니라 이름 있는 그룹이다(공용 Toolbar와 같은 판단).
 */
export function BulkActionBar({
  selection,
  showSelectAll,
  actions,
}: {
  selection: NotebookSelection;
  showSelectAll: boolean;
  actions?: (state: { disabled: boolean }) => ReactNode;
}) {
  const { count } = selection;
  return (
    <div
      role="group"
      aria-label="노트북 선택"
      data-testid="bulk-action-bar"
      className="mb-3 flex min-h-9 flex-wrap items-center gap-x-3 gap-y-2 text-sm"
    >
      {showSelectAll && (
        <label className="flex items-center gap-2 text-muted-foreground coarse:min-h-11">
          <SelectAllCheckbox selection={selection} />
          <span aria-hidden>{SELECT_ALL_LABEL}</span>
        </label>
      )}
      {count > 0 && (
        <>
          <span className="font-medium" role="status" data-testid="bulk-selected-count">
            {count}개 선택됨
          </span>
          <Button variant="ghost" size="sm" className="coarse:min-h-11" onClick={selection.clear}>
            선택 해제
          </Button>
        </>
      )}
      {actions && <div className="ml-auto flex items-center gap-2">{actions({ disabled: count === 0 })}</div>}
    </div>
  );
}
