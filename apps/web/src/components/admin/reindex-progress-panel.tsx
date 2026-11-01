"use client";

import { useEffect, useState } from "react";
import { ReindexRequestId } from "@/components/admin/reindex-request-id";
import { Badge } from "@/components/ui/badge";
import { StatusBadge, type StatusTone } from "@/components/ui/status-badge";
import type { ReindexRun, ReindexStartOut } from "@/lib/admin-types";
import { EMPTY, formatDateTime } from "@/lib/format";
import {
  formatElapsed,
  isAuthError,
  scopeLabel,
  stallNotice,
  summarizeRun,
  type KnownReindexScope,
} from "@/lib/reindex-progress";

/**
 * 재인덱싱 진행 패널 (reindex-progress PLAN D-7) — 프레젠테이션 전용.
 * 데이터 페칭은 하지 않는다(페이지가 훅 결과를 넘긴다). 진행 바·텍스트·ARIA는 모두 같은
 * `summarizeRun` 결과에서 나온다(AC-2).
 */
export interface ReindexProgressPanelProps {
  run?: ReindexRun | null;
  /** 포커스 run 상세 조회 오류(마지막 데이터와 함께 표시). */
  error?: unknown;
  /** 포커스 run이 404로 사라짐. */
  notFound?: boolean;
  /** 방금 시작한 run의 첫 상세 응답 전. */
  unconfirmed?: boolean;
  knownScope?: KnownReindexScope | null;
  notebookTitles?: ReadonlyMap<string, string>;
  lastStart?: ReindexStartOut | null;
  /** 포커스 외 활성 run. */
  otherActive?: readonly ReindexRun[];
}

const EMPTY_TITLES: ReadonlyMap<string, string> = new Map();

/** 시작·종료 시각 — 표기는 `lib/format`(초 포함, DP-7). 해석할 수 없으면 「알 수 없음」. */
function formatTimestamp(value: string): string {
  const text = formatDateTime(value, { seconds: true });
  return text === EMPTY ? "알 수 없음" : text;
}

function errorMessage(error: unknown): string {
  return isAuthError(error)
    ? "세션 또는 권한 오류 — 다시 로그인하세요"
    : "진행 상태 조회에 실패했습니다. 마지막으로 확인한 카운트를 표시합니다.";
}

function ErrorAlert({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <p role="alert" className="mt-3 text-sm text-destructive">
      {errorMessage(error)}
    </p>
  );
}

const PANEL_CLASS = "rounded-md border p-4";

