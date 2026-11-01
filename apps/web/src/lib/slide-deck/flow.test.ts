import { describe, expect, it } from "vitest";
import legacyPresenter from "@/lib/__fixtures__/slide-deck/legacy-presenter-continue.json";
import type { AgentRun } from "@/lib/agent-types";
import {
  commonParamsOf,
  continuedDeckRun,
  continueRequest,
  SLIDE_IMAGE_MAX,
  SLIDE_OUTLINE_MAX,
} from "@/lib/slide-deck/flow";
import {
  OUTLINE_MAX_SLIDES,
  OUTLINE_SOURCE_IDS_MAX,
  parseSlideOutline,
  type SlideOutlineDoc,
} from "@/lib/slide-deck/parse";

function run(overrides: Partial<AgentRun>): AgentRun {
  return { id: "run-outline", status: "succeeded", params: {}, input_source_ids: null, ...overrides } as AgentRun;
}

const OUTLINE: SlideOutlineDoc = {
  title: "LLM 위키",
  format: "presenter",
  slides: [{ title: "표지", summary: "오프닝" }, { title: "정리" }],
  sources: [],
  degraded: [],
};

describe("commonParamsOf", () => {
  it("공통 키만 꺼내고 모르는 값은 기본값으로 둔다", () => {
    expect(
      commonParamsOf(run({ params: { format: "presenter", length: "short", language: "English", description: "d", x: 1 } })),
    ).toEqual({ format: "presenter", length: "short", language: "English", description: "d", image_count: 0 });
    expect(commonParamsOf(run({ params: { format: "poster", length: 3, language: "日本語" } }))).toEqual({
      format: "detailed",
      length: "default",
      language: "한국어",
      description: "",
      image_count: 0,
    });
  });

  it("삽화 장수는 0~SLIDE_IMAGE_MAX 정수만 넘기고, 그 밖(옛 run·문자열·범위 밖)은 0이다", () => {
    const count = (image_count: unknown) => commonParamsOf(run({ params: { image_count } })).image_count;
    expect(count(SLIDE_IMAGE_MAX)).toBe(SLIDE_IMAGE_MAX);
    expect(count(0)).toBe(0);
    for (const bad of [undefined, "4", 4.5, -1, SLIDE_IMAGE_MAX + 1, null, true]) expect(count(bad)).toBe(0);
  });
});

