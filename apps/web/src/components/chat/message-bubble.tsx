"use client";

import { Copy, FileText, RefreshCw, Sparkles } from "lucide-react";
import { MarkdownContent } from "@/components/chat/markdown-content";
import { InsufficientContextBanner } from "@/components/chat/insufficient-context-banner";
import { Button } from "@/components/ui/button";
import type { ChatEvidence, Citation } from "@/lib/chat-types";
import { PENDING_STATE_LABEL, type PendingState } from "@/lib/pending-messages";
import { filterDisplayableItems, toProvenanceBadge } from "@/lib/provenance";
import { useSourceFocus } from "@/stores/source-focus";

export function MessageBubble({
  role,
  content,
  citations,
  insufficientContext,
  evidence = [],
  onAddSource,
  onSaveNote,
  onCopy,
  onRegenerate,
  mindmapPrompt = false,
  pendingState,
  onDismiss,
}: {
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  insufficientContext?: boolean;
  /** [BL-22] 라이브·재조회 공통 — SSE `done.provenance.items` 또는 저장된 provenance.items. */
  evidence?: ChatEvidence[];
  onAddSource?: () => void;
  onSaveNote?: () => void;
  /** 답변 원문 복사. 스트리밍 중인 답변에는 넘기지 않는다. */
  onCopy?: () => void;
  /** 이 답변을 다시 생성한다 — 마지막 답변에만 넘긴다. */
  onRegenerate?: () => void;
  /** 마인드맵 노드 질문이면 「마인드맵 질문」 표시(mindmap-agent-skill AC-2.6) */
  mindmapPrompt?: boolean;
  /** 대기 user 메시지 상태(AC-2.12) */
  pendingState?: PendingState;
  onDismiss?: () => void;
}) {
  const requestSourceFocus = useSourceFocus((st) => st.request);
  if (role === "user") {
    const notice = pendingState ? PENDING_STATE_LABEL[pendingState] : undefined;
    return (
      <div
        className="ml-auto flex max-w-[80%] flex-col items-end gap-1"
        data-testid={pendingState ? "pending-user-message" : undefined}
        data-pending-state={pendingState}
      >
        {mindmapPrompt && (
          <span className="text-xs text-muted-foreground" data-testid="mindmap-prompt-badge">
            마인드맵 질문
          </span>
        )}
        <div
          className={`rounded-2xl rounded-br-md bg-primary px-4 py-2.5 text-sm text-primary-foreground ${
            pendingState === "sending" ? "opacity-80" : ""
          }`}
        >
          <span className="whitespace-pre-wrap">{content}</span>
        </div>
        {notice && (
          <span className="flex items-center gap-2 text-xs text-destructive-text" role="status">
            {notice}
            {onDismiss && (
              <button
                type="button"
                onClick={onDismiss}
                className="rounded px-1 text-muted-foreground underline hover:text-foreground"
                data-testid="pending-dismiss"
              >
                닫기
              </button>
            )}
          </span>
        )}
      </div>
    );
  }
  // BL-17: 성공한 run_skill/mcp 호출은 저장 계층에서 kind="skill"/"mcp" 항목과
  // kind="tool" 항목으로 이중 기록된다 — 화면에는 더 구체적인 항목만 남긴다.
  const displayEvidence = filterDisplayableItems(evidence);
  return (
    <div className="flex flex-col gap-2">
      <div className="flex gap-3">
        <div className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-primary/10">
          <Sparkles className="size-3.5 text-primary" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-sm leading-relaxed">
            <MarkdownContent content={content} citations={citations} />
          </div>
          {displayEvidence.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5 text-xs text-muted-foreground" data-testid="agent-evidence">
              {displayEvidence.map((item, index) => {
                const badge = toProvenanceBadge(item, index);
                const className = badge.failed
                  ? "rounded-full bg-destructive/10 px-2 py-0.5 text-destructive"
                  : "rounded-full bg-primary/10 px-2 py-0.5";
                // 소스 근거는 원문을 바로 열 수 있다 — 우측 패널이 읽기 폭으로 넓어진다(ui-ux-improvements ②)
                if (item.kind === "source" && item.source_id) {
                  const sourceId = item.source_id;
                  return (
                    <button
                      key={badge.key}
                      type="button"
                      data-testid={`evidence-badge-${item.kind}`}
                      aria-label={`${badge.label} 원문 보기`}
                      onClick={() => requestSourceFocus(sourceId)}
                      className={`${className} transition-colors hover:bg-primary/20 focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none coarse:py-2`}
                    >
                      {badge.label}
                    </button>
                  );
                }
                return (
                  <span key={badge.key} data-testid={`evidence-badge-${item.kind}`} className={className}>
                    {badge.label}
                  </span>
                );
              })}
            </div>
          )}
          {(onCopy || onSaveNote || onRegenerate) && (
            // 동작 바는 항상 보인다 — hover로만 드러내면 터치 기기·키보드 사용자가 닿지 못한다(ui-ux-improvements B2)
            <div role="group" aria-label="답변 동작" className="mt-1 flex items-center gap-0.5">
              {onCopy && (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  className="text-muted-foreground coarse:size-11"
                  aria-label="답변 복사"
                  title="복사"
                  onClick={onCopy}
                  data-testid="copy-message"
                >
                  <Copy aria-hidden />
                </Button>
              )}
              {onRegenerate && (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  className="text-muted-foreground coarse:size-11"
                  aria-label="다시 생성"
                  title="다시 생성"
                  onClick={onRegenerate}
                  data-testid="regenerate-message"
                >
                  <RefreshCw aria-hidden />
                </Button>
              )}
              {onSaveNote && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-xs text-muted-foreground coarse:h-11"
                  onClick={onSaveNote}
                  data-testid="save-to-note"
                >
                  <FileText className="size-3" aria-hidden />
                  메모에 저장
                </Button>
              )}
            </div>
          )}
        </div>
      </div>
      {/* [Phase 18b/18c-D3] 저장된 메시지도 token_usage.insufficient_context로 배너를 복원한다.
          onAddSource는 canWrite=false(읽기 전용 공유 노트북)면 undefined — 근거 부족 안내
          자체는 계속 보여주되 소스 추가 버튼만 숨긴다(codex D8). */}
      {insufficientContext && <InsufficientContextBanner onAddSource={onAddSource} />}
    </div>
  );
}
