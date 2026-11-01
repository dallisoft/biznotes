/**
 * 슬라이드 자료 산출물 해석(docs/plans/slide-deck-studio.md D-6·D-7) — 뷰어 입력 형식의 단일 정의.
 *
 * 형식(시드 워크플로우 프롬프트와 같다 — test_seed_slide_deck.py가 키를 대조한다):
 *   slide-outline.json  { "title": str, "format": "detailed"|"presenter", "slides": [장],
 *                         "sources"?: [{ "id", "title", "cached"?, "model_name"? }], "degraded"?: [{ "id", "title", "reason" }] }
 *   장 = { "title", "summary", "layout"?, "query"?, "source_ids"?: [str], "format"?, "language"? }
 *        — 장의 format·language는 시드 개요가 더는 쓰지 않는다(덱 loop 공통 params, 코어 #390). 사용자 복제본(옛 loop)이
 *          장 항목에서 형식·언어를 읽으므로 복제본이 남아 있는 동안 해석해 「계속」으로 보낸다(flow.ts `continueRequest`)
 *   slide-deck.json     { "title": str, "format": ..., "slides": [슬라이드], "coverage"?: 반영 표시 }
 *   슬라이드 = { "layout", "heading", "subheading"?, "body"?, "bullets"?: [str],
 *               "columns"?: [{ "heading", "bullets" }], "steps"?: [{ "label", "detail" }],
 *               "stats"?: [{ "value", "label" }], "quote"?: { "text", "attribution"? }, "notes"? }
 *   반영 표시(#218·#217) = { "sources", "degraded", "assigned": [[id] | null], "cited": [[id]],
 *                            "outline": [제목], "failed": [{ "index" }] } — assigned·cited는 slides와 같은 순서
 * 개요의 sources·degraded와 덱의 coverage는 이 변경(방안 ④) 전 산출물에는 없다 — 없으면 표시하지 않는다.
 * sources의 model_name(요약을 만든 모델, 코어 v0.27.0 #396)도 그 전 산출물에는 없다 — 없으면 요약 모델을 표시하지 않는다.
 * 코어 json_validate에 슬라이드 스키마가 없어 서버 검증이 없다. 경계(bounded) → 관용 해석(펜스) →
 * 형태 판정 → 정규화 순이고, 판정은 너그럽다 — 슬라이드는 heading만 있으면 그리고, 타입이 틀린 선택
 * 필드는 버린다. layout이 모르는 값이거나 그 layout의 필수 필드가 없으면 bullets로 그린다.
 */
import type { ArtifactRef } from "@/lib/agent-types";
import { bounded } from "@/lib/mindmap/bounded";
import { parseLenientJson } from "@/lib/mindmap/parse";

export const SLIDE_DECK_ARTIFACT_NAME = "slide-deck.json";
export const SLIDE_OUTLINE_ARTIFACT_NAME = "slide-outline.json";
/**
 * 슬라이드 삽화 산출물 이름(core-v0.23.0-upgrade AC-9) — 시드 본문 워크플로우 「슬라이드 삽화」의
 * `image_generate` `name: "slide-image-{params.item.slide}"`에 코어가 바이트로 정한 확장자를 붙인다.
 * n은 슬라이드 번호(1부터)다. test_slide_deck_images.py가 시드 이름과 대조한다.
 */
export const SLIDE_IMAGE_NAME = /^slide-image-([1-9][0-9]{0,2})\.(?:png|jpe?g)$/;
const SLIDE_IMAGE_TYPES = new Set(["image/png", "image/jpeg"]);

export const SLIDE_LAYOUTS = ["title", "section", "bullets", "two_column", "steps", "stats", "quote", "closing"] as const;
export type SlideLayout = (typeof SLIDE_LAYOUTS)[number];
export type SlideFormat = "detailed" | "presenter";

