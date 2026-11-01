"use client";

import Link from "next/link";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

/** 사이드바·드로어 행의 공통 모양 — 터치 기기(`coarse`)에서는 최소 44px(plan §6.4.4). */
export const NAV_ROW_CLASS =
  "flex items-center gap-2.5 rounded-lg px-2.5 text-sm outline-none transition-colors focus-visible:ring-2 focus-visible:ring-sidebar-ring coarse:min-h-11";

export const NAV_ROW_IDLE_CLASS =
  "text-sidebar-foreground/80 hover:bg-sidebar-accent hover:text-sidebar-foreground";

export const NAV_ROW_ACTIVE_CLASS = "bg-sidebar-accent font-medium text-sidebar-primary";

/**
 * 사이드바·드로어 면 위에 놓이는 공용 `Button`의 포커스 표시를 셸 링 토큰으로 바꾼다(P2 리뷰 P2-02).
 * `Button` 기본(`border-ring`·`ring-ring/50`)은 배경·카드 면 기준으로 검증된 값이라, 틸 라이트에서는 사이드바 면 위
 * 2.97:1(sidebar-accent 2.70:1)로 3:1에 못 미친다. `sidebar-ring`은 theme-contrast X6이 6조합 모두 3:1 이상을 검사한다.
 */
export const SIDEBAR_BUTTON_FOCUS_CLASS = "focus-visible:border-sidebar-ring focus-visible:ring-sidebar-ring/50";

/**
 * 내비 링크 한 행. 활성이면 `aria-current="page"` — 스크롤 영역이 진입 시 이 항목을 보이게 한다.
 * `nested`는 「관리」 하위 항목(들여쓰기 28px·행 높이 32 — plan §6.2.2 ④), 기본은 행 높이 36.
 */
export function NavLink({
  href,
  label,
  Icon,
  active,
  nested = false,
  onNavigate,
}: {
  href: string;
  label: string;
  Icon?: LucideIcon;
  active: boolean;
  nested?: boolean;
  onNavigate?: () => void;
}) {
  return (
    <Link
      href={href}
      aria-current={active ? "page" : undefined}
      onClick={onNavigate}
      className={cn(
        NAV_ROW_CLASS,
        nested ? "h-8 pl-7" : "h-9",
        active ? NAV_ROW_ACTIVE_CLASS : NAV_ROW_IDLE_CLASS,
      )}
    >
      {Icon && <Icon className="size-4 shrink-0" aria-hidden />}
      <span className="min-w-0 truncate">{label}</span>
    </Link>
  );
}
