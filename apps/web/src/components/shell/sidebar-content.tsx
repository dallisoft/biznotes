"use client";

import { Search, Zap } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { AdminNavGroup } from "@/components/shell/admin-nav-group";
import { AppVersions } from "@/components/shell/app-versions";
import { MAIN_NAV, isNavActive } from "@/components/shell/nav-items";
import { NavLink } from "@/components/shell/nav-link";
import { NotebookNavSection } from "@/components/shell/notebook-nav-section";
import { UserAccountList, UserMenu } from "@/components/shell/user-menu";
import { ThemeSwitcher } from "@/components/theme/theme-switcher";
import type { User } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useCommandPalette } from "@/stores/command-palette";

/** `/notebooks/{id}` 경로의 노트북 id(쿼리 `?s=`는 경로에 없다). */
export function activeNotebookIdFrom(pathname: string): string | null {
  const match = pathname.match(/^\/notebooks\/([^/?]+)/);
  return match ? match[1] : null;
}

function edgesOf(el: HTMLElement) {
  return {
    top: el.scrollTop > 0,
    bottom: el.scrollTop + el.clientHeight < el.scrollHeight - 1,
  };
}

/** 그림자 단서(h-3 = 12px)에 가리지 않도록 남기는 여유 */
const REVEAL_MARGIN_PX = 12;

/**
 * `block: "nearest"`와 같은 규칙으로 스크롤 영역 **안에서만** 대상을 보이게 한다(이미 보이면 그대로).
 * `Element.scrollIntoView`를 쓰지 않는 이유: Chromium은 scrollIntoView 대상으로 「순차 포커스 탐색 시작점」을
 * 옮겨, 페이지 진입 직후 첫 Tab이 「본문으로 건너뛰기」가 아니라 활성 내비 항목 다음으로 간다(AC-2.5 위반 —
 * P2 e2e 실측). scrollTop만 바꾸면 시작점과 조상 스크롤 컨테이너를 건드리지 않는다.
 */
export function revealWithin(container: HTMLElement, target: HTMLElement): boolean {
  const box = container.getBoundingClientRect();
  const rect = target.getBoundingClientRect();
  if (rect.top < box.top + REVEAL_MARGIN_PX) {
    container.scrollTop -= box.top + REVEAL_MARGIN_PX - rect.top;
    return true;
  }
  if (rect.bottom > box.bottom - REVEAL_MARGIN_PX) {
    container.scrollTop += rect.bottom - (box.bottom - REVEAL_MARGIN_PX);
    return true;
  }
  return false;
}

/**
 * 사이드바·드로어의 **하나의** 세로 스크롤 영역(plan §6.2.2 「세로 스크롤 경계」, r1 — PR-11).
 * 주 메뉴·관리 그룹·노트북 섹션·대화 목록을 모두 담아, 관리 그룹을 펼친 관리자도 짧은 화면에서 마지막
 * 노트북까지 닿는다. 위·아래 그림자 단서는 넘친 쪽에만 보인다. 경로가 바뀌면 활성 항목
 * (`aria-current="page"`)을 `block: "nearest"` 규칙으로 보이게 한다(`revealWithin` — scrollIntoView를 쓰지 않는
 * 이유는 그 주석) — 목록이 늦게 오면 내용 크기 변화 때 한 번 더 시도.
 */
function ScrollRegion({ scrollKey, children }: { scrollKey: string; children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const pendingReveal = useRef(true);
  const [edges, setEdges] = useState({ top: false, bottom: false });

  const update = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const next = edgesOf(el);
    setEdges((prev) => (prev.top === next.top && prev.bottom === next.bottom ? prev : next));
  }, []);

  const revealActive = useCallback(() => {
    const container = ref.current;
    const active = container?.querySelector<HTMLElement>('[aria-current="page"]');
    if (!container || !active) return;
    revealWithin(container, active);
    pendingReveal.current = false;
  }, []);

  useEffect(() => {
    pendingReveal.current = true;
    revealActive();
    update();
  }, [scrollKey, revealActive, update]);

  useEffect(() => {
    const el = ref.current;
    const content = contentRef.current;
    if (!el || !content) return;
    window.addEventListener("resize", update);
    let observer: ResizeObserver | undefined;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(() => {
        if (pendingReveal.current) revealActive();
        update();
      });
      observer.observe(el);
      observer.observe(content);
    }
    return () => {
      window.removeEventListener("resize", update);
      observer?.disconnect();
    };
  }, [revealActive, update]);

  return (
    <div
      ref={ref}
      onScroll={update}
      data-slot="sidebar-scroll"
      data-overflow-top={edges.top || undefined}
      data-overflow-bottom={edges.bottom || undefined}
      className="min-h-0 flex-1 overflow-y-auto overscroll-contain"
    >
      <div
        aria-hidden
        className={cn(
          "pointer-events-none sticky top-0 z-10 -mb-3 h-3 bg-gradient-to-b from-foreground/10 to-transparent transition-opacity motion-reduce:transition-none",
          edges.top ? "opacity-100" : "opacity-0",
        )}
      />
      <div ref={contentRef}>{children}</div>
      <div
        aria-hidden
        className={cn(
          "pointer-events-none sticky bottom-0 z-10 -mt-3 h-3 bg-gradient-to-t from-foreground/10 to-transparent transition-opacity motion-reduce:transition-none",
          edges.bottom ? "opacity-100" : "opacity-0",
        )}
      />
    </div>
  );
}

