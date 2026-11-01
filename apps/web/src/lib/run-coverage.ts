/**
 * 실행의 소스 반영 현황(`coverage.json`, #217 · core-v0.30.0-upgrade PR-3).
 *
 * 모든 소스를 반영하는 시드 워크플로우(보고서·브리핑·스터디 가이드·퀴즈·플래시카드·오디오·인포그래픽·샘플)가 실행마다
 * 남긴다. 값은 코어 `rag_search` 커버리지 요약(#446)과 `source_digest` 출력(#448)이다.
 * - `scope_count`: 검색 범위 소스 수(고른 소스 중 실패하지 않은 것)
 * - `hit_sources`: 근거 발췌가 나온 소스 id, `missing`: 범위에 있지만 발췌가 없는 소스 id
 * - `digest_sources`: 요약으로 들어간 소스 id, `degraded`: 요약을 만들지 못해 대체 본문으로 들어간 소스
 * 형식이 맞지 않으면 null이다 — 화면은 표시를 생략한다(결과 본문은 그대로 보인다).
 */
export const COVERAGE_ARTIFACT_NAME = "coverage.json";

export interface DegradedSource {
  id: string;
  title: string;
  reason: string;
}

export interface RunCoverage {
  scopeCount: number;
  hitSources: string[];
  missing: string[];
  digestSources: string[] | null;
  degraded: DegradedSource[];
}

function stringList(value: unknown): string[] | null {
  return Array.isArray(value) && value.every((v) => typeof v === "string") ? value : null;
}

export function parseRunCoverage(text: string): RunCoverage | null {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return null;
  }
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const o = raw as Record<string, unknown>;
  const hitSources = stringList(o.hit_sources);
  const missing = stringList(o.missing);
  if (typeof o.scope_count !== "number" || !hitSources || !missing) return null;
  const degraded = Array.isArray(o.degraded)
    ? o.degraded.flatMap((d): DegradedSource[] =>
        d && typeof d === "object" && typeof (d as DegradedSource).id === "string"
          ? [
              {
                id: (d as DegradedSource).id,
                title: typeof (d as DegradedSource).title === "string" ? (d as DegradedSource).title : "",
                reason: typeof (d as DegradedSource).reason === "string" ? (d as DegradedSource).reason : "",
              },
            ]
          : [],
      )
    : [];
  return {
    scopeCount: o.scope_count,
    hitSources,
    missing,
    digestSources: o.digest_sources === undefined ? null : stringList(o.digest_sources),
    degraded,
  };
}

/** 표시할 것이 있는가 — 근거가 없는 소스나 대체 본문이 있을 때만(#217 「빠진 소스가 없으면 표시하지 않는다」). */
export function coverageNeedsNotice(coverage: RunCoverage): boolean {
  return coverage.missing.length > 0 || coverage.degraded.length > 0;
}

/** 산출물 칩·선택 줄에 보일 산출물 — 반영 현황 파일은 뺀다(결과 위 표시로 보인다). */
export function visibleArtifacts<T extends { artifact_keys: { name: string }[] }>(
  run: T,
): T["artifact_keys"] {
  return run.artifact_keys.filter((a) => a.name !== COVERAGE_ARTIFACT_NAME);
}
