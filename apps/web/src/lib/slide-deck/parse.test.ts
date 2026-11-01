import { describe, expect, it } from "vitest";
import {
  OUTLINE_MAX_SLIDES,
  OUTLINE_SOURCE_IDS_MAX,
  parseSlideDeck,
  parseSlideOutline,
  serializeOutline,
  SLIDE_MAX_SLIDES,
  slideImageArtifacts,
  unassignedOutlineSources,
} from "@/lib/slide-deck/parse";

const deck = {
  title: "LLM 위키",
  format: "presenter",
  slides: [
    { layout: "title", heading: "안드레이 카파시의 LLM 위키", subheading: "에이전틱 지식 베이스" },
    { layout: "bullets", heading: "붕괴의 법칙", bullets: ["수집", " ", "구조화", 3], notes: "대본" },
    {
      layout: "two_column",
      heading: "RAG vs 위키",
      columns: [
        { heading: "RAG", bullets: ["매번 조립"] },
        { heading: "위키", bullets: ["복리 축적"] },
      ],
    },
    { layout: "steps", heading: "수집 단계", steps: [{ label: "분석", detail: "문서 읽기" }, { label: "갱신" }] },
    { layout: "stats", heading: "사례", stats: [{ value: 2188, label: "문서" }, { value: "$50", label: "비용" }] },
    { layout: "quote", heading: "인용", quote: { text: "지식은 복리다", attribution: "카파시" } },
    { layout: "closing", heading: "정리", bullets: ["시작하기"] },
  ],
};

describe("parseSlideDeck", () => {
  it("정상 덱을 정규화한다 — 빈 문자열·비문자열 항목은 버리고 수치는 문자열로", () => {
    const r = parseSlideDeck(JSON.stringify(deck));
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.doc.title).toBe("LLM 위키");
    expect(r.doc.format).toBe("presenter");
    expect(r.doc.truncated).toBe(false);
    expect(r.doc.slides.map((s) => s.layout)).toEqual([
      "title",
      "bullets",
      "two_column",
      "steps",
      "stats",
      "quote",
      "closing",
    ]);
    expect(r.doc.slides[1].bullets).toEqual(["수집", "구조화"]);
    expect(r.doc.slides[1].notes).toBe("대본");
    expect(r.doc.slides[3].steps).toEqual([{ label: "분석", detail: "문서 읽기" }, { label: "갱신" }]);
    expect(r.doc.slides[4].stats).toEqual([
      { value: "2188", label: "문서" },
      { value: "$50", label: "비용" },
    ]);
    expect(r.doc.slides[5].quote).toEqual({ text: "지식은 복리다", attribution: "카파시" });
  });

  it("코드 펜스·설명 문장이 붙어도 읽는다", () => {
    const r = parseSlideDeck("결과입니다:\n```json\n" + JSON.stringify(deck) + "\n```");
    expect(r.ok).toBe(true);
  });

  it("모르는 layout·필수 필드 없는 layout은 bullets로 그린다", () => {
    const r = parseSlideDeck(
      JSON.stringify({
        title: "t",
        slides: [
          { layout: "poster", heading: "a", bullets: ["x"] },
          { layout: "two_column", heading: "b", columns: [{ heading: "하나", bullets: [] }] },
          { layout: "stats", heading: "c", stats: [{ value: "", label: "빈 값" }] },
          { layout: "quote", heading: "d", quote: { text: " " } },
          { heading: "e" },
        ],
      }),
    );
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.doc.slides.map((s) => s.layout)).toEqual(["bullets", "bullets", "bullets", "bullets", "bullets"]);
    expect(r.doc.format).toBe("detailed");
  });

  it("형태 위반은 사유와 함께 실패한다", () => {
    expect(parseSlideDeck("[]")).toMatchObject({ ok: false, reason: "invalid_shape" });
    expect(parseSlideDeck('{"title": "", "slides": [{"heading": "a"}]}')).toMatchObject({
      reason: "invalid_shape",
      detail: "title이 비어있습니다",
    });
    expect(parseSlideDeck('{"title": "t", "slides": []}')).toMatchObject({ detail: "slides가 비어있습니다" });
    expect(parseSlideDeck('{"title": "t", "slides": [{"layout": "title"}]}')).toMatchObject({
      detail: "slides[0].heading이 비어있습니다",
    });
    expect(parseSlideDeck("슬라이드를 만들지 못했습니다")).toMatchObject({ reason: "invalid_json" });
  });

  it("슬라이드 상한을 넘으면 잘라 내고 truncated를 켠다", () => {
    const many = { title: "t", slides: Array.from({ length: SLIDE_MAX_SLIDES + 5 }, (_, i) => ({ heading: `s${i}` })) };
    const r = parseSlideDeck(JSON.stringify(many));
    expect(r.ok && r.doc.slides.length).toBe(SLIDE_MAX_SLIDES);
    expect(r.ok && r.doc.truncated).toBe(true);
  });

  it("크기 상한을 넘는 본문은 파싱하지 않는다", () => {
    const r = parseSlideDeck(`{"title": "${"가".repeat(400_000)}", "slides": []}`);
    expect(r).toMatchObject({ ok: false, reason: "too_large" });
  });

  it("방안 ④ 전 덱(coverage 없음)은 반영 표시가 없다", () => {
    const r = parseSlideDeck(JSON.stringify(deck));
    expect(r.ok && r.doc.coverage).toBeUndefined();
    const bad = parseSlideDeck(JSON.stringify({ ...deck, coverage: [] }));
    expect(bad.ok && bad.doc.coverage).toBeUndefined();
  });
});

