"use client";

import { Checkbox } from "@/components/ui/checkbox";
import type { Modality } from "@/lib/admin-types";

export const PROVIDER_TYPES = [
  "openai",
  "anthropic",
  "google",
  "ollama",
  "vllm",
  "lmstudio",
  "azure",
  "bedrock",
  "custom",
];

/**
 * 모델 능력 — 순서까지 코어 `biznotes_core.models.llm.MODALITIES`와 같다.
 *
 * 코어 v0.18.0(biznotes-core#218)부터 생성 계약의 `capabilities`가 능력 enum이라 `Modality`가 그
 * 유니온이다. 그래서 컴파일 타임에 양방향으로 검사한다 — `satisfies`는 이 배열에 생성 타입 밖의 값이
 * 없음을, 아래 `ModalitiesComplete`는 생성 타입의 값이 이 배열에서 빠지지 않았음을 본다(어느 쪽이든
 * `pnpm --filter web typecheck` 실패). 타입 검사는 순서를 보지 못하므로 코어 정본과의 순서 일치는
 * `make ci`의 pytest `apps/api/tests/test_web_modalities_contract.py`가 이 배열 리터럴을 읽어 계속
 * 대조한다(core-v0.20.0-upgrade D-8).
 */
export const MODALITIES = [
  "llm",
  "vlm",
  "embedding",
  "reranker",
  "image",
] as const satisfies readonly Modality[];

/** 생성 타입 `Modality` 중 {@link MODALITIES}에 없는 값 — `never`가 아니면 아래 제약이 타입 오류다. */
type MissingModality = Exclude<Modality, (typeof MODALITIES)[number]>;
type AssertNoMissingModality<T extends never> = T;
export type ModalitiesComplete = AssertNoMissingModality<MissingModality>;

/** 다른 능력과 함께 둘 수 없는 능력 — 코어 v0.15.0 #209(reranker)·v0.23.0 #338(image), 섞으면 422 */
const EXCLUSIVE_MODALITIES: readonly string[] = ["reranker", "image"];

/** 체크 토글 — 단독 능력을 고르면 나머지를 해제하고, 다른 능력을 고르면 단독 능력을 해제한다. */
export function toggleCapability(value: string[], modality: string): string[] {
  if (value.includes(modality)) return value.filter((x) => x !== modality);
  if (EXCLUSIVE_MODALITIES.includes(modality)) return [modality];
  return [...value.filter((x) => !EXCLUSIVE_MODALITIES.includes(x)), modality];
}

/** 능력(modality) 중복 선택 — 체크박스 그룹 */
export function CapabilityPicker({
  value,
  onChange,
}: {
  value: string[];
  onChange: (v: string[]) => void;
}) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-2">
      {MODALITIES.map((m) => (
        <label key={m} className="flex items-center gap-1.5 text-sm">
          <Checkbox
            checked={value.includes(m)}
            onCheckedChange={() => onChange(toggleCapability(value, m))}
          />
          {m}
          {m === "embedding" && <span className="text-xs text-muted-foreground">(전역 1개)</span>}
          {EXCLUSIVE_MODALITIES.includes(m) && (
            <span className="text-xs text-muted-foreground">(단독)</span>
          )}
        </label>
      ))}
    </div>
  );
}
