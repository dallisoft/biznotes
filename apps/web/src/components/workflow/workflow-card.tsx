"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Card } from "@/components/ui/card";
import { RowActions, type RowActionItem } from "@/components/ui/row-actions";
import { StatusBadge } from "@/components/ui/status-badge";
import { useDeleteWorkflow, useDuplicateWorkflow } from "@/hooks/use-workflows";
import { formatNumber } from "@/lib/format";
import { agentScopeLabel, workflowStatusLabel } from "@/lib/labels";
import { notifyError, notifySuccess, successMessage } from "@/lib/notify";
import type { Workflow } from "@/lib/workflow-types";

/** 카드 메타 한 줄 — 「노드 3개 · v2」, 시스템 범위면 「· 시스템」. */
export function workflowMeta(wf: Pick<Workflow, "definition" | "version" | "scope">): string {
  const parts = [`노드 ${formatNumber(wf.definition?.nodes?.length ?? 0)}개`, `v${wf.version}`];
  if (wf.scope === "system") parts.push(agentScopeLabel(wf.scope));
  return parts.join(" · ");
}

/** 이름·설명 검색(클라이언트 필터 — 목록 전체를 이미 받는다, plan §6.6.2). 대소문자·앞뒤 공백 무시. */
export function filterWorkflows(list: Workflow[], query: string): Workflow[] {
  const q = query.trim().toLowerCase();
  if (!q) return list;
  return list.filter(
    (wf) =>
      wf.name.toLowerCase().includes(q) ||
      (wf.description ?? "").toLowerCase().includes(q),
  );
}

/**
 * 워크플로우 목록 카드(plan §6.6.2 워크플로우 목록, HCI-3 ③): 제목 2줄 · 상태(게시됨/초안) · 설명 · 메타 한 줄 ·
 * 더보기. 더보기는 `can_edit`면 「복제 · 편집 · 삭제」, 아니면 「복제」만이다(AC-4.3 — 노출 경계일 뿐, 판정은 서버).
 * 「삭제」는 확인 다이얼로그를 거쳐서만 `DELETE /workflows/{id}`를 1회 보낸다. 편집할 수 없는 카드의 제목은
 * 제자리 링크 대신 「복제해서 편집」 안내를 연다(`onLocked`).
 */
export function WorkflowCard({ wf, onLocked }: { wf: Workflow; onLocked: (wf: Workflow) => void }) {
  const router = useRouter();
  const duplicate = useDuplicateWorkflow();
  const remove = useDeleteWorkflow();

  const items: RowActionItem[] = [
    {
      label: "복제",
      disabled: duplicate.isPending,
      onSelect: () =>
        duplicate.mutate(wf.id, {
          onSuccess: (clone) => {
            notifySuccess(successMessage("워크플로우", "복제"));
            router.push(`/workflows/${clone.id}/edit`);
          },
          onError: (err) => notifyError("워크플로우를 복제", err),
        }),
    },
  ];
  if (wf.can_edit) {
    items.push(
      { label: "편집", onSelect: () => router.push(`/workflows/${wf.id}/edit`) },
      {
        label: "삭제",
        destructive: true,
        disabled: remove.isPending,
        confirm: {
          title: `‘${wf.name}’ 워크플로우를 삭제할까요?`,
          description: "삭제한 워크플로우는 되돌릴 수 없습니다.",
          actionLabel: "삭제",
        },
        onConfirm: () =>
          remove.mutate(wf.id, {
            onSuccess: () => notifySuccess(successMessage("워크플로우", "삭제")),
            onError: (err) => notifyError("워크플로우를 삭제", err),
          }),
      },
    );
  }

  const linkClass =
    "rounded-sm text-left outline-none after:absolute after:inset-0 after:content-[''] focus-visible:ring-3 focus-visible:ring-ring/50";

  return (
    <Card
      data-testid="workflow-card"
      className="relative h-full gap-3 px-4 transition-colors hover:ring-primary/40"
    >
      <div className="flex items-start gap-2">
        {wf.icon && (
          <span aria-hidden className="text-xl leading-6">
            {wf.icon}
          </span>
        )}
        <h3 className="line-clamp-2 min-w-0 flex-1 leading-6 font-semibold">
          {/* 카드 전체가 제목 동작이다(::after가 카드를 덮는다). 더보기 메뉴는 그 위(z-10)에 둔다. */}
          {wf.can_edit ? (
            <Link href={`/workflows/${wf.id}/edit`} className={linkClass}>
              {wf.name}
            </Link>
          ) : (
            <button type="button" className={linkClass} onClick={() => onLocked(wf)}>
              {wf.name}
            </button>
          )}
        </h3>
        <RowActions className="relative z-10 -my-1 -mr-2" rowLabel={wf.name} items={items} />
      </div>
      <p className="line-clamp-2 min-h-10 text-sm text-muted-foreground">
        {wf.description || "설명 없음"}
      </p>
      <div className="mt-auto flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
        <StatusBadge tone={wf.enabled ? "success" : "neutral"}>{workflowStatusLabel(wf.enabled)}</StatusBadge>
        <span className="truncate">{workflowMeta(wf)}</span>
      </div>
    </Card>
  );
}