describe("parseSlideDeck — 소스 반영 표시(#218)", () => {
  // 시드 json_compose 템플릿 그대로의 모양 — assigned·cited는 그린 장과 같은 순서, failed.index는 개요 위치
  const covered = {
    title: "커피 입문",
    format: "detailed",
    slides: [
      { layout: "title", heading: "표지" },
      { layout: "bullets", heading: "추출", bullets: ["압력"] },
    ],
    coverage: {
      sources: [
        { id: "a", title: "원두", cached: true },
        { id: "b", title: "추출", cached: false },
        { id: "c", title: "로스팅", cached: false },
        { id: "d", title: "포장", cached: false },
        { id: "a", title: "겹친 원두", cached: false },
        { title: "id 없음" },
      ],
      degraded: [{ id: "d", title: "포장", reason: "generation_failed", error_code: "model_call_failed", fallback: "excerpt" }],
      assigned: [["a"], ["b", "outside"]],
      cited: [["a", "a"], ["b", "outside"]],
      outline: ["표지", "추출", "로스팅"],
      failed: [{ index: 2, error_code: "agent_runtime_error", error: "x" }, { index: -1 }, { index: "1" }],
    },
  };

  it("고른 소스·대체 본문·미반영 소스·장마다 근거·만들지 못한 장을 정규화한다", () => {
    const r = parseSlideDeck(JSON.stringify(covered));
    if (!r.ok) throw new Error("parse");
    const c = r.doc.coverage;
    expect(c?.sources.map((s) => s.id)).toEqual(["a", "b", "c", "d"]); // 겹친 id·id 없는 항목은 뺀다
    expect(c?.degraded).toEqual([{ id: "d", title: "포장", reason: "generation_failed" }]);
    // 로스팅(c)을 맡은 장은 만들지 못했다 — 배정·인용 목록은 그린 장만 담으므로 c는 미반영이다
    expect(c?.uncovered).toEqual([
      { id: "c", title: "로스팅" },
      { id: "d", title: "포장" },
    ]);
    // 장마다 근거 — 고른 소스에 없는 id는 그리지 않고, 겹친 id는 한 번만
    expect(c?.bySlide).toEqual([[{ id: "a", title: "원두" }], [{ id: "b", title: "추출" }]]);
    expect(c?.failed).toEqual([{ n: 3, title: "로스팅" }]);
  });

  it("목록이 비거나 빠져도 그린다 — 근거 없는 장은 빈 목록", () => {
    const r = parseSlideDeck(JSON.stringify({ ...covered, coverage: { sources: [{ id: "a", title: "원두" }] } }));
    if (!r.ok) throw new Error("parse");
    expect(r.doc.coverage).toEqual({
      sources: [{ id: "a", title: "원두" }],
      degraded: [],
      uncovered: [{ id: "a", title: "원두" }],
      bySlide: [[], []],
      failed: [],
    });
  });

  it("소스 요약 모델(#239) — model_name·cached를 소스별로 읽고, 모델 키가 없는 옛 덱은 digest가 없다", () => {
    const old = parseSlideDeck(JSON.stringify(covered));
    if (!old.ok) throw new Error("parse");
    expect(old.doc.coverage).not.toHaveProperty("digest");
    const withModels = {
      ...covered,
      coverage: {
        ...covered.coverage,
        sources: [
          { id: "a", title: "원두", cached: true, model_id: "m1", model_name: "gemini-3.8-flash" },
          { id: "b", title: "추출", cached: false, model_id: "m2", model_name: "gpt-oss-120b" },
          { id: "d", title: "포장", cached: false, model_id: null, model_name: null },
          { id: "e", title: "긴 이름", cached: "yes", model_name: "m".repeat(150) },
        ],
      },
    };
    const r = parseSlideDeck(JSON.stringify(withModels));
    if (!r.ok) throw new Error("parse");
    expect(r.doc.coverage?.digest).toEqual([
      { id: "a", title: "원두", model: "gemini-3.8-flash", cached: true },
      { id: "b", title: "추출", model: "gpt-oss-120b", cached: false },
      { id: "d", title: "포장", model: null, cached: false }, // 대체 본문 — 요약 없음
      { id: "e", title: "긴 이름", model: "m".repeat(100), cached: false }, // bool이 아닌 cached는 끔, 이름은 상한까지
    ]);
    // 반영 계산(sources·uncovered)은 모델 키와 상관없다
    expect(r.doc.coverage?.sources).toEqual([
      { id: "a", title: "원두" },
      { id: "b", title: "추출" },
      { id: "d", title: "포장" },
      { id: "e", title: "긴 이름" },
    ]);
  });
});

