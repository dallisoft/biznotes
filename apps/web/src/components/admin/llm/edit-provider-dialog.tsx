"use client";

import { useState } from "react";
import { CrudFormDialog } from "@/components/admin/crud-form-dialog";
import { EmbeddingConcurrencyField } from "@/components/admin/llm/embedding-concurrency-field";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import type { useAdminProviders } from "@/hooks/use-admin-llm";
import type { AdminProvider } from "@/lib/admin-types";
import { parseEmbeddingConcurrency } from "@/lib/embedding-concurrency";

/**
 * 프로바이더 수정 다이얼로그 — 제어형(트리거 없음). 표 행의 주 동작 「수정」(RowActions)이 연다(§6.6.2 LLM 행).
 * 열릴 때마다 현재 provider 값으로 폼을 다시 심는다. 닫히면 포커스는 연 요소(「수정」 버튼)로 돌아간다.
 */
export function EditProviderDialog({
  provider,
  update,
  open,
  onOpenChange,
}: {
  provider: AdminProvider;
  update: ReturnType<typeof useAdminProviders>["update"];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const seed = () => ({
    base_url: provider.base_url ?? "",
    api_key: "",
    enabled: provider.enabled,
    embedding_max_concurrency: provider.embedding_max_concurrency?.toString() ?? "",
  });
  const [form, setForm] = useState(seed);
  // 닫힘 → 열림 전이마다 시드한다(렌더 중 상태 조정 — 첫 렌더부터 현재 값으로 그린다).
  const [seededOpen, setSeededOpen] = useState(open);
  if (open !== seededOpen) {
    setSeededOpen(open);
    if (open) setForm(seed());
  }

  return (
    <CrudFormDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`${provider.name} 수정`}
      pending={update.isPending}
      onSubmit={() => {
        const limit = parseEmbeddingConcurrency(form.embedding_max_concurrency);
        if (!limit.ok) return;
        update.mutate(
          { id: provider.id, ...form, embedding_max_concurrency: limit.value },
          { onSuccess: () => onOpenChange(false) },
        );
      }}
    >
      <p className="text-xs text-muted-foreground">
        유형(<span className="font-mono">{provider.type}</span>)·이름은 바꿀 수 없습니다. 바꾸려면 삭제한 뒤
        다시 추가하세요.
      </p>
      <Label htmlFor={`provider-${provider.id}-base-url`}>Base URL</Label>
      <Input
        id={`provider-${provider.id}-base-url`}
        value={form.base_url}
        onChange={(e) => setForm({ ...form, base_url: e.target.value })}
        placeholder="비워 두면 기본값 사용"
      />
      <Label htmlFor={`provider-${provider.id}-api-key`}>API 키 (비워 두면 기존 키 유지)</Label>
      <Input
        id={`provider-${provider.id}-api-key`}
        type="password"
        value={form.api_key}
        onChange={(e) => setForm({ ...form, api_key: e.target.value })}
        placeholder={provider.has_api_key ? "저장된 키 있음 (바꿀 때만 입력)" : "설정 안 됨"}
      />
      <div className="flex items-center gap-2">
        <Switch
          id={`provider-${provider.id}-enabled`}
          checked={form.enabled}
          onCheckedChange={(c) => setForm({ ...form, enabled: c })}
        />
        <Label htmlFor={`provider-${provider.id}-enabled`}>활성</Label>
      </div>
      <EmbeddingConcurrencyField
        value={form.embedding_max_concurrency}
        onChange={(v) => setForm({ ...form, embedding_max_concurrency: v })}
      />
    </CrudFormDialog>
  );
}