/** 표시 상한 — 넘는 슬라이드·항목은 잘라 내고 `truncated`로 알린다. */
export const SLIDE_MAX_SLIDES = 40;
const MAX_ITEMS = 8;
const MAX_STATS = 6;
/** 개요 항목 상한 — 「계속」이 넘기는 개요가 시드 params `outline` maxLength(flow.ts `SLIDE_OUTLINE_MAX`) 안에 들게 한다 */
export const OUTLINE_TITLE_MAX = 120;
export const OUTLINE_SUMMARY_MAX = 300;
export const OUTLINE_QUERY_MAX = 200;
/** 장마다 근거 소스 상한 — 소스가 장보다 훨씬 많으면(짧은 덱에 소스 수십 개) 장마다 여럿을 배정한다 */
export const OUTLINE_SOURCE_IDS_MAX = 20;
/** 개요 장 상한 — 덱 loop `max_items`(시드 `SLIDE_MAX_SLIDES`)와 같다. 넘는 장은 덱에 들어가지 않는다 */
export const OUTLINE_MAX_SLIDES = 20;
/** 소스 목록 표시 상한 — 소스 id·제목 */
const SOURCES_MAX = 500;
const SOURCE_ID_MAX = 64;
const SOURCE_TITLE_MAX = 200;
const LANGUAGE_MAX = 20;
const MODEL_NAME_MAX = 100;

export interface SlideColumn {
  heading: string;
  bullets: string[];
}

export interface SlideStep {
  label: string;
  detail?: string;
}

export interface SlideStat {
  value: string;
  label: string;
}

export interface Slide {
  layout: SlideLayout;
  heading: string;
  subheading?: string;
  body?: string;
  bullets: string[];
  columns: SlideColumn[];
  steps: SlideStep[];
  stats: SlideStat[];
  quote?: { text: string; attribution?: string };
  notes?: string;
}

export interface SlideSourceRef {
  id: string;
  title: string;
}

/** 요약 대신 대체 본문(소스 가이드 요약·앞부분 발췌·제목)으로 들어간 소스 — 코어 source_digest `degraded` */
export interface SlideDegradedSource extends SlideSourceRef {
  reason: string;
}

/**
 * 소스 요약을 만든 모델(#239) — 코어 source_digest `sources[]`의 `model_name`·`cached`(v0.27.0 #396). 캐시 행의 모델이거나
 * 이번 run이 만든 모델이다. `model`이 null이면 대체 본문(요약 없음)이다.
 */
export interface SlideDigestSource extends SlideSourceRef {
  model: string | null;
  cached: boolean;
}

/** 덱의 소스 반영 표시(#218 수락 기준, #217) — 덱 run의 소스 요약과 장별 loop 결과에서 온다. */
export interface SlideCoverage {
  /** 고른 소스 전부 */
  sources: SlideSourceRef[];
  degraded: SlideDegradedSource[];
  /** 어느 장에도 배정되거나 인용되지 않은 소스 */
  uncovered: SlideSourceRef[];
  /** 장마다 근거로 인용한 소스 — `slides`와 같은 순서 */
  bySlide: SlideSourceRef[][];
  /** 만들지 못해 뺀 장 — 개요 번호(1부터)·제목 */
  failed: { n: number; title?: string }[];
  /** 소스마다 요약을 만든 모델 — v0.27.0 전 덱에는 없다 */
  digest?: SlideDigestSource[];
}

export interface SlideDeckDoc {
  title: string;
  format: SlideFormat;
  slides: Slide[];
  truncated: boolean;
  /** 방안 ④ 전 덱에는 없다 */
  coverage?: SlideCoverage;
}

export interface SlideOutlineItem {
  title: string;
  summary?: string;
  layout?: SlideLayout;
  /** 그 장의 근거를 찾을 검색어와 근거 소스(#218) — 덱 run의 장 작성이 그대로 쓴다 */
  query?: string;
  source_ids?: string[];
  format?: SlideFormat;
  language?: string;
}

export interface SlideOutlineDoc {
  title: string;
  format: SlideFormat;
  slides: SlideOutlineItem[];
  /** 개요가 읽은 소스 전부와 대체 본문으로 들어간 소스 — 방안 ④ 전 개요는 빈 목록 */
  sources: SlideSourceRef[];
  degraded: SlideDegradedSource[];
  /** 소스마다 요약을 만든 모델 — v0.27.0 전 개요에는 없다 */
  digest?: SlideDigestSource[];
}

type Failure =
  | { ok: false; reason: "too_large" | "too_deep"; preview: string }
  | { ok: false; reason: "invalid_json" | "invalid_shape"; detail: string };

export type SlideDeckParseResult = { ok: true; doc: SlideDeckDoc } | Failure;
export type SlideOutlineParseResult = { ok: true; doc: SlideOutlineDoc } | Failure;

type Obj = Record<string, unknown>;