describe("parseSlideOutline", () => {
  const outline = {
    title: "카르파티 LLM 위키 구축하기",
    format: "detailed",
    slides: [
      { title: "LLM 위키", summary: "오프닝" },
      { title: "붕괴의 법칙", summary: "" },
    ],
  };

  it("제목·요약 목록으로 정규화하고 빈 요약은 뺀다", () => {
    const r = parseSlideOutline(JSON.stringify(outline));
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.doc.slides).toEqual([{ title: "LLM 위키", summary: "오프닝" }, { title: "붕괴의 법칙" }]);
  });

  it("슬라이드 제목이 없으면 실패한다", () => {
    expect(parseSlideOutline('{"title": "t", "slides": [{"summary": "s"}]}')).toMatchObject({
      reason: "invalid_shape",
      detail: "slides[0].title이 비어있습니다",
    });
  });

  it("serializeOutline은 정규화한 개요를 그대로 되읽힌다", () => {
    const r = parseSlideOutline(JSON.stringify(outline));
    if (!r.ok) throw new Error("parse");
    const again = parseSlideOutline(serializeOutline(r.doc));
    expect(again.ok && again.doc).toEqual(r.doc);
  });

  it("옛 개요(소스 목록 없음)는 빈 소스 목록이다", () => {
    const r = parseSlideOutline(JSON.stringify(outline));
    expect(r.ok && [r.doc.sources, r.doc.degraded]).toEqual([[], []]);
    expect(r.ok && "digest" in r.doc).toBe(false);
  });

  it("소스 요약 모델(#239) — 개요의 sources에서 소스별 모델·캐시 여부를 읽는다. serializeOutline은 넘기지 않는다", () => {
    const r = parseSlideOutline(
      JSON.stringify({
        ...outline,
        sources: [
          { id: "a", title: "원두", cached: true, model_id: "m1", model_name: "gemini-3.8-flash" },
          { id: "b", title: "추출", cached: false, model_id: "m2", model_name: "gpt-oss-120b" },
        ],
      }),
    );
    if (!r.ok) throw new Error("parse");
    expect(r.doc.digest).toEqual([
      { id: "a", title: "원두", model: "gemini-3.8-flash", cached: true },
      { id: "b", title: "추출", model: "gpt-oss-120b", cached: false },
    ]);
    expect(JSON.parse(serializeOutline(r.doc))).not.toHaveProperty("digest");
  });
});

