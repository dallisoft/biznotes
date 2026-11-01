"use client";

import { useStudioCatalog } from "@/hooks/use-studio";
import type { Agent } from "@/lib/agent-types";
import { findStudioAgent, VISIBLE_STUDIO_CARDS, type StudioCard } from "@/lib/studio-registry";

/** 바인딩 대상이 없거나 실행할 수 없는 카드의 안내 문구. */
export function studioMissingText(card: StudioCard): string {
  return `${card.label} 에이전트를 찾을 수 없습니다`;
}

/**
 * 에이전트 탭 상단 「스튜디오」 띠(mindmap-agent-skill AC-1.9, D-5 · #180).
 * 카드 클릭은 언제나 생성 다이얼로그를 연다 — 즉시 실행하지 않는다.
 */
export function StudioStrip({ onOpen }: { onOpen: (agent: Agent, card: StudioCard) => void }) {
  // 카드는 시스템 스킬·시스템 에이전트에 묶인다 — 에이전트 패널 탭과 같은 조회 키를 쓴다
  const studio = useStudioCatalog();
  return (
    <section className="shrink-0 border-b px-3 py-2" aria-labelledby="studio-heading" data-testid="studio-strip">
      <h2 id="studio-heading" className="mb-1.5 text-xs font-semibold text-muted-foreground">
        스튜디오
      </h2>
      {/* 컴팩트 타일(아이콘 + 이름, 높이 ~36px) — 좁은 패널(≈280px)에서도 2열 이상. 설명·비활성 안내는 툴팁·스크린리더로 둔다.
          카드가 늘어도 「만들기」 갤러리가 높이를 잃지 않게 띠는 최대 높이를 넘으면 세로 스크롤한다(#180). */}
      <ul
        className="grid max-h-[8.5rem] gap-1.5 overflow-y-auto [grid-template-columns:repeat(auto-fill,minmax(112px,1fr))]"
        data-testid="studio-strip-list"
      >
        {VISIBLE_STUDIO_CARDS.map((card) => {
          const agent = studio.isPending ? null : findStudioAgent(card, studio.catalog);
          const missing = !studio.isPending && agent === null;
          const text = missing ? studioMissingText(card) : card.description;
          return (
            <li key={card.id}>
              <button
                type="button"
                disabled={agent === null}
                onClick={() => agent && onOpen(agent, card)}
                className="flex h-9 w-full items-center gap-1.5 rounded-md border bg-accent/40 px-2 text-left transition-colors hover:bg-accent disabled:cursor-not-allowed disabled:opacity-60"
                title={text}
                aria-describedby={`studio-card-desc-${card.id}`}
                data-testid={`studio-card-${card.id}`}
              >
                <span aria-hidden className="text-base">
                  {card.icon}
                </span>
                <span className="truncate text-xs font-medium">{card.label}</span>
              </button>
              <span id={`studio-card-desc-${card.id}`} className="sr-only">
                {text}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
