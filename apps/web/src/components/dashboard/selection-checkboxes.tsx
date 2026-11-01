"use client";

import { Checkbox } from "@/components/ui/checkbox";
import { isBulkSelectable, type NotebookSelection } from "@/hooks/use-notebook-selection";
import type { Notebook } from "@/lib/types";
import { cn } from "@/lib/utils";

export const OWNER_ONLY_HINT = "소유자만 일괄 작업할 수 있습니다";
export const SELECT_ALL_LABEL = "표시된 노트북 모두 선택";

// 터치 기기(pointer: coarse)에서 체크박스 히트 영역 44×44(plan §6.4.4 「체크박스 히트 영역」).
// 상자는 16px이므로 보이지 않는 ::after를 사방 14px씩 넓힌다(기본은 Checkbox의 가로 12·세로 8px).
const COARSE_HIT_AREA = "coarse:after:-inset-x-3.5 coarse:after:-inset-y-3.5";

/**
 * 카드·테이블 행의 선택 체크박스(S-8, D-03). 비소유 항목은 보이되 비활성이고, 설명을 접근 가능한
 * 이름과 툴팁(title)으로 준다. 클릭이 카드의 링크 오버레이로 전파되지 않게 막는다 — 기본 동작
 * (숨은 input 토글)은 막지 않는다. `className`은 감싸는 요소에 붙는다(카드의 표시 규칙 — HCI-4).
 */
export function NotebookSelectCheckbox({
  notebook,
  selection,
  className,
}: {
  notebook: Notebook;
  selection: NotebookSelection;
  className?: string;
}) {
  const eligible = isBulkSelectable(notebook);
  return (
    <span
      data-slot="notebook-select"
      className={cn("relative z-10 inline-flex", className)}
      title={eligible ? undefined : OWNER_ONLY_HINT}
      onClick={(e) => e.stopPropagation()}
    >
      <Checkbox
        aria-label={
          eligible ? `${notebook.title} 선택` : `${notebook.title} 선택 불가 — ${OWNER_ONLY_HINT}`
        }
        data-testid="notebook-select"
        className={COARSE_HIT_AREA}
        disabled={!eligible}
        checked={eligible && selection.isSelected(notebook.id)}
        onCheckedChange={(checked) => selection.setSelected(notebook.id, checked)}
      />
    </span>
  );
}

/** 「표시된 노트북 모두 선택」(S-4) — 일부만 선택이면 mixed, 선택 가능 항목이 없으면 비활성. */
export function SelectAllCheckbox({ selection }: { selection: NotebookSelection }) {
  const { selectAllState, selectableCount } = selection;
  return (
    <Checkbox
      aria-label={SELECT_ALL_LABEL}
      data-testid="notebook-select-all"
      className={COARSE_HIT_AREA}
      disabled={selectableCount === 0}
      checked={selectAllState === "checked"}
      indeterminate={selectAllState === "mixed"}
      mixedIndicator
      onCheckedChange={() => selection.toggleAll()}
    />
  );
}
