"use client";

import { KeyRound, LogOut, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useId } from "react";
import { NAV_ROW_CLASS, NAV_ROW_IDLE_CLASS, SIDEBAR_BUTTON_FOCUS_CLASS } from "@/components/shell/nav-link";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useLogout } from "@/hooks/use-auth";
import { changePasswordHref } from "@/lib/safe-redirect";
import type { User } from "@/lib/types";
import { cn } from "@/lib/utils";

type AccountLink = { key: string; label: string; href: string; Icon: LucideIcon; testId?: string };

/** 표시 이름 — 이름이 비어 있으면 이메일. */
export function userDisplayName(me: User): string {
  return me.name || me.email;
}

/**
 * 계정 항목(plan §6.2.2 사용자 메뉴·§6.2.4 진입점 단일화): 비밀번호 변경. 데스크톱 드롭다운과
 * 모바일 드로어 「계정」 섹션이 같은 목록을 쓰고, 항목이 없으면 드로어 섹션을 그리지 않는다.
 *
 * 「비밀번호 변경」은 서버가 그 능력을 가질 때만 보인다(능력 감지 — §11.4, HCI-11 ②·⑥):
 * `me` 응답 객체에 `must_change_password` 키가 실제로 있어야 한다. 생성 타입은 필수 필드라
 * 런타임 검사(`in`)로 본다 — 구 API(코어 v0.10.x)에는 키가 없다. 노출은 캐시 기준이어도
 * 되지만(최악은 변경 화면의 미지원 안내), 클릭하면 변경 화면이 진입마다 fresh 판정한다.
 */
export function useAccountLinks(me: User): AccountLink[] {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const here = `${pathname}${searchParams.size ? `?${searchParams.toString()}` : ""}`;
  const supportsPasswordChange = "must_change_password" in me;

  const links: AccountLink[] = [];
  if (supportsPasswordChange) {
    links.push({
      key: "password",
      label: "비밀번호 변경",
      href: changePasswordHref(here),
      Icon: KeyRound,
      testId: "user-menu-change-password",
    });
  }
  return links;
}

export function UserAvatar({ me, className }: { me: User; className?: string }) {
  const initial = userDisplayName(me).slice(0, 1).toUpperCase();
  return (
    <Avatar className={cn("size-7", className)}>
      <AvatarFallback className="bg-primary/10 text-xs font-semibold text-primary">{initial}</AvatarFallback>
    </Avatar>
  );
}

/** 데스크톱 사이드바 하단의 사용자 메뉴(드롭다운). 트리거는 아바타 + 이름이고 바닥 폭을 채운다. */
export function UserMenu({ me }: { me: User }) {
  const logout = useLogout();
  const links = useAccountLinks(me);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <button
            type="button"
            aria-label="사용자 메뉴"
            className="flex h-10 min-w-0 flex-1 items-center gap-2 rounded-lg px-2 text-left outline-none hover:bg-sidebar-accent focus-visible:ring-2 focus-visible:ring-sidebar-ring coarse:min-h-11"
            data-testid="user-menu"
          >
            <UserAvatar me={me} />
            <span className="min-w-0 flex-1 truncate text-sm text-sidebar-foreground/80">
              {userDisplayName(me)}
            </span>
          </button>
        }
      />
      <DropdownMenuContent side="top" align="start" className="w-60">
        <DropdownMenuGroup>
          <DropdownMenuLabel>
            <p className="truncate text-sm font-medium text-foreground">{me.name}</p>
            <p className="truncate text-xs font-normal text-muted-foreground">{me.email}</p>
          </DropdownMenuLabel>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        {links.length > 0 && (
          <>
            {links.map((link) => (
              <DropdownMenuItem key={link.key} render={<Link href={link.href} />} data-testid={link.testId}>
                <link.Icon className="size-4" />
                {link.label}
              </DropdownMenuItem>
            ))}
            <DropdownMenuSeparator />
          </>
        )}
        <DropdownMenuItem onClick={() => logout.mutate()}>
          <LogOut className="size-4" />
          로그아웃
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * 모바일 드로어의 「계정」 섹션 — 사용자 메뉴 항목을 펼친 목록으로 보인다(plan §6.2.3 ②).
 * 로그아웃은 여기 두지 않는다: 드로어 고정 바닥에서 스크롤과 무관하게 항상 보인다.
 */
export function UserAccountList({ me, onNavigate }: { me: User; onNavigate?: () => void }) {
  const labelId = useId();
  const links = useAccountLinks(me);
  if (links.length === 0) return null;

  return (
    <div
      role="group"
      aria-labelledby={labelId}
      className="mt-4 flex flex-col gap-0.5 border-t border-sidebar-border pt-3"
      data-testid="drawer-account-section"
    >
      <span id={labelId} className="pb-1 pl-2.5 text-xs font-medium text-muted-foreground">
        계정
      </span>
      <ul className="flex flex-col gap-0.5">
        {links.map((link) => (
          <li key={link.key}>
            <Link
              href={link.href}
              onClick={onNavigate}
              data-testid={link.testId}
              className={cn(NAV_ROW_CLASS, NAV_ROW_IDLE_CLASS, "h-9")}
            >
              <link.Icon className="size-4 shrink-0" aria-hidden />
              <span className="min-w-0 truncate">{link.label}</span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** 드로어 고정 바닥의 「로그아웃」 — 드롭다운과 같은 `useLogout` 경로(쿼리 캐시 비우기 → /login). */
export function LogoutButton({ className }: { className?: string }) {
  const logout = useLogout();
  return (
    <Button
      variant="outline"
      onClick={() => logout.mutate()}
      disabled={logout.isPending}
      className={cn("gap-1.5 coarse:min-h-11", SIDEBAR_BUTTON_FOCUS_CLASS, className)}
    >
      <LogOut className="size-4" aria-hidden />
      로그아웃
    </Button>
  );
}
