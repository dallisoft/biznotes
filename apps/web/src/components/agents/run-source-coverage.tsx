"use client";

import { useQuery } from "@tanstack/react-query";
import { fetchArtifact } from "@/hooks/use-agents";
import { useSources } from "@/hooks/use-sources";
import type { AgentRun } from "@/lib/agent-types";
import { formatNumber } from "@/lib/format";
import { COVERAGE_ARTIFACT_NAME, coverageNeedsNotice, parseRunCoverage } from "@/lib/run-coverage";

/**
 * 결과 위의 소스 반영 표시(#217 · core-v0.30.0-upgrade PR-3 AC-14). 근거가 없는 소스나 대체 본문으로 들어간 소스가
 * 있을 때만 「고른 소스 N개 중 M개에서 근거를 찾았습니다」와 그 목록을 보인다. 빠진 소스가 없거나 `coverage.json`을
 * 읽지 못하면 아무것도 그리지 않는다. 제목은 노트북 소스 목록에서 찾고, 지워진 소스는 「삭제된 소스」다.
 */
export function RunSourceCoverage({ run, notebookId }: { run: AgentRun; notebookId: string }) {
  const has = run.artifact_keys.some((a) => a.name === COVERAGE_ARTIFACT_NAME);
  const coverage = useQuery({
    queryKey: ["run-artifact", run.id, COVERAGE_ARTIFACT_NAME],
    queryFn: async () => parseRunCoverage(await fetchArtifact(run.id, COVERAGE_ARTIFACT_NAME)),
    enabled: has,
    staleTime: Infinity,
  });
  const sources = useSources(notebookId);
  const data = coverage.data;
  if (!has || !data || !coverageNeedsNotice(data)) return null;

  const titles = new Map((sources.data ?? []).map((s) => [s.id, s.title]));
  const titleOf = (id: string) => titles.get(id) ?? "삭제된 소스";
  return (
    <div
      className="mx-3 mt-2 shrink-0 rounded-md border border-warning/40 bg-warning/10 p-2 text-xs"
      role="note"
      data-testid="run-source-coverage"
    >
      <p className="font-medium">
        고른 소스 {formatNumber(data.scopeCount)}개 중 {formatNumber(data.hitSources.length)}개에서 근거를 찾았습니다.
        {data.digestSources !== null && ` 요약에는 ${formatNumber(data.digestSources.length)}개가 들어갔습니다.`}
      </p>
      {data.missing.length > 0 && (
        <details className="mt-1" data-testid="run-source-coverage-missing">
          <summary className="cursor-pointer">근거 발췌가 없는 소스 {formatNumber(data.missing.length)}개</summary>
          <ul className="mt-1 list-disc pl-4">
            {data.missing.map((id) => (
              <li key={id}>{titleOf(id)}</li>
            ))}
          </ul>
        </details>
      )}
      {data.degraded.length > 0 && (
        <details className="mt-1" data-testid="run-source-coverage-degraded">
          <summary className="cursor-pointer">
            요약 대신 대체 본문(소스 가이드 요약·앞부분 발췌 등)으로 들어간 소스 {formatNumber(data.degraded.length)}개
          </summary>
          <ul className="mt-1 list-disc pl-4">
            {data.degraded.map((d) => (
              <li key={d.id}>{titles.get(d.id) ?? (d.title || "삭제된 소스")}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
