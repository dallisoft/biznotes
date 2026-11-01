"use client";

import { useMemo, useState } from "react";
import { toast } from "sonner";
import {
  outlineItemSources,
  SlideOutlineSources,
} from "@/components/agents/slide-deck/slide-coverage";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useAgentRuns, useCreateRun } from "@/hooks/use-agents";
import { useStudioCatalog } from "@/hooks/use-studio";
import type { AgentRun } from "@/lib/agent-types";
import { boundedPrettyJson } from "@/lib/mindmap/bounded";
import { continuedDeckRun, continueRequest, SLIDE_FEEDBACK_MAX } from "@/lib/slide-deck/flow";
import { parseSlideOutline } from "@/lib/slide-deck/parse";
import { findStudioAgent, SLIDE_DECK_CARD_ID, studioCardById } from "@/lib/studio-registry";

/**
 * 슬라이드 개요 검토(docs/plans/slide-deck-studio.md AC-5) — 번호 붙은 「제목: 요약」 목록과 의견 입력.
 * 「계속」은 같은 소스·형식·길이·언어·설명으로 덱 run(「슬라이드 자료」)을 만든다. run 생성은 노트북 read
 * 이상이면 된다(현행 규칙). 이미 이 개요로 만든 덱 run이 있으면 입력을 닫는다(D-4).
 * 개요가 읽은 소스(#218)와 장마다 배정한 근거 소스를 함께 보여 준다 — 빠진 소스는 의견으로 고칠 수 있다.
 */
export function SlideOutlineReview({
  content,
  run,
  notebookId,
}: {
  content: string;
  run: AgentRun;
  notebookId: string;
}) {
  const parsed = useMemo(() => parseSlideOutline(content), [content]);
  const [feedback, setFeedback] = useState("");
  const runs = useAgentRuns(notebookId);
  const createRun = useCreateRun(notebookId);
  const studio = useStudioCatalog();
  const deckAgent = studio.isPending ? null : findStudioAgent(studioCardById(SLIDE_DECK_CARD_ID), studio.catalog);
  const continued = continuedDeckRun(run.id, runs.data);

  if (!parsed.ok) {
    return (
      <div className="flex flex-col gap-2" data-testid="slide-outline-fallback">
        <p className="text-sm text-muted-foreground">
          개요 형식이 아니어서 원문을 보여 줍니다{parsed.reason === "invalid_shape" ? ` (${parsed.detail})` : ""}.
        </p>
        <pre className="text-xs whitespace-pre-wrap">
          {"preview" in parsed ? parsed.preview : boundedPrettyJson(content)}
        </pre>
      </div>
    );
  }
  const doc = parsed.doc;
  // 개요가 너무 길면 덱 run 요청을 만들지 않는다(서버 params 상한)
  const request = continueRequest(run, doc, feedback);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!deckAgent || !request) return;
    createRun.mutate(
      { agent_id: deckAgent.id, ...request },
      {
        onSuccess: () => toast.success("슬라이드 자료 생성을 시작했습니다"),
        onError: (err) => toast.error(`실행 실패: ${err.message}`),
      },
    );
  }

  return (
    <div className="flex flex-col gap-3" data-testid="slide-outline-review">
      <h3 className="text-sm font-semibold">{doc.title}</h3>
      <SlideOutlineSources doc={doc} />
      <ol aria-label="슬라이드 개요" className="flex flex-col gap-2.5 text-sm leading-relaxed">
        {doc.slides.map((s, i) => {
          const sources = outlineItemSources(doc, s.source_ids);
          return (
            <li key={i} className="flex gap-2">
              <span className="w-6 shrink-0 text-right tabular-nums text-muted-foreground">{i + 1}.</span>
              <span className="flex min-w-0 flex-col">
                <span>
                  <strong className="font-semibold">{s.title}</strong>
                  {s.summary && <>: {s.summary}</>}
                </span>
                {sources.length > 0 && (
                  <span className="text-xs text-muted-foreground" data-testid="slide-outline-item-sources">
                    근거: {sources.map((src) => src.title).join(" · ")}
                  </span>
                )}
              </span>
            </li>
          );
        })}
      </ol>
      {continued ? (
        <p className="rounded-md bg-muted/60 p-2 text-xs text-muted-foreground" data-testid="slide-outline-continued">
          이 개요로 슬라이드 자료를 만들었습니다. 결과 목록에서 확인하세요.
        </p>
      ) : (
        <form onSubmit={submit} className="flex flex-col gap-2 border-t pt-3">
          <label htmlFor={`outline-feedback-${run.id}`} className="text-xs font-medium">
            개요 의견 (선택)
          </label>
          <Textarea
            id={`outline-feedback-${run.id}`}
            value={feedback}
            maxLength={SLIDE_FEEDBACK_MAX}
            onChange={(e) => setFeedback(e.target.value)}
            placeholder="슬라이드 개요 미세 조정을 위한 의견 추가 — 예: 3번과 4번을 합치고 사례를 하나 더 넣어 줘."
            rows={3}
            data-testid="slide-outline-feedback"
          />
          {!request && (
            <p className="text-xs text-destructive" data-testid="slide-outline-too-long">
              개요가 너무 길어 이어서 만들 수 없습니다. 슬라이드 수를 줄여 개요를 다시 만들어 주세요.
            </p>
          )}
          {!studio.isPending && !deckAgent && (
            <p className="text-xs text-destructive">슬라이드 자료 에이전트를 찾을 수 없습니다</p>
          )}
          <Button
            type="submit"
            size="sm"
            className="self-end"
            disabled={!deckAgent || !request || createRun.isPending}
            data-testid="slide-outline-continue"
          >
            {createRun.isPending ? "시작 중..." : "계속"}
          </Button>
        </form>
      )}
    </div>
  );
}
