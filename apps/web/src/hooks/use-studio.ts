"use client";

import { useMemo } from "react";
import { useAgents } from "@/hooks/use-agents";
import { useSkills } from "@/hooks/use-skills";
import { studioCardsByAgentId, type StudioCatalog } from "@/lib/studio-registry";

/**
 * 스튜디오 카드 바인딩에 쓰는 목록(#180) — 스킬 목록 + skill·workflow 에이전트 목록.
 * 에이전트 패널 탭과 같은 조회 키를 쓰므로 선택한 탭과 무관하게 캐시를 나눠 쓴다.
 */
export function useStudioCatalog() {
  const skills = useSkills();
  const skillAgents = useAgents("skill");
  const workflows = useAgents("workflow");
  const catalog = useMemo<StudioCatalog>(
    () => ({
      skills: skills.data,
      agents: [...(skillAgents.data ?? []), ...(workflows.data ?? [])],
    }),
    [skills.data, skillAgents.data, workflows.data],
  );
  const byAgentId = useMemo(() => studioCardsByAgentId(catalog), [catalog]);
  return {
    catalog,
    byAgentId,
    isPending: skills.isPending || skillAgents.isPending || workflows.isPending,
  };
}
