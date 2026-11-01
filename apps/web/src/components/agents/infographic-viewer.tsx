"use client";

import { useMemo } from "react";
import { Card, CardContent } from "@/components/ui/card";
import {
  parseInfographic,
  type InfographicChart,
  type InfographicDoc,
} from "@/lib/infographic/parse";
import { boundedPrettyJson } from "@/lib/mindmap/bounded";

/**
 * 인포그래픽 산출물 뷰어(#181) — 제목·핵심 수치 카드·섹션·막대 차트(순수 CSS).
 * 형식이 맞지 않거나 JSON이 깨졌으면 안내 문구와 함께 원문 JSON으로 대체한다.
 */

const NUMBER_FORMAT = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 });

/** 단위는 숫자에 붙인다 — 「42%」·「405잔」·「1,200톤」(한국어 표기). */
function formatValue(value: number, unit?: string): string {
  const text = NUMBER_FORMAT.format(value);
  return unit ? `${text}${unit}` : text;
}

function ChartBlock({ chart }: { chart: InfographicChart }) {
  const maxAbs = Math.max(...chart.data.map((d) => Math.abs(d.value)));
  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-sm font-semibold">{chart.title}</h3>
      <ul aria-label={chart.title} className="flex flex-col gap-1.5">
        {chart.data.map((d, i) => {
          const pct = maxAbs > 0 ? (Math.abs(d.value) / maxAbs) * 100 : 0;
          const valueText = formatValue(d.value, chart.unit);
          return (
            <li
              key={`${d.label}-${i}`}
              aria-label={`${d.label}: ${valueText}`}
              className="grid grid-cols-[minmax(4rem,8rem)_1fr_auto] items-center gap-2 text-xs"
            >
              <span className="truncate" title={d.label}>
                {d.label}
              </span>
              <span className="h-3 overflow-hidden rounded-sm bg-muted">
                <span
                  data-testid="infographic-bar"
                  aria-hidden="true"
                  className={
                    d.value < 0 ? "block h-full bg-destructive/70" : "block h-full bg-primary/80"
                  }
                  style={{ width: `${Math.round(pct * 100) / 100}%` }}
                />
              </span>
              <span className="tabular-nums text-muted-foreground">{valueText}</span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function InfographicBody({ doc }: { doc: InfographicDoc }) {
  const empty = doc.key_stats.length === 0 && doc.sections.length === 0 && doc.charts.length === 0;
  return (
    <article className="flex flex-col gap-4" data-testid="infographic-viewer">
      <header className="flex flex-col gap-1">
        <h2 className="text-lg font-bold leading-tight">{doc.title}</h2>
        {doc.subtitle && <p className="text-sm text-muted-foreground">{doc.subtitle}</p>}
      </header>

      {empty && (
        <p className="text-sm text-muted-foreground" data-testid="infographic-empty">
          표시할 수치나 항목이 없습니다
        </p>
      )}

      {doc.key_stats.length > 0 && (
        <section aria-label="핵심 수치">
          <ul className="grid grid-cols-[repeat(auto-fill,minmax(9rem,1fr))] gap-2">
            {doc.key_stats.map((s, i) => (
              <li key={`${s.label}-${i}`} data-testid="infographic-stat">
                <Card size="sm" className="h-full">
                  <CardContent className="flex flex-col gap-0.5">
                    <span className="text-2xl font-bold tabular-nums text-primary break-words">
                      {s.value}
                    </span>
                    <span className="text-xs font-medium">{s.label}</span>
                    {s.note && <span className="text-xs text-muted-foreground">{s.note}</span>}
                  </CardContent>
                </Card>
              </li>
            ))}
          </ul>
        </section>
      )}

      {doc.charts.map((c, i) => (
        <ChartBlock key={`${c.title}-${i}`} chart={c} />
      ))}

      {doc.sections.map((s, i) => (
        <section key={`${s.heading}-${i}`} className="flex flex-col gap-1.5 border-l-2 border-primary/40 pl-3">
          <h3 className="text-sm font-semibold">{s.heading}</h3>
          {s.points.length > 0 && (
            <ul className="list-disc space-y-1 pl-4 text-sm">
              {s.points.map((p, j) => (
                <li key={j}>{p}</li>
              ))}
            </ul>
          )}
        </section>
      ))}
    </article>
  );
}

export function InfographicViewer({ content }: { content: string }) {
  const parsed = useMemo(() => parseInfographic(content), [content]);
  if (parsed.ok) return <InfographicBody doc={parsed.doc} />;
  const rejected = parsed.reason === "too_large" || parsed.reason === "too_deep";
  return (
    <div className="flex flex-col gap-2" data-testid="infographic-fallback">
      <p className="text-xs text-muted-foreground" role="status">
        {rejected
          ? "인포그래픽 데이터가 너무 커서 앞부분 원문만 표시합니다"
          : "인포그래픽 형식이 아니어서 원문을 표시합니다"}
      </p>
      <pre
        className="whitespace-pre-wrap text-sm leading-relaxed"
        data-testid="infographic-raw"
      >
        {"preview" in parsed ? parsed.preview : boundedPrettyJson(content)}
      </pre>
    </div>
  );
}