describe("parseSlideOutline — 장별 검색어·근거 소스(#218)", () => {
  const outline = {
    title: "커피 입문",
    format: "presenter",
    slides: [
      {
        title: "표지",
        summary: "오프닝",
        layout: "title",
        query: " 커피 역사 ",
        source_ids: ["a", "a", " b ", 3, "", "x".repeat(65)],
        format: "presenter",
        language: "한국어",
      },
      { title: "모르는 값", layout: "poster", format: "poster", language: "가".repeat(21), source_ids: [] },
      { title: "키 없음" },
    ],
    sources: [
      { id: "a", title: "원두", cached: true },
      { id: "b", title: "추출", cached: false },
      { id: "c", title: "로스팅", cached: false },
    ],
    degraded: [{ id: "c", title: "로스팅", reason: "no_content", fallback: "none" }],
  };

  it("장 항목의 새 키를 정규화한다 — 모르는 layout·format·긴 언어는 빼고, 빈 근거 목록은 남긴다", () => {
    const r = parseSlideOutline(JSON.stringify(outline));
    if (!r.ok) throw new Error("parse");
    expect(r.doc.slides).toEqual([
      {
        title: "표지",
        summary: "오프닝",
        layout: "title",
        query: "커피 역사",
        source_ids: ["a", "b"],
        format: "presenter",
        language: "한국어",
      },
      { title: "모르는 값", source_ids: [] },
      { title: "키 없음" },
    ]);
    expect(r.doc.sources).toEqual([
      { id: "a", title: "원두" },
      { id: "b", title: "추출" },
      { id: "c", title: "로스팅" },
    ]);
    expect(r.doc.degraded).toEqual([{ id: "c", title: "로스팅", reason: "no_content" }]);
    expect(unassignedOutlineSources(r.doc)).toEqual([{ id: "c", title: "로스팅" }]);
  });

  it("근거는 장마다 상한까지, 장은 덱 loop 상한까지", () => {
    const many = {
      title: "t",
      slides: Array.from({ length: OUTLINE_MAX_SLIDES + 3 }, (_, i) => ({
        title: `s${i}`,
        source_ids: Array.from({ length: OUTLINE_SOURCE_IDS_MAX + 4 }, (_, j) => `id-${j}`),
      })),
    };
    const r = parseSlideOutline(JSON.stringify(many));
    if (!r.ok) throw new Error("parse");
    expect(r.doc.slides).toHaveLength(OUTLINE_MAX_SLIDES);
    expect(r.doc.slides[0].source_ids).toHaveLength(OUTLINE_SOURCE_IDS_MAX);
  });

  it("serializeOutline은 장의 새 키를 남기고 소스 목록·대체 본문은 뺀다(덱 run이 소스 요약에서 다시 읽는다)", () => {
    const r = parseSlideOutline(JSON.stringify(outline));
    if (!r.ok) throw new Error("parse");
    const sent = JSON.parse(serializeOutline(r.doc));
    expect(Object.keys(sent)).toEqual(["title", "format", "slides"]);
    expect(sent.slides).toEqual(r.doc.slides);
  });
});

describe("slideImageArtifacts(core-v0.23.0-upgrade AC-9)", () => {
  const ref = (name: string, content_type = "image/png") => ({ key: `run/${name}`, name, content_type });

  it("이름의 번호(1부터)로 장 위치에 붙이고, 덱 밖·다른 타입·다른 이름은 버린다", () => {
    const map = slideImageArtifacts(
      [
        ref("slide-deck.json", "application/json"),
        ref("slide-image-1.png"),
        ref("slide-image-3.jpg", "image/jpeg"),
        ref("slide-image-4.png"), // 덱이 3장이면 밖
        ref("slide-image-0.png"), // 0은 슬라이드 번호가 아니다
        ref("slide-image-2.png", "application/octet-stream"),
        ref("slide-image-02.png"),
        ref("slide-image-2.png.txt"),
        ref("slide-image-.png"),
        ref("other-2.png"),
      ],
      3,
    );
    expect([...map.keys()].sort()).toEqual([0, 2]);
    expect(map.get(0)?.name).toBe("slide-image-1.png");
    expect(map.get(2)?.name).toBe("slide-image-3.jpg");
  });

  it("같은 번호가 둘이면 뒤의 것을 쓴다", () => {
    const map = slideImageArtifacts([ref("slide-image-2.png"), { ...ref("slide-image-2.jpeg", "image/jpeg") }], 5);
    expect(map.get(1)?.name).toBe("slide-image-2.jpeg");
    expect(map.size).toBe(1);
  });
});
