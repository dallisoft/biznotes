"use client";

import { Plus } from "lucide-react";
import { useState } from "react";
import { CrudFormDialog } from "@/components/admin/crud-form-dialog";
import { PROVIDER_TYPES } from "@/components/admin/llm/capability-picker";
import { EmbeddingConcurrencyField } from "@/components/admin/llm/embedding-concurrency-field";
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
import type { useAdminProviders } from "@/hooks/use-admin-llm";
import { parseEmbeddingConcurrency } from "@/lib/embedding-concurrency";

const EMPTY = { name: "", type: "openai", base_url: "", api_key: "", embedding_max_concurrency: "" };

/** 「프로바이더 추가」 — 페이지 헤더의 주 동작(primary, `Plus` + 텍스트 — §6.3.3) + 등록 다이얼로그. */
export function CreateProviderDialog({
  create,
}: {
  create: ReturnType<typeof useAdminProviders>["create"];
}) {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState(EMPTY);

  return (
    <>
      {/* 트리거는 CrudFormDialog 밖에 둔다 — 주 버튼 크기·터치 타깃(coarse 44px)을 이 화면이 정한다. 닫히면 포커스는 이 버튼으로 돌아간다. */}
      <Button className="coarse:min-h-11" onClick={() => setOpen(true)}>
        <Plus aria-hidden />
        프로바이더 추가
      </Button>
      <CrudFormDialog
        open={open}
        onOpenChange={setOpen}
        title="프로바이더 추가"
        submitLabel="추가"
        pending={create.isPending}
        onReset={() => setForm(EMPTY)}
        onSubmit={() => {
          const limit = parseEmbeddingConcurrency(form.embedding_max_concurrency);
          if (!limit.ok) return;
          create.mutate(
            { ...form, embedding_max_concurrency: limit.value },
            { onSuccess: () => setOpen(false) },
          );
        }}
      >
        <Label htmlFor="create-provider-name">이름</Label>
        <Input
          id="create-provider-name"
          required
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          placeholder="예: bedrock-us"
        />
        <Label>유형</Label>
        <Select value={form.type} onValueChange={(v) => v && setForm({ ...form, type: v })}>
          <SelectTrigger aria-label="유형">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {PROVIDER_TYPES.map((t) => (
              <SelectItem key={t} value={t}>
                {t}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Label htmlFor="create-provider-base-url">Base URL (선택 — ollama·vllm·lmstudio·custom)</Label>
        <Input
          id="create-provider-base-url"
          value={form.base_url}
          onChange={(e) => setForm({ ...form, base_url: e.target.value })}
          placeholder="http://ollama:11434"
        />
        <Label htmlFor="create-provider-api-key">API 키 (암호화해 저장)</Label>
        <Input
          id="create-provider-api-key"
          type="password"
          value={form.api_key}
          onChange={(e) => setForm({ ...form, api_key: e.target.value })}
        />
        <EmbeddingConcurrencyField
          value={form.embedding_max_concurrency}
          onChange={(v) => setForm({ ...form, embedding_max_concurrency: v })}
        />
      </CrudFormDialog>
    </>
  );
}
