import type { SkillCatalogEntry } from "@/lib/skill-types";

/** 채팅 카탈로그 상태 — 등재됨, 같은 name의 다른 스킬에 가려짐, 또는 없음(biznotes-core D2·D6-1). */
export type CatalogStatus = "listed" | "shadowed" | null;

export function catalogStatus(
  skill: { id: string },
  entries: SkillCatalogEntry[] | undefined,
): CatalogStatus {
  const entry = entries?.find((e) => e.skill_id === skill.id);
  if (!entry) return null;
  return entry.shadowed_by ? "shadowed" : "listed";
}

const SCRIPT_EXTENSIONS = [".py", ".sh", ".js", ".mjs"];

/** 실행할 수 있는 번들 스크립트가 있는가 — `scripts/` 아래 허용 확장자(biznotes-core D7). */
export function hasRunnableScripts(paths: string[]): boolean {
  return paths.some(
    (p) =>
      p.startsWith("scripts/") && SCRIPT_EXTENSIONS.some((ext) => p.toLowerCase().endsWith(ext)),
  );
}
