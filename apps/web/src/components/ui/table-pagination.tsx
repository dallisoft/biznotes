"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { PAGE_SIZE_OPTIONS, type PaginationState } from "@/hooks/use-pagination";

// 테이블 하단 페이지네이션 — 페이지당 개수(10/20/30/50) 선택 + 이전/다음 이동.
// usePagination() 결과를 그대로 전달한다: <TablePagination pagination={pg} />
// 전체 개수가 최소 페이지 크기 이하면(어떤 페이지 크기로도 1페이지) 그리지 않는다(plan §6.5 DataTable 규칙).
// 판정 기준을 현재 페이지 크기가 아닌 최소 크기로 두어, 큰 페이지 크기를 고른 뒤에도 되돌릴 수 있게 한다.
export function TablePagination({
  pagination,
  className,
}: {
  pagination: PaginationState<unknown>;
  className?: string;
}) {
  const { page, pageCount, pageSize, total, rangeStart, rangeEnd, setPage, setPageSize } =
    pagination;
  if (total <= PAGE_SIZE_OPTIONS[0]) return null;

  return (
    <div
      className={`flex flex-wrap items-center justify-between gap-3 px-1 py-3 text-sm text-muted-foreground ${className ?? ""}`}
    >
      <div className="flex items-center gap-2">
        <span>페이지당</span>
        <Select value={String(pageSize)} onValueChange={(v) => v && setPageSize(Number(v))}>
          <SelectTrigger className="h-8 w-[4.5rem] coarse:min-h-11" aria-label="페이지당 개수">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {PAGE_SIZE_OPTIONS.map((n) => (
              <SelectItem key={n} value={String(n)}>
                {n}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <span>개</span>
      </div>

      <div className="flex items-center gap-3">
        <span className="tabular-nums">
          {total === 0 ? "0" : `${rangeStart}–${rangeEnd}`} / {total}
        </span>
        <div className="flex items-center gap-1">
          <Button
            variant="outline"
            size="icon"
            className="size-8 coarse:size-11"
            disabled={page <= 1}
            onClick={() => setPage(page - 1)}
            aria-label="이전 페이지"
          >
            <ChevronLeft className="size-4" />
          </Button>
          <span className="px-1 tabular-nums">
            {page} / {pageCount}
          </span>
          <Button
            variant="outline"
            size="icon"
            className="size-8 coarse:size-11"
            disabled={page >= pageCount}
            onClick={() => setPage(page + 1)}
            aria-label="다음 페이지"
          >
            <ChevronRight className="size-4" />
          </Button>
        </div>
      </div>
    </div>
  );
}
