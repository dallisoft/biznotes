"use client";

import { TableScrollFrame } from "@/components/ui/table";

// 이전 경로에서 가져오던 이름 — 정의는 공용 표(`components/ui/table.tsx`)로 옮겼다(P6 정리)
export { hasMoreToRight, STICKY_FIRST_COLUMN_CLASS } from "@/components/ui/table";

/**
 * 관리 표의 좁은 화면 규칙(plan §6.7 「표(< 768)」: 가로 스크롤 유지 + 오른쪽 가장자리 그라데이션 단서 + 첫 열 고정) —
 * 공용 `TableScrollFrame`에 첫 열 고정을 켠 관리 화면용 묶음(opt-in). 안의 공용 `Table`은 그대로 쓴다. 행 동작 접기는
 * `RowActions`의 `foldPrimaryOnNarrow`가 맡는다. 단서는 장식(`aria-hidden`)이다 — 스크롤 가능 여부는 스크린리더가 표
 * 탐색으로 이미 안다. P5에서 따로 구현했던 단서 측정은 P6에서 공용 틀 하나로 합쳤다(`impl-clean.md` ④).
 */
export function AdminTableFrame({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <TableScrollFrame data-slot="admin-table-frame" stickyFirstColumn className={className}>
      {children}
    </TableScrollFrame>
  );
}
