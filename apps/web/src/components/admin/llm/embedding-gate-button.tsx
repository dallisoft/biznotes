"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useProviderEmbeddingGate } from "@/hooks/use-admin-llm";
import type { AdminProvider } from "@/lib/admin-types";
import { formatDuration } from "@/lib/format";

export function EmbeddingGateButton({ provider }: { provider: AdminProvider }) {
  const gate = useProviderEmbeddingGate(provider.id);
  const limit = provider.embedding_max_concurrency;
  if (limit == null) return null;

  const data = gate.data;
  const saturated = data != null && data.limit != null && data.in_use >= data.limit;

  return (
    <span className="flex items-center gap-1">
      <Badge variant="outline">임베딩 상한 {limit}</Badge>
      <Popover
        onOpenChange={(open) => {
          if (open && !gate.isFetching) void gate.refetch();
        }}
      >
        <PopoverTrigger
          render={<Button variant="ghost" size="sm" className="h-6 px-2 text-xs" />}
          disabled={gate.isFetching}
        >
          게이트 상태
        </PopoverTrigger>
        <PopoverContent className="w-72 text-sm" data-testid="embedding-gate-popover">
          {gate.isFetching ? (
            <p className="text-muted-foreground">조회 중…</p>
          ) : gate.error ? (
            <p className="text-destructive">
              {gate.error.status >= 500
                ? "게이트 상태 확인 불가(Redis)"
                : `조회 실패: ${gate.error.message}`}
            </p>
          ) : data ? (
            <div className="flex flex-col gap-1">
              <p>
                사용 중{" "}
                <Badge variant={saturated ? "destructive" : "secondary"}>
                  {data.in_use}/{data.limit ?? "∞"}
                </Badge>
              </p>
              {data.earliest_expiry_in != null && (
                <p className="text-xs text-muted-foreground">
                  가장 이른 해제까지 {formatDuration(data.earliest_expiry_in * 1000)}
                </p>
              )}
              {data.last_error && (
                <Badge variant="destructive" title={data.last_error}>
                  Redis 오류: {data.last_error}
                </Badge>
              )}
            </div>
          ) : null}
          <Button
            variant="outline"
            size="sm"
            disabled={gate.isFetching}
            onClick={() => void gate.refetch()}
          >
            다시 조회
          </Button>
        </PopoverContent>
      </Popover>
    </span>
  );
}
