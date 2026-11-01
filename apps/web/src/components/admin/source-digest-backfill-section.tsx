"use client";

import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { StatusBadge, type StatusTone } from "@/components/ui/status-badge";
import { ALL_NOTEBOOKS, useSourceDigestBackfill, useStartSourceDigestBackfill } from "@/hooks/use-source-digest-backfill";
import type { SourceDigestBackfillProgress } from "@/lib/admin-types";
import { formatDateTime, formatNumber } from "@/lib/format";

/**
 * 관리 › RAG 관리 「소스 요약」(코어 v0.30.0 #454, #285). 스튜디오의 `source_digest`와 노트북 개요가 쓰는 소스 요약
 * 캐시를 미리 만든다. 새 소스는 인제스트 때 만들어지고(`SOURCE_DIGEST_ON_INGEST`), 이 화면은 그 전에 들어온 소스를
 * 채운다. 이미 최신 요약이 있는 소스는 건너뛰므로 멈춘 뒤 다시 시작하면 이어서 진행한다. 하루 비용·개수 상한은
 * env(`SOURCE_DIGEST_BACKFILL_DAILY_*`)가 정한다.
 */
export function SourceDigestBackfillSection({
  notebooks,
}: {
  notebooks: ReadonlyArray<{ notebook_id: string; title: string }>;
}) {
  const titleId = useId();
  const [scope, setScope] = useState<string>(ALL_NOTEBOOKS);
  const notebookId = scope === ALL_NOTEBOOKS ? null : scope;
  const status = useSourceDigestBackfill(notebookId);
  const start = useStartSourceDigestBackfill();
  const progress = status.data?.progress ?? null;
  const active = progress?.status === "queued" || progress?.status === "running";
  const scopeLabel =
    notebookId === null ? "모든 노트북" : (notebooks.find((n) => n.notebook_id === notebookId)?.title ?? "노트북");

  return (
    <section aria-labelledby={titleId} data-testid="digest-backfill">
      <h2 id={titleId} className="text-lg font-semibold">
        소스 요약
      </h2>
      <p className="mt-1 text-sm text-muted-foreground">
        스튜디오와 노트북 개요가 쓰는 소스 요약을 미리 만듭니다. 새 소스는 추가될 때 만들어지고, 여기서는 그 전에 들어온
        소스를 채웁니다. 이미 요약이 있는 소스는 건너뛰며 LLM 비용이 듭니다(하루 상한은 서버 설정).
      </p>
      <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center">
        <Select value={scope} onValueChange={(v) => v && setScope(v)}>
          <SelectTrigger aria-label="요약을 만들 범위" className="sm:w-72">
            <SelectValue>
              {(val) =>
                val === ALL_NOTEBOOKS ? "모든 노트북" : (notebooks.find((n) => n.notebook_id === val)?.title ?? "노트북")
              }
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL_NOTEBOOKS}>모든 노트북</SelectItem>
            {notebooks.map((n) => (
              <SelectItem key={n.notebook_id} value={n.notebook_id}>
                {n.title}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <ConfirmButton
          type="button"
          variant="outline"
          destructive={false}
          title={`${scopeLabel}의 소스 요약을 만들까요?`}
          description="요약이 없는 소스마다 LLM을 부릅니다. 소스 수에 따라 오래 걸리고 비용이 들며, 하루 상한에 닿으면 멈춥니다. 다시 시작하면 이어서 진행합니다."
          actionLabel="만들기"
          onConfirm={() => start.mutate({ notebookId })}
          disabled={active || start.isPending}
          className="coarse:min-h-11"
        >
          요약 만들기…
        </ConfirmButton>
        {status.isError && (
          <Button type="button" variant="ghost" size="sm" onClick={() => void status.refetch()}>
            다시 확인
          </Button>
        )}
      </div>
      <div className="mt-3 text-sm" role="status" aria-live="polite" data-testid="digest-backfill-status">
        {status.isPending ? (
          <span className="text-muted-foreground">진행률을 확인하는 중…</span>
        ) : status.isError ? (
          <span className="text-destructive-text">진행률을 읽지 못했습니다</span>
        ) : progress ? (
          <BackfillProgress progress={progress} />
        ) : (
          <span className="text-muted-foreground">{scopeLabel}에서 요약을 만든 기록이 없습니다.</span>
        )}
      </div>
    </section>
  );
}

const STATUS_LABEL: Record<SourceDigestBackfillProgress["status"], { label: string; tone: StatusTone }> = {
  queued: { label: "대기", tone: "info" },
  running: { label: "진행 중", tone: "info" },
  done: { label: "끝남", tone: "success" },
  stopped: { label: "상한으로 멈춤", tone: "warning" },
  failed: { label: "실패", tone: "danger" },
};

const STOP_REASON_LABEL: Record<NonNullable<SourceDigestBackfillProgress["stop_reason"]>, string> = {
  daily_cost_cap: "하루 비용 상한",
  daily_source_cap: "하루 생성 수 상한",
};

/** 진행률 한 줄 — 상태 배지, 만든·건너뛴·실패 수, 남은 수(멈췄을 때), 마지막 갱신 시각. */
export function BackfillProgress({ progress }: { progress: SourceDigestBackfillProgress }) {
  const { label, tone } = STATUS_LABEL[progress.status] ?? { label: progress.status, tone: "neutral" as const };
  const remaining = Math.max(progress.total - progress.generated - progress.skipped - progress.failed, 0);
  const updated = progress.finished_at ?? progress.updated_at ?? progress.started_at;
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <StatusBadge tone={tone}>{label}</StatusBadge>
      <span className="tabular-nums">
        만듦 {formatNumber(progress.generated)} · 건너뜀 {formatNumber(progress.skipped)} · 실패{" "}
        {formatNumber(progress.failed)} / 전체 {formatNumber(progress.total)}
      </span>
      {progress.status === "stopped" && (
        <span className="text-muted-foreground">
          {progress.stop_reason ? STOP_REASON_LABEL[progress.stop_reason] : "상한"}으로 멈췄습니다 — 남은{" "}
          {formatNumber(remaining)}개는 다시 시작하면 이어서 만듭니다
        </span>
      )}
      {updated && <span className="text-xs text-muted-foreground">{formatDateTime(updated)}</span>}
    </div>
  );
}
