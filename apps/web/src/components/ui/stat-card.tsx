import Link from "next/link";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

/**
 * 지표 카드(§6.5) — 모니터링 화면의 지역 구현을 옮겼다(렌더 결과 동일). 값·보조 문구는 호출부가 포맷해 넘긴다
 * (`lib/format.ts`). `valueTitle`은 값이 축약됐을 때의 정확값(예: 「< $0.01」 → `formatCurrencyUSDTitle`의 「$0.004」)으로
 * 값 요소의 `title`이 된다. 링크(선택)는 카드 아래 텍스트 링크로 붙는다.
 */
export function StatCard({
  label,
  value,
  valueTitle,
  hint,
  href,
  linkLabel = "자세히 보기",
}: {
  label: string;
  value: string;
  /** 축약 표시의 정확값 — 값 요소의 `title`(없으면 속성 없음) */
  valueTitle?: string;
  hint?: string;
  href?: string;
  linkLabel?: string;
}) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription>{label}</CardDescription>
        <CardTitle className="text-2xl tabular-nums" title={valueTitle}>
          {value}
        </CardTitle>
      </CardHeader>
      {hint && <CardContent className="pt-0 text-xs text-muted-foreground">{hint}</CardContent>}
      {href && (
        <CardContent className="pt-0 text-sm">
          {/* 같은 문구의 링크가 여러 카드에 있어도 구분되게 카드 라벨을 이름에 붙인다(보이는 문구로 시작 — WCAG 2.5.3). */}
          <Link
            href={href}
            aria-label={`${linkLabel} — ${label}`}
            className="rounded-sm text-primary underline-offset-4 hover:underline focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none coarse:inline-flex coarse:min-h-11 coarse:items-center"
          >
            {linkLabel}
          </Link>
        </CardContent>
      )}
    </Card>
  );
}
