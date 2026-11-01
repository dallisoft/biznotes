"use client";

import { Badge } from "@/components/ui/badge";
import type { SkillReport, SkillReportFinding } from "@/lib/skill-types";

/**
 * 네 층 검증 보고서(biznotes-core 표준 계획 D5).
 *
 * - `standard`: 표준 적합성. **거부가 아니라 보고**다 — 위반해도 저장·실행된다.
 * - `portability`: 다른 클라이언트에서의 문제.
 * - `policy`: 제품 정책. 오류가 있으면 가져오기·동결이 거부된다.
 * - `capability`: 이 제품이 실제로 해 줄 수 있는 것.
 */
export function SkillReportPanel({ report }: { report?: SkillReport | null }) {
  if (!report) return null;

  const standardErrors = report.standard?.errors ?? [];
  const portability = report.portability?.warnings ?? [];
  const policyErrors = report.policy?.errors ?? [];
  const policyWarnings = report.policy?.warnings ?? [];
  const capability = report.capability?.warnings ?? [];
  const profile = report.standard?.profile;
  const runtime = report.capability?.runtime_profile;

  return (
    <div className="flex flex-col gap-2 text-xs">
      <div className="flex flex-wrap items-center gap-1.5">
        {profile && (
          <Badge variant={profile === "standard_v1" ? "secondary" : "outline"}>
            {profile === "standard_v1" ? "표준 준수" : "표준 비준수"}
          </Badge>
        )}
        {runtime && <Badge variant="outline">{runtime}</Badge>}
        {report.root_mismatch && (
          <Badge variant="outline" title={report.root_mismatch.message}>
            루트 폴더 이름 다름
          </Badge>
        )}
      </div>

      <ReportList
        title="표준 위반 (보고만 — 저장·실행에는 영향 없음)"
        tone="warning"
        items={standardErrors.map((message, index) => ({ code: `standard-${index}`, message }))}
      />
      <ReportList title="정책 오류 (거부 사유)" tone="error" items={policyErrors} />
      <ReportList title="이식성 경고" tone="muted" items={portability} />
      <ReportList title="정책 경고" tone="muted" items={policyWarnings} />
      <ReportList title="실행 능력" tone="muted" items={capability} />
    </div>
  );
}

const TONE_CLASS = {
  error: "text-destructive",
  warning: "text-warning-text",
  muted: "text-muted-foreground",
} as const;

function ReportList({
  title,
  items,
  tone,
}: {
  title: string;
  items: SkillReportFinding[];
  tone: keyof typeof TONE_CLASS;
}) {
  if (items.length === 0) return null;
  return (
    <div>
      <p className={`font-semibold ${TONE_CLASS[tone]}`}>{title}</p>
      <ul className="ml-4 list-disc">
        {items.map((item) => (
          <li key={`${item.code}-${item.message}`} className={TONE_CLASS[tone]}>
            {item.message}
          </li>
        ))}
      </ul>
    </div>
  );
}
