"use client";

import {
  type SlideCoverage,
  type SlideDegradedSource,
  type SlideDigestSource,
  type SlideOutlineDoc,
  type SlideSourceRef,
  unassignedOutlineSources,
} from "@/lib/slide-deck/parse";
import { cn } from "@/lib/utils";

/**
 * 슬라이드 자료의 소스 반영 표시(#218 수락 기준, #217) — 개요는 고른 소스를 모두 요약으로 읽는다(누락 0).
 * 그래도 사람이 확인할 것은 셋이다: 요약 대신 대체 본문으로 들어간 소스, 어느 장에도 배정·인용되지 않은 소스,
 * 만들지 못해 뺀 장. 덱은 산출물 하나(`coverage`)로, 개요 검토 화면은 개요 산출물(`sources`·`degraded`)로 그린다.
 * 소스 요약을 만든 모델(#239)은 「요약: 모델 N개(캐시)」로 덧붙인다 — 확인용 정보라 경고 색에 넣지 않는다.
 * 소스 제목·모델 이름은 텍스트 노드로만 출력한다.
 */

const DEGRADED_REASON: Record<string, string> = {
  generation_failed: "요약 생성 실패",
  no_content: "본문 없음",
};

function degradedReason(s: SlideDegradedSource): string {
  return DEGRADED_REASON[s.reason] ?? "요약 없음";
}

function SourceList({
  label,
  testId,
  items,
}: {
  label: string;
  testId: string;
  items: { key: string; text: string }[];
}) {
  if (items.length === 0) return null;
  return (
    <details className="text-xs" data-testid={testId}>
      <summary className="cursor-pointer select-none font-medium">
        {label} {items.length}개
      </summary>
      <ul className="mt-1 flex list-disc flex-col gap-0.5 pl-5 text-muted-foreground">
        {items.map((item) => (
          <li key={item.key} className="break-words">
            {item.text}
          </li>
        ))}
      </ul>
    </details>
  );
}

function degradedItems(degraded: SlideDegradedSource[]) {
  return degraded.map((s) => ({ key: s.id, text: `${s.title} (${degradedReason(s)})` }));
}

function refItems(refs: SlideSourceRef[]) {
  return refs.map((s) => ({ key: s.id, text: s.title }));
}

/**
 * 「요약: 모델 N개(캐시)」 — 요약을 만든 모델별 소스 수(많은 순)와 캐시 여부(#239). 모두 캐시면 「(캐시)」, 일부면
 * 「(캐시 N)」이다. 모델이 없는 소스(대체 본문)는 세지 않는다 — 「요약 대신 원문 일부로 읽은 소스」가 따로 보인다.
 */
export function digestModelSummary(digest: SlideDigestSource[]): string | null {
  const groups = new Map<string, { total: number; cached: number }>();
  for (const s of digest) {
    if (!s.model) continue;
    const g = groups.get(s.model) ?? { total: 0, cached: 0 };
    g.total += 1;
    if (s.cached) g.cached += 1;
    groups.set(s.model, g);
  }
  if (groups.size === 0) return null;
  const parts = [...groups]
    .sort((a, b) => b[1].total - a[1].total)
    .map(([model, g]) => {
      const note = g.cached === 0 ? "" : g.cached === g.total ? "(캐시)" : `(캐시 ${g.cached})`;
      return `${model} ${g.total}개${note}`;
    });
  return `요약: ${parts.join(" · ")}`;
}

/** 요약 모델 줄 — 펼치면 소스마다 「제목 — 모델(캐시)」. 모델 정보가 없는 옛 산출물은 그리지 않는다. */
function DigestModels({ digest, testId }: { digest: SlideDigestSource[] | undefined; testId: string }) {
  const summary = digest ? digestModelSummary(digest) : null;
  if (!digest || !summary) return null;
  return (
    <details className="text-xs" data-testid={testId}>
      <summary className="cursor-pointer select-none font-medium">{summary}</summary>
      <ul className="mt-1 flex list-disc flex-col gap-0.5 pl-5 text-muted-foreground">
        {digest.map((s) => (
          <li key={s.id} className="break-words">
            {s.title} — {s.model ? `${s.model}${s.cached ? "(캐시)" : ""}` : "요약 없음"}
          </li>
        ))}
      </ul>
    </details>
  );
}

