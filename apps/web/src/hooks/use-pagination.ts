"use client";

import { useState } from "react";

export const PAGE_SIZE_OPTIONS = [10, 20, 30, 50] as const;

export interface PaginationState<T> {
  /** 현재 페이지에 표시할 항목 */
  pageItems: T[];
  page: number;
  pageCount: number;
  pageSize: number;
  total: number;
  /** 1-based 표시 범위 (예: 11–20) */
  rangeStart: number;
  rangeEnd: number;
  setPage: (p: number) => void;
  setPageSize: (s: number) => void;
}

/**
 * 클라이언트 사이드 페이지네이션 — 전체 배열을 받아 현재 페이지 조각을 반환.
 * 페이지 크기 변경/데이터 축소 시 페이지 번호를 안전 범위로 클램프한다.
 */
export function usePagination<T>(items: T[], initialSize = 10): PaginationState<T> {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSizeRaw] = useState<number>(initialSize);

  const total = items.length;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const current = Math.min(Math.max(1, page), pageCount);
  const start = (current - 1) * pageSize;
  const pageItems = items.slice(start, start + pageSize);

  return {
    pageItems,
    page: current,
    pageCount,
    pageSize,
    total,
    rangeStart: total === 0 ? 0 : start + 1,
    rangeEnd: Math.min(start + pageSize, total),
    setPage: (p) => setPage(p),
    // 페이지 크기를 바꾸면 1페이지로 이동
    setPageSize: (s) => {
      setPageSizeRaw(s);
      setPage(1);
    },
  };
}
