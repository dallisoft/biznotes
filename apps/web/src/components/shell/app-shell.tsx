"use client";

import { useEffect, useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { CommandPalette } from "@/components/shell/command-palette";
import { MobileTopBar } from "@/components/shell/mobile-top-bar";
import {
  SidebarContent,
  SidebarDesktopFooter,
  SidebarDesktopHeader,
} from "@/components/shell/sidebar-content";
import { SkipLink } from "@/components/shell/skip-link";
import { useMediaQuery } from "@/hooks/use-media-query";
import type { User } from "@/lib/types";

/** 데스크톱 사이드바 폭(px) — 기본 264, 리사이즈 220–360(plan §6.2.2). */
export const SIDEBAR_WIDTH_PX = { default: 264, min: 220, max: 360 } as const;

/**
 * react-resizable-panels v2는 크기를 % 로만 받는다 — 셸이 창 전체 폭을 쓰므로 창 폭으로 px를 % 로 바꾼다.
 * 창 폭이 바뀌면 최소·최대를 다시 계산한다(패널 라이브러리가 저장된 배치를 새 범위로 맞춘다).
 */
export function sidebarPanelSizes(viewportWidth: number) {
  const width = Math.max(viewportWidth, 1);
  const pct = (px: number) => Math.min(Math.max((px / width) * 100, 1), 90);
  return {
    defaultSize: pct(SIDEBAR_WIDTH_PX.default),
    minSize: pct(SIDEBAR_WIDTH_PX.min),
    maxSize: pct(SIDEBAR_WIDTH_PX.max),
  };
}

function useViewportWidth(): number {
  // 셸은 인증 가드 뒤에서 클라이언트 마운트로만 그려진다(SSR은 스켈레톤) — 첫 렌더에서 창 폭을 읽어도 된다
  const [width, setWidth] = useState(() => (typeof window === "undefined" ? 1280 : window.innerWidth));
  useEffect(() => {
    const onResize = () => setWidth(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return width;
}

/**
 * 로그인 후 모든 화면의 단일 셸(D-01 대안 B, plan §6.2.1~6.2.3).
 * - ≥ 768px: 리사이즈 가능한 사이드바(`SidebarContent`) — 관리 화면에서도 교체하지 않는다(DP-1).
 * - < 768px: 상단 바 + 드로어(같은 `SidebarContent`).
 * - 랜드마크: 셸의 `main#main-content` 하나(AC-2.5). 페이지는 `main` 대신 `PageContainer`를 쓴다.
 * - 높이·스크롤 계약(P2 통합 — 트랙 pages·admin): `main`은 세로 flex 열(데스크톱 패널 / 모바일은 상단 바 아래)의
 *   `min-h-0 flex-1` 항목이라 **확정 높이**를 가진다. **세로 스크롤의 주인은 하나 — 페이지다**: 일반 페이지는
 *   `div.h-full.overflow-y-auto`, 관리 화면은 `admin/layout.tsx`의 스크롤 div, bleed(노트북·편집기)는 `h-full`로
 *   `main` 높이를 채우고 안에서 스크롤한다. 그래서 `main`은 스크롤 영역이 아니다(`overflow-hidden` — 겹친 스크롤
 *   영역이 생기지 않는다). 페이지가 래퍼를 빠뜨려 넘치면 e2e 「랜드마크…」의 `main` 넘침(scrollHeight) 단언이 잡는다.
 * - 첫 탭 정지점은 「본문으로 건너뛰기」.
 */
export function AppShell({ me, children }: { me: User; children: React.ReactNode }) {
  const isDesktop = useMediaQuery("(min-width: 768px)", true);
  const sizes = sidebarPanelSizes(useViewportWidth());

  return (
    <div className="flex h-screen overflow-hidden supports-[height:100dvh]:h-dvh">
      <SkipLink targetId="main-content" />
      <PanelGroup direction="horizontal" autoSaveId="app-shell" className="flex flex-1">
        {isDesktop && (
          <>
            <Panel
              id="sidebar"
              order={1}
              defaultSize={sizes.defaultSize}
              minSize={sizes.minSize}
              maxSize={sizes.maxSize}
              className="flex"
            >
              <div className="flex h-full w-full border-r border-sidebar-border" data-testid="app-sidebar">
                <SidebarContent
                  me={me}
                  variant="sidebar"
                  header={<SidebarDesktopHeader />}
                  footer={<SidebarDesktopFooter me={me} />}
                />
              </div>
            </Panel>
            <PanelResizeHandle className="group relative flex w-2 items-center justify-center outline-none">
              <div className="h-10 w-1 rounded-full bg-border transition-colors group-hover:bg-primary/50 group-data-[resize-handle-state=drag]:bg-primary" />
            </PanelResizeHandle>
          </>
        )}

        <Panel id="main" order={2} className="flex min-h-0 min-w-0 flex-col">
          {!isDesktop && <MobileTopBar me={me} />}
          <main
            id="main-content"
            tabIndex={-1}
            className="min-h-0 min-w-0 flex-1 overflow-hidden focus:outline-none"
          >
            {children}
          </main>
        </Panel>
      </PanelGroup>
      <CommandPalette />
    </div>
  );
}
