"use client";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  EMBEDDING_CONCURRENCY_MAX,
  EMBEDDING_CONCURRENCY_MIN,
  parseEmbeddingConcurrency,
} from "@/lib/embedding-concurrency";

export function EmbeddingConcurrencyField({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  const invalid = !parseEmbeddingConcurrency(value).ok;
  return (
    <>
      <Label htmlFor="embedding-max-concurrency">
        임베딩 동시 요청 상한 (선택, {EMBEDDING_CONCURRENCY_MIN}~{EMBEDDING_CONCURRENCY_MAX} — 비우면
        제한 없음)
      </Label>
      <Input
        id="embedding-max-concurrency"
        inputMode="numeric"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="제한 없음"
        aria-invalid={invalid}
      />
      {invalid ? (
        <p className="text-xs text-destructive">
          {EMBEDDING_CONCURRENCY_MIN}~{EMBEDDING_CONCURRENCY_MAX} 사이의 정수를 입력하거나 비워 두세요.
        </p>
      ) : (
        <p className="text-xs text-muted-foreground">
          임베딩 서버의 동시 처리 한도(MAX_CONCURRENT_REQUESTS)와 같게 둡니다. 너무 작으면 인제스트 중
          검색이 실패할 수 있습니다.
        </p>
      )}
    </>
  );
}