describe("continueRequest", () => {
  it("개요 run의 설정·소스 핀에 개요·의견·개요 run id를 더한다", () => {
    const req = continueRequest(
      run({
        params: { format: "presenter", length: "short", language: "English", description: "d", image_count: SLIDE_IMAGE_MAX },
        input_source_ids: ["s1"],
      }),
      OUTLINE,
      "  더 짧게  ",
    );
    expect(req).toEqual({
      params: {
        format: "presenter",
        length: "short",
        language: "English",
        description: "d",
        image_count: SLIDE_IMAGE_MAX,
        // 장마다 개요 run의 형식·언어를 덮어쓴다(#218 독립 리뷰 M)
        outline: JSON.stringify({
          title: OUTLINE.title,
          format: OUTLINE.format,
          slides: OUTLINE.slides.map((s) => ({ ...s, format: "presenter", language: "English" })),
        }),
        feedback: "더 짧게",
        outline_run_id: "run-outline",
      },
      source_ids: ["s1"],
    });
  });

  it("노트 전체(null) 핀이면 source_ids를 보내지 않는다", () => {
    expect(continueRequest(run({}), OUTLINE, "")).not.toHaveProperty("source_ids");
  });

  it("직렬화한 개요가 시드 상한을 넘으면 null — 서버가 거절할 요청을 만들지 않는다", () => {
    // 따옴표는 JSON에서 두 글자로 늘어난다 — 정규화 상한(제목 120·요약 300·검색어 200·근거 20개)을 지켜도 넘을 수 있다
    const quoted = {
      title: "t",
      slides: Array.from({ length: OUTLINE_MAX_SLIDES }, () => ({
        title: '"'.repeat(200),
        summary: '"'.repeat(400),
        query: '"'.repeat(300),
        source_ids: Array.from({ length: OUTLINE_SOURCE_IDS_MAX + 5 }, (_, i) => `${i}${'"'.repeat(60)}`),
      })),
    };
    const parsed = parseSlideOutline(JSON.stringify(quoted));
    if (!parsed.ok) throw new Error("parse");
    expect(parsed.doc.slides[0].title).toHaveLength(120);
    expect(parsed.doc.slides[0].summary).toHaveLength(300);
    expect(parsed.doc.slides[0].query).toHaveLength(200);
    expect(parsed.doc.slides[0].source_ids).toHaveLength(OUTLINE_SOURCE_IDS_MAX);
    expect(JSON.stringify(parsed.doc).length).toBeGreaterThan(SLIDE_OUTLINE_MAX);
    expect(continueRequest(run({}), parsed.doc, "")).toBeNull();
  });

  it("정규화 상한 안의 보통 개요(20장 × 최대 길이 한글·근거 20개)는 상한 안이다", () => {
    const uuid = (i: number) => `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`;
    const long = {
      title: "가".repeat(200),
      slides: Array.from({ length: OUTLINE_MAX_SLIDES + 5 }, () => ({
        title: "가".repeat(500),
        summary: "나".repeat(2000),
        layout: "two_column",
        query: "다".repeat(500),
        source_ids: Array.from({ length: 30 }, (_, i) => uuid(i)),
        format: "presenter",
        language: "한국어",
      })),
    };
    const parsed = parseSlideOutline(JSON.stringify(long));
    if (!parsed.ok) throw new Error("parse");
    expect(parsed.doc.slides).toHaveLength(OUTLINE_MAX_SLIDES); // 덱 loop max_items와 같다
    expect(continueRequest(run({}), parsed.doc, "")).not.toBeNull();
  });

  it("「계속」은 장의 검색어·근거 소스를 그대로 넘기고, 개요가 읽은 소스 목록은 넘기지 않는다(#218)", () => {
    const doc: SlideOutlineDoc = {
      ...OUTLINE,
      slides: [{ title: "표지", layout: "title", query: "위키", source_ids: ["s1"], format: "presenter", language: "한국어" }],
      sources: [{ id: "s1", title: "소스" }],
      degraded: [{ id: "s1", title: "소스", reason: "no_content" }],
    };
    const request = continueRequest(run({ params: { format: "presenter", language: "한국어" } }), doc, "");
    expect(JSON.parse(request?.params.outline as string)).toEqual({
      title: "LLM 위키",
      format: "presenter",
      slides: doc.slides,
    });
  });

  it("방안 ④ 전에 저장한 발표자 개요(장에 형식 없음)도 장마다 개요 run의 형식·언어를 실어 보낸다 — 공유 fixture", () => {
    // 같은 fixture를 API 런타임 테스트(test_slide_deck_digest.py)가 덱 run에 넣어 장 작성이 발표자 형식을 받는지 본다
    const saved = parseSlideOutline(JSON.stringify(legacyPresenter.saved_outline));
    if (!saved.ok) throw new Error("parse");
    expect(saved.doc.slides.every((s) => s.format === undefined && s.language === undefined)).toBe(true);
    const request = continueRequest(run({ params: legacyPresenter.outline_run_params }), saved.doc, "");
    expect(request?.params).toMatchObject({ format: "presenter", language: "English", feedback: "" });
    expect(JSON.parse(request?.params.outline as string)).toEqual(legacyPresenter.continue_outline);
  });

  it("개요 LLM이 옮겨 쓴 장 형식·언어가 run 설정과 어긋나면 run 설정이 이긴다", () => {
    const doc: SlideOutlineDoc = {
      ...OUTLINE,
      slides: [{ title: "표지", format: "detailed", language: "English" }, { title: "정리" }],
    };
    const request = continueRequest(run({ params: { format: "presenter", language: "한국어" } }), doc, "");
    const sent = JSON.parse(request?.params.outline as string);
    expect(sent.slides.map((s: { format: string; language: string }) => [s.format, s.language])).toEqual([
      ["presenter", "한국어"],
      ["presenter", "한국어"],
    ]);
  });
});

describe("continuedDeckRun", () => {
  it("같은 outline_run_id의 덱 run을 찾되 실패·취소는 세지 않는다", () => {
    const failed = run({ id: "d1", status: "failed", params: { outline_run_id: "run-outline" } });
    const cancelled = run({ id: "d2", status: "cancelled", params: { outline_run_id: "run-outline" } });
    const other = run({ id: "d3", status: "succeeded", params: { outline_run_id: "other" } });
    expect(continuedDeckRun("run-outline", [failed, cancelled, other])).toBeUndefined();
    const queued = run({ id: "d4", status: "queued", params: { outline_run_id: "run-outline" } });
    expect(continuedDeckRun("run-outline", [failed, queued])?.id).toBe("d4");
    expect(continuedDeckRun("run-outline", undefined)).toBeUndefined();
  });
});

describe("continueRequest — 실행 모델(#216)", () => {
  it("개요 run의 model_id를 그대로 넘기고, null·없음(기본 모델)이면 키를 넣지 않는다", () => {
    expect(continueRequest(run({ model_id: "m-flash" }), OUTLINE, "")?.model_id).toBe("m-flash");
    expect(continueRequest(run({ model_id: null }), OUTLINE, "")).not.toHaveProperty("model_id");
    expect(continueRequest(run({}), OUTLINE, "")).not.toHaveProperty("model_id");
  });
});
