"use client";

import { NotebookPen, Plus, Share2, Star, Tag as TagIcon, Trash2, Users } from "lucide-react";
import { useMemo, useState } from "react";
import { BulkActionBar } from "@/components/dashboard/bulk-action-bar";
import { BulkDeleteDialog } from "@/components/dashboard/bulk-delete-dialog";
import { BulkShareDialog } from "@/components/dashboard/bulk-share-dialog";
import { CreateNotebookDialog } from "@/components/dashboard/create-notebook-dialog";
import { ManageTagsDialog } from "@/components/dashboard/manage-tags-dialog";
import { NotebookCard } from "@/components/dashboard/notebook-card";
import { NotebookTable } from "@/components/dashboard/notebook-table";
import { RenameNotebookDialog } from "@/components/dashboard/rename-notebook-dialog";
import { ShareNotebookDialog } from "@/components/dashboard/share-notebook-dialog";
import { useDashboardViewMode, ViewModeToggle } from "@/components/dashboard/view-mode-toggle";
import { RecentChats } from "@/components/dashboard/recent-chats";
import { PageContainer } from "@/components/layout/page-container";
import { Toolbar } from "@/components/layout/toolbar";
import { PageHeader } from "@/components/shell/page-header";
import { Button } from "@/components/ui/button";
import { EmptyState, FilteredEmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { LoadingState } from "@/components/ui/loading-state";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { isBulkSelectable, useNotebookSelection } from "@/hooks/use-notebook-selection";
import type { BulkRunReport } from "@/hooks/use-notebook-bulk";
import { useDialogSlot } from "@/hooks/use-dialog-slot";
import { useNotebooks } from "@/hooks/use-notebooks";
import { type BulkTarget, settledIds } from "@/lib/notebook-bulk";
import type { Notebook, NotebookFilter } from "@/lib/types";
import { cn } from "@/lib/utils";

/** 탭 라벨(plan §6.3.1 — 「공유받은 노트북」, AC-3.7). 값은 API `filter` 그대로. */
const NOTEBOOK_FILTER_TABS: readonly { value: NotebookFilter; label: string }[] = [
  { value: "all", label: "전체" },
  { value: "mine", label: "내 노트북" },
  { value: "shared", label: "공유받은 노트북" },
];

/**
 * 카드 그리드(T1, plan §6.6.1): 열 수는 뷰포트가 아니라 콘텐츠 폭이 정한다 — `repeat(auto-fill, minmax(260px, 1fr))`,
 * 간격 16. 콘텐츠가 260px보다 좁은 화면에서도 가로로 넘치지 않게 최소 폭을 100%로 묶는다.
 */
const NOTEBOOK_GRID_CLASS = "grid grid-cols-[repeat(auto-fill,minmax(min(260px,100%),1fr))] gap-4";

export default function DashboardPage() {
  const [filter, setFilter] = useState<NotebookFilter>("all");
  // 「공유받은 노트북」은 내 것이 아닌, 볼 수 있는 노트북 전부(공유 + 공개)다. 코어의 `shared`는 공유 기록이 있는
  // 노트북만 돌려줘서, 공유 기록 없이 공개된 노트북(예: 샘플)이 「전체」에는 보이는데 이 탭에서는 빠졌다. 그래서 이 탭은
  // `all`을 받아 소유하지 않은 것만 남긴다 — 「전체 = 내 노트북 + 공유받은 노트북」이 늘 성립한다.
  const notebooks = useNotebooks(filter === "shared" ? "all" : filter);
  const tabNotebooks = useMemo(
    () => (notebooks.data ?? []).filter((nb) => filter !== "shared" || !nb.is_owner),
    [notebooks.data, filter],
  );
  const [createOpen, setCreateOpen] = useState(false);
  // 단건 다이얼로그 — 닫은 창의 요청이 늦게 끝나도 그사이 다른 노트북으로 연 같은 창을 닫지 않는다(P3 리뷰 P3-02)
  const shareDialog = useDialogSlot<string>();
  // 공유 관리 대화상자의 대상 — 카드·표 메뉴는 id만 넘기므로 목록 캐시에서 찾는다(전체 공개 토글이 `visibility`·`is_owner`를
  // 쓴다). 토글 응답으로 목록 캐시가 바뀌면 이 값도 바뀐다. 목록에서 사라졌으면(다른 곳에서 삭제) 대화상자를 닫는다
  const shareTarget = useMemo(
    () => (shareDialog.value === null ? null : (notebooks.data ?? []).find((nb) => nb.id === shareDialog.value) ?? null),
    [shareDialog.value, notebooks.data],
  );
  const renameDialog = useDialogSlot<Notebook>();
  const tagsDialog = useDialogSlot<Notebook>();
  const [activeTag, setActiveTag] = useState<string | null>(null);
  const [favoritesOnly, setFavoritesOnly] = useState(false);
  const [viewMode, setViewMode] = useDashboardViewMode();

  const allTags = useMemo(() => {
    const set = new Set<string>();
    for (const nb of tabNotebooks) {
      for (const t of nb.tags ?? []) set.add(t);
    }
    return [...set].sort();
  }, [tabNotebooks]);

  const visibleNotebooks = useMemo(() => {
    return tabNotebooks.filter((nb) => {
      if (favoritesOnly && !nb.is_favorite) return false;
      if (activeTag && !(nb.tags ?? []).includes(activeTag)) return false;
      return true;
    });
  }, [tabNotebooks, favoritesOnly, activeTag]);

  // S-3: 탭·즐겨찾기만·태그 필터가 바뀌면 선택을 비운다. 뷰 모드는 넣지 않는다(전환해도 선택 유지).
  const selection = useNotebookSelection(
    visibleNotebooks,
    JSON.stringify([filter, favoritesOnly, activeTag]),
  );
  // 일괄 삭제·공유 대상 — 버튼을 누른 순간의 유효 선택(보이는 소유 노트북, 표시 순서)을 캡처한다.
  // 다이얼로그가 열려 있는 동안(확인·실행·결과)은 이 목록이 바뀌지 않는다.
  const [deleteTargets, setDeleteTargets] = useState<BulkTarget[] | null>(null);
  const [bulkShareTargets, setBulkShareTargets] = useState<BulkTarget[] | null>(null);
  const bulkDialogOpen = deleteTargets !== null || bulkShareTargets !== null;
  const filtersActive = favoritesOnly || activeTag !== null;

  function captureTargets(): BulkTarget[] | null {
    const targets = visibleNotebooks
      .filter((nb) => isBulkSelectable(nb) && selection.selectedIds.has(nb.id))
      .map((nb) => ({ id: nb.id, title: nb.title }));
    return targets.length > 0 ? targets : null;
  }

  function finishBulk(report: BulkRunReport) {
    selection.deselect(settledIds(report.results));
  }

  function clearFilters() {
    setFavoritesOnly(false);
    setActiveTag(null);
  }

  function renderList() {
    if (notebooks.isPending) return <LoadingState variant="cards" label="노트북 목록을 불러오는 중" />;
    if (notebooks.isError) {
      return (
        <ErrorState
          title="노트북 목록을 불러오지 못했습니다"
          onRetry={() => void notebooks.refetch()}
          retrying={notebooks.isFetching}
        />
      );
    }
    if (visibleNotebooks.length === 0) {
      if (filtersActive) return <FilteredEmptyState onClearFilters={clearFilters} />;
      if (filter === "shared") {
        return (
          <EmptyState
            icon={<Users />}
            title="공유받은 노트북이 없습니다"
            description="다른 사람이 노트북을 공유하면 여기에 보입니다."
          />
        );
      }
      return (
        <EmptyState
          icon={<NotebookPen />}
          title="아직 노트북이 없습니다"
          description="노트북을 만들고 소스를 넣으면 AI에게 질문할 수 있습니다."
          action={{ label: "새 노트북", icon: <Plus aria-hidden />, onClick: () => setCreateOpen(true) }}
        />
      );
    }
    return (
      <>
        <BulkActionBar
          selection={selection}
          showSelectAll={viewMode === "card"}
          actions={({ disabled }) => {
            const off = disabled || bulkDialogOpen;
            return (
              <>
                {/* 0개 선택이면 둘 다 outline·비활성(빨강 아님), 1개 이상이면 삭제만 위험 글자(AC-3.6) */}
                <Button
                  variant="outline"
                  size="sm"
                  className="coarse:min-h-11"
                  disabled={off}
                  onClick={() => setBulkShareTargets(captureTargets())}
                  data-testid="bulk-share"
                >
                  <Share2 aria-hidden />
                  공유
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  className={cn(
                    "coarse:min-h-11",
                    !off && "text-destructive-text hover:bg-destructive/10 hover:text-destructive-text",
                  )}
                  disabled={off}
                  onClick={() => setDeleteTargets(captureTargets())}
                  data-testid="bulk-delete"
                >
                  <Trash2 aria-hidden />
                  삭제
                </Button>
              </>
            );
          }}
        />
        {viewMode === "table" ? (
          <NotebookTable
            notebooks={visibleNotebooks}
            selection={selection}
            onShare={shareDialog.open}
            onRename={renameDialog.open}
            onManageTags={tagsDialog.open}
          />
        ) : (
          <ul role="list" aria-label="노트북 목록" className={NOTEBOOK_GRID_CLASS}>
            {visibleNotebooks.map((nb) => (
              <li key={nb.id} className="min-w-0">
                <NotebookCard
                  notebook={nb}
                  onShare={shareDialog.open}
                  onRename={renameDialog.open}
                  onManageTags={tagsDialog.open}
                  selection={selection}
                />
              </li>
            ))}
          </ul>
        )}
      </>
    );
  }

  // 스크롤 컨테이너는 페이지가 갖는다. 랜드마크 `main`은 셸의 하나뿐이므로(AC-2.5) 페이지 래퍼는 div다.
  return (
    <div className="h-full overflow-y-auto">
      <PageContainer width="wide">
        {/* 진입점 단일화(plan §6.2.4 D-02·D-03): 전역 검색은 사이드바 검색 트리거·⌘K, 퀵 캡처 설정은 사용자 메뉴. */}
        <PageHeader
          title="노트북"
          description="소스를 모으고, AI와 대화하고, 에이전트로 결과물을 만드세요"
          actions={
            <CreateNotebookDialog
              open={createOpen}
              onOpenChange={setCreateOpen}
              triggerClassName="max-sm:h-11 coarse:h-11"
            />
          }
        />

        <RecentChats />

        <Toolbar>
          <Tabs
            value={filter}
            onValueChange={(v) => setFilter(v as NotebookFilter)}
            className="min-w-0 max-w-full"
          >
            {/* 좁은 화면에서는 탭 줄이 가로로 스크롤한다(목업 M1). 터치 기기에서 탭 높이 44px 이상(§6.4.4) */}
            <TabsList
              aria-label="노트북 범위"
              // overflow-x만 auto로 두면 세로도 auto가 되어, 활성 탭 밑줄(after:bottom-[-5px])이 세로 스크롤바를 만든다
              className="max-w-full justify-start overflow-x-auto overflow-y-hidden coarse:h-13!"
            >
              {NOTEBOOK_FILTER_TABS.map((t) => (
                <TabsTrigger key={t.value} value={t.value} className="flex-none px-2">
                  {t.label}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
          <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
            <Button
              variant={favoritesOnly ? "secondary" : "outline"}
              className="coarse:min-h-11"
              aria-pressed={favoritesOnly}
              onClick={() => setFavoritesOnly((v) => !v)}
              data-testid="favorites-only-toggle"
            >
              <Star aria-hidden className={favoritesOnly ? "fill-warning text-warning" : undefined} />
              즐겨찾기만
            </Button>
            {allTags.length > 0 && (
              <Select value={activeTag} onValueChange={(v) => setActiveTag((v as string | null) ?? null)}>
                <SelectTrigger aria-label="태그 필터" data-testid="tag-filter" className="max-w-48 coarse:min-h-11">
                  <TagIcon aria-hidden />
                  <SelectValue placeholder="전체 태그" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={null}>전체 태그</SelectItem>
                  {allTags.map((tag) => (
                    <SelectItem key={tag} value={tag}>
                      {tag}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            <ViewModeToggle value={viewMode} onChange={setViewMode} />
          </div>
        </Toolbar>

        {/* 목록 경계(P2 HCI-P2-2): 「다시 시도」가 성공해 ErrorState가 사라지면 포커스가 이 경계 안의 첫 요소
            (모두 선택·첫 카드)로 온다 — 경계가 없으면 페이지 맨 위 「새 노트북」으로 튄다. 배치는 바꾸지 않는다(contents). */}
        <div className="contents" data-slot="notebook-list-region">
          {renderList()}
        </div>

        <BulkDeleteDialog
          targets={deleteTargets}
          onClose={() => setDeleteTargets(null)}
          onFinished={finishBulk}
        />
        <BulkShareDialog
          targets={bulkShareTargets}
          onClose={() => setBulkShareTargets(null)}
          onFinished={finishBulk}
        />
        <ShareNotebookDialog
          notebook={shareTarget}
          open={shareTarget !== null}
          onOpenChange={shareDialog.onOpenChange}
        />
        <RenameNotebookDialog
          notebook={renameDialog.value}
          open={renameDialog.value !== null}
          onOpenChange={renameDialog.onOpenChange}
        />
        <ManageTagsDialog
          notebook={tagsDialog.value}
          open={tagsDialog.value !== null}
          onOpenChange={tagsDialog.onOpenChange}
        />
      </PageContainer>
    </div>
  );
}
