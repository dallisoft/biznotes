"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useId } from "react";
import { CreateNotebookDialog } from "@/components/dashboard/create-notebook-dialog";
import {
  NAV_ROW_ACTIVE_CLASS,
  NAV_ROW_CLASS,
  NAV_ROW_IDLE_CLASS,
  SIDEBAR_BUTTON_FOCUS_CLASS,
} from "@/components/shell/nav-link";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useChatSessions } from "@/hooks/use-chat";
import { useNotebooks } from "@/hooks/use-notebooks";
import { formatNumber } from "@/lib/format";
import type { Notebook } from "@/lib/types";
import { cn } from "@/lib/utils";

/** 사이드바에 보이는 노트북 수 상한(plan §6.2.2 ⑤, HCI-3) — 나머지는 「모든 노트북 보기」·검색으로. */
export const SIDEBAR_NOTEBOOK_LIMIT = 10;
/** 현재 노트북 아래에 들여 보이는 대화 수 상한 — 전체는 노트북 화면의 「대화 기록」 메뉴. */
export const SIDEBAR_SESSION_LIMIT = 5;

function updatedAtMs(nb: Notebook): number {
  const ms = Date.parse(nb.updated_at);
  return Number.isFinite(ms) ? ms : 0;
}

/**
 * 사이드바 노트북 목록: 즐겨찾기 먼저, 그다음 최근 수정 순으로 최대 `limit`개(AC-2.6).
 * 현재 노트북이 그 안에 없으면 `pinnedActive`로 따로 돌려준다(목록 맨 위에 추가로 보인다).
 * 정렬은 응답 배열 전체로 한다 — 서버 정렬(`updated_at desc`)에 기대지 않는다.
 */
export function selectSidebarNotebooks(
  notebooks: readonly Notebook[],
  activeId: string | null,
  limit: number = SIDEBAR_NOTEBOOK_LIMIT,
): { items: Notebook[]; pinnedActive: Notebook | null } {
  const sorted = [...notebooks].sort(
    (a, b) => Number(b.is_favorite) - Number(a.is_favorite) || updatedAtMs(b) - updatedAtMs(a),
  );
  const items = sorted.slice(0, limit);
  const active = activeId ? (notebooks.find((nb) => nb.id === activeId) ?? null) : null;
  const pinnedActive = active && !items.some((nb) => nb.id === active.id) ? active : null;
  return { items, pinnedActive };
}

