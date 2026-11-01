import { afterEach, describe, expect, it, vi } from "vitest";
import {
  bounded,
  boundedPrettyJson,
  exceedsBracketDepth,
  MINDMAP_MAX_BYTES,
  MINDMAP_MAX_DEPTH,
  MINDMAP_PREVIEW_CHARS,
  utf8ByteLength,
} from "@/lib/mindmap/bounded";
import { parseMindmap } from "@/lib/mindmap/parse";

function nested(depth: number): string {
  return `${"[".repeat(depth)}${"]".repeat(depth)}`;
}

afterEach(() => vi.restoreAllMocks());

describe("bounded — 산출물 소비 경계(AC-1.1 (c))", () => {
  it("UTF-8 바이트로 잰다(TextEncoder 길이)", () => {
    expect(utf8ByteLength("abc")).toBe(3);
    expect(utf8ByteLength("가")).toBe(3);
  });

  it("정확히 1 MiB는 통과하고, 다바이트 문자로 1바이트를 넘기면 거부한다", () => {
    const exact = "a".repeat(MINDMAP_MAX_BYTES);
    expect(bounded(exact).kind).toBe("ok");
    // 1 MiB − 2바이트 + 「가」(3바이트) = 1 MiB + 1바이트 — 글자 수로는 상한보다 작다
    const over = "a".repeat(MINDMAP_MAX_BYTES - 2) + "가";
    expect(over.length).toBeLessThan(MINDMAP_MAX_BYTES);
    const r = bounded(over);
    expect(r.kind).toBe("too_large");
    if (r.kind !== "ok") expect(r.preview.length).toBe(MINDMAP_PREVIEW_CHARS);
  });

  it("문자열 밖 괄호 깊이 64는 통과, 65는 거부한다", () => {
    expect(bounded(nested(MINDMAP_MAX_DEPTH)).kind).toBe("ok");
    expect(bounded(nested(MINDMAP_MAX_DEPTH + 1)).kind).toBe("too_deep");
  });

  it("문자열 안 괄호와 이스케이프된 따옴표는 깊이에 세지 않는다", () => {
    const inString = JSON.stringify({ s: "[".repeat(200) + '\\"' + "{".repeat(200) });
    expect(exceedsBracketDepth(inString, MINDMAP_MAX_DEPTH)).toBe(false);
  });

  it("거부된 본문은 JSON.parse를 한 번도 부르지 않는다 — parseMindmap·원본 보기 모두", () => {
    const spy = vi.spyOn(JSON, "parse");
    const deep = nested(MINDMAP_MAX_DEPTH + 1);
    const large = `{"root":{"id":"r","label":"${"가".repeat(MINDMAP_MAX_BYTES / 3 + 1)}"}}`;
    const r1 = parseMindmap(deep);
    const r2 = parseMindmap(large);
    const raw = boundedPrettyJson(deep);
    expect(spy).not.toHaveBeenCalled();
    expect(r1).toMatchObject({ ok: false, reason: "too_deep" });
    expect(r2).toMatchObject({ ok: false, reason: "too_large" });
    expect(raw).toBe(deep);
  });

  it("boundedPrettyJson은 경계 안 JSON을 들여쓰고, JSON이 아니면 원문을 돌려준다", () => {
    expect(boundedPrettyJson('{"a":1}')).toBe('{\n  "a": 1\n}');
    expect(boundedPrettyJson("그냥 글")).toBe("그냥 글");
  });
});