/**
 * 데스크톱 사이드바와 모바일 드로어가 공유하는 내용(plan §6.2.2·§6.2.3, D-01 대안 B).
 * 세로 flex 3구획: ① 고정 머리(`header`) ② 하나의 스크롤 영역 ③ 고정 바닥(`footer`).
 * 「관리」 그룹은 `role === "admin"`일 때만 렌더한다(AC-2.2 — 일반 사용자 DOM에 `/admin` 링크 0개).
 */
export function SidebarContent({
  me,
  variant,
  header,
  footer,
  onNavigate,
}: {
  me: User;
  variant: "sidebar" | "drawer";
  header: React.ReactNode;
  footer: React.ReactNode;
  /** 항목을 눌러 이동할 때(드로어는 닫는다) */
  onNavigate?: () => void;
}) {
  const pathname = usePathname();
  const activeNotebookId = activeNotebookIdFrom(pathname);

  return (
    <div
      data-slot="sidebar-content"
      data-variant={variant}
      className="flex h-full min-h-0 w-full flex-col bg-sidebar text-sidebar-foreground"
    >
      <div data-slot="sidebar-header" className="shrink-0">
        {header}
      </div>
      <ScrollRegion scrollKey={pathname}>
        <nav aria-label="주 메뉴" className="flex flex-col gap-0.5 px-2 pb-3 pt-1">
          <ul className="flex flex-col gap-0.5" data-testid="primary-nav">
            {MAIN_NAV.map((item) => (
              <li key={item.href}>
                <NavLink
                  href={item.href}
                  label={item.label}
                  Icon={item.Icon}
                  active={isNavActive(pathname, item)}
                  onNavigate={onNavigate}
                />
              </li>
            ))}
          </ul>
          {me.role === "admin" && (
            <div className="mt-2">
              <AdminNavGroup pathname={pathname} onNavigate={onNavigate} />
            </div>
          )}
          <NotebookNavSection activeNotebookId={activeNotebookId} onNavigate={onNavigate} />
          {variant === "drawer" && <UserAccountList me={me} onNavigate={onNavigate} />}
        </nav>
      </ScrollRegion>
      <div data-slot="sidebar-footer" className="shrink-0">
        {footer}
      </div>
    </div>
  );
}

/**
 * 데스크톱 고정 머리: 브랜드 행(높이 56, 로고 = 노트북 홈 링크, 이름 아래 앱·코어 버전 줄) + 검색 트리거(높이 36,
 * 전역 검색 진입점 하나).
 */
export function SidebarDesktopHeader() {
  const { setOpen } = useCommandPalette();
  return (
    <>
      <div className="flex h-14 items-center px-3">
        <Link
          href="/"
          className="flex min-w-0 items-center gap-2.5 rounded-lg pr-2 outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring"
        >
          <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <Zap className="size-4" aria-hidden />
          </span>
          <span className="min-w-0">
            <span className="block text-sm font-semibold">biznotes</span>
            <AppVersions />
          </span>
        </Link>
      </div>
      <div className="px-2 pb-2">
        <button
          type="button"
          onClick={() => setOpen(true)}
          data-testid="open-search"
          aria-keyshortcuts="Meta+K Control+K"
          className="flex h-9 w-full items-center gap-2.5 rounded-lg border border-sidebar-border bg-background/60 px-2.5 text-sm text-sidebar-foreground/80 outline-none transition-colors hover:bg-sidebar-accent hover:text-sidebar-foreground focus-visible:ring-2 focus-visible:ring-sidebar-ring coarse:min-h-11"
        >
          <Search className="size-4 shrink-0" aria-hidden />
          <span className="flex-1 text-left">검색</span>
          <kbd aria-hidden className="rounded border border-sidebar-border px-1.5 font-sans text-xs text-muted-foreground">
            ⌘K
          </kbd>
        </button>
      </div>
    </>
  );
}

/** 데스크톱 고정 바닥(높이 56, 위 테두리): 사용자 메뉴 트리거 + 테마 버튼 — 스크롤과 무관하게 항상 보인다. */
export function SidebarDesktopFooter({ me }: { me: User }) {
  return (
    <div className="flex h-14 items-center gap-1 border-t border-sidebar-border px-2">
      <UserMenu me={me} />
      <ThemeSwitcher />
    </div>
  );
}
