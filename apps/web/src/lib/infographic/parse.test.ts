import { describe, expect, it } from "vitest";
import { parseInfographic, validateInfographicShape } from "@/lib/infographic/parse";

const VALID = {
  title: "녹차 시장 개요",
  subtitle: "2025년 기준",
  key_stats: [
    { label: "시장 규모", value: "1.2조 원" },
    { label: "성장률", value: 7.5, note: "전년 대비" },
  ],
  sections: [{ heading: "주요 산지", points: ["보성", "하동"] }],
  charts: [
    {
      title: "산지별 생산량",
      unit: "톤",
      data: [
        { label: "보성", value: 1200 },
        { label: "하동", value: 800 },
      ],
    },
  ],
};

describe("validateInfographicShape", () => {
  it("정상 형식은 통과한다", () => {
    expect(validateInfographicShape(VALID)).toBeNull();
  });

  it("subtitle·charts 없는 기존 형식(title·key_stats·sections)도 통과한다", () => {
    expect(
      validateInfographicShape({ title: "t", key_stats: [], sections: [] }),
    ).toBeNull();
  });

  it.each([
    ["배열 최상위", []],
    ["title 누락", { key_stats: [], sections: [] }],
    ["빈 title", { title: " ", key_stats: [], sections: [] }],
    ["key_stats 누락", { title: "t", sections: [] }],
    ["sections 누락", { title: "t", key_stats: [] }],
    ["stat value가 객체", { title: "t", key_stats: [{ label: "a", value: {} }], sections: [] }],
    ["stat label 누락", { title: "t", key_stats: [{ value: "1" }], sections: [] }],
    ["points가 문자열", { title: "t", key_stats: [], sections: [{ heading: "h", points: "p" }] }],
    ["point가 숫자", { title: "t", key_stats: [], sections: [{ heading: "h", points: [1] }] }],
    ["subtitle이 숫자", { title: "t", subtitle: 1, key_stats: [], sections: [] }],
    [
      "chart value가 문자열",
      { ...VALID, charts: [{ title: "c", data: [{ label: "a", value: "10" }] }] },
    ],
    ["chart data가 빈 배열", { ...VALID, charts: [{ title: "c", data: [] }] }],
    [
      "chart value가 무한대",
      { ...VALID, charts: [{ title: "c", data: [{ label: "a", value: Infinity }] }] },
    ],
  ])("%s → 거부", (_name, data) => {
    expect(validateInfographicShape(data)).not.toBeNull();
  });
});

describe("parseInfographic", () => {
  it("순수 JSON을 해석한다", () => {
    const r = parseInfographic(JSON.stringify(VALID));
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.doc.title).toBe("녹차 시장 개요");
      expect(r.doc.key_stats[1].value).toBe("7.5");
      expect(r.doc.charts).toHaveLength(1);
    }
  });

  it("```json 펜스 안의 JSON을 해석한다", () => {
    const r = parseInfographic("결과입니다.\n```json\n" + JSON.stringify(VALID) + "\n```");
    expect(r.ok).toBe(true);
  });

  it("charts가 없으면 빈 배열로 정규화한다", () => {
    const r = parseInfographic(JSON.stringify({ title: "t", key_stats: [], sections: [] }));
    expect(r.ok && r.doc.charts).toEqual([]);
  });

  it("JSON이 아니면 invalid_json", () => {
    expect(parseInfographic("JSON 아님")).toMatchObject({ ok: false, reason: "invalid_json" });
  });

  it("형식 불일치면 invalid_shape", () => {
    expect(parseInfographic(JSON.stringify({ root: {} }))).toMatchObject({
      ok: false,
      reason: "invalid_shape",
    });
  });

  it("깊이 초과 본문은 JSON.parse 없이 too_deep", () => {
    const deep = "[".repeat(80) + "]".repeat(80);
    expect(parseInfographic(deep)).toMatchObject({ ok: false, reason: "too_deep" });
  });
});
