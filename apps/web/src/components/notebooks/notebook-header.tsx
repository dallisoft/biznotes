"use client";

import { PencilLine, Share2, Tag as TagIcon, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ManageTagsDialog } from "@/components/dashboard/manage-tags-dialog";
import { RenameNotebookDialog } from "@/components/dashboard/rename-notebook-dialog";
import { ShareNotebookDialog } from "@/components/dashboard/share-notebook-dialog";
import { Button } from "@/components/ui/button";
import { RowActions, type RowActionItem } from "@/components/ui/row-actions";
import { StatusBadge } from "@/components/ui/status-badge";
import { useDialogSlot } from "@/hooks/use-dialog-slot";
import { useDeleteNotebook } from "@/hooks/use-notebooks";
import { NOTEBOOK_PERMISSION_LABEL } from "@/lib/labels";
import { notifyError, notifySuccess, successMessage } from "@/lib/notify";
import type { Notebook } from "@/lib/types";

/**
 * 비소유자에게 보이는 권한 배지(plan §6.6.2 노트북 작업 화면 행, AC-3.2). 소유자면 배지가 없다.
 * 서버는 관리자를 owner 권한으로 승격하지만(`is_owner=false`·`permission="owner"`) 웹의 소유자 전용 UI는
 * `is_owner`로만 판정한다(§8.3) — 그 경우 쓰기는 되므로 「편집 가능」으로 보인다.
 */
export function notebookAccessLabel(notebook: Pick<Notebook, "is_owner" | "permission">): string | null {
  if (notebook.is_owner) return null;
  return notebook.permission === "read" ? NOTEBOOK_PERMISSION_LABEL.read : NOTEBOOK_PERMISSION_LABEL.write;
}

type HeaderDialog = "share" | "rename" | "tags";

/**
 * 노트북 작업 화면 헤더(plan §6.6.2, AC-3.1·3.2·3.10): 브레드크럼 「노트북 /」 + 이모지 + 제목 `h1` + 권한 배지(비소유자)
 * · 오른쪽 「공유」(소유자) + 더보기(이름 변경·태그 관리 / 노트북 삭제). 공유·이름 변경·태그·삭제는
 * `is_owner`일 때만 렌더한다(§8.3 — 서버 판정이 진실 공급원이고 이 노출 조건은 편의). 비소유자에게는 더보기가 없다.
 *
 * 브레드크럼은 heading이 아니라 `nav[aria-label=위치]` 안의 링크다 — e2e가 홈의 `heading "노트북" exact`를 단언하므로
 * 같은 이름의 heading을 만들지 않는다(§9 공통 규칙). 삭제가 성공하면 `onDeleted`를 먼저 부른 뒤(작업 화면이 지워진
 * 노트북을 다시 읽지 않게) 노트북 홈으로 replace한다. 실패하면 화면·URL과 확인 다이얼로그를 그대로 두고(그 자리에서
 * 다시 시도) 실패 토스트를 보인다(§6.6.2 — P3 리뷰 P3-01). 요청 중에는 확인 버튼이 「삭제하는 중…」으로 비활성이다.
 *
 * 다이얼로그는 한 번에 하나다. 닫기 콜백은 그 창이 아직 현재 창일 때만 닫는다(`useDialogSlot` — P3 리뷰 P3-02): 사용자가
 * 닫은 공유·이름 변경 창의 요청이 늦게 끝나도 그사이 연 다른 창(또는 다시 연 같은 종류의 창)을 닫지 않는다.
 */
export function NotebookHeader({
  notebook,
  onDeleted,
}: {
  notebook: Notebook;
  onDeleted?: () => void;
}) {
  const router = useRouter();
  const del = useDeleteNotebook();
  const dialog = useDialogSlot<HeaderDialog>();
  const isOwner = notebook.is_owner === true;
  const accessLabel = notebookAccessLabel(notebook);

  /** 확인 다이얼로그(`awaitConfirm`)가 기다리는 삭제 — 실패하면 토스트 뒤 거부해 다이얼로그를 남긴다. */
  async function deleteNotebook() {
    try {
      await del.mutateAsync(notebook.id);
    } catch (err) {
      notifyError("노트북을 삭제", err);
      throw err;
    }
    onDeleted?.();
    notifySuccess(successMessage("노트북", "삭제"));
    router.replace("/");
  }

  const items: RowActionItem[] = isOwner
    ? [
        { label: "이름 변경", icon: <PencilLine aria-hidden />, onSelect: () => dialog.open("rename") },
        { label: "태그 관리", icon: <TagIcon aria-hidden />, onSelect: () => dialog.open("tags") },
        {
          label: "노트북 삭제",
          icon: <Trash2 aria-hidden />,
          destructive: true,
          disabled: del.isPending,
          disabledReason: del.isPending ? "삭제하는 중입니다" : undefined,
          confirm: {
            title: `‘${notebook.title}’ 노트북을 삭제할까요?`,
            description: "소스·메모·대화가 모두 삭제되며 되돌릴 수 없습니다.",
            actionLabel: "삭제",
            awaitConfirm: true,
            pendingLabel: "삭제하는 중…",
          },
          onConfirm: deleteNotebook,
        },
      ]
    : [];

  return (
    <div
      data-testid="notebook-header"
      className="flex h-12 shrink-0 items-center gap-2 border-b px-3 md:h-14 md:gap-3 md:px-4"
    >
      <div className="flex min-w-0 flex-1 items-center gap-2">
        <nav aria-label="위치" className="shrink-0">
          <ol className="flex items-center gap-1 text-sm text-muted-foreground">
            <li>
              <Link
                href="/"
                className="rounded-sm underline-offset-4 hover:text-foreground hover:underline focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none coarse:inline-flex coarse:min-h-11 coarse:items-center"
              >
                노트북
              </Link>
            </li>
            <li aria-hidden className="select-none">
              /
            </li>
          </ol>
        </nav>
        <span aria-hidden className="shrink-0 text-lg leading-none">
          {notebook.emoji}
        </span>
        <h1 className="min-w-0 truncate text-lg font-semibold" title={notebook.title}>
          {notebook.title}
        </h1>
        {accessLabel && (
          <StatusBadge tone="neutral" data-testid="notebook-access-badge">
            {accessLabel}
          </StatusBadge>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {isOwner && (
          <Button
            variant="outline"
            size="sm"
            className="coarse:min-h-11 coarse:min-w-11"
            onClick={() => dialog.open("share")}
            data-testid="notebook-share"
          >
            <Share2 aria-hidden />
            <span className="max-sm:sr-only">공유</span>
          </Button>
        )}
        {items.length > 0 && <RowActions rowLabel="노트북" items={items} />}
      </div>

      {isOwner && (
        <>
          <ShareNotebookDialog
            notebook={notebook}
            open={dialog.value === "share"}
            onOpenChange={dialog.onOpenChangeFor("share")}
          />
          <RenameNotebookDialog
            notebook={notebook}
            open={dialog.value === "rename"}
            onOpenChange={dialog.onOpenChangeFor("rename")}
          />
          <ManageTagsDialog
            notebook={notebook}
            open={dialog.value === "tags"}
            onOpenChange={dialog.onOpenChangeFor("tags")}
          />
        </>
      )}
    </div>
  );
}
