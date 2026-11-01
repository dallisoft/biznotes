"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Card } from "@/components/ui/card";
import { RowActions, type RowActionItem } from "@/components/ui/row-actions";
import { StatusBadge, type StatusTone } from "@/components/ui/status-badge";
import { useCloneSkill } from "@/hooks/use-skills";
import { formatNumber } from "@/lib/format";
import { SKILL_STATUS_TONE, skillStatusLabel, skillVisibilityLabel } from "@/lib/labels";
import { notifyError, notifySuccess } from "@/lib/notify";
import type { CatalogStatus } from "@/lib/skill-catalog";
import type { Skill } from "@/lib/skill-types";

/**
 * 카드 상태 배지 — 실행 가능 여부는 공개 포인터에서 나온다(biznotes-core D8: 비소유자는 노출 version, 소유자는 최신
 * 동결). 실행할 수 없으면 「초안」이다(원시값 `draft`를 보이지 않는다 — AC-4.4).
 */
export function skillCardStatus(skill: Pick<Skill, "can_run">): { label: string; tone: StatusTone } {
  return skill.can_run
    ? { label: "실행 가능", tone: "success" }
    : { label: skillStatusLabel("draft"), tone: SKILL_STATUS_TONE.draft };
}

/**
 * 스킬 목록 카드(plan §6.6.2 스킬 목록): 제목(카드 전체가 상세 링크) · 공개 범위 배지(「기본 제공」 등) · 설명 2줄 ·
 * 상태 배지 · 메타(「복제 0 · 실행 0」) · 더보기(복제 · 편집 — 편집은 `can_edit`일 때만).
 */
export function SkillCard({ skill, catalog }: { skill: Skill; catalog: CatalogStatus }) {
  const router = useRouter();
  const clone = useCloneSkill();
  const status = skillCardStatus(skill);

  const items: RowActionItem[] = [
    {
      label: "복제",
      disabled: clone.isPending,
      onSelect: () =>
        clone.mutate(skill.id, {
          onSuccess: (r) => {
            notifySuccess("스킬을 복제했습니다");
            router.push(`/skills/${r.skill_id}/edit`);
          },
          onError: (err) => notifyError("스킬을 복제", err),
        }),
    },
  ];
  if (skill.can_edit) {
    items.push({ label: "편집", onSelect: () => router.push(`/skills/${skill.id}/edit`) });
  }

  return (
    <Card
      data-testid="skill-card"
      className="relative h-full gap-3 px-4 transition-colors hover:ring-primary/40"
    >
      <div className="flex items-start gap-2">
        <h3 className="line-clamp-2 min-w-0 flex-1 font-semibold">
          {/* 카드 전체가 상세로 가는 링크다(::after가 카드를 덮는다). 더보기 메뉴는 그 위(z-10)에 둔다. */}
          <Link
            href={`/skills/${skill.id}`}
            className="rounded-sm outline-none after:absolute after:inset-0 after:content-[''] focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            {skill.name}
          </Link>
        </h3>
        <StatusBadge>{skillVisibilityLabel(skill.visibility)}</StatusBadge>
        <RowActions className="relative z-10 -my-1 -mr-2" rowLabel={skill.name} items={items} />
      </div>
      <p className="line-clamp-2 min-h-10 text-sm text-muted-foreground">
        {skill.description || "설명 없음"}
      </p>
      <div className="mt-auto flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
        <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
        {skill.can_edit && skill.published_version_id && (
          <StatusBadge tone={SKILL_STATUS_TONE.published}>{skillStatusLabel("published")}</StatusBadge>
        )}
        {catalog === "listed" && <StatusBadge tone="info">채팅 카탈로그</StatusBadge>}
        {catalog === "shadowed" && (
          <StatusBadge title="같은 이름의 다른 스킬이 채팅 카탈로그에서 우선합니다">가려짐</StatusBadge>
        )}
        <span>
          복제 {formatNumber(skill.clone_count)} · 실행 {formatNumber(skill.run_count)}
          {skill.forked_from_version_id && " · 복제본"}
        </span>
      </div>
    </Card>
  );
}
