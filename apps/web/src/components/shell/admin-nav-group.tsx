"use client";

import { ChevronDown, ShieldCheck } from "lucide-react";
import { useId, useState } from "react";
import { ADMIN_NAV, isAdminPath, isNavActive } from "@/components/shell/nav-items";
import {
  NAV_ROW_CLASS,
  NAV_ROW_IDLE_CLASS,
  NavLink,
} from "@/components/shell/nav-link";
import { cn } from "@/lib/utils";

/** 사용자가 바꾼 「관리」 펼침 상태(plan §6.2.2 ④, §11.1 — 새 localStorage 키 1개). */
export const ADMIN_OPEN_STORAGE_KEY = "sidebar-admin-open";

/** 저장값 읽기 — 없거나 손상됐거나 저장소 접근이 막히면 null(기본값으로 동작, §11.1). */
export function readAdminOpen(): boolean | null {
  try {
    const value = window.localStorage.getItem(ADMIN_OPEN_STORAGE_KEY);
    if (value === "true") return true;
    if (value === "false") return false;
    return null;
  } catch {
    return null;
  }
}

function writeAdminOpen(open: boolean) {
  try {
    window.localStorage.setItem(ADMIN_OPEN_STORAGE_KEY, String(open));
  } catch {
    // 저장소를 쓸 수 없는 환경(사파리 비공개 창 등) — 이번 화면에서만 펼침 상태를 유지한다
  }
}

/**
 * 관리자 전용 「관리」 접이식 그룹(D-01 대안 B). 호출부가 `role === "admin"`일 때만 렌더한다(AC-2.2) —
 * 이 컴포넌트 자체는 권한을 판정하지 않는다(페이지 접근 가드는 `admin/layout.tsx`).
 *
 * 펼침 기본값: `/admin/*`에서는 펼침, 그 밖에서는 접힘. 사용자가 바꾸면 그 값을 저장해 우선한다.
 * 저장값은 첫 렌더에서 읽는다 — 셸은 인증 가드 뒤에서 클라이언트 마운트로만 그려지므로(SSR은 스켈레톤)
 * 하이드레이션 불일치가 없고, 마운트 뒤 접힘·펼침이 한 번 튀지 않는다.
 */
export function AdminNavGroup({
  pathname,
  onNavigate,
}: {
  pathname: string;
  onNavigate?: () => void;
}) {
  const listId = useId();
  const [stored, setStored] = useState<boolean | null>(() =>
    typeof window === "undefined" ? null : readAdminOpen(),
  );

  const onAdminPath = isAdminPath(pathname);
  const open = stored ?? onAdminPath;

  function toggle() {
    const next = !open;
    setStored(next);
    writeAdminOpen(next);
  }

  return (
    <div className="flex flex-col gap-0.5" data-testid="admin-nav-group">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        onClick={toggle}
        className={cn(NAV_ROW_CLASS, NAV_ROW_IDLE_CLASS, "h-9 w-full text-left", onAdminPath && "font-medium")}
      >
        <ShieldCheck className="size-4 shrink-0" aria-hidden />
        <span className="min-w-0 flex-1 truncate">관리</span>
        <ChevronDown
          className={cn("size-4 shrink-0 transition-transform motion-reduce:transition-none", open && "rotate-180")}
          aria-hidden
        />
      </button>
      {open && (
        <ul id={listId} className="flex flex-col gap-0.5">
          {ADMIN_NAV.map((item) => (
            <li key={item.href}>
              <NavLink
                href={item.href}
                label={item.label}
                Icon={item.Icon}
                active={isNavActive(pathname, item)}
                nested
                onNavigate={onNavigate}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
