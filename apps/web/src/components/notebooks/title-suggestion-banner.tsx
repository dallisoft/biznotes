"use client";

import { Sparkles, X } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { useUpdateNotebook } from "@/hooks/use-notebooks";

function dismissedKey(notebookId: string): string {
  return `nb-title-suggestion-dismissed:${notebookId}`;
}

/** 노트북 제목 자동 제안 배너 — 자동 변경 없이 후보만 제시(phase-22-design.md §3.3). */
export function TitleSuggestionBanner({
  notebookId,
  suggestions,
}: {
  notebookId: string;
  suggestions: string[];
}) {
  const [dismissed, setDismissed] = useState(false);
  const update = useUpdateNotebook();

  useEffect(() => {
    setDismissed(localStorage.getItem(dismissedKey(notebookId)) === "1");
  }, [notebookId]);

  if (dismissed || suggestions.length === 0) return null;

  function dismiss() {
    localStorage.setItem(dismissedKey(notebookId), "1");
    setDismissed(true);
  }

  function applyTitle(title: string) {
    update.mutate(
      { id: notebookId, title },
      {
        onSuccess: () => {
          toast.success("제목을 변경했습니다");
          dismiss();
        },
        onError: (err) => toast.error(`제목 변경 실패: ${err.message}`),
      },
    );
  }

  // 한 줄로 압축한다 — 대화 시작 화면에서 첫 화면을 덜 차지하게(ui-ux-improvements ①)
  return (
    <div
      role="group"
      aria-label="노트북 제목 제안"
      className="flex flex-wrap items-center gap-1.5 rounded-xl border bg-card px-3 py-2 shadow-xs"
      data-testid="title-suggestion-banner"
    >
      <span className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        <Sparkles className="size-3.5 text-primary" aria-hidden />
        제목 제안
      </span>
      {suggestions.map((title) => (
        <Button
          key={title}
          type="button"
          variant="outline"
          size="xs"
          disabled={update.isPending}
          onClick={() => applyTitle(title)}
          data-testid="title-suggestion-option"
          className="coarse:h-11"
        >
          {title}
        </Button>
      ))}
      <button
        type="button"
        onClick={dismiss}
        aria-label="제목 제안 닫기"
        data-testid="title-suggestion-dismiss"
        className="ml-auto rounded-md p-1 text-muted-foreground hover:bg-accent hover:text-foreground coarse:p-3"
      >
        <X className="size-3.5" aria-hidden />
      </button>
    </div>
  );
}
