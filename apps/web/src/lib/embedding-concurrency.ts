export const EMBEDDING_CONCURRENCY_MIN = 1;
export const EMBEDDING_CONCURRENCY_MAX = 64;

export type ParsedEmbeddingConcurrency = { ok: true; value: number | null } | { ok: false };

/** 빈 칸은 「제한 없음」(null), 1~64 정수만 유효하다(코어 `ProviderCreate/Update` 범위). */
export function parseEmbeddingConcurrency(raw: string): ParsedEmbeddingConcurrency {
  const text = raw.trim();
  if (text === "") return { ok: true, value: null };
  if (!/^\d+$/.test(text)) return { ok: false };
  const value = Number(text);
  if (value < EMBEDDING_CONCURRENCY_MIN || value > EMBEDDING_CONCURRENCY_MAX) return { ok: false };
  return { ok: true, value };
}
