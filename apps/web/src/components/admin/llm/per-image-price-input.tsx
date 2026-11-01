"use client";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * 이미지 생성 모델의 장당 가격(USD) — 코어 v0.23.0 `cost_per_1k.per_image`(#338). 노드 비용은
 * `per_image × 장수`이고, 비우면 응답 토큰 단가로 계산하거나(있을 때) 집계하지 않는다.
 * `step="any"` — 고정 step이면 `0.0416` 같은 가격이 네이티브 검증에 막혀 제출되지 않는다.
 */
export function PerImagePriceInput({
  id,
  value,
  onChange,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <>
      <Label htmlFor={id}>장당 가격 (USD / 이미지 1장, 선택)</Label>
      <Input
        id={id}
        type="number"
        min={0}
        step="any"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="예: 0.04"
      />
    </>
  );
}
