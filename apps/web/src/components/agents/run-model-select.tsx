"use client";

import { useId, useMemo } from "react";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useModels } from "@/hooks/use-chat";
import type { LLMModelInfo } from "@/lib/chat-types";

export const DEFAULT_RUN_MODEL_LABEL = "기본 모델";

/**
 * 「기본 모델」 항목의 라벨 — 지정하지 않으면 코어가 고르는 모델(`is_default`, 없으면 목록 첫 모델 — 코어
 * `resolve_model`과 같은 순서)의 이름을 괄호로 보인다. 목록이 비었으면 이름 없이 「기본 모델」이다.
 */
export function defaultRunModelLabel(models: LLMModelInfo[] | undefined): string {
  const fallback = models?.find((m) => m.is_default) ?? models?.[0];
  return fallback ? `${DEFAULT_RUN_MODEL_LABEL} (${fallback.display_name})` : DEFAULT_RUN_MODEL_LABEL;
}

/**
 * 스튜디오 실행 모델 선택(#216, core-v0.25.0-upgrade D4). 기본값 「기본 모델」은 `null`이고, 호출부는 요청에
 * `model_id`를 넣지 않는다(시스템 기본 모델 + 폴백 체인 — 지금과 같다). 목록은 채팅 입력창과 같은
 * `GET /api/v1/models`(켜진 llm 모델)이다. 고른 모델이 실패하면 폴백 체인이 잇고, 결과 상세 「실행 로그」에
 * 실제로 응답한 모델과 「폴백」이 보인다.
 */
export function RunModelSelect({
  value,
  onChange,
}: {
  value: string | null;
  onChange: (modelId: string | null) => void;
}) {
  const models = useModels();
  const triggerId = useId();
  // Base UI Select의 `items`(닫힌 상태 라벨)와 children(팝업 목록)이 갈라지지 않게 단일 배열에서 파생한다 —
  // `items`가 없으면 닫힌 트리거에 모델 UUID가 보인다(ChatComposer 주석)
  const options = useMemo(
    () => [
      { value: null, label: defaultRunModelLabel(models.data) },
      ...(models.data ?? []).map((m) => ({ value: m.id, label: m.display_name })),
    ],
    [models.data],
  );

  return (
    <div className="flex min-w-0 flex-col gap-2">
      <Label htmlFor={triggerId}>모델</Label>
      <Select<string | null>
        value={value}
        onValueChange={(v) => onChange(v ?? null)}
        items={options}
      >
        <SelectTrigger id={triggerId} size="sm" className="max-w-64" data-testid="run-model-select">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map((o) => (
            <SelectItem key={o.value ?? "default"} value={o.value}>
              {o.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