function Summary({ ok, children, testId }: { ok: boolean; children: React.ReactNode; testId: string }) {
  return (
    <section
      aria-label="소스 반영"
      data-testid={testId}
      className={cn(
        "flex flex-col gap-1.5 rounded-md p-2 text-xs",
        ok ? "bg-muted/60 text-muted-foreground" : "border border-warning/30 bg-warning/10 text-warning-text dark:bg-warning/15",
      )}
    >
      {children}
    </section>
  );
}

/** 덱 위 반영 요약 — 고른 소스 수, 대체 본문·미반영 소스·만들지 못한 장. */
export function SlideDeckCoverage({ coverage }: { coverage: SlideCoverage }) {
  const { sources, degraded, uncovered, failed } = coverage;
  if (sources.length === 0 && failed.length === 0) return null;
  const ok = degraded.length === 0 && uncovered.length === 0 && failed.length === 0;
  return (
    <Summary ok={ok} testId="slide-deck-coverage">
      <p data-testid="slide-coverage-summary">
        {uncovered.length === 0
          ? `고른 소스 ${sources.length}개를 모두 장에 반영했습니다.`
          : `고른 소스 ${sources.length}개 중 ${sources.length - uncovered.length}개를 장에 반영했습니다.`}
      </p>
      <DigestModels digest={coverage.digest} testId="slide-coverage-digest" />
      <SourceList label="어느 장에도 쓰이지 않은 소스" testId="slide-coverage-uncovered" items={refItems(uncovered)} />
      <SourceList
        label="요약 대신 원문 일부로 읽은 소스"
        testId="slide-coverage-degraded"
        items={degradedItems(degraded)}
      />
      <SourceList
        label="만들지 못한 장"
        testId="slide-coverage-failed"
        items={failed.map((f) => ({ key: String(f.n), text: f.title ? `${f.n}. ${f.title}` : `${f.n}번째 장` }))}
      />
    </Summary>
  );
}

/** 장의 근거 소스 한 줄 — 목록·확대 보기의 슬라이드 아래. 근거가 없으면 그리지 않는다. */
export function SlideSourcesLine({ sources }: { sources: SlideSourceRef[] | undefined }) {
  if (!sources || sources.length === 0) return null;
  return (
    <p className="px-1 text-xs text-muted-foreground" data-testid="slide-sources">
      근거: {sources.map((s) => s.title).join(" · ")}
    </p>
  );
}

/** 개요 검토 화면의 소스 요약 — 개요가 읽은 소스 수, 대체 본문, 어느 장에도 배정되지 않은 소스. */
export function SlideOutlineSources({ doc }: { doc: SlideOutlineDoc }) {
  if (doc.sources.length === 0) return null;
  const unassigned = unassignedOutlineSources(doc);
  const ok = doc.degraded.length === 0 && unassigned.length === 0;
  return (
    <Summary ok={ok} testId="slide-outline-sources">
      <p>고른 소스 {doc.sources.length}개를 모두 요약으로 읽고 개요를 만들었습니다.</p>
      <DigestModels digest={doc.digest} testId="slide-outline-digest" />
      <SourceList
        label="요약 대신 원문 일부로 읽은 소스"
        testId="slide-outline-degraded"
        items={degradedItems(doc.degraded)}
      />
      <SourceList label="어느 장에도 배정되지 않은 소스" testId="slide-outline-unassigned" items={refItems(unassigned)} />
    </Summary>
  );
}

/** 개요 장의 근거 소스 제목들 — 개요가 읽은 소스 목록에 있는 id만. */
export function outlineItemSources(doc: SlideOutlineDoc, ids: string[] | undefined): SlideSourceRef[] {
  if (!ids || ids.length === 0) return [];
  const byId = new Map(doc.sources.map((s) => [s.id, s]));
  return ids.flatMap((id) => {
    const s = byId.get(id);
    return s ? [s] : [];
  });
}
