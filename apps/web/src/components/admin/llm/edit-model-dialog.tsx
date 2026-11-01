"use client";

import { useState } from "react";
import { CrudFormDialog } from "@/components/admin/crud-form-dialog";
import { CapabilityPicker } from "@/components/admin/llm/capability-picker";
import { PerImagePriceInput } from "@/components/admin/llm/per-image-price-input";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  costPer1k,
  MAX_OUTPUT_TOKENS_LIMIT,
  type ModelForm,
  REASONING_EFFORT_OPTIONS,
  type ReasoningEffort,
  type useAdminModels,
} from "@/hooks/use-admin-llm";
import type { AdminModel } from "@/lib/admin-types";

/** 사고 양 「기본값 없음」의 선택 값 — 폼은 빈 문자열로 들고, 보낼 때 null이다. */
const NO_EFFORT = "none";

/**
 * 모델 수정 다이얼로그 — 제어형(트리거 없음). 표 행의 주 동작 「수정」(RowActions)이 연다(§6.6.2 LLM 행).
 * 열릴 때마다 현재 model 값으로 폼을 다시 심는다.
 */
export function EditModelDialog({
  model,
  update,
  open,
  onOpenChange,
}: {
  model: AdminModel;
  update: ReturnType<typeof useAdminModels>["update"];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const seed = (): ModelForm => ({
    display_name: model.display_name,
    context_window: model.context_window?.toString() ?? "",
    reasoning_effort: (model.reasoning_effort ?? "") as ModelForm["reasoning_effort"],
    max_output_tokens: model.max_output_tokens?.toString() ?? "",
    capabilities: model.capabilities as string[],
    cost_input: model.cost_per_1k.input?.toString() ?? "",
    cost_output: model.cost_per_1k.output?.toString() ?? "",
    cost_per_image: model.cost_per_1k.per_image?.toString() ?? "",
  });
  const [form, setForm] = useState(seed);
  const [seededOpen, setSeededOpen] = useState(open);
  if (open !== seededOpen) {
    setSeededOpen(open);
    if (open) setForm(seed());
  }
  const idPrefix = `model-${model.id}`;

  function submit() {
    update.mutate(
      {
        id: model.id,
        display_name: form.display_name,
        context_window: form.context_window === "" ? null : Number(form.context_window),
        // 빈 값은 해제(null) — 코어 PATCH는 키를 두고 null을 보내면 지운다(#429·#428)
        reasoning_effort: form.reasoning_effort === "" ? null : form.reasoning_effort,
        max_output_tokens: form.max_output_tokens === "" ? null : Number(form.max_output_tokens),
        capabilities: form.capabilities,
        cost_per_1k: costPer1k(
          form.cost_input,
          form.cost_output,
          form.cost_per_image,
          form.capabilities,
        ),
      },
      { onSuccess: () => onOpenChange(false) },
    );
  }

  return (
    <CrudFormDialog
      open={open}
      onOpenChange={onOpenChange}
      title="모델 수정"
      // 능력을 하나도 고르지 않으면 서버가 거부한다 — 제출 자체를 막는다.
      pending={update.isPending || form.capabilities.length === 0}
      onSubmit={submit}
    >
      <p className="font-mono text-xs text-muted-foreground">{model.model_name}</p>
      <Label htmlFor={`${idPrefix}-display-name`}>표시명</Label>
      <Input
        id={`${idPrefix}-display-name`}
        required
        value={form.display_name}
        onChange={(e) => setForm({ ...form, display_name: e.target.value })}
      />
      <Label>능력 (여러 개 선택 가능)</Label>
      <CapabilityPicker
        value={form.capabilities}
        onChange={(capabilities) => setForm({ ...form, capabilities })}
      />
      <Label htmlFor={`${idPrefix}-context-window`}>컨텍스트 창 (토큰, 선택)</Label>
      <Input
        id={`${idPrefix}-context-window`}
        type="number"
        min={1}
        value={form.context_window}
        onChange={(e) => setForm({ ...form, context_window: e.target.value })}
        placeholder="—"
      />
      <Label htmlFor={`${idPrefix}-max-output`}>최대 출력 (토큰, 선택)</Label>
      <Input
        id={`${idPrefix}-max-output`}
        type="number"
        min={1}
        max={MAX_OUTPUT_TOKENS_LIMIT}
        value={form.max_output_tokens}
        onChange={(e) => setForm({ ...form, max_output_tokens: e.target.value })}
        placeholder="—"
        aria-describedby={`${idPrefix}-max-output-help`}
      />
      <p id={`${idPrefix}-max-output-help`} className="text-xs text-muted-foreground">
        모델이 한 번에 낼 수 있는 최대 출력입니다. 채우면 모든 호출의 출력 상한을 이 값으로 줄입니다(비우면 줄이지 않음).
      </p>
      <Label htmlFor={`${idPrefix}-reasoning`}>사고 양 기본값 (선택)</Label>
      <Select
        value={form.reasoning_effort === "" ? NO_EFFORT : form.reasoning_effort}
        onValueChange={(v) =>
          setForm({ ...form, reasoning_effort: !v || v === NO_EFFORT ? "" : (v as ReasoningEffort) })
        }
      >
        <SelectTrigger id={`${idPrefix}-reasoning`} aria-label="사고 양 기본값">
          <SelectValue>
            {(val) => REASONING_EFFORT_OPTIONS.find((o) => o.value === val)?.label ?? "기본값 없음"}
          </SelectValue>
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={NO_EFFORT}>기본값 없음</SelectItem>
          {REASONING_EFFORT_OPTIONS.map((o) => (
            <SelectItem key={o.value} value={o.value}>
              {o.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <p className="text-xs text-muted-foreground">
        노드에 사고 양이 없을 때 이 모델 호출에 씁니다. 지원하지 않는 모델에서는 빠집니다.
      </p>
      <Label>단가 (USD / 1K 토큰, 선택 — 비우면 비용을 집계하지 않음)</Label>
      <div className="flex gap-2">
        <Input
          type="number"
          min={0}
          step="0.0001"
          aria-label="입력 단가 (USD / 1K 토큰)"
          value={form.cost_input}
          onChange={(e) => setForm({ ...form, cost_input: e.target.value })}
          placeholder="입력"
        />
        <Input
          type="number"
          min={0}
          step="0.0001"
          aria-label="출력 단가 (USD / 1K 토큰)"
          value={form.cost_output}
          onChange={(e) => setForm({ ...form, cost_output: e.target.value })}
          placeholder="출력"
        />
      </div>
      {form.capabilities.includes("image") && (
        <PerImagePriceInput
          id={`${idPrefix}-per-image`}
          value={form.cost_per_image}
          onChange={(cost_per_image) => setForm({ ...form, cost_per_image })}
        />
      )}
    </CrudFormDialog>
  );
}
