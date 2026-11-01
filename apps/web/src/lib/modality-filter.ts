import { MODALITIES } from "@/components/admin/llm/capability-picker";

type HasCapabilities = { capabilities: string[] };

/** 목록에 실제 등장하는 능력과 개수 — 알려진 MODALITIES 순서 우선, 그 외(코어가 새로 낸 능력)는 뒤에 이름순 */
export function countModalities(models: HasCapabilities[]): { modality: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const m of models) {
    for (const c of new Set(m.capabilities)) counts.set(c, (counts.get(c) ?? 0) + 1);
  }
  const knownOrder: readonly string[] = MODALITIES;
  const known = knownOrder.filter((m) => counts.has(m));
  const extra = [...counts.keys()].filter((c) => !knownOrder.includes(c)).sort();
  return [...known, ...extra].map((modality) => ({ modality, count: counts.get(modality)! }));
}

/** 선택한 능력 중 하나라도 가진 모델만 남긴다. 선택이 비어 있으면 전체. */
export function filterByModalities<T extends HasCapabilities>(models: T[], active: string[]): T[] {
  if (active.length === 0) return models;
  return models.filter((m) => m.capabilities.some((c) => active.includes(c)));
}
