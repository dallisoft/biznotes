"use client";

import { TriangleAlert } from "lucide-react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import type { Citation } from "@/lib/chat-types";
import { useSourceFocus } from "@/stores/source-focus";

/** [n] 인용 마커 칩 — 클릭하면 출처 발췌 팝오버 + 소스 패널로 포커스 이동 */
export function CitationChip({ n, citation }: { n: number; citation: Citation }) {
  const unverified = citation.verified === false;
  const requestFocus = useSourceFocus((s) => s.request);
  return (
    <Popover>
      <PopoverTrigger
        render={
          <button
            type="button"
            title={unverified ? "출처 미확인 인용" : undefined}
            onClick={() => citation.source_id && requestFocus(citation.source_id, citation.quote)}
            className={
              unverified
                ? "mx-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded-sm border border-dashed border-warning/60 bg-warning/10 px-1 align-super text-xs font-semibold text-warning-text hover:bg-warning/20"
                : "mx-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded-sm bg-primary/15 px-1 align-super text-xs font-semibold text-primary hover:bg-primary/25"
            }
          />
        }
      >
        {n}
      </PopoverTrigger>
      <PopoverContent className="w-80 text-sm">
        <p className="mb-1 font-semibold">{citation.source_title}</p>
        {unverified && (
          <p className="mb-1 flex items-center gap-1 text-xs font-medium text-warning-text">
            <TriangleAlert className="size-3 shrink-0" />
            이 인용은 출처 발췌와의 일치가 확인되지 않았습니다
          </p>
        )}
        <p className="line-clamp-6 text-xs text-muted-foreground">
          {citation.quote}
        </p>
      </PopoverContent>
    </Popover>
  );
}
