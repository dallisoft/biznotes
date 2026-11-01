"use client";

import { LayoutGrid, Rows3 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { type SegmentedOption, SegmentedControl } from "@/components/ui/segmented-control";

export type DashboardViewMode = "card" | "table";

export const DASHBOARD_VIEW_STORAGE_KEY = "biznotes:dashboard-view";

/** 저장값이 없거나 알 수 없는 값이면 카드(D-11). */
export function parseDashboardViewMode(raw: string | null): DashboardViewMode {
  return raw === "table" ? "table" : "card";
}

/**
 * 대시보드 보기 방식(D-11). 서버 렌더와 첫 클라이언트 렌더는 항상 card로 같게 두고,
 * 저장값은 마운트 뒤에 읽는다(hydration 불일치 방지). 저장소를 쓸 수 없으면(사생활 보호 모드 등)
 * 저장만 건너뛰고 화면 전환은 그대로 한다. 저장값 형식(`card`·`table`)은 바꾸지 않는다(plan §8.2).
 */
export function useDashboardViewMode(): [DashboardViewMode, (mode: DashboardViewMode) => void] {
  const [mode, setMode] = useState<DashboardViewMode>("card");

  useEffect(() => {
    let stored: string | null = null;
    try {
      stored = localStorage.getItem(DASHBOARD_VIEW_STORAGE_KEY);
    } catch {
      // 저장소 접근 불가 — 기본값(card)을 유지한다
    }
    setMode(parseDashboardViewMode(stored));
  }, []);

  const update = useCallback((next: DashboardViewMode) => {
    setMode(next);
    try {
      localStorage.setItem(DASHBOARD_VIEW_STORAGE_KEY, next);
    } catch {
      // 저장 실패 — 이번 화면에서만 전환한다
    }
  }, []);

  return [mode, update];
}

// testid는 기존 e2e 선택자 그대로(D-17) — `view-mode-card`·`view-mode-table`
const OPTIONS: readonly SegmentedOption<DashboardViewMode>[] = [
  { value: "card", label: "카드", icon: <LayoutGrid />, testId: "view-mode-card" },
  { value: "table", label: "표", icon: <Rows3 />, testId: "view-mode-table" },
];

/**
 * 보기 전환 「카드 | 표」(plan §6.6.2 노트북 홈) — 공용 SegmentedControl(radiogroup·화살표 키). 좁은 화면(< 768)에서는
 * 아이콘만 보인다(목업 M1, P3 IR-H5 — 이름 「카드」「표」는 그대로, 묶음 이름 「보기 방식」이 맥락을 준다).
 */
export function ViewModeToggle({
  value,
  onChange,
}: {
  value: DashboardViewMode;
  onChange: (mode: DashboardViewMode) => void;
}) {
  return (
    <SegmentedControl label="보기 방식" options={OPTIONS} value={value} onValueChange={onChange} iconOnlyOnNarrow />
  );
}
