import { describe, expect, it } from "vitest";
import { catalogStatus, hasRunnableScripts } from "@/lib/skill-catalog";
import type { SkillCatalogEntry } from "@/lib/skill-types";

const entry = (skillId: string, shadowedBy: string | null = null): SkillCatalogEntry => ({
  name: "brief",
  description: "d",
  skill_id: skillId,
  version_id: "v",
  source: "own",
  shadowed_by: shadowedBy,
});

describe("catalogStatus", () => {
  it("등재·가려짐·없음을 구분한다", () => {
    const entries = [entry("a"), entry("b", "a")];
    expect(catalogStatus({ id: "a" }, entries)).toBe("listed");
    expect(catalogStatus({ id: "b" }, entries)).toBe("shadowed");
    expect(catalogStatus({ id: "c" }, entries)).toBeNull();
  });

  it("카탈로그가 꺼져 있으면(목록 없음) 표시하지 않는다", () => {
    expect(catalogStatus({ id: "a" }, undefined)).toBeNull();
  });
});

describe("hasRunnableScripts", () => {
  it("scripts/ 아래 허용 확장자만 실행 가능한 스크립트로 본다", () => {
    expect(hasRunnableScripts(["SKILL.md", "scripts/fill.py"])).toBe(true);
    expect(hasRunnableScripts(["scripts/run.SH"])).toBe(true);
    expect(hasRunnableScripts(["scripts/a.mjs"])).toBe(true);
    expect(hasRunnableScripts(["scripts/notes.txt", "tools/x.py", "SKILL.md"])).toBe(false);
    expect(hasRunnableScripts([])).toBe(false);
  });
});
