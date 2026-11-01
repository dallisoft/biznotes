"use client";

import Link from "next/link";
import { NotebookFavoriteButton, NotebookOwnerMenu } from "@/components/dashboard/notebook-actions";
import {
  NotebookSelectCheckbox,
  SelectAllCheckbox,
} from "@/components/dashboard/selection-checkboxes";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/ui/status-badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { NotebookSelection } from "@/hooks/use-notebook-selection";
import { formatDate, formatNumber } from "@/lib/format";
import { notebookAccessLabel, notebookRowPermissionLabel } from "@/lib/labels";
import type { Notebook } from "@/lib/types";

/** 고정 열 공통 — 스크롤되는 칸 위에 그리고, 행 배경(선택)을 따른다. ≥ 768에서는 아무 효과 없음. */
const STICKY_CELL =
  "max-md:sticky max-md:z-10 max-md:bg-background max-md:in-data-[state=selected]:bg-muted";
/**
 * 선택 열 — 좁은 화면에서 표가 넘치면 열 너비는 내용 최소 폭(왼쪽 여백 12px + 체크박스 16px = 1.75rem)이 되고 `w-10`은
 * 지켜지지 않는다(실브라우저 390 측정 28px). 제목 열의 고정 위치가 이 폭과 같아야 사이에 스크롤되는 칸이 비치거나 제목
 * 칸이 다음 열을 덮지 않으므로, 좁은 화면에서는 폭을 1.75rem으로 못 박는다.
 */
const STICKY_SELECT_CELL = `pl-3 ${STICKY_CELL} max-md:left-0 max-md:w-7`;
/**
 * 제목 열 — 선택 열(1.75rem) 바로 오른쪽에 고정하고 오른쪽 경계선으로 스크롤되는 칸과 나눈다. 고정 열이 좁은 화면을
 * 다 차지하지 않게 제목 링크 폭을 줄인다(말줄임 — 링크의 접근 가능한 이름은 전체 제목 그대로).
 */
const STICKY_TITLE_CELL = `${STICKY_CELL} max-md:left-7 max-md:shadow-[inset_-1px_0_0_var(--border)]`;

/**
 * 대시보드 테이블 뷰(S-9). 카드 그리드와 같은 `visibleNotebooks`를 같은 순서(서버 `updated_at desc`)로
 * 그리고, 헤더 체크박스는 「모두 선택」(S-4)이다. 행 동작은 카드와 같은 소유자 더보기(RowActions)다.
 * 「권한」은 원시값 대신 「소유자/편집 가능/읽기 전용」(AC-3.7 — `lib/labels`), 비소유 행의 구분은 「공유받음」.
 *
 * 좁은 화면(< 768, plan §6.7 「표」 — P3 리뷰 P3-03): 가로 스크롤은 그대로 두고, 행을 알아보는 앞 두 열(선택·제목)을
 * 왼쪽에 고정하며(`sticky`, 배경 있음 — 선택 행은 선택 배경), 오른쪽에 더 볼 열이 남아 있으면 가장자리 그라데이션
 * 단서를 보인다(`Table scrollCue`). 행 동작은 이미 더보기(⋯) 하나다.
 */
export function NotebookTable({
  notebooks,
  selection,
  onShare,
  onRename,
  onManageTags,
}: {
  notebooks: readonly Notebook[];
  selection: NotebookSelection;
  onShare: (id: string) => void;
  onRename: (notebook: Notebook) => void;
  onManageTags?: (notebook: Notebook) => void;
}) {
  return (
    <div className="overflow-hidden rounded-xl border">
      <Table data-testid="notebook-table" scrollCue>
        <TableHeader>
          <TableRow>
            <TableHead className={`w-10 ${STICKY_SELECT_CELL}`}>
              <SelectAllCheckbox selection={selection} />
            </TableHead>
            <TableHead className={STICKY_TITLE_CELL}>제목</TableHead>
            <TableHead>구분</TableHead>
            <TableHead>권한</TableHead>
            <TableHead>태그</TableHead>
            <TableHead className="text-right">소스 수</TableHead>
            <TableHead>생성일</TableHead>
            <TableHead>수정일</TableHead>
            <TableHead>즐겨찾기</TableHead>
            <TableHead>
              <span className="sr-only">메뉴</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {notebooks.map((nb) => (
            <TableRow
              key={nb.id}
              data-testid="notebook-row"
              data-state={selection.isSelected(nb.id) ? "selected" : undefined}
            >
              <TableCell className={STICKY_SELECT_CELL}>
                <NotebookSelectCheckbox notebook={nb} selection={selection} />
              </TableCell>
              <TableCell className={`max-w-80 ${STICKY_TITLE_CELL}`}>
                <Link
                  href={`/notebooks/${nb.id}`}
                  className="flex items-center gap-2 rounded-sm font-medium outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50 coarse:min-h-11 max-md:max-w-36"
                >
                  <span aria-hidden className="text-lg">
                    {nb.emoji}
                  </span>
                  <span className="truncate">{nb.title}</span>
                </Link>
              </TableCell>
              <TableCell>
                {nb.is_owner ? "내 노트북" : <StatusBadge tone="neutral">{notebookAccessLabel(nb)}</StatusBadge>}
              </TableCell>
              <TableCell className="text-muted-foreground">{notebookRowPermissionLabel(nb)}</TableCell>
              <TableCell>
                <div className="flex flex-wrap gap-1">
                  {(nb.tags ?? []).map((tag) => (
                    <Badge key={tag} variant="outline" className="text-xs">
                      {tag}
                    </Badge>
                  ))}
                </div>
              </TableCell>
              <TableCell className="text-right tabular-nums">{formatNumber(nb.source_count)}</TableCell>
              <TableCell className="text-muted-foreground">{formatDate(nb.created_at)}</TableCell>
              <TableCell className="text-muted-foreground">{formatDate(nb.updated_at)}</TableCell>
              <TableCell>
                <NotebookFavoriteButton notebook={nb} />
              </TableCell>
              <TableCell className="w-10">
                {nb.is_owner && (
                  <NotebookOwnerMenu
                    notebook={nb}
                    onShare={onShare}
                    onRename={onRename}
                    onManageTags={onManageTags}
                  />
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
