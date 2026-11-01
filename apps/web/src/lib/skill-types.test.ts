import { describe, expect, it } from "vitest";
import { REVIEW_KIND_LABEL, reviewPendingMessage, VISIBILITY_LABEL } from "@/lib/skill-types";

describe("REVIEW_KIND_LABEL (기준선 #96)", () => {
  it("코어 검토 kind 세 가지에 라벨이 있다", () => {
    expect(Object.keys(REVIEW_KIND_LABEL).sort()).toEqual(["expose_org", "expose_system", "scripts"]);
    expect(REVIEW_KIND_LABEL.scripts).toBe("스크립트 실행 허가");
  });

  it("기본 제공 승격은 「기본 제공」 용어를 쓴다(plan §6.3.1 — 「빌트인」 금지, P5 리뷰 P5-06)", () => {
    expect(REVIEW_KIND_LABEL.expose_system).toBe("기본 제공 승격");
    expect(Object.values(REVIEW_KIND_LABEL).join(" ")).not.toContain("빌트인");
  });
});

describe("VISIBILITY_LABEL (plan §6.3.1)", () => {
  it("공개 범위 system은 「기본 제공」이고 「빌트인」은 쓰지 않는다", () => {
    expect(VISIBILITY_LABEL).toEqual({ private: "비공개", org: "조직 공유", system: "기본 제공" });
    expect(Object.values(VISIBILITY_LABEL).join(" ")).not.toContain("빌트인");
  });
});

describe("reviewPendingMessage (P0-16 — 승인이 실제로 하는 일)", () => {
  it("스크립트 허가는 공개를 말하지 않는다", () => {
    const text = reviewPendingMessage("scripts", "imported");
    expect(text).toContain("스크립트를 실행");
    expect(text).toContain("공개 범위는 바뀌지 않습니다");
  });

  it("authored 스킬의 조직 공개 요청은 카탈로그 등재다(승인해도 공개하지 않는다)", () => {
    const text = reviewPendingMessage("expose_org", "authored");
    expect(text).toContain("채팅 카탈로그에 등재");
    expect(text).not.toBe("승인되면 조직에 공개됩니다.");
  });

  it("가져온 스킬의 조직 공개 요청은 승인 시 공개된다", () => {
    expect(reviewPendingMessage("expose_org", "imported")).toBe("승인되면 조직에 공개됩니다.");
  });

  it("기본 제공 승격과 알 수 없는 kind", () => {
    expect(reviewPendingMessage("expose_system", null)).toBe("승인되면 기본 제공 스킬로 승격되어 모든 사용자에게 공개됩니다.");
    expect(reviewPendingMessage("expose_system", null)).not.toContain("빌트인");
    expect(reviewPendingMessage("future_kind", undefined)).toBe("관리자 승인 뒤에 반영됩니다.");
  });
});
