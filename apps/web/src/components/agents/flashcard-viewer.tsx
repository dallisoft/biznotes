"use client";

import { ChevronLeft, ChevronRight, RotateCcw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useSourceFocus } from "@/stores/source-focus";

/** phase-21-design.md §2.2 플래시카드 산출물 스키마. */
export interface FlashcardItem {
  id: string;
  question: string;
  answer: string;
  citation?: {
    chunk_id?: string;
    quote?: string;
    source_id?: string;
    source_title?: string;
  };
}

function parseFlashcards(raw: string): FlashcardItem[] | null {
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) return parsed as FlashcardItem[];
    return null;
  } catch {
    return null;
  }
}

// 학습 상태(알았음/몰랐음)는 서버 영속화 없이 localStorage에만 저장한다
// (phase-21-design.md §2.5·A-04 — 신규 테이블 회피, 기기 간 동기화 스코프 밖).
type KnownState = Record<string, "known" | "unknown">;

function storageKey(runId: string, artifactName: string): string {
  return `rn:flashcards:${runId}:${artifactName}`;
}

function loadKnownState(key: string): KnownState {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as KnownState) : {};
  } catch {
    return {};
  }
}

export function FlashcardViewer({
  content,
  runId,
  artifactName,
}: {
  content: string;
  runId: string;
  artifactName: string;
}) {
  const cards = useMemo(() => parseFlashcards(content), [content]);
  const key = storageKey(runId, artifactName);
  const [index, setIndex] = useState(0);
  const [flipped, setFlipped] = useState(false);
  const [known, setKnown] = useState<KnownState>({});
  const requestFocus = useSourceFocus((s) => s.request);

  useEffect(() => {
    setKnown(loadKnownState(key));
  }, [key]);

  if (!cards || cards.length === 0) {
    return <p className="text-destructive">플래시카드 데이터를 해석하지 못했습니다</p>;
  }

  const card = cards[Math.min(index, cards.length - 1)];
  const knownCount = Object.values(known).filter((v) => v === "known").length;

  function mark(state: "known" | "unknown") {
    const next = { ...known, [card.id]: state };
    setKnown(next);
    localStorage.setItem(key, JSON.stringify(next));
    goNext();
  }

  function goNext() {
    setFlipped(false);
    setIndex((i) => Math.min(i + 1, cards!.length - 1));
  }

  function goPrev() {
    setFlipped(false);
    setIndex((i) => Math.max(i - 1, 0));
  }

  function reset() {
    setKnown({});
    localStorage.removeItem(key);
    setIndex(0);
    setFlipped(false);
  }

  const sourceId = card.citation?.source_id;

  return (
    <div className="flex flex-col gap-3" data-testid="flashcard-viewer">
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>
          {index + 1} / {cards.length}
        </span>
        <div className="flex items-center gap-2">
          <span data-testid="flashcard-known-count">학습 완료 {knownCount}장</span>
          <Button variant="ghost" size="sm" onClick={reset} data-testid="flashcard-reset">
            <RotateCcw className="size-3.5" />
            초기화
          </Button>
        </div>
      </div>

      <Card
        className="min-h-48 cursor-pointer justify-center"
        onClick={() => setFlipped((f) => !f)}
        data-testid="flashcard-card"
      >
        <CardContent className="flex flex-col items-center gap-3 text-center">
          <Badge variant="outline">{flipped ? "답" : "질문"}</Badge>
          <p className="text-base leading-relaxed">{flipped ? card.answer : card.question}</p>
          {flipped && card.citation?.source_title && (
            <button
              type="button"
              className="text-xs text-primary hover:underline"
              onClick={(e) => {
                e.stopPropagation();
                if (sourceId) requestFocus(sourceId);
              }}
              data-testid="flashcard-source-link"
            >
              출처: {card.citation.source_title}
            </button>
          )}
          {!flipped && (
            <p className="text-xs text-muted-foreground">클릭하여 답 보기</p>
          )}
        </CardContent>
      </Card>

      <div className="flex items-center justify-between gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={goPrev}
          disabled={index === 0}
          data-testid="flashcard-prev"
        >
          <ChevronLeft className="size-3.5" />
          이전
        </Button>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => mark("unknown")}
            data-testid="flashcard-mark-unknown"
          >
            몰랐음
          </Button>
          <Button
            size="sm"
            onClick={() => mark("known")}
            data-testid="flashcard-mark-known"
          >
            알았음
          </Button>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={goNext}
          disabled={index === cards.length - 1}
          data-testid="flashcard-next"
        >
          다음
          <ChevronRight className="size-3.5" />
        </Button>
      </div>
    </div>
  );
}
