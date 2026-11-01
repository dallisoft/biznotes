"use client";

import { RefreshCw, Sparkles } from "lucide-react";
import { useId, useState } from "react";
import { ConnectCards } from "@/components/chat/connect-cards";
import { TitleSuggestionBanner } from "@/components/notebooks/title-suggestion-banner";
import type { NotebookOverview } from "@/hooks/use-overview";
import { formatNumber } from "@/lib/format";

/**
 * 메시지가 하나도 없을 때의 화면.
 *
 * 소스 요약(overview)이 있으면 요약 카드 + 추천 질문을, 없으면 안내 문구를 보여준다.
 * 「소스 추가하기」 카드(ConnectCards)는 소스가 하나도 없을 때만 붙인다(ui-ux-improvements ① — 소스가 있으면
 * 대화가 주인공이다). 쓰기 권한이 없으면(읽기 전용 공유) 소스를 추가할 수 없으므로(서버 403) 그리지 않는다.
 */

/** 이 길이를 넘거나 줄바꿈이 있으면 요약을 2줄로 접고 「더 보기」를 둔다. */
const SUMMARY_COLLAPSE_CHARS = 80;
export function ChatEmptyState({
  notebookId,
  overview,
  isStreaming,
  regenerating,
  onRegenerate,
  onAskQuestion,
  canWrite = true,
  sourceCount = 0,
}: {
  notebookId: string;
  overview?: NotebookOverview;
  isStreaming: boolean;
  regenerating: boolean;
  onRegenerate: () => void;
  onAskQuestion: (question: string) => void;
  /** 쓰기 권한(`permission !== "read"`) — 없으면 「소스 추가하기」 카드를 숨긴다. */
  canWrite?: boolean;
  /** 노트북의 소스 수 — 1개 이상이면 「소스 추가하기」 카드를 숨긴다. */
  sourceCount?: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const summaryId = useId();
  const showConnect = canWrite && sourceCount === 0;

  if (!overview?.summary) {
    return (
      <div className="flex flex-col gap-6 pt-16">
        <div className="flex flex-col items-center justify-center gap-3 text-center text-sm text-muted-foreground">
          <div className="flex size-12 items-center justify-center rounded-2xl bg-primary/10">
            <Sparkles className="size-6 text-primary" />
          </div>
          <p className="text-base font-medium text-foreground">이 노트북에서 무엇을 작업할까요?</p>
          <p>
            {canWrite
              ? "소스를 추가한 뒤, 출처 인용 [n]과 함께 답변을 받아보세요."
              : "이 노트북의 소스에 대해 질문하면 출처 인용 [n]과 함께 답변을 받을 수 있습니다."}
          </p>
          {overview?.stale && <p className="text-xs">소스 요약을 생성하는 중…</p>}
        </div>
        {showConnect && <ConnectCards notebookId={notebookId} />}
      </div>
    );
  }

  const collapsible =
    overview.summary.length > SUMMARY_COLLAPSE_CHARS || overview.summary.includes("\n");

  return (
    <div className="flex flex-col gap-4 pt-4" data-testid="overview-card">
      {(overview.title_suggestions?.length ?? 0) > 0 && (
        <TitleSuggestionBanner
          notebookId={notebookId}
          suggestions={overview.title_suggestions}
        />
      )}
      <div className="rounded-2xl border bg-card p-4 shadow-xs">
        <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold">
          <Sparkles className="size-4 text-primary" />
          소스 요약
          {(overview.source_count ?? 0) > 0 && (
            <span className="text-xs font-normal text-muted-foreground" data-testid="overview-source-count">
              소스 {formatNumber(overview.source_count ?? 0)}개 기준
            </span>
          )}
          <button
            type="button"
            onClick={onRegenerate}
            disabled={overview.stale || regenerating}
            title="요약 다시 생성"
            aria-label="요약 다시 생성"
            data-testid="overview-regenerate"
            className="ml-auto rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:pointer-events-none disabled:opacity-50"
          >
            <RefreshCw
              className={`size-3.5 ${overview.stale || regenerating ? "animate-spin" : ""}`}
            />
          </button>
        </h3>
        <p
          id={summaryId}
          data-testid="overview-summary"
          className={`whitespace-pre-wrap text-sm leading-relaxed ${collapsible && !expanded ? "line-clamp-2" : ""}`}
        >
          {overview.summary}
        </p>
        {collapsible && (
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls={summaryId}
            onClick={() => setExpanded((v) => !v)}
            className="mt-1 rounded-sm text-xs font-medium text-primary hover:underline focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none coarse:py-2"
          >
            {expanded ? "접기" : "더 보기"}
          </button>
        )}
        {overview.stale && (
          <p className="mt-2 text-xs text-muted-foreground">요약을 갱신하는 중…</p>
        )}
      </div>
      {overview.suggested_questions.length > 0 && (
        <div className="flex flex-col gap-2">
          <h4 className="text-xs font-medium text-muted-foreground">추천 질문</h4>
          <div className="flex flex-wrap gap-2" data-testid="suggested-questions">
            {overview.suggested_questions.map((q) => (
              <button
                key={q}
                type="button"
                onClick={() => onAskQuestion(q)}
                disabled={isStreaming}
                className="rounded-full border bg-card px-3.5 py-2 text-left text-xs shadow-xs transition-colors hover:border-primary/40 hover:bg-accent"
              >
                {q}
              </button>
            ))}
          </div>
        </div>
      )}
      {showConnect && <ConnectCards notebookId={notebookId} />}
    </div>
  );
}
