"use client";

import { Search, Zap } from "lucide-react";
import Link from "next/link";
import { NavDrawer } from "@/components/shell/nav-drawer";
import { Button } from "@/components/ui/button";
import type { User } from "@/lib/types";
import { useCommandPalette } from "@/stores/command-palette";

/**
 * 모바일(< 768px) 상단 바(plan §6.2.3, 높이 56): 왼쪽 메뉴 버튼(44×44 → 드로어), 가운데 로고 + 「biznotes」
 * (노트북 홈 링크), 오른쪽 검색 아이콘(44×44 — 모바일의 전역 검색 진입점). 기존 「노트북 홈」 격자 링크는
 * 드로어·로고와 겹쳐 없앴다.
 */
export function MobileTopBar({ me }: { me: User }) {
  const { setOpen } = useCommandPalette();
  return (
    <header className="flex h-14 shrink-0 items-center gap-1 border-b bg-background px-1.5">
      <NavDrawer me={me} />
      <Link
        href="/"
        className="mx-auto flex items-center gap-2 rounded-lg px-2 py-1 outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span className="flex size-7 items-center justify-center rounded-lg bg-primary text-primary-foreground">
          <Zap className="size-3.5" aria-hidden />
        </span>
        <span className="text-sm font-semibold">biznotes</span>
      </Link>
      <Button
        variant="ghost"
        size="icon"
        aria-label="검색"
        className="size-11"
        data-testid="open-search"
        onClick={() => setOpen(true)}
      >
        <Search className="size-5" aria-hidden />
      </Button>
    </header>
  );
}