export function ReindexProgressPanel({
  run = null,
  error,
  notFound = false,
  unconfirmed = false,
  knownScope,
  notebookTitles = EMPTY_TITLES,
  lastStart,
  otherActive = [],
}: ReindexProgressPanelProps) {
  const active = run !== null && run.queued > 0;
  const [now, setNow] = useState(() => Date.now());

  // 장시간 무변화 안내(D-7): (queued, done, failed) 튜플이 마지막으로 바뀐 시각을 추적한다.
  // 렌더 중 상태 조정 패턴 — run이 바뀌거나 카운트가 변하면 기준 시각을 갱신한다.
  const tupleKey = run ? `${run.id}:${run.queued}:${run.done}:${run.failed}` : null;
  const [lastChange, setLastChange] = useState(() => ({ key: tupleKey, at: Date.now() }));
  if (lastChange.key !== tupleKey) {
    setLastChange({ key: tupleKey, at: Date.now() });
  }

  // 활성 중에만 1초 틱 — 종결·unmount 시 cleanup으로 정리(AC-5·AC-18).
  const runId = run?.id;
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [active, runId]);

  // P1-11: role=status 라이브 영역은 패널이 비어 있을 때도 같은 위치에 마운트해 두고 내용만 바꾼다
  // (새로 생긴 라이브 영역의 첫 문구는 일부 스크린리더가 읽지 않는다). 모든 분기가 같은
  // section > header > status 구조를 공유하고, 패널 테두리·testid는 내용이 있을 때만 붙인다.
  const shell = (status: React.ReactNode, aside: React.ReactNode, body: React.ReactNode) => {
    const hasContent = status !== null;
    return (
      <section
        data-testid={hasContent ? "reindex-progress" : undefined}
        className={hasContent ? PANEL_CLASS : undefined}
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div role="status" aria-live="polite" className="flex items-center gap-2">
            {status}
          </div>
          {aside}
        </div>
        {body}
      </section>
    );
  };

  if (unconfirmed) {
    return shell(
      <StatusBadge data-testid="reindex-status">run 상태 확인 중</StatusBadge>,
      null,
      <>
        <p className="mt-2 text-xs text-muted-foreground">
          방금 시작한 run의 첫 상태 응답을 기다리고 있습니다.
        </p>
        <ErrorAlert error={error} />
      </>,
    );
  }

  if (!run) {
    if (!notFound && !error) return shell(null, null, null);
    return shell(
      <StatusBadge tone={notFound ? "neutral" : "danger"} data-testid="reindex-status">
        {notFound ? "run을 찾을 수 없습니다(삭제됨)" : "조회 실패"}
      </StatusBadge>,
      null,
      !notFound && <ErrorAlert error={error} />,
    );
  }

  const summary = summarizeRun(run, now, { lastStart });
  const statusLabel =
    summary.status === "active"
      ? "진행 중"
      : summary.status === "done"
        ? "완료"
        : summary.status === "done_with_failures"
          ? "완료 · 실패 있음"
          : `종료됨 · 집계 제외 ${summary.missing}건`;
  // 상태는 말 + 의미 톤(DP-4): 진행 중 info, 완료 success, 실패 있음 danger, 집계 제외 warning
  const statusTone: StatusTone =
    summary.status === "active"
      ? "info"
      : summary.status === "done"
        ? "success"
        : summary.status === "done_with_failures"
          ? "danger"
          : "warning";
  const progressText = `${summary.completed} / ${summary.total} (${summary.pct}%)`;
  const valueText = `완료 ${summary.completed} / 대상 ${summary.total}${
    summary.removed > 0 ? ` · 삭제로 제외 ${summary.removed}건` : ""
  }${summary.missing > 0 ? ` · 집계 제외 ${summary.missing}건` : ""}`;
  const retrying = run.retrying ?? 0;
  const notice = active
    ? stallNotice([{ queued: run.queued, done: run.done, failed: run.failed, at: lastChange.at }], now)
    : null;

  return shell(
    <>
      <StatusBadge data-testid="reindex-status" tone={statusTone}>
        {statusLabel}
      </StatusBadge>
      <span className="text-sm font-medium">{scopeLabel(run, notebookTitles, knownScope)}</span>
    </>,
    <ReindexRequestId runId={run.id} className="text-xs text-muted-foreground" />,
    <>
      <ErrorAlert error={error} />

      <p className="mt-3 text-sm">이 run의 등록 대상 {run.requested}건</p>
      {summary.candidate && (
        <p className="text-sm">
          선택 후보 {summary.candidate.total} = 등록 {summary.candidate.enqueued} + 건너뜀{" "}
          {summary.candidate.skipped}(이미 대기·처리 중)
        </p>
      )}

      <div className="mt-3">
        <div
          role="progressbar"
          aria-label="재인덱싱 진행률"
          aria-valuemin={0}
          aria-valuemax={summary.total}
          aria-valuenow={summary.completed}
          aria-valuetext={valueText}
          className="h-2 overflow-hidden rounded-full bg-muted"
        >
          <div
            className="h-full bg-primary transition-[width]"
            style={{ width: `${Math.min(summary.pct, 100)}%` }}
          />
        </div>
        <p className="mt-1 text-sm font-medium" data-testid="reindex-progress-text">
          {progressText}
        </p>
      </div>

      <div className="mt-3 flex flex-wrap gap-2 text-sm">
        <Badge variant="outline">대기·처리 미완료 {run.queued}</Badge>
        <Badge variant="outline">완료 {run.done}</Badge>
        <Badge variant={run.failed > 0 ? "destructive" : "outline"}>실패 {run.failed}</Badge>
        {summary.removed > 0 && (
          <Badge variant="outline" title="처리 전에 소스가 삭제되어 대상에서 빠짐(완료로 셈)">
            삭제로 제외 {summary.removed}건
          </Badge>
        )}
        {retrying > 0 && (
          <Badge variant="outline" title="정체로 재발행된 대기 항목">
            재시도 중 {retrying}건(최대 {run.max_attempts ?? 0}회)
          </Badge>
        )}
        {summary.missing > 0 && (
          <Badge variant="outline" title="이전 버전 run — 소스 삭제로 항목이 사라져 집계에서 빠짐">
            집계 제외 {summary.missing}건
          </Badge>
        )}
      </div>

      {notice && <p className="mt-3 text-xs text-muted-foreground">{notice}</p>}

      <dl className="mt-3 grid gap-1 text-xs text-muted-foreground sm:grid-cols-3">
        <div>
          <dt>경과</dt>
          <dd data-testid="reindex-elapsed">
            {summary.elapsedMs === null ? "알 수 없음" : formatElapsed(summary.elapsedMs)}
          </dd>
        </div>
        <div>
          <dt>시작 시각</dt>
          <dd>{formatTimestamp(run.created_at)}</dd>
        </div>
        <div>
          <dt>
            {summary.finishedAtKind === "last_item" ? "종료(마지막 항목 갱신) 시각" : "종료 시각"}
          </dt>
          <dd>
            {summary.finishedAtKind === "none"
              ? "진행 중"
              : summary.finishedAtKind === "unknown" || !run.finished_at
                ? "알 수 없음"
                : formatTimestamp(run.finished_at)}
          </dd>
        </div>
      </dl>

      {otherActive.length > 0 && (
        <p className="mt-3 text-sm" data-testid="reindex-other-active">
          다른 진행 중 run {otherActive.length}건 — 범위{" "}
          {otherActive.map((item) => scopeLabel(item, notebookTitles)).join(", ")}
        </p>
      )}
      {summary.finishedAtKind === "last_item" && summary.removed > 0 && (
        <p className="mt-2 text-xs text-muted-foreground" data-testid="reindex-finished-note">
          소스 삭제로 끝난 run의 종료 시각은 마지막 항목 갱신 시각이라 실제 종료보다 이를 수 있습니다.
        </p>
      )}
    </>,
  );
}
