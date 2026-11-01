"use client";

import { Menu, X } from "lucide-react";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { AppVersions } from "@/components/shell/app-versions";
import { SIDEBAR_BUTTON_FOCUS_CLASS } from "@/components/shell/nav-link";
import { SidebarContent } from "@/components/shell/sidebar-content";
import { LogoutButton, UserAvatar, userDisplayName } from "@/components/shell/user-menu";
import { ThemeSwitcher } from "@/components/theme/theme-switcher";
import { Button } from "@/components/ui/button";
import { Sheet, SheetClose, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import type { User } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 모바일(< 768px) 내비 드로어(plan §6.2.3). 왼쪽 `Sheet`(폭 300px)에 데스크톱과 같은 `SidebarContent`를 담는다.
 * ① 고정 머리 「biznotes」 + 「메뉴 닫기」 ② 스크롤 영역(주 메뉴 → 관리 → 노트북 → 계정)
 * ③ 고정 바닥(사용자 표시 + 「로그아웃」 + 테마 — 스크롤과 무관하게 항상 보인다).
 * 항목을 누르면 이동 후 닫힌다. Esc·바깥 클릭·「메뉴 닫기」로 닫고, 포커스는 드로어 안에 갇혔다가
 * 닫히면 메뉴 버튼으로 돌아간다(Base UI Dialog 기본 동작 — 트리거가 `SheetTrigger`여야 한다).
 */
export function NavDrawer({ me }: { me: User }) {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();
  const close = useCallback(() => setOpen(false), []);

  // 드로어 밖 경로 이동(뒤로 가기 등)에도 닫는다
  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger
        render={
          <Button
            variant="ghost"
            size="icon"
            aria-label="메뉴 열기"
            className="size-11"
            data-testid="open-nav-drawer"
          />
        }
      >
        <Menu className="size-5" aria-hidden />
      </SheetTrigger>
      <SheetContent
        side="left"
        showCloseButton={false}
        data-testid="nav-drawer"
        className="gap-0 bg-sidebar p-0 text-sidebar-foreground data-[side=left]:w-[300px]"
      >
        <SidebarContent
          me={me}
          variant="drawer"
          onNavigate={close}
          header={
            <div className="flex h-14 items-center justify-between border-b border-sidebar-border pl-4 pr-1.5">
              <div className="min-w-0">
                {/* 다이얼로그 이름 — heading이 아니라 일반 글자(§9 공통 규칙: 사이드바에 새 heading 금지).
                    버전 줄은 이름 밖에 두어 다이얼로그 이름이 「biznotes」 그대로다 */}
                <SheetTitle render={<p />} className="text-sm font-semibold text-sidebar-foreground">
                  biznotes
                </SheetTitle>
                <AppVersions />
              </div>
              <SheetClose
                render={
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="메뉴 닫기"
                    className={cn("size-11", SIDEBAR_BUTTON_FOCUS_CLASS)}
                  />
                }
              >
                <X className="size-5" aria-hidden />
              </SheetClose>
            </div>
          }
          footer={
            <div className="flex items-center gap-2 border-t border-sidebar-border px-3 py-2.5">
              <UserAvatar me={me} />
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium">{userDisplayName(me)}</p>
                <p className="truncate text-xs text-muted-foreground">{me.email}</p>
              </div>
              <LogoutButton />
              <ThemeSwitcher />
            </div>
          }
        />
      </SheetContent>
    </Sheet>
  );
}
