"use client";

import { useQuery } from "@tanstack/react-query";
import { Bot, FileText, MessageSquare, PanelRight, StickyNote } from "lucide-react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  type ImperativePanelGroupHandle,
  Panel,
  PanelGroup,
  PanelResizeHandle,
} from "react-resizable-panels";
import { AgentPanel } from "@/components/agents/agent-panel";
import { ChatPanel } from "@/components/chat/chat-panel";
import { NotePanel } from "@/components/notes/note-panel";
import { PageContainer } from "@/components/layout/page-container";
import { NotebookHeader } from "@/components/notebooks/notebook-header";
import { SourcePanel } from "@/components/sources/source-panel";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { notebookDetailKey } from "@/hooks/use-notebooks";
import { useMediaQuery } from "@/hooks/use-media-query";
import { api, ApiError } from "@/lib/api-client";
import type { Notebook } from "@/lib/types";
import { useChatPrompt } from "@/stores/chat-prompt";
import { useSourceFocus } from "@/stores/source-focus";

// 우측 패널 탭: 소스 / 메모 / 에이전트. 저장값(`nb-right-panel`의 tab)은 기존 형식 `files`를 유지한다(plan §8.2).
type RightTab = "files" | "notes" | "agents";
// 모바일(<md): 한 번에 한 화면만 — 하단 탭으로 전환
type MobileView = "chat" | "files" | "notes" | "agents";

const PANEL_STORAGE_KEY = "nb-right-panel";
/** 소스 상세(읽기 모드)의 우측 패널 폭(%) — 패널 maxSize(55) 안이다. */
const READING_PANEL_SIZE = 45;

const RIGHT_TABS: { tab: RightTab; label: string; Icon: typeof FileText }[] = [
  { tab: "files", label: "소스", Icon: FileText },
  { tab: "notes", label: "메모", Icon: StickyNote },
  { tab: "agents", label: "에이전트", Icon: Bot },
];

/** 모바일 하단 탭 4개(plan §6.6.2 — 채팅·소스·메모·에이전트). */
const MOBILE_TABS: { view: MobileView; label: string; Icon: typeof FileText }[] = [
  { view: "chat", label: "채팅", Icon: MessageSquare },
  { view: "files", label: "소스", Icon: FileText },
  { view: "notes", label: "메모", Icon: StickyNote },
  { view: "agents", label: "에이전트", Icon: Bot },
];

const tabId = (tab: RightTab) => `nb-right-tab-${tab}`;
const panelId = (tab: RightTab) => `nb-right-panel-${tab}`;

