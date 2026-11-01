"use client";

import { Info } from "lucide-react";
import { useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import { useSourceFocus } from "@/stores/source-focus";

/** phase-21-design.md §2.2 퀴즈 산출물 스키마. */
export interface QuizItem {
  id: string;
  question: string;
  choices: string[];
  answer_index: number;
  citation?: {
    chunk_id?: string;
    quote?: string;
    source_id?: string;
    source_title?: string;
  };
}

function parseQuiz(raw: string): QuizItem[] | null {
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) return parsed as QuizItem[];
    return null;
  } catch {
    return null;
  }
}

function QuizQuestion({ item, index }: { item: QuizItem; index: number }) {
  const [selected, setSelected] = useState<number | null>(null);
  const requestFocus = useSourceFocus((s) => s.request);
  const graded = selected !== null;
  const isCorrect = selected === item.answer_index;

  return (
    <Card data-testid="quiz-question">
      <CardContent className="flex flex-col gap-3">
        <div className="flex items-start justify-between gap-2">
          <p className="text-sm font-medium">
            {index + 1}. {item.question}
          </p>
          {graded && (
            <Badge
              variant={isCorrect ? "default" : "destructive"}
              data-testid="quiz-result-badge"
            >
              {isCorrect ? "정답" : "오답"}
            </Badge>
          )}
        </div>
        <div className="flex flex-col gap-1.5">
          {item.choices.map((choice, i) => {
            const isAnswer = i === item.answer_index;
            const isSelected = i === selected;
            return (
              <button
                key={i}
                type="button"
                disabled={graded}
                onClick={() => setSelected(i)}
                data-testid="quiz-choice"
                className={cn(
                  "rounded-md border px-3 py-2 text-left text-sm transition-colors",
                  !graded && "hover:bg-muted",
                  graded && isAnswer && "border-primary bg-primary/10",
                  graded && isSelected && !isAnswer && "border-destructive bg-destructive/10",
                )}
              >
                {choice}
              </button>
            );
          })}
        </div>
        {graded && item.citation?.source_title && (
          <Popover>
            <PopoverTrigger
              render={<Button variant="ghost" size="sm" data-testid="quiz-citation-trigger" />}
            >
              <Info className="size-3.5" />
              근거 보기
            </PopoverTrigger>
            <PopoverContent data-testid="quiz-citation-popover">
              <p className="text-xs text-muted-foreground">
                {item.citation.quote}
              </p>
              <button
                type="button"
                className="self-start text-xs text-primary hover:underline"
                onClick={() => {
                  const sourceId = item.citation?.source_id;
                  if (sourceId) requestFocus(sourceId);
                }}
              >
                출처: {item.citation.source_title}
              </button>
            </PopoverContent>
          </Popover>
        )}
      </CardContent>
    </Card>
  );
}

export function QuizViewer({ content }: { content: string }) {
  const items = useMemo(() => parseQuiz(content), [content]);
  if (!items || items.length === 0) {
    return <p className="text-destructive">퀴즈 데이터를 해석하지 못했습니다</p>;
  }
  return (
    <div className="flex flex-col gap-3" data-testid="quiz-viewer">
      {items.map((item, i) => (
        <QuizQuestion key={item.id} item={item} index={i} />
      ))}
    </div>
  );
}
