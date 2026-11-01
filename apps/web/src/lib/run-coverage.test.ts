import { describe, expect, it } from "vitest";
import { coverageNeedsNotice, parseRunCoverage, visibleArtifacts } from "@/lib/run-coverage";

describe("parseRunCoverage — 실행의 소스 반영 현황(#217)", () => {
  it("코어 커버리지·요약 출력을 읽는다", () => {
    const c = parseRunCoverage(
      JSON.stringify({
        scope_count: 3,
        hit_sources: ["a", "b"],
        missing: ["c"],
        digest_sources: ["a", "b", "c"],
        degraded: [{ id: "c", title: "C", reason: "generation_failed", fallback: "excerpt" }],
      }),
    );
    expect(c).toEqual({
      scopeCount: 3,
      hitSources: ["a", "b"],
      missing: ["c"],
      digestSources: ["a", "b", "c"],
      degraded: [{ id: "c", title: "C", reason: "generation_failed" }],
    });
    expect(coverageNeedsNotice(c!)).toBe(true);
  });

  it("digest가 없는 문항형은 digestSources가 null이고, 빠진 것이 없으면 표시하지 않는다", () => {
    const c = parseRunCoverage(JSON.stringify({ scope_count: 2, hit_sources: ["a", "b"], missing: [] }));
    expect(c?.digestSources).toBeNull();
    expect(c?.degraded).toEqual([]);
    expect(coverageNeedsNotice(c!)).toBe(false);
  });

  it.each(["not json", "[]", JSON.stringify({ scope_count: "3", hit_sources: [], missing: [] }), JSON.stringify({ scope_count: 1, hit_sources: [1], missing: [] })])(
    "형식이 틀리면 null — %s",
    (text) => {
      expect(parseRunCoverage(text)).toBeNull();
    },
  );

  it("visibleArtifacts는 coverage.json을 뺀다", () => {
    const run = { artifact_keys: [{ name: "report.md" }, { name: "coverage.json" }] };
    expect(visibleArtifacts(run).map((a) => a.name)).toEqual(["report.md"]);
  });
});