// 채팅 중심 작업 화면: 노트북 헤더 + 중앙 채팅 + 우측 소스/메모/에이전트 탭 패널
export default function NotebookPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const searchParams = useSearchParams();
  const sessionId = searchParams.get("s");

  const [rightOpen, setRightOpen] = useState(true);
  const [rightTab, setRightTab] = useState<RightTab>("files");
  const [mobileView, setMobileView] = useState<MobileView>("chat");
  // 헤더에서 노트북을 지웠으면 노트북 홈으로 가는 동안 지워진 노트북을 다시 읽지 않는다(상세 조회 중지 + 패널 언마운트)
  const [deleted, setDeleted] = useState(false);
  // 데스크톱(md+)은 리사이즈 가능한 패널 그룹, 모바일은 단일 화면 — 한쪽만 마운트
  const isDesktop = useMediaQuery("(min-width: 768px)", true);

  // 노트북을 떠나는 링크(브레드크럼·사이드바 등)를 눌렀는가 — 화면이 열린 뒤 늦게 끝난 자동 대화 선택(첫 대화 생성 등)이
  // `router.replace(?s=)`로 진행 중인 이동을 덮어 이 노트북으로 되돌려 보내지 않게 한다(P3 게이트 3회차에서 e2e
  // `core-flows` 2가 드러낸 경합: 세션 POST가 브레드크럼 클릭 직후 끝나면 노트북 홈 이동이 취소됐다).
  // 캡처 단계에서 링크의 이동 처리보다 먼저 본다. 새 탭·다운로드·다른 출처·같은 경로는 떠나는 것이 아니다.
  // 이동이 끝내 일어나지 않으면(5초) 다시 자동 선택을 받는다.
  const leavingRef = useRef(false);
  // 읽기 모드(ui-ux-improvements ②) — 소스 상세가 열리면 우측 패널을 읽기 폭으로 넓히고, 닫히면 원래 폭으로 되돌린다
  // 그룹 레이아웃([채팅, 우측])으로 다룬다 — 패널 단위 getSize는 조건부로 붙는 우측 패널에서 등록 시점이 어긋날 수 있다
  const panelGroupRef = useRef<ImperativePanelGroupHandle>(null);
  const layoutBeforeReadingRef = useRef<number[] | null>(null);
  const handleSourceDetailOpenChange = useCallback((open: boolean) => {
    const group = panelGroupRef.current;
    if (!group) return;
    const layout = group.getLayout();
    if (layout.length !== 2) return; // 우측 패널이 접혀 있다
    if (open) {
      if (layout[1] < READING_PANEL_SIZE) {
        layoutBeforeReadingRef.current = layout;
        group.setLayout([100 - READING_PANEL_SIZE, READING_PANEL_SIZE]);
      }
    } else if (layoutBeforeReadingRef.current) {
      group.setLayout(layoutBeforeReadingRef.current);
      layoutBeforeReadingRef.current = null;
    }
  }, []);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    function onClick(e: MouseEvent) {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const a = (e.target as Element | null)?.closest?.("a[href]") as HTMLAnchorElement | null;
      if (!a || a.hasAttribute("download") || (a.target && a.target !== "_self")) return;
      const url = new URL(a.href, window.location.href);
      if (!/^https?:$/.test(url.protocol) || url.origin !== window.location.origin) return;
      if (url.pathname === window.location.pathname) return;
      leavingRef.current = true;
      clearTimeout(timer);
      timer = setTimeout(() => (leavingRef.current = false), 5000);
    }
    document.addEventListener("click", onClick, true);
    return () => {
      document.removeEventListener("click", onClick, true);
      clearTimeout(timer);
    };
  }, []);

  // URL의 세션 파라미터로 현재 대화를 제어 (사이드바·드롭다운과 단일 출처 공유)
  const handleSessionChange = useCallback(
    (next: string, options?: { auto?: boolean }) => {
      if (options?.auto && leavingRef.current) return;
      router.replace(`/notebooks/${id}?s=${next}`, { scroll: false });
    },
    [router, id],
  );

  // 저장된 우측 패널 상태 복원 (SSR hydration mismatch 방지를 위해 effect에서)
  useEffect(() => {
    try {
      const stored = localStorage.getItem(PANEL_STORAGE_KEY);
      if (!stored) return;
      const parsed = JSON.parse(stored) as { open?: boolean; tab?: RightTab };
      if (typeof parsed.open === "boolean") setRightOpen(parsed.open);
      if (parsed.tab === "files" || parsed.tab === "notes" || parsed.tab === "agents") {
        setRightTab(parsed.tab);
      }
    } catch {
      // 손상된 저장값은 무시
    }
  }, []);

  // [Phase 18c] 인용 클릭으로 소스 포커스가 요청되면 파일 탭을 열어 SourcePanel이 보이게 한다.
  const requestedSourceId = useSourceFocus((s) => s.requestedSourceId);
  useEffect(() => {
    if (!requestedSourceId) return;
    setRightTab("files");
    setRightOpen(true);
    setMobileView("files");
  }, [requestedSourceId]);

  // [mindmap-agent-skill AC-2.7] 마인드맵 노드가 이 노트북의 채팅 질문을 요청하면 모바일은 채팅 화면으로 옮긴다.
  const promptNonces = useChatPrompt((s) =>
    Object.values(s.requests)
      .filter((r) => r.notebookId === id)
      .map((r) => r.nonce)
      .join(","),
  );
  const seenPromptRef = useRef("");
  useEffect(() => {
    if (!promptNonces || promptNonces === seenPromptRef.current) return;
    seenPromptRef.current = promptNonces;
    setMobileView("chat");
  }, [promptNonces]);

  function persist(open: boolean, tab: RightTab) {
    try {
      localStorage.setItem(PANEL_STORAGE_KEY, JSON.stringify({ open, tab }));
    } catch {
      // 저장소를 쓸 수 없으면(사생활 보호 모드 등) 기억하지 않을 뿐 동작은 같다
    }
  }

  function selectTab(tab: RightTab) {
    setRightTab(tab);
    setRightOpen(true);
    persist(true, tab);
  }

  function toggleRight() {
    setRightOpen((v) => {
      persist(!v, rightTab);
      return !v;
    });
  }

  // 우측 탭의 화살표·Home·End 이동(WAI-ARIA 탭 패턴 — 이동하면 바로 선택)
  function onTabKeyDown(e: React.KeyboardEvent<HTMLButtonElement>, index: number) {
    const last = RIGHT_TABS.length - 1;
    const next =
      e.key === "ArrowRight"
        ? (index + 1) % RIGHT_TABS.length
        : e.key === "ArrowLeft"
          ? (index + last) % RIGHT_TABS.length
          : e.key === "Home"
            ? 0
            : e.key === "End"
              ? last
              : null;
    if (next === null) return;
    e.preventDefault();
    const { tab } = RIGHT_TABS[next];
    selectTab(tab);
    document.getElementById(tabId(tab))?.focus();
  }

  const notebook = useQuery<Notebook, ApiError>({
    queryKey: notebookDetailKey(id),
    queryFn: () => api.get<Notebook>(`/api/v1/notebooks/${id}`),
    enabled: !deleted,
  });

  if (deleted) {
    return (
      <PageContainer width="default">
        <p role="status" className="text-sm text-muted-foreground">
          노트북을 삭제했습니다. 노트북 홈으로 이동하는 중입니다…
        </p>
      </PageContainer>
    );
  }

  if (notebook.isPending) {
    return (
      <PageContainer width="default">
        <Skeleton className="h-10 w-64" />
      </PageContainer>
    );
  }

  if (notebook.isError) {
    const notFound = notebook.error instanceof ApiError && notebook.error.status === 404;
    return (
      <PageContainer width="default" className="flex h-full flex-col items-center justify-center">
        <ErrorState
          className="w-full max-w-md"
          title={notFound ? "노트북을 찾을 수 없습니다." : "노트북을 불러오지 못했습니다"}
          description={
            notFound ? "삭제되었거나 볼 수 있는 권한이 없는 노트북입니다." : "잠시 후 다시 시도해 주세요."
          }
          onRetry={notFound ? undefined : () => void notebook.refetch()}
          retrying={notebook.isFetching}
          back={{ href: "/", label: "노트북 홈으로" }}
        />
      </PageContainer>
    );
  }

  const canWrite = notebook.data.permission !== "read";

  // [Phase 18c·codex D7] 파일 탭이 실제로 화면에 보이는지 — 데스크톱은 우측 패널이
  // 열려 있고 탭이 files일 때, 모바일은 mobileView가 files일 때만 true.
  // SourcePanel의 인용 포커스 스크롤이 hidden 상태에서 no-op으로 소모되지 않도록 전달한다.
  const filesVisible = isDesktop
    ? rightOpen && rightTab === "files"
    : mobileView === "files";

  // 우측 패널 3탭 — 닫힌 탭도 hidden으로 유지(언마운트 X)해 선택 상태·쿼리 보존.
  // 데스크톱/모바일 분기 어느 쪽이든 한 번만 렌더되므로 컴포넌트는 단일 마운트.
  // 데스크톱에서는 위 탭 목록(role=tablist)의 tabpanel이고, 모바일에서는 하단 탭바가 고르는 화면이다.
  function renderRightPanels(asTabPanels: boolean) {
    // flex-col 래퍼: 자식 패널이 w-full 여부와 무관하게 교차축 stretch로 패널 폭을 채움
    // (이전엔 row flex라 w-full 없는 NotePanel/AgentPanel이 콘텐츠 폭에 머물렀음)
    const panelProps = (tab: RightTab) =>
      asTabPanels ? { role: "tabpanel", id: panelId(tab), "aria-labelledby": tabId(tab) } : {};
    return (
      <>
        <div
          {...panelProps("files")}
          className={`min-h-0 flex-1 flex-col ${rightTab === "files" ? "flex" : "hidden"}`}
        >
          <SourcePanel
            notebookId={id}
            canWrite={canWrite}
            visible={filesVisible}
            onDetailOpenChange={isDesktop ? handleSourceDetailOpenChange : undefined}
          />
        </div>
        <div
          {...panelProps("notes")}
          className={`min-h-0 flex-1 flex-col ${rightTab === "notes" ? "flex" : "hidden"}`}
        >
          <NotePanel notebookId={id} canWrite={canWrite} />
        </div>
        <div
          {...panelProps("agents")}
          className={`min-h-0 flex-1 flex-col ${rightTab === "agents" ? "flex" : "hidden"}`}
        >
          <AgentPanel notebookId={id} canWrite={canWrite} />
        </div>
      </>
    );
  }

  // 우측 패널 상단 — 소스/메모/에이전트 탭 + 접기 토글 (데스크톱 전용, 모바일은 하단 탭바).
  function rightPanelHeader() {
    return (
      <div className="hidden h-12 shrink-0 items-center gap-1 border-b px-2 md:flex">
        {/* 패널이 좁으면(1024 폭의 기본 28% 등) 탭 줄만 가로 스크롤하고 접기 버튼은 항상 보인다 */}
        <div
          role="tablist"
          aria-label="오른쪽 패널"
          className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto overflow-y-hidden"
        >
          {RIGHT_TABS.map(({ tab, label, Icon }, index) => {
            const active = rightTab === tab;
            return (
              <Button
                key={tab}
                id={tabId(tab)}
                role="tab"
                aria-selected={active}
                aria-controls={panelId(tab)}
                tabIndex={active ? 0 : -1}
                variant={active ? "secondary" : "ghost"}
                size="sm"
                onClick={() => selectTab(tab)}
                onKeyDown={(e) => onTabKeyDown(e, index)}
                className={`coarse:min-h-11 ${active ? "text-primary" : "text-muted-foreground"}`}
                data-testid={`right-tab-${tab}`}
              >
                <Icon className="size-4" aria-hidden />
                {label}
              </Button>
            );
          })}
        </div>
        <Tooltip>
          <TooltipTrigger
            render={
              <Button
                variant="ghost"
                size="icon"
                aria-label="우측 패널 접기"
                onClick={toggleRight}
                className="ml-auto shrink-0 text-muted-foreground coarse:size-11"
                data-testid="toggle-right-panel"
              >
                <PanelRight className="size-4" />
              </Button>
            }
          />
          <TooltipContent>패널 접기</TooltipContent>
        </Tooltip>
      </div>
    );
  }

  return (
    <PageContainer width="bleed" className="flex flex-col">
      <NotebookHeader notebook={notebook.data} onDeleted={() => setDeleted(true)} />
      <div className="flex min-h-0 flex-1">
        {isDesktop ? (
          // 데스크톱: 채팅 | 드래그 핸들 | 우측 패널 (폭은 autoSaveId로 localStorage 저장)
          <>
          <PanelGroup
            ref={panelGroupRef}
            direction="horizontal"
            autoSaveId="nb-workspace"
            className="flex min-h-0 flex-1"
          >
            <Panel id="chat" order={1} minSize={35} className="flex min-h-0 min-w-0">
              <ChatPanel
                notebookId={id}
                sessionId={sessionId}
                onSessionChange={handleSessionChange}
                canWrite={canWrite}
              />
            </Panel>
            {rightOpen && (
              <>
                <PanelResizeHandle className="group relative flex w-2 items-center justify-center outline-none">
                  <div className="h-10 w-1 rounded-full bg-border transition-colors group-hover:bg-primary/50 group-data-[resize-handle-state=drag]:bg-primary" />
                </PanelResizeHandle>
                <Panel
                  id="right"
                  order={2}
                  defaultSize={28}
                  minSize={18}
                  maxSize={55}
                  className="flex min-h-0 flex-col border-l"
                >
                  {rightPanelHeader()}
                  {renderRightPanels(true)}
                </Panel>
              </>
            )}
          </PanelGroup>
          {/* 우측 패널이 접혀 있을 때, 다시 펼칠 수 있는 아이콘 레일 */}
          {!rightOpen && (
            <div className="hidden w-12 shrink-0 flex-col items-center gap-1 border-l py-2 md:flex">
              {RIGHT_TABS.map(({ tab, label, Icon }) => (
                <Tooltip key={tab}>
                  <TooltipTrigger
                    render={
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={label}
                        onClick={() => selectTab(tab)}
                        className="text-muted-foreground coarse:size-11"
                        data-testid={`right-tab-${tab}`}
                      >
                        <Icon className="size-4" aria-hidden />
                      </Button>
                    }
                  />
                  <TooltipContent side="left">{label}</TooltipContent>
                </Tooltip>
              ))}
            </div>
          )}
          </>
        ) : (
          // 모바일: 한 화면만 — 닫힌 패널도 hidden으로 유지(언마운트 X)해 상태 보존
          <>
            <div className={`min-w-0 flex-1 ${mobileView === "chat" ? "flex" : "hidden"}`}>
              <ChatPanel
                notebookId={id}
                sessionId={sessionId}
                onSessionChange={handleSessionChange}
                canWrite={canWrite}
              />
            </div>
            <aside
              className={`min-h-0 w-full flex-col border-l ${
                mobileView !== "chat" ? "flex" : "hidden"
              }`}
            >
              {renderRightPanels(false)}
            </aside>
          </>
        )}
      </div>

      {/* 모바일 하단 탭바 — 데스크톱은 숨김. 탭 4개, 각 터치 영역 44px 이상(AC-3.4) */}
      <nav
        className="flex shrink-0 border-t bg-card md:hidden"
        aria-label="패널 전환"
        data-testid="mobile-tabbar"
      >
        {MOBILE_TABS.map(({ view, label, Icon }) => (
          <button
            key={view}
            type="button"
            onClick={() => {
              setMobileView(view);
              if (view !== "chat") setRightTab(view);
            }}
            aria-pressed={mobileView === view}
            data-testid={`mobile-tab-${view}`}
            className={`flex min-h-11 min-w-11 flex-1 flex-col items-center justify-center gap-0.5 py-2 text-xs transition-colors ${
              mobileView === view
                ? "text-primary"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            <Icon className="size-5" aria-hidden />
            {label}
          </button>
        ))}
      </nav>
    </PageContainer>
  );
}
