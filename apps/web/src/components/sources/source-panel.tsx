"use client";

import { ChevronRight, FolderOpen, RotateCw, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { AddSourceDialog } from "@/components/sources/add-source-dialog";
import { SourceDetailInline } from "@/components/sources/source-detail-inline";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import { useDeleteSource, useRetrySource, useSources } from "@/hooks/use-sources";
import { STATUS_LABEL, sourceTypeLucide, type Source } from "@/lib/source-types";
import { useSourceFocus } from "@/stores/source-focus";
import { useSourceSelection } from "@/stores/source-selection";

/** 소스 처리 상태 배지 — 준비됨은 표시하지 않는다. 실패는 위험 톤(사유는 title), 처리 중은 정보 톤 + 진행 표시. */
export function SourceStatusBadge({ source }: { source: Source }) {
  if (source.status === "ready") return null;
  if (source.status === "failed") {
    return (
      <StatusBadge tone="danger" title={source.error_message ?? undefined}>
        실패
      </StatusBadge>
    );
  }
  return (
    <StatusBadge tone="info" pulse>
      {STATUS_LABEL[source.status] ?? source.status}
    </StatusBadge>
  );
}

export function SourcePanel({
  notebookId,
  canWrite,
  visible = true,
  onDetailOpenChange,
}: {
  notebookId: string;
  canWrite: boolean;
  /** [Phase 18c·codex D7] 패널이 실제로 화면에 보이는지 — false면 인용 포커스
   * 요청을 hidden 상태에서 소모하지 않고 visible해질 때까지 대기한다. */
  visible?: boolean;
  /** 소스 상세가 열리고(true) 닫힐(false) 때 — 노트북 화면이 우측 패널 폭을 읽기 폭으로 바꾼다. */
  onDetailOpenChange?: (open: boolean) => void;
}) {
  const sources = useSources(notebookId);
  const del = useDeleteSource(notebookId);
  const retry = useRetrySource(notebookId);
  const { selected, toggle, setAll } = useSourceSelection();
  // detailId: null=목록, 소스 id=그 소스의 상세 화면(목록 대신 패널 전체를 차지한다 — 메모 패널과 같은 방식)
  const [detailId, setDetailId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Source | null>(null);
  const { requestedSourceId, requestedQuote, clear: clearFocusRequest } = useSourceFocus();
  // 인용으로 연 상세에서 표시할 발췌 — 목록에서 직접 연 상세에는 없다
  const [highlight, setHighlight] = useState<{ sourceId: string; quote: string } | null>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  // 상세를 닫으면 포커스를 돌려줄 목록 항목(소스 id) — 상세를 연 항목 버튼으로 돌린다(모달이 하던 일)
  const restoreFocusRef = useRef<string | null>(null);

  const sourceList = sources.data ?? [];
  const selectedIds = selected[notebookId];
  // 폴링·무효화된 목록의 최신 소스를 쓴다(다른 곳에서 지워졌으면 목록으로 돌아간다).
  const detailSource = detailId ? (sourceList.find((s) => s.id === detailId) ?? null) : null;

  // [Phase 18c] 인용 클릭 → 해당 소스의 상세 화면을 바로 연다.
  // 패널이 아직 hidden이면(codex D7) 상세 제목으로 옮길 포커스가 no-op이 되므로 visible해질 때까지
  // 요청을 소모하지 않고 대기한다 — 요청 자체는 남겨두고 재렌더를 기다린다.
  useEffect(() => {
    if (!requestedSourceId || !visible) return;
    const target = sources.data?.find((s) => s.id === requestedSourceId);
    if (!target) return; // 아직 목록 로드 전이거나 삭제된 소스 — 다음 리스트 갱신 시 재시도
    setDetailId(requestedSourceId);
    setHighlight(requestedQuote ? { sourceId: requestedSourceId, quote: requestedQuote } : null);
    clearFocusRequest();
  }, [requestedSourceId, requestedQuote, visible, sources.data, clearFocusRequest]);

  // 상세가 열리고 닫히는 것을 알린다 — 노트북 화면이 우측 패널을 읽기 폭으로 넓히고 되돌린다(②)
  const detailOpen = detailSource !== null;
  useEffect(() => {
    onDetailOpenChange?.(detailOpen);
  }, [detailOpen, onDetailOpenChange]);

  // 상세를 연 소스가 목록에서 사라지면(다른 곳에서 삭제) 목록으로 돌아간다. 포커스가 상세 화면과 함께
  // 사라졌을 때만 패널 머리글로 보낸다 — 채팅 입력 등 다른 곳에 있던 포커스는 빼앗지 않는다.
  useEffect(() => {
    if (!detailId || !sources.data || sources.data.some((s) => s.id === detailId)) return;
    if (!document.activeElement || document.activeElement === document.body) {
      restoreFocusRef.current = detailId;
    }
    setDetailId(null);
  }, [detailId, sources.data]);

  // 목록으로 돌아오면 상세를 연 항목 버튼으로, 그 항목이 없으면(삭제됨) 패널 머리글로 포커스를 돌린다.
  useEffect(() => {
    const id = restoreFocusRef.current;
    if (detailId || !id) return;
    restoreFocusRef.current = null;
    const openers = listRef.current?.querySelectorAll<HTMLElement>("[data-source-opener]") ?? [];
    const target = Array.from(openers).find((el) => el.dataset.sourceOpener === id);
    (target ?? headingRef.current)?.focus();
  }, [detailId]);

  // 첫 로드 시 ready 소스 전체 선택
  useEffect(() => {
    if (sources.data && selectedIds === undefined) {
      setAll(
        notebookId,
        sources.data.filter((s) => s.status === "ready").map((s) => s.id),
      );
    }
  }, [sources.data, selectedIds, notebookId, setAll]);

  // 새로 ready가 된 소스는 자동 선택
  useEffect(() => {
    if (!sources.data || selectedIds === undefined) return;
    const readyIds = sources.data.filter((s) => s.status === "ready").map((s) => s.id);
    const missing = readyIds.filter((id) => !selectedIds.includes(id));
    const stale = selectedIds.filter((id) => !readyIds.includes(id));
    if (missing.length > 0 || stale.length > 0) {
      setAll(notebookId, [...selectedIds.filter((id) => readyIds.includes(id)), ...missing]);
    }
    // 정당: selectedIds/setAll을 deps에 넣으면 setAll이 다시 트리거되어 루프.
    // 소스 목록이 바뀔 때만 선택 상태를 동기화한다.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sources.data]);

  function handleRetry(s: Source) {
    retry.mutate(s.id, {
      onSuccess: () => toast.success("다시 가져오는 중입니다"),
      onError: (err) => toast.error(`재시도 실패: ${err.message}`),
    });
  }

  function confirmDelete() {
    if (!deleteTarget) return;
    del.mutate(deleteTarget.id, {
      onSuccess: () => toast.success("소스가 삭제되었습니다"),
      onError: (err) => toast.error(`삭제 실패: ${err.message}`),
    });
    setDeleteTarget(null);
  }

  return (
    <aside className="flex h-full w-full shrink-0 flex-col border-r bg-sidebar/50">
      {detailSource ? (
        <SourceDetailInline
          key={detailSource.id}
          source={detailSource}
          highlightQuote={highlight?.sourceId === detailSource.id ? highlight.quote : null}
          onBack={() => {
            restoreFocusRef.current = detailSource.id;
            setDetailId(null);
          }}
        />
      ) : (
        <>
          <div className="flex h-11 shrink-0 items-center justify-between border-b px-3">
            <h2 ref={headingRef} tabIndex={-1} className="text-sm font-semibold outline-none">
              소스
              {sourceList.length > 0 && (
                <span className="ml-1.5 text-xs font-normal text-muted-foreground">
                  {sourceList.length}
                </span>
              )}
            </h2>
            {canWrite && (
              <AddSourceDialog notebookId={notebookId} sourceCount={sourceList.length} />
            )}
          </div>

          {/* min-h-0: flex 아이템 기본 min-height:auto가 목록 길이만큼 패널을 늘리는 것 방지.
              네이티브 overflow 스크롤 — Base UI ScrollArea는 이 중첩 flex에서 viewport
              높이가 잡히지 않아 스크롤되지 않았다 (ChatPanel과 동일 패턴으로 통일). */}
          <div className="min-h-0 flex-1 overflow-y-auto">
            {sources.isPending ? (
              <div className="flex flex-col gap-2 p-3">
                {Array.from({ length: 3 }).map((_, i) => (
                  <Skeleton key={i} className="h-12" />
                ))}
              </div>
            ) : sourceList.length === 0 ? (
              <div className="flex flex-col items-center gap-3 px-4 py-16 text-center text-sm text-muted-foreground">
                <FolderOpen className="size-7 opacity-60" />
                <p>파일·웹·텍스트 소스를 추가하면 여기서 질의에 사용할 수 있습니다.</p>
              </div>
            ) : (
              <ul ref={listRef} className="flex flex-col gap-0.5 p-2" data-testid="source-list">
                {sourceList.map((s) => {
                  const TypeIcon = sourceTypeLucide(s.type);
                  return (
                    <li key={s.id} data-source-id={s.id} className="flex flex-col">
                      <div className="group flex items-center gap-2 rounded-lg px-2 py-2 transition-colors hover:bg-accent/60">
                        <Checkbox
                          checked={(selectedIds ?? []).includes(s.id)}
                          onCheckedChange={() => toggle(notebookId, s.id)}
                          disabled={s.status !== "ready"}
                          aria-label={`${s.title} 선택`}
                        />
                        <button
                          type="button"
                          className="flex min-w-0 flex-1 items-center gap-2 text-left"
                          onClick={() => {
                            setHighlight(null);
                            setDetailId(s.id);
                          }}
                          data-source-opener={s.id}
                        >
                          <TypeIcon className="size-4 shrink-0 text-muted-foreground" />
                          <span className="truncate text-sm" title={s.title}>
                            {s.title}
                          </span>
                          <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
                        </button>
                        <SourceStatusBadge source={s} />
                        {canWrite && s.status === "failed" && (
                          <Button
                            variant="ghost"
                            size="icon"
                            className="size-7 text-muted-foreground opacity-0 group-hover:opacity-100 hover:text-foreground"
                            onClick={() => handleRetry(s)}
                            disabled={retry.isPending}
                            aria-label="다시 가져오기"
                            title="다시 가져오기"
                          >
                            <RotateCw className="size-3.5" />
                          </Button>
                        )}
                        {canWrite && (
                          <Button
                            variant="ghost"
                            size="icon"
                            className="size-7 text-muted-foreground opacity-0 group-hover:opacity-100 hover:text-destructive"
                            onClick={() => setDeleteTarget(s)}
                            aria-label="소스 삭제"
                          >
                            <Trash2 className="size-3.5" />
                          </Button>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </>
      )}

      <AlertDialog
        open={deleteTarget !== null}
        onOpenChange={(o) => !o && setDeleteTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>소스 삭제</AlertDialogTitle>
            <AlertDialogDescription>
              “{deleteTarget?.title}” 소스를 삭제할까요? 인덱싱된 내용도 함께 제거됩니다.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>취소</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive-hover"
              onClick={confirmDelete}
            >
              삭제
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </aside>
  );
}
