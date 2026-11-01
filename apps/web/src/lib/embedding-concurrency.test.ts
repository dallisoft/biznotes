import { describe, expect, it } from "vitest";
import { parseEmbeddingConcurrency } from "./embedding-concurrency";

describe("parseEmbeddingConcurrency", () => {
  it("빈 칸(공백 포함)은 제한 없음(null)이다", () => {
    expect(parseEmbeddingConcurrency("")).toEqual({ ok: true, value: null });
    expect(parseEmbeddingConcurrency("  ")).toEqual({ ok: true, value: null });
  });

  it("1~64 정수를 숫자로 돌려준다", () => {
    expect(parseEmbeddingConcurrency("1")).toEqual({ ok: true, value: 1 });
    expect(parseEmbeddingConcurrency(" 64 ")).toEqual({ ok: true, value: 64 });
  });

  it.each(["0", "65", "-1", "2.5", "abc", "1e2"])("%s는 거부한다", (raw) => {
    expect(parseEmbeddingConcurrency(raw)).toEqual({ ok: false });
  });
});
