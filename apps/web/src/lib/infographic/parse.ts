/**
 * 인포그래픽 산출물(`infographic.json`, #181) 해석 — 뷰어 입력 형식의 단일 정의.
 *
 * 형식(시드 워크플로우 「인포그래픽 데이터」 프롬프트와 같다 — test_seed_infographic.py가 대조):
 *   { title: str(비어있지 않음), subtitle?: str,
 *     key_stats: [{ label: str, value: str | number, note?: str }],
 *     sections: [{ heading: str, points: [str] }],
 *     charts?: [{ title: str, unit?: str, data: [{ label: str, value: number }](1개 이상) }] }
 * 정의되지 않은 키는 무시한다. 경계(bounded) → 관용 해석(펜스) → 형태 판정 → 정규화 순이다.
 */
import { bounded } from "@/lib/mindmap/bounded";
import { parseLenientJson } from "@/lib/mindmap/parse";

export interface InfographicStat {
  label: string;
  value: string;
  note?: string;
}

export interface InfographicSection {
  heading: string;
  points: string[];
}

export interface InfographicChart {
  title: string;
  unit?: string;
  data: { label: string; value: number }[];
}

export interface InfographicDoc {
  title: string;
  subtitle?: string;
  key_stats: InfographicStat[];
  sections: InfographicSection[];
  charts: InfographicChart[];
}

export type InfographicParseResult =
  | { ok: true; doc: InfographicDoc }
  | { ok: false; reason: "too_large" | "too_deep"; preview: string }
  | { ok: false; reason: "invalid_json" | "invalid_shape"; detail: string };

type Obj = Record<string, unknown>;

function isObject(v: unknown): v is Obj {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function isNonEmptyString(v: unknown): v is string {
  return typeof v === "string" && v.trim().length > 0;
}

function optionalString(o: Obj, key: string): boolean {
  return o[key] === undefined || typeof o[key] === "string";
}

/** 형태 판정 — 통과하면 null, 아니면 사유. */
export function validateInfographicShape(data: unknown): string | null {
  if (!isObject(data)) return "최상위가 객체가 아닙니다";
  if (!isNonEmptyString(data.title)) return "title이 비어있습니다";
  if (!optionalString(data, "subtitle")) return "subtitle이 문자열이 아닙니다";

  if (!Array.isArray(data.key_stats)) return "key_stats가 배열이 아닙니다";
  for (const [i, s] of data.key_stats.entries()) {
    if (!isObject(s)) return `key_stats[${i}]가 객체가 아닙니다`;
    if (!isNonEmptyString(s.label)) return `key_stats[${i}].label이 비어있습니다`;
    const v = s.value;
    const okValue = isNonEmptyString(v) || (typeof v === "number" && Number.isFinite(v));
    if (!okValue) return `key_stats[${i}].value가 문자열·숫자가 아닙니다`;
    if (!optionalString(s, "note")) return `key_stats[${i}].note가 문자열이 아닙니다`;
  }

  if (!Array.isArray(data.sections)) return "sections가 배열이 아닙니다";
  for (const [i, s] of data.sections.entries()) {
    if (!isObject(s)) return `sections[${i}]가 객체가 아닙니다`;
    if (!isNonEmptyString(s.heading)) return `sections[${i}].heading이 비어있습니다`;
    if (!Array.isArray(s.points) || !s.points.every((p) => typeof p === "string")) {
      return `sections[${i}].points가 문자열 배열이 아닙니다`;
    }
  }

  if (data.charts !== undefined) {
    if (!Array.isArray(data.charts)) return "charts가 배열이 아닙니다";
    for (const [i, c] of data.charts.entries()) {
      if (!isObject(c)) return `charts[${i}]가 객체가 아닙니다`;
      if (!isNonEmptyString(c.title)) return `charts[${i}].title이 비어있습니다`;
      if (!optionalString(c, "unit")) return `charts[${i}].unit이 문자열이 아닙니다`;
      if (!Array.isArray(c.data) || c.data.length === 0) return `charts[${i}].data가 비어있습니다`;
      for (const [j, d] of c.data.entries()) {
        if (!isObject(d) || !isNonEmptyString(d.label)) {
          return `charts[${i}].data[${j}].label이 비어있습니다`;
        }
        if (typeof d.value !== "number" || !Number.isFinite(d.value)) {
          return `charts[${i}].data[${j}].value가 숫자가 아닙니다`;
        }
      }
    }
  }
  return null;
}

function normalize(data: Obj): InfographicDoc {
  const stats = data.key_stats as Obj[];
  const sections = data.sections as Obj[];
  const charts = (data.charts as Obj[] | undefined) ?? [];
  const doc: InfographicDoc = {
    title: String(data.title).trim(),
    key_stats: stats.map((s) => {
      const stat: InfographicStat = { label: String(s.label), value: String(s.value) };
      if (isNonEmptyString(s.note)) stat.note = s.note;
      return stat;
    }),
    sections: sections.map((s) => ({
      heading: String(s.heading),
      points: (s.points as string[]).filter((p) => p.trim().length > 0),
    })),
    charts: charts.map((c) => {
      const chart: InfographicChart = {
        title: String(c.title),
        data: (c.data as Obj[]).map((d) => ({ label: String(d.label), value: d.value as number })),
      };
      if (isNonEmptyString(c.unit)) chart.unit = c.unit;
      return chart;
    }),
  };
  if (isNonEmptyString(data.subtitle)) doc.subtitle = data.subtitle;
  return doc;
}

/** 본문 문자열 → 인포그래픽 문서. */
export function parseInfographic(text: string): InfographicParseResult {
  const b = bounded(text);
  if (b.kind !== "ok") return { ok: false, reason: b.kind, preview: b.preview };
  const parsed = parseLenientJson(b.text);
  if (!parsed.ok) return { ok: false, reason: "invalid_json", detail: "JSON이 아닙니다" };
  const err = validateInfographicShape(parsed.value);
  if (err) return { ok: false, reason: "invalid_shape", detail: err };
  return { ok: true, doc: normalize(parsed.value as Obj) };
}
