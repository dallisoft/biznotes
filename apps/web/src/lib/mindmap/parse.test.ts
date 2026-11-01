/**
 * 관용 파서·형태 판정·절단·정화(AC-1.1). 공유 fixture(`lib/__fixtures__/mindmap/`)는
 * `apps/api/tests/test_mindmap_contract.py`가 코어 판정기로도 읽는다 — 판정이 하나라도 다르면 양쪽 중 한쪽이 실패한다.
 */
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import {
  countMindmapNodes,
  MINDMAP_NODE_CAP,
  parseLenientJson,
  parseMindmap,
  sanitizeLabel,
  serializeMindmap,
  truncateMindmapBfs,
  validateMindmapShape,
} from "@/lib/mindmap/parse";

const DIR = path.resolve(__dirname, "../__fixtures__/mindmap");
const files = readdirSync(DIR).filter((f) => f.endsWith(".json"));
const read = (f: string): unknown => JSON.parse(readFileSync(path.join(DIR, f), "utf8"));

const valid = files.filter((f) => f.startsWith("valid-"));
const invalid = files.filter((f) => f.startsWith("invalid-"));
const fenced = files.filter((f) => f.startsWith("fenced-"));

describe("공유 fixture 판정 — 코어 _validate_mindmap_shape와 같다", () => {
  it("fixture 수: valid ≥ 5, invalid ≥ 6, fenced ≥ 1", () => {
    expect(valid.length).toBeGreaterThanOrEqual(5);
    expect(invalid.length).toBeGreaterThanOrEqual(6);
    expect(fenced.length).toBeGreaterThanOrEqual(1);
  });

  it.each(valid)("%s 통과", (f) => {
    expect(validateMindmapShape(read(f))).toBeNull();
    expect(parseMindmap(readFileSync(path.join(DIR, f), "utf8")).ok).toBe(true);
  });

  it.each(invalid)("%s 거부", (f) => {
    expect(validateMindmapShape(read(f))).not.toBeNull();
    expect(parseMindmap(readFileSync(path.join(DIR, f), "utf8"))).toMatchObject({
      ok: false,
      reason: "invalid_shape",
    });
  });

  it.each(fenced)("%s — 펜스·설명 문장이 붙은 본문 문자열을 해석한다", (f) => {
    const { body, root_label } = read(f) as { body: string; root_label: string };
    const r = parseMindmap(body);
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.doc.root.title).toBe(root_label);
  });

  it("version 없는 문서도 연다(코어도 통과)", () => {
    const r = parseMindmap(readFileSync(path.join(DIR, "valid-no-version.json"), "utf8"));
    expect(r.ok && r.doc.version).toBeUndefined();
    expect(r.ok && r.doc.root.children).toEqual([]);
  });
});

describe("BFS 절단 — 코어 _truncate_mindmap_bfs와 같은 결과", () => {
  it("truncate-input을 절단한 결과가 코어가 만든 truncate-expected와 같다", () => {
    const input = read("truncate-input.json") as { root: Record<string, unknown> };
    const expected = read("truncate-expected.json") as { root: unknown; truncated: boolean };
    expect(countMindmapNodes(input.root)).toBeGreaterThan(MINDMAP_NODE_CAP);
    const [root, truncated] = truncateMindmapBfs(structuredClone(input.root), MINDMAP_NODE_CAP);
    expect(root).toEqual(expected.root);
    expect(truncated).toBe(expected.truncated);
    expect(countMindmapNodes(root)).toBe(MINDMAP_NODE_CAP);
  });

  it("parseMindmap은 200개를 넘는 트리를 절단하고 truncated를 켠다", () => {
    const r = parseMindmap(readFileSync(path.join(DIR, "truncate-input.json"), "utf8"));
    expect(r.ok && r.doc.truncated).toBe(true);
  });

  it("코어가 이미 표시한 truncated는 그대로 둔다", () => {
    const r = parseMindmap(JSON.stringify({ root: { id: "r", label: "x" }, truncated: true }));
    expect(r.ok && r.doc.truncated).toBe(true);
  });
});

describe("관용 해석·정화", () => {
  it("JSON이 아니면 invalid_json", () => {
    expect(parseMindmap("이것은 JSON이 아닙니다")).toMatchObject({ ok: false, reason: "invalid_json" });
    expect(parseLenientJson("{ 깨진")).toEqual({ ok: false });
  });

  it("라벨 정화: Cc·Cf 제거, 공백·줄바꿈 접기, 표시 120자(원문은 title)", () => {
    expect(sanitizeLabel("  앞\n\t뒤\u202e\u0000끝\u200b ")).toBe("앞 뒤끝");
    const long = "가".repeat(150);
    const r = parseMindmap(JSON.stringify({ root: { id: "r", label: long } }));
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(Array.from(r.doc.root.label)).toHaveLength(120);
      expect(r.doc.root.label.endsWith("…")).toBe(true);
      expect(r.doc.root.title).toBe(long);
    }
  });

  it("source_ref.source_id는 문자열일 때만 남긴다", () => {
    const r = parseMindmap(readFileSync(path.join(DIR, "valid-source-ref.json"), "utf8"));
    expect(r.ok).toBe(true);
    if (r.ok) {
      const [a, b, c] = r.doc.root.children;
      expect(a.source_ref).toEqual({
        source_id: "11111111-1111-1111-1111-111111111111",
        chunk_id: "c1",
      });
      expect(b.source_ref).toBeUndefined();
      expect(c.source_ref).toBeUndefined();
    }
  });

  it("serializeMindmap은 정화한 전체 라벨로 정규화본을 만든다", () => {
    const r = parseMindmap(readFileSync(path.join(DIR, "valid-unicode-label.json"), "utf8"));
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    const out = JSON.parse(serializeMindmap(r.doc));
    expect(out.version).toBe(1);
    expect(out.truncated).toBe(false);
    expect(out.root.label).toBe("앞뒤 공백 줄바꿈뒤집기");
    expect(validateMindmapShape(out)).toBeNull();
  });
});
