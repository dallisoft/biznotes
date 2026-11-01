"use client";

import { Plus } from "lucide-react";
import { useState } from "react";
import { CrudFormDialog } from "@/components/admin/crud-form-dialog";
import { CapabilityPicker } from "@/components/admin/llm/capability-picker";
import { PerImagePriceInput } from "@/components/admin/llm/per-image-price-input";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  costPer1k,
  useProviderAvailableModels,
  type useAdminModels,
} from "@/hooks/use-admin-llm";
import type { AdminProvider } from "@/lib/admin-types";

/** 「모델 추가」 — 모델 섹션의 보조 동작(outline, `Plus` + 텍스트 — §6.3.3·§6.6.2) + 등록 다이얼로그. */
export function CreateModelDialog({
  providers,
  create,
}: {
  providers: AdminProvider[];
  create: ReturnType<typeof useAdminModels>["create"];
}) {
  const [open, setOpen] = useState(false);
  const [providerId, setProviderId] = useState("");
  const [modelName, setModelName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [capabilities, setCapabilities] = useState<string[]>(["llm"]);
  const [costInput, setCostInput] = useState("");
  const [costOutput, setCostOutput] = useState("");
  const [costPerImage, setCostPerImage] = useState("");

  // 선택한 프로바이더가 제공하는 모델 목록 (등록 전 선택용)
  const available = useProviderAvailableModels(providerId, open);

  function reset() {
    setProviderId("");
    setModelName("");
    setDisplayName("");
    setCapabilities(["llm"]);
    setCostInput("");
    setCostOutput("");
    setCostPerImage("");
  }

  // 디스커버리에서 모델 선택 → 모델명/표시명/능력 기본값 채움
  function pickModel(name: string) {
    setModelName(name);
    const m = available.data?.find((x) => x.model_name === name);
    if (m) {
      setDisplayName(m.display_name);
      setCapabilities(m.capabilities);
    }
  }

  const models = available.data ?? [];
  const canSubmit = !!providerId && !!modelName && !!displayName && capabilities.length > 0;

  return (
    <>
      {/* 트리거는 CrudFormDialog 밖에 둔다(outline 보조 버튼). 닫히면 포커스는 이 버튼으로 돌아간다. */}
      <Button variant="outline" className="coarse:min-h-11" onClick={() => setOpen(true)}>
        <Plus aria-hidden />
        모델 추가
      </Button>
      <CrudFormDialog
        open={open}
        onOpenChange={setOpen}
        title="모델 추가"
        submitLabel="추가"
        pending={create.isPending || !canSubmit}
        onReset={reset}
        onSubmit={() => {
          if (!canSubmit) return;
          create.mutate(
            {
              provider_id: providerId,
              model_name: modelName,
              display_name: displayName,
              capabilities,
              cost_per_1k: costPer1k(costInput, costOutput, costPerImage, capabilities),
            },
            { onSuccess: () => setOpen(false) },
          );
        }}
      >
        <Label>프로바이더</Label>
        <Select
          value={providerId}
          onValueChange={(v) => {
            if (!v) return;
            setProviderId(v);
            setModelName("");
            setDisplayName("");
            setCapabilities(["llm"]);
          }}
        >
          <SelectTrigger aria-label="프로바이더">
            <SelectValue placeholder="선택">
              {(val) => {
                const p = providers.find((pp) => pp.id === val);
                return p ? `${p.name} (${p.type})` : "선택";
              }}
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            {providers.map((p) => (
              <SelectItem key={p.id} value={p.id}>
                {p.name} ({p.type})
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Label htmlFor="create-model-name">모델명 (LiteLLM 식별자)</Label>
        {providerId && available.isLoading ? (
          <p className="text-xs text-muted-foreground">제공 모델을 불러오는 중…</p>
        ) : models.length > 0 ? (
          <Select value={modelName} onValueChange={(v) => v && pickModel(v)}>
            <SelectTrigger id="create-model-name">
              <SelectValue placeholder="모델 선택" />
            </SelectTrigger>
            <SelectContent
              alignItemWithTrigger={false}
              className="w-auto min-w-(--anchor-width) max-w-[min(32rem,90vw)]"
            >
              {models.map((m) => (
                <SelectItem key={m.model_name} value={m.model_name} disabled={m.already_registered}>
                  {m.model_name}
                  {m.already_registered ? " (등록됨)" : ""}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : (
          <Input
            id="create-model-name"
            required
            value={modelName}
            onChange={(e) => setModelName(e.target.value)}
            placeholder="예: bedrock/anthropic.claude-haiku-4-5"
          />
        )}
        {!!providerId && available.isError && (
          <p className="text-xs text-muted-foreground">
            이 프로바이더는 모델 목록 조회를 지원하지 않습니다 — 직접 입력하세요.
          </p>
        )}

        <Label htmlFor="create-model-display-name">표시명</Label>
        <Input
          id="create-model-display-name"
          required
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          placeholder="선택한 모델 이름이 기본값"
        />

        <Label>능력 (여러 개 선택 가능)</Label>
        <CapabilityPicker value={capabilities} onChange={setCapabilities} />

        <Label>단가 (USD / 1K 토큰, 선택 — 비우면 비용을 집계하지 않음)</Label>
        <div className="flex gap-2">
          <Input
            type="number"
            min={0}
            step="0.0001"
            value={costInput}
            onChange={(e) => setCostInput(e.target.value)}
            aria-label="입력 단가 (USD / 1K 토큰)"
            placeholder="입력"
          />
          <Input
            type="number"
            min={0}
            step="0.0001"
            value={costOutput}
            onChange={(e) => setCostOutput(e.target.value)}
            aria-label="출력 단가 (USD / 1K 토큰)"
            placeholder="출력"
          />
        </div>
        {capabilities.includes("image") && (
          <PerImagePriceInput id="create-model-per-image" value={costPerImage} onChange={setCostPerImage} />
        )}
      </CrudFormDialog>
    </>
  );
}
