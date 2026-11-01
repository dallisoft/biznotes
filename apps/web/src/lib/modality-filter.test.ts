import { describe, expect, it } from "vitest";
import { countModalities, filterByModalities } from "./modality-filter";

const models = [
  { name: "a", capabilities: ["llm"] },
  { name: "b", capabilities: ["llm", "vlm"] },
  { name: "c", capabilities: ["embedding"] },
  { name: "d", capabilities: ["image"] },
  { name: "e", capabilities: ["video", "video"] },
  { name: "f", capabilities: ["reranker"] },
];

describe("countModalities", () => {
  it("알려진 모달리티 순서 뒤에 미지 능력을 붙이고 모델 단위로 센다", () => {
    expect(countModalities(models)).toEqual([
      { modality: "llm", count: 2 },
      { modality: "vlm", count: 1 },
      { modality: "embedding", count: 1 },
      { modality: "reranker", count: 1 },
      { modality: "image", count: 1 },
      { modality: "video", count: 1 },
    ]);
  });

  it("빈 목록이면 빈 배열", () => {
    expect(countModalities([])).toEqual([]);
  });
});

describe("filterByModalities", () => {
  it("선택이 없으면 전체를 반환한다", () => {
    expect(filterByModalities(models, [])).toBe(models);
  });

  it("선택한 능력 중 하나라도 가진 모델만 남긴다", () => {
    expect(filterByModalities(models, ["vlm", "image"]).map((m) => m.name)).toEqual(["b", "d"]);
    expect(filterByModalities(models, ["llm"]).map((m) => m.name)).toEqual(["a", "b"]);
    expect(filterByModalities(models, ["reranker"]).map((m) => m.name)).toEqual(["f"]);
  });
});
