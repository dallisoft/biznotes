"use client";

import Link from "next/link";
import { NotebookFavoriteButton, NotebookOwnerMenu } from "@/components/dashboard/notebook-actions";
import { NotebookSelectCheckbox } from "@/components/dashboard/selection-checkboxes";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { StatusBadge } from "@/components/ui/status-badge";
import type { NotebookSelection } from "@/hooks/use-notebook-selection";
import { formatDateTime, formatNumber, formatRelative } from "@/lib/format";
import { notebookAccessLabel } from "@/lib/labels";
import type { Notebook } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 카드 동작(체크박스·즐겨찾기·`⋯`)의 표시 규칙(HCI-4, D-14, AC-3.6). 마우스 기기(`pointer: fine`)에서는
 * 카드에 hover·포커스(카드 안 어느 요소든)·선택 중일 때만 보인다. 터치 기기(`pointer: coarse`)와 포인터를
 * 판정할 수 없는 기기에서는 숨기지 않는다(항상 보임 — S-8의 터치 취지).
 * 숨김은 `opacity`로만 한다 — 키보드 Tab·스크린리더에서 빠지지 않고, 포커스를 받으면 바로 보인다.
 */
export const CARD_CONTROL_REVEAL = cn(
  "transition-opacity motion-reduce:transition-none",
  "pointer-fine:opacity-0",
  "pointer-fine:group-hover/notebook:opacity-100",
  "pointer-fine:group-focus-within/notebook:opacity-100",
  "pointer-fine:group-data-selected/notebook:opacity-100",
);

/** 카드 메타 한 줄 — 「소스 1개 · 3일 전 수정」(plan §6.6.2, 7일 이상은 날짜 — `formatRelative`). */
export function notebookCardMeta(notebook: Pick<Notebook, "source_count" | "updated_at">, now?: Date) {
  return {
    sources: `소스 ${formatNumber(notebook.source_count)}개`,
    updatedAt: formatRelative(notebook.updated_at, now),
  };
}

export function NotebookCard({
  notebook,
  onShare,
  onRename,
  onManageTags,
  selection,
}: {
  notebook: Notebook;
  onShare: (id: string) => void;
  onRename: (notebook: Notebook) => void;
  onManageTags?: (notebook: Notebook) => void;
  /** 대시보드 선택 모델 — 주면 카드 오른쪽 위에 선택 체크박스를 그린다(S-8). */
  selection?: NotebookSelection;
}) {
  const selected = selection?.isSelected(notebook.id) ?? false;
  const meta = notebookCardMeta(notebook);
  return (
    <Card
      data-testid="notebook-card"
      // Tailwind v4의 `data-selected:` 변형은 `[data-selected=true]`로 컴파일된다(빈 값이면 선택 테두리·HCI-4 표시가 적용되지 않음)
      data-selected={selected ? "true" : undefined}
      // 그림자 없이 ring-1, hover는 shadow-sm + primary/30 테두리(튀어 오르는 이동 없음 — plan §6.4.4)
      className="group/notebook relative h-full gap-3 px-4 motion-safe:transition-shadow hover:shadow-sm hover:ring-primary/30 data-selected:ring-2 data-selected:ring-primary/60"
    >
      <Link
        href={`/notebooks/${notebook.id}`}
        className="absolute inset-0 z-0 rounded-xl outline-none focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:ring-inset"
      >
        <span className="sr-only">{notebook.title} 열기</span>
      </Link>
      <div className="flex h-10 items-center gap-1">
        <span
          aria-hidden
          className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-accent text-xl"
        >
          {notebook.emoji}
        </span>
        <span className="flex-1" />
        {selection && (
          <NotebookSelectCheckbox
            notebook={notebook}
            selection={selection}
            className={cn("mx-1.5 coarse:mx-3.5", CARD_CONTROL_REVEAL)}
          />
        )}
        <NotebookFavoriteButton
          notebook={notebook}
          // 즐겨찾기한 노트북의 채운 별은 상태 표시이므로 항상 보인다
          className={cn("relative z-10", !notebook.is_favorite && CARD_CONTROL_REVEAL)}
        />
        {notebook.is_owner && (
          <NotebookOwnerMenu
            notebook={notebook}
            onShare={onShare}
            onRename={onRename}
            onManageTags={onManageTags}
            className={cn(
              "relative z-10",
              CARD_CONTROL_REVEAL,
              // 메뉴가 열린 동안에는 포인터가 카드를 벗어나도 트리거를 보인다
              "pointer-fine:has-data-popup-open:opacity-100",
            )}
          />
        )}
      </div>
      <h2 className="line-clamp-2 min-h-12 text-base leading-6 font-semibold break-keep wrap-anywhere">
        {notebook.title}
      </h2>
      {(notebook.tags ?? []).length > 0 && (
        <div className="flex flex-wrap gap-1" data-testid="notebook-tags">
          {(notebook.tags ?? []).map((tag) => (
            <Badge key={tag} variant="outline" className="text-xs">
              {tag}
            </Badge>
          ))}
        </div>
      )}
      {/* 회색 푸터 띠 없이 본문 안 메타 줄(plan §6.4.4 「카드 푸터」) */}
      <div className="mt-auto flex flex-wrap items-center justify-between gap-x-2 gap-y-1">
        <p className="flex flex-wrap items-center gap-x-1.5 text-xs text-muted-foreground" data-testid="notebook-meta">
          {/* 공백 텍스트는 flex 배치에 영향이 없고 읽기·복사 텍스트에서 단어를 띄운다 */}
          <span className="whitespace-nowrap">{meta.sources}</span>{" "}
          <span aria-hidden>·</span>{" "}
          <span className="whitespace-nowrap">
            <time dateTime={notebook.updated_at} title={formatDateTime(notebook.updated_at)}>
              {meta.updatedAt}
            </time>{" "}
            수정
          </span>
        </p>
        {!notebook.is_owner && <StatusBadge tone="neutral">{notebookAccessLabel(notebook)}</StatusBadge>}
      </div>
    </Card>
  );
}
