"use client";

import { PencilLine, Share2, Star, Tag as TagIcon, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { RowActions, type RowActionItem } from "@/components/ui/row-actions";
import { useDeleteNotebook, useToggleFavorite } from "@/hooks/use-notebooks";
import { notifyError, notifySuccess, successMessage } from "@/lib/notify";
import type { Notebook } from "@/lib/types";
import { cn } from "@/lib/utils";

// 카드와 테이블 행이 같은 동작을 쓰도록 공유한다(plan S-9 「카드 메뉴와 같은 동작」).

export function NotebookFavoriteButton({
  notebook,
  className,
}: {
  notebook: Notebook;
  className?: string;
}) {
  const toggleFavorite = useToggleFavorite();

  function handleToggleFavorite(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    toggleFavorite.mutate(
      { id: notebook.id, favorite: !notebook.is_favorite },
      { onError: (err) => notifyError("즐겨찾기를 변경", err) },
    );
  }

  return (
    <Button
      variant="ghost"
      size="icon-sm"
      className={cn("coarse:size-11", className)}
      aria-label={notebook.is_favorite ? "즐겨찾기 해제" : "즐겨찾기"}
      aria-pressed={notebook.is_favorite}
      data-testid="notebook-favorite-toggle"
      onClick={handleToggleFavorite}
    >
      <Star
        aria-hidden
        className={`size-4 ${notebook.is_favorite ? "fill-warning text-warning" : ""}`}
      />
    </Button>
  );
}

/** 단건 삭제 확인 문구(질문형 제목 — plan §6.5 ConfirmDialog). */
export function deleteNotebookConfirm(title: string) {
  return {
    title: `‘${title}’ 노트북을 삭제할까요?`,
    description: "소스·메모·대화가 모두 삭제되며 되돌릴 수 없습니다.",
    actionLabel: "삭제",
  };
}

/**
 * 소유자 동작 더보기(`⋯` — 공용 RowActions): 이름 변경·공유·태그 관리 / 삭제(확인 다이얼로그).
 * 호출자가 `is_owner`일 때만 렌더한다(plan §8.3 — 현행 카드 메뉴 규칙).
 *
 * 삭제는 노트북 화면 헤더와 같은 훅(`useDeleteNotebook`)을 쓰고 홈에서는 이동하지 않는다(§6.6.2 PR-09).
 * 결과 토스트는 `mutateAsync`의 약속으로 띄운다 — 성공하면 이 카드·행이 목록에서 사라지는데, `mutate(…, { onSuccess })`
 * 콜백은 관찰자(이 컴포넌트)가 언마운트되면 호출되지 않는다. 훅의 캐시 정책(트랙 notebook — PR-09)이 바뀌어
 * 카드가 먼저 사라지더라도 토스트가 빠지지 않게 컴포넌트 수명과 떼어 둔다.
 * 확인 뒤 이 행이 사라지면 포커스는 RowActions 계약대로 다음 행(카드)의 `⋯`로 간다.
 */
export function NotebookOwnerMenu({
  notebook,
  onShare,
  onRename,
  onManageTags,
  className,
}: {
  notebook: Notebook;
  onShare: (id: string) => void;
  onRename: (notebook: Notebook) => void;
  onManageTags?: (notebook: Notebook) => void;
  /** RowActions 바깥 요소에 붙는다(카드의 표시 규칙 — HCI-4). */
  className?: string;
}) {
  const del = useDeleteNotebook();

  function handleDelete() {
    del.mutateAsync(notebook.id).then(
      () => notifySuccess(successMessage("노트북", "삭제")),
      (err: unknown) => notifyError("노트북을 삭제", err),
    );
  }

  const items: RowActionItem[] = [
    // 다이얼로그를 여는 항목 — 열린 다이얼로그가 초기 포커스를 가져가고 닫히면 ⋯로 돌아온다(RowActions 포커스 규칙)
    { label: "이름 변경", icon: <PencilLine aria-hidden />, onSelect: () => onRename(notebook) },
    { label: "공유", icon: <Share2 aria-hidden />, onSelect: () => onShare(notebook.id) },
  ];
  if (onManageTags) {
    items.push({ label: "태그 관리", icon: <TagIcon aria-hidden />, onSelect: () => onManageTags(notebook) });
  }
  items.push({
    label: "삭제",
    icon: <Trash2 aria-hidden />,
    destructive: true,
    confirm: deleteNotebookConfirm(notebook.title),
    onConfirm: handleDelete,
  });

  return <RowActions rowLabel={notebook.title} items={items} className={className} />;
}