// 현재 노트북 아래의 대화 목록(최대 5개). 「새 대화」 실행 컨트롤은 노트북 화면 대화 바 하나다(D-03).
function SessionList({ notebookId, onNavigate }: { notebookId: string; onNavigate?: () => void }) {
  const sessions = useChatSessions(notebookId);
  const searchParams = useSearchParams();
  const activeSession = searchParams.get("s");
  const list = (sessions.data ?? []).slice(0, SIDEBAR_SESSION_LIMIT);
  if (list.length === 0) return null;

  return (
    <ul
      className="ml-5 mt-0.5 flex flex-col gap-0.5 border-l border-sidebar-border pl-2"
      data-testid="sidebar-session-list"
    >
      {list.map((s) => (
        <li key={s.id}>
          <Link
            href={`/notebooks/${notebookId}?s=${s.id}`}
            title={s.title}
            aria-current={s.id === activeSession ? "true" : undefined}
            onClick={onNavigate}
            className={cn(
              "flex h-7 items-center truncate rounded-md px-2 text-xs outline-none transition-colors focus-visible:ring-2 focus-visible:ring-sidebar-ring coarse:min-h-11",
              s.id === activeSession
                ? "bg-sidebar-accent text-sidebar-foreground"
                : "text-sidebar-foreground/70 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
            )}
          >
            <span className="truncate">{s.title}</span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function NotebookRow({
  notebook,
  active,
  onNavigate,
}: {
  notebook: Notebook;
  active: boolean;
  onNavigate?: () => void;
}) {
  return (
    <li>
      {/* 링크 이름은 「{이모지} {제목}」 — 기존 e2e(dashboard-bulk-actions P1-06)가 이 이름으로 찾는다.
          두 span 사이 공백은 flex 배치에 영향이 없고, CSS 없이 계산해도 이름에 공백이 들어가게 한다 */}
      <Link
        href={`/notebooks/${notebook.id}`}
        title={notebook.title}
        aria-current={active ? "page" : undefined}
        onClick={onNavigate}
        className={cn(NAV_ROW_CLASS, "h-8 gap-2", active ? NAV_ROW_ACTIVE_CLASS : NAV_ROW_IDLE_CLASS)}
      >
        <span className="text-base leading-none">{notebook.emoji}</span>{" "}
        <span className="min-w-0 truncate">{notebook.title}</span>
      </Link>
      {active && <SessionList notebookId={notebook.id} onNavigate={onNavigate} />}
    </li>
  );
}

/**
 * 사이드바 노트북 섹션(plan §6.2.2 ⑤). 머리글 「노트북」은 heading이 아니다 — e2e 공용 헬퍼가 홈의
 * `heading "노트북" exact`를 단언하므로 같은 이름의 heading을 새로 만들지 않는다(§9 공통 규칙).
 */
export function NotebookNavSection({
  activeNotebookId,
  onNavigate,
}: {
  activeNotebookId: string | null;
  onNavigate?: () => void;
}) {
  const labelId = useId();
  const notebooks = useNotebooks("all");
  const all = notebooks.data ?? [];
  const { items, pinnedActive } = selectSidebarNotebooks(all, activeNotebookId);

  return (
    <div role="group" aria-labelledby={labelId} className="mt-4 flex flex-col gap-0.5">
      <div className="flex items-center justify-between gap-2 pb-1 pl-2.5 pr-0.5">
        <span id={labelId} className="text-xs font-medium text-muted-foreground">
          노트북
        </span>
        {/* 노트북 생성 보조 진입점(D-03) — 주 진입점은 홈 PageHeader의 「새 노트북」 */}
        <CreateNotebookDialog
          testId="create-notebook-sidebar"
          triggerLabel="새 노트북"
          iconOnly
          triggerClassName={cn(
            "text-sidebar-foreground/80 hover:bg-sidebar-accent hover:text-sidebar-foreground coarse:size-11",
            SIDEBAR_BUTTON_FOCUS_CLASS,
          )}
        />
      </div>

      {notebooks.isPending && (
        <div role="status" className="flex flex-col gap-1.5 px-2.5 py-1">
          <span className="sr-only">불러오는 중</span>
          <Skeleton className="h-6 w-full" />
          <Skeleton className="h-6 w-4/5" />
          <Skeleton className="h-6 w-3/5" />
        </div>
      )}

      {notebooks.isError && (
        <div className="flex flex-col items-start gap-1 px-2.5 py-1 text-xs text-muted-foreground">
          <p>노트북 목록을 불러오지 못했습니다</p>
          {/* 터치 기기에서 44×44(plan §6.8-8 — P2 리뷰 P2-03). 데스크톱은 작은 글자 버튼 */}
          <Button
            variant="ghost"
            size="xs"
            onClick={() => notebooks.refetch()}
            className={cn(
              "-ml-2 text-sidebar-foreground hover:bg-sidebar-accent coarse:min-h-11 coarse:min-w-11",
              SIDEBAR_BUTTON_FOCUS_CLASS,
            )}
          >
            다시 시도
          </Button>
        </div>
      )}

      {notebooks.isSuccess && all.length === 0 && (
        <p className="px-2.5 py-1 text-xs text-muted-foreground">아직 노트북이 없습니다</p>
      )}

      {notebooks.isSuccess && all.length > 0 && (
        <>
          <ul className="flex flex-col gap-0.5" data-testid="sidebar-notebook-list">
            {pinnedActive && (
              <NotebookRow notebook={pinnedActive} active onNavigate={onNavigate} />
            )}
            {items.map((nb) => (
              <NotebookRow
                key={nb.id}
                notebook={nb}
                active={nb.id === activeNotebookId}
                onNavigate={onNavigate}
              />
            ))}
          </ul>
          <Link
            href="/"
            onClick={onNavigate}
            className={cn(NAV_ROW_CLASS, NAV_ROW_IDLE_CLASS, "h-8 text-xs")}
          >
            모든 노트북 보기 ({formatNumber(all.length)})
          </Link>
        </>
      )}
    </div>
  );
}