function isObject(v: unknown): v is Obj {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** 비어 있지 않은 문자열이면 다듬은 값, 아니면 undefined. */
function text(v: unknown): string | undefined {
  if (typeof v !== "string") return undefined;
  const t = v.trim();
  return t.length > 0 ? t : undefined;
}

function strings(v: unknown, max = MAX_ITEMS): string[] {
  if (!Array.isArray(v)) return [];
  return v.map(text).filter((s): s is string => s !== undefined).slice(0, max);
}

function format(v: unknown): SlideFormat {
  return v === "presenter" ? "presenter" : "detailed";
}

function sourceId(v: unknown): string | undefined {
  const id = text(v);
  return id && id.length <= SOURCE_ID_MAX ? id : undefined;
}

/** 순서를 지키며 겹친 id를 뺀다. */
function sourceIds(v: unknown, max: number): string[] {
  if (!Array.isArray(v)) return [];
  return [...new Set(v.map(sourceId).filter((id): id is string => id !== undefined))].slice(0, max);
}

type SourceItem = SlideSourceRef & { reason: string; model: string | null; cached: boolean };

/**
 * `[{ id, title, ... }]` → id가 있는 항목만, 겹친 id는 앞의 것. 대체 본문 목록은 사유(`reason`)를, 소스 요약 목록은
 * 요약 모델(`model_name`)·캐시 여부(`cached`)를 함께 읽는다.
 */
function sourceList(v: unknown): SourceItem[] {
  if (!Array.isArray(v)) return [];
  const seen = new Set<string>();
  const out: SourceItem[] = [];
  for (const s of v.filter(isObject)) {
    const id = sourceId(s.id);
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const title = (text(s.title) ?? "제목 없음").slice(0, SOURCE_TITLE_MAX);
    out.push({
      id,
      title,
      reason: text(s.reason) ?? "unknown",
      model: text(s.model_name)?.slice(0, MODEL_NAME_MAX) ?? null,
      cached: s.cached === true,
    });
  }
  return out.slice(0, SOURCES_MAX);
}

function sourceRefs(v: unknown): SlideSourceRef[] {
  return sourceList(v).map(({ id, title }) => ({ id, title }));
}

function degradedRefs(v: unknown): SlideDegradedSource[] {
  return sourceList(v).map(({ id, title, reason }) => ({ id, title, reason }));
}

/** 소스 요약 목록 → 소스별 요약 모델. 어느 항목에도 `model_name` 키가 없으면(v0.27.0 전 산출물) undefined. */
function digestSources(v: unknown): SlideDigestSource[] | undefined {
  if (!Array.isArray(v) || !v.some((s) => isObject(s) && "model_name" in s)) return undefined;
  return sourceList(v).map(({ id, title, model, cached }) => ({ id, title, model, cached }));
}

/** 덱 `coverage` → 반영 표시. 객체가 아니면 undefined(옛 덱). */
function coverage(v: unknown, slideCount: number): SlideCoverage | undefined {
  if (!isObject(v)) return undefined;
  const sources = sourceRefs(v.sources);
  const titles = new Map(sources.map((s) => [s.id, s.title]));
  const refs = (ids: string[]) => ids.flatMap((id) => (titles.has(id) ? [{ id, title: titles.get(id) as string }] : []));
  const cited = Array.isArray(v.cited) ? v.cited.map((ids) => sourceIds(ids, SOURCES_MAX)) : [];
  const assigned = Array.isArray(v.assigned) ? v.assigned.map((ids) => sourceIds(ids, SOURCES_MAX)) : [];
  const used = new Set([...cited.flat(), ...assigned.flat()]);
  const outline = Array.isArray(v.outline) ? v.outline : [];
  const failed: SlideCoverage["failed"] = [];
  if (Array.isArray(v.failed)) {
    for (const f of v.failed.filter(isObject)) {
      const index = f.index;
      if (typeof index !== "number" || !Number.isInteger(index) || index < 0) continue;
      const title = text(outline[index])?.slice(0, OUTLINE_TITLE_MAX);
      failed.push(title ? { n: index + 1, title } : { n: index + 1 });
    }
  }
  const digest = digestSources(v.sources);
  return {
    sources,
    degraded: degradedRefs(v.degraded),
    uncovered: sources.filter((s) => !used.has(s.id)),
    bySlide: Array.from({ length: slideCount }, (_, i) => refs(cited[i] ?? [])),
    failed: failed.slice(0, SLIDE_MAX_SLIDES),
    ...(digest ? { digest } : {}),
  };
}

function outlineItem(s: Obj): SlideOutlineItem {
  const item: SlideOutlineItem = { title: (text(s.title) as string).slice(0, OUTLINE_TITLE_MAX) };
  const summary = text(s.summary)?.slice(0, OUTLINE_SUMMARY_MAX);
  if (summary) item.summary = summary;
  if ((SLIDE_LAYOUTS as readonly string[]).includes(s.layout as string)) item.layout = s.layout as SlideLayout;
  const query = text(s.query)?.slice(0, OUTLINE_QUERY_MAX);
  if (query) item.query = query;
  // 빈 목록도 남긴다 — 「배정 없음」(검색 안 함)과 「키 없음」(run 범위 전체 검색)은 다르다
  if (Array.isArray(s.source_ids)) item.source_ids = sourceIds(s.source_ids, OUTLINE_SOURCE_IDS_MAX);
  if (s.format === "detailed" || s.format === "presenter") item.format = s.format;
  const language = text(s.language);
  if (language && language.length <= LANGUAGE_MAX) item.language = language;
  return item;
}

/** 공통 판정 — 최상위 객체 ∧ title ∧ 비어 있지 않은 slides 배열. 통과하면 null, 아니면 사유. */
function validateTop(data: unknown): string | null {
  if (!isObject(data)) return "최상위가 객체가 아닙니다";
  if (!text(data.title)) return "title이 비어있습니다";
  if (!Array.isArray(data.slides) || data.slides.length === 0) return "slides가 비어있습니다";
  return null;
}

export function validateSlideDeckShape(data: unknown): string | null {
  const err = validateTop(data);
  if (err) return err;
  for (const [i, s] of ((data as Obj).slides as unknown[]).entries()) {
    if (!isObject(s)) return `slides[${i}]가 객체가 아닙니다`;
    if (!text(s.heading)) return `slides[${i}].heading이 비어있습니다`;
  }
  return null;
}

export function validateSlideOutlineShape(data: unknown): string | null {
  const err = validateTop(data);
  if (err) return err;
  for (const [i, s] of ((data as Obj).slides as unknown[]).entries()) {
    if (!isObject(s)) return `slides[${i}]가 객체가 아닙니다`;
    if (!text(s.title)) return `slides[${i}].title이 비어있습니다`;
  }
  return null;
}

function columns(v: unknown): SlideColumn[] {
  if (!Array.isArray(v)) return [];
  return v
    .filter(isObject)
    .map((c) => ({ heading: text(c.heading) ?? "", bullets: strings(c.bullets) }))
    .filter((c) => c.heading || c.bullets.length > 0)
    .slice(0, 2);
}

function steps(v: unknown): SlideStep[] {
  if (!Array.isArray(v)) return [];
  const out: SlideStep[] = [];
  for (const s of v.filter(isObject)) {
    const label = text(s.label);
    if (!label) continue;
    const detail = text(s.detail);
    out.push(detail ? { label, detail } : { label });
  }
  return out.slice(0, MAX_ITEMS);
}

function stats(v: unknown): SlideStat[] {
  if (!Array.isArray(v)) return [];
  const out: SlideStat[] = [];
  for (const s of v.filter(isObject)) {
    const value =
      typeof s.value === "number" && Number.isFinite(s.value) ? String(s.value) : text(s.value);
    const label = text(s.label);
    if (value && label) out.push({ value, label });
  }
  return out.slice(0, MAX_STATS);
}

/** 선언한 layout을 그릴 수 있는가 — 필수 필드가 없으면 bullets로 그린다. */
function effectiveLayout(raw: unknown, slide: Omit<Slide, "layout">): SlideLayout {
  const layout = (SLIDE_LAYOUTS as readonly string[]).includes(raw as string)
    ? (raw as SlideLayout)
    : "bullets";
  if (layout === "two_column" && slide.columns.length < 2) return "bullets";
  if (layout === "steps" && slide.steps.length === 0) return "bullets";
  if (layout === "stats" && slide.stats.length === 0) return "bullets";
  if (layout === "quote" && !slide.quote) return "bullets";
  return layout;
}

function normalizeSlide(s: Obj): Slide {
  const slide: Omit<Slide, "layout"> = {
    heading: text(s.heading) as string,
    bullets: strings(s.bullets),
    columns: columns(s.columns),
    steps: steps(s.steps),
    stats: stats(s.stats),
  };
  const subheading = text(s.subheading);
  if (subheading) slide.subheading = subheading;
  const body = text(s.body);
  if (body) slide.body = body;
  const notes = text(s.notes);
  if (notes) slide.notes = notes;
  if (isObject(s.quote)) {
    const quoteText = text(s.quote.text);
    if (quoteText) {
      const attribution = text(s.quote.attribution);
      slide.quote = attribution ? { text: quoteText, attribution } : { text: quoteText };
    }
  }
  return { layout: effectiveLayout(s.layout, slide), ...slide };
}

function prepare(raw: string): { ok: true; value: unknown } | Failure {
  const b = bounded(raw);
  if (b.kind !== "ok") return { ok: false, reason: b.kind, preview: b.preview };
  const parsed = parseLenientJson(b.text);
  if (!parsed.ok) return { ok: false, reason: "invalid_json", detail: "JSON이 아닙니다" };
  return { ok: true, value: parsed.value };
}

/** 본문 문자열 → 슬라이드 덱. */
export function parseSlideDeck(raw: string): SlideDeckParseResult {
  const p = prepare(raw);
  if (!p.ok) return p;
  const err = validateSlideDeckShape(p.value);
  if (err) return { ok: false, reason: "invalid_shape", detail: err };
  const data = p.value as Obj;
  const all = data.slides as Obj[];
  const slides = all.slice(0, SLIDE_MAX_SLIDES).map(normalizeSlide);
  const doc: SlideDeckDoc = {
    title: text(data.title) as string,
    format: format(data.format),
    slides,
    truncated: all.length > SLIDE_MAX_SLIDES,
  };
  const covered = coverage(data.coverage, slides.length);
  if (covered) doc.coverage = covered;
  return { ok: true, doc };
}

/** 본문 문자열 → 슬라이드 개요. */
export function parseSlideOutline(raw: string): SlideOutlineParseResult {
  const p = prepare(raw);
  if (!p.ok) return p;
  const err = validateSlideOutlineShape(p.value);
  if (err) return { ok: false, reason: "invalid_shape", detail: err };
  const data = p.value as Obj;
  const digest = digestSources(data.sources);
  return {
    ok: true,
    doc: {
      title: text(data.title) as string,
      format: format(data.format),
      slides: (data.slides as Obj[]).slice(0, OUTLINE_MAX_SLIDES).map(outlineItem),
      sources: sourceRefs(data.sources),
      degraded: degradedRefs(data.degraded),
      ...(digest ? { digest } : {}),
    },
  };
}

/**
 * 덱 run에 넘길 개요 — 정규화한 개요를 JSON 문자열로(시드 params `outline` maxLength 안). 장 항목의 검색어·근거
 * 소스는 그대로 넘기고(덱이 그대로 쓴다), 소스 목록·대체 본문은 덱 run이 소스 요약에서 다시 읽으므로 뺀다.
 */
export function serializeOutline(doc: SlideOutlineDoc): string {
  return JSON.stringify({ title: doc.title, format: doc.format, slides: doc.slides });
}

/** 개요에서 어느 장에도 배정되지 않은 소스 — 검토 화면이 의견을 쓰기 전에 보인다. */
export function unassignedOutlineSources(doc: SlideOutlineDoc): SlideSourceRef[] {
  const assigned = new Set(doc.slides.flatMap((s) => s.source_ids ?? []));
  return doc.sources.filter((s) => !assigned.has(s.id));
}

/**
 * 덱 run 산출물 → 장 위치(0부터)별 삽화. 덱 JSON의 `images`가 아니라 실제로 만들어진 산출물로 붙인다 — 생성이
 * 실패한 장은 이미지 없이 그린다. 덱 밖 번호·PNG/JPEG가 아닌 타입은 버리고, 같은 번호가 둘이면 뒤의 것을 쓴다.
 */
export function slideImageArtifacts(artifacts: readonly ArtifactRef[], total: number): Map<number, ArtifactRef> {
  const out = new Map<number, ArtifactRef>();
  for (const a of artifacts) {
    const m = SLIDE_IMAGE_NAME.exec(a.name);
    if (!m || !SLIDE_IMAGE_TYPES.has(a.content_type)) continue;
    const index = Number(m[1]) - 1;
    if (index < total) out.set(index, a);
  }
  return out;
}
