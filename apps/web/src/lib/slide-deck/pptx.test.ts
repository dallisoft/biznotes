/**
 * 슬라이드 자료 PPTX(docs/plans/slide-deck-studio.md AC-11) — 생성 파일을 JSZip으로 열어 슬라이드·노트를 센다.
 * 삽화(core-v0.23.0-upgrade AC-10·D3)는 그림 수·칸 위치·본문 너비를 XML에서 잰다.
 */
import JSZip from "jszip";
import PptxGenJS from "pptxgenjs";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";
import { parseSlideDeck, type SlideDeckDoc } from "@/lib/slide-deck/parse";
import {
  buildSlideDeckPptx,
  downloadSlideDeckPptx,
  fitFontSize,
  loadPptxImage,
  PPTX_IMAGE_BOX,
  type PptxImage,
  xmlSafe,
} from "@/lib/slide-deck/pptx";

const DECK = {
  title: "LLM 위키",
  format: "presenter",
  slides: [
    { layout: "title", heading: "안드레이 카파시의 LLM 위키", subheading: "설계와 구현", notes: "인사와 소개" },
    { layout: "section", heading: "1부: 문제", subheading: "왜 무너지는가" },
    { layout: "bullets", heading: "붕괴의 법칙", body: "관리 비용이 커진다.", bullets: ["수집", "구조화"] },
    {
      layout: "two_column",
      heading: "RAG vs LLM 위키",
      columns: [
        { heading: "RAG", bullets: ["매번 조립"] },
        { heading: "LLM 위키", bullets: ["복리 축적"] },
      ],
    },
    { layout: "steps", heading: "수집 단계", steps: [{ label: "분석", detail: "문서를 읽는다" }, { label: "갱신" }] },
    { layout: "stats", heading: "사례", body: "출처: 공개 사례", stats: [{ value: "2,188", label: "문서" }, { value: "$50", label: "비용" }] },
    { layout: "quote", heading: "핵심", quote: { text: "지식은 복리로 쌓인다", attribution: "카파시" } },
    { layout: "closing", heading: "정리", bullets: ["오늘 시작하기"], notes: "질문을 받는다" },
  ],
};

function deck(raw: unknown): SlideDeckDoc {
  const parsed = parseSlideDeck(JSON.stringify(raw));
  if (!parsed.ok) throw new Error("parse");
  return parsed.doc;
}

async function unzip(doc: SlideDeckDoc, images?: ReadonlyMap<number, PptxImage>): Promise<JSZip> {
  const data = (await buildSlideDeckPptx(PptxGenJS, doc, images).write({ outputType: "uint8array" })) as Uint8Array;
  return JSZip.loadAsync(data);
}

function names(zip: JSZip, re: RegExp): string[] {
  return Object.keys(zip.files).filter((n) => re.test(n));
}

async function read(zip: JSZip, name: string): Promise<string> {
  const f = zip.file(name);
  if (!f) throw new Error(`missing ${name}`);
  return f.async("string");
}

function parsesAsXml(xml: string): boolean {
  return new DOMParser().parseFromString(xml, "application/xml").getElementsByTagName("parsererror").length === 0;
}

describe("buildSlideDeckPptx", () => {
  it("와이드 한 장에 슬라이드 하나 — layout 8종의 장 수와 본문, 발표자 노트를 담는다", async () => {
    const zip = await unzip(deck(DECK));
    expect(names(zip, /^ppt\/slides\/slide\d+\.xml$/)).toHaveLength(8);
    expect(await read(zip, "ppt/presentation.xml")).toContain('<p:sldSz cx="12192000" cy="6858000"');

    const slides = await Promise.all(DECK.slides.map((_, i) => read(zip, `ppt/slides/slide${i + 1}.xml`)));
    DECK.slides.forEach((s, i) => expect(slides[i]).toContain(s.heading));
    expect(slides[0]).toContain("설계와 구현");
    expect(slides[0]).toContain("1 / 8");
    expect(slides[3]).toContain("매번 조립");
    expect(slides[3]).toContain("복리 축적");
    expect(slides[4]).toContain("문서를 읽는다");
    expect(slides[5]).toContain("$50");
    expect(slides[5]).toContain("출처: 공개 사례");
    expect(slides[6]).toContain("지식은 복리로 쌓인다");
    expect(slides[6]).toContain("— 카파시");
    expect(slides[7]).toContain("오늘 시작하기");
    expect(slides[7]).toContain("8 / 8");

    // 노트가 있는 장(1·8)만 노트 본문을 담는다
    const notes = names(zip, /^ppt\/notesSlides\/notesSlide\d+\.xml$/);
    expect(notes).toHaveLength(8);
    expect(await read(zip, "ppt/notesSlides/notesSlide1.xml")).toContain("인사와 소개");
    expect(await read(zip, "ppt/notesSlides/notesSlide8.xml")).toContain("질문을 받는다");
    // 노트 슬라이드의 첫 텍스트가 노트 본문이다(둘째는 쪽 번호)
    expect((await read(zip, "ppt/notesSlides/notesSlide2.xml")).match(/<a:t>([^<]*)<\/a:t>/)?.[1]).toBe("");
  });

  it("본문의 마크업은 이스케이프하고 XML 금지 제어 문자는 지운다 — 파일이 깨지지 않는다", async () => {
    const hostile = {
      title: "t\u0000",
      slides: [
        {
          heading: '<a:t>머리</a:t> & "따옴표"\u0007',
          bullets: ["줄\u000b바꿈", "<p:sp/>"],
          notes: "노트\u001f]]>",
        },
      ],
    };
    const zip = await unzip(deck(hostile));
    const slide = await read(zip, "ppt/slides/slide1.xml");
    const note = await read(zip, "ppt/notesSlides/notesSlide1.xml");
    expect(parsesAsXml(slide)).toBe(true);
    expect(parsesAsXml(note)).toBe(true);
    expect(slide).toContain("&lt;a:t&gt;머리&lt;/a:t&gt; &amp; &quot;따옴표&quot;");
    expect(slide).toContain("줄바꿈");
    expect(slide).toContain("&lt;p:sp/&gt;");
    expect(note).toContain("노트]]&gt;");
    expect(`${slide}${note}`).not.toMatch(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/);
  });

  it("xmlSafe는 탭·줄바꿈은 남기고 나머지 C0 제어 문자와 U+FFFE·FFFF를 지운다", () => {
    expect(xmlSafe("a\tb\nc\rd\u0000e\u0008f\u000bg\u001fh\ufffei\uffff")).toBe("a\tb\nc\rdefghi");
  });
});

/** 텍스트 `t`를 담은 run의 글꼴 크기(pt). `<a:rPr … sz="1920">…<a:t>t</a:t>`. */
function sizeOf(xml: string, t: string): number | null {
  const at = xml.indexOf(`<a:t>${t}</a:t>`);
  if (at < 0) return null;
  const sizes = [...xml.slice(0, at).matchAll(/\bsz="(\d+)"/g)];
  return sizes.length > 0 ? Number(sizes[sizes.length - 1][1]) / 100 : null;
}

const LONE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;

describe("짝 없는 서로게이트(리뷰 H1)", () => {
  it("xmlSafe는 짝 없는 서로게이트만 U+FFFD로 바꾸고 정상 쌍(그림 문자)은 둔다", () => {
    expect(xmlSafe("a\ud800b\udc00c😀d\udbff")).toBe("a\ufffdb\ufffdc😀d\ufffd");
  });

  it("라이브러리에 넘기는 모든 문자열(제목·본문·불릿·노트)에 짝 없는 서로게이트가 없다", async () => {
    const seen: string[] = [];
    // 브라우저 JSZip은 짝 없는 서로게이트를 잘못된 UTF-8로 쓰고(Node는 Buffer가 대신 고친다) — 넘기는 값을 직접 본다
    function Recording() {
      const pres = new PptxGenJS();
      const addSlide = pres.addSlide.bind(pres);
      pres.addSlide = ((props?: Parameters<typeof addSlide>[0]) => {
        const slide = addSlide(props);
        const addText = slide.addText.bind(slide);
        const addNotes = slide.addNotes.bind(slide);
        slide.addText = (t: Parameters<typeof addText>[0], o: Parameters<typeof addText>[1]) => {
          seen.push(...(typeof t === "string" ? [t] : t.map((r) => r.text ?? "")));
          return addText(t, o);
        };
        slide.addNotes = (n: string) => {
          seen.push(n);
          return addNotes(n);
        };
        return slide;
      }) as typeof pres.addSlide;
      return pres;
    }
    const pres = buildSlideDeckPptx(Recording as unknown as typeof PptxGenJS, deck({
      title: "제목\ud800",
      slides: [{ heading: "머리\udc00", body: "본문\ud83d", bullets: ["불릿\ude00", "정상😀"], notes: "노트\ud800" }],
    }));
    expect(pres.title).toBe("제목\ufffd");
    expect(seen.join("")).not.toMatch(LONE);
    expect(seen).toEqual(expect.arrayContaining(["머리\ufffd", "본문\ufffd", "불릿\ufffd", "정상😀", "노트\ufffd"]));
  });
});

describe("처음 열 때의 넘침(리뷰 M1)", () => {
  it("fitFontSize — 들어가면 그대로, 넘치면 줄이고, 끝없이 길면 하한 8pt", () => {
    expect(fitFontSize(["짧은 제목"], 4, 1, 19.2)).toBe(19.2);
    const long = "가".repeat(30);
    const fitted = fitFontSize([long], 2, 1.2, 19.2);
    expect(fitted).toBeLessThan(19.2);
    expect(fitted).toBeGreaterThan(8);
    // 줄인 크기에서 어림 높이가 상자 안이다(한 줄 폭 = 폭 × 72 / pt × 0.9 em)
    const lines = Math.ceil(30 / (((2 * 72) / fitted) * 0.9));
    expect((lines * fitted * 1.2) / 72).toBeLessThanOrEqual(1.2);
    expect(fitFontSize(["가".repeat(5000)], 1, 0.5, 19.2)).toBe(8);
  });

  it("단계 8개·긴 레이블이면 레이블 글꼴을 줄이고, 짧은 레이블은 기본 크기(19.2pt)를 지킨다", async () => {
    const long = "가".repeat(40);
    const steps = (label: string) => Array.from({ length: 8 }, () => ({ label, detail: "설명" }));
    const zip = await unzip(deck({ title: "t", slides: [
      { layout: "steps", heading: "긴 단계", steps: steps(long) },
      { layout: "steps", heading: "짧은 단계", steps: steps("분석") },
    ] }));
    const longXml = await read(zip, "ppt/slides/slide1.xml");
    const shortXml = await read(zip, "ppt/slides/slide2.xml");
    expect(sizeOf(longXml, "긴 단계")).toBe(34.6);
    expect(sizeOf(longXml, long)).toBeLessThan(19.2);
    expect(sizeOf(shortXml, "분석")).toBe(19.2);
  });

  it("불릿이 많고 길면 불릿 글꼴을 줄인다", async () => {
    const many = Array.from({ length: 8 }, (_, i) => `${i}번 ${"나".repeat(120)}`);
    const zip = await unzip(deck({ title: "t", slides: [
      { heading: "많은 불릿", bullets: many },
      { heading: "적은 불릿", bullets: ["하나", "둘"] },
    ] }));
    expect(sizeOf(await read(zip, "ppt/slides/slide1.xml"), many[0])).toBeLessThan(18.2);
    expect(sizeOf(await read(zip, "ppt/slides/slide2.xml"), "하나")).toBe(18.2);
  });
});

describe("downloadSlideDeckPptx", () => {
  let saved: { blob: Blob; download: string }[];

  beforeEach(() => {
    saved = [];
    const blobs = new Map<string, Blob>();
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn((b: Blob) => {
        const u = `blob:mock/${blobs.size + 1}`;
        blobs.set(u, b);
        return u;
      }),
      revokeObjectURL: vi.fn(),
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      saved.push({ blob: blobs.get(this.href) as Blob, download: this.download });
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("덱 제목으로 .pptx를 저장하고, 저장한 Blob이 열리는 PPTX다", async () => {
    await downloadSlideDeckPptx(deck(DECK));
    expect(saved.map((s) => s.download)).toEqual(["LLM 위키.pptx"]);
    const zip = await JSZip.loadAsync(await saved[0].blob.arrayBuffer());
    expect(names(zip, /^ppt\/slides\/slide\d+\.xml$/)).toHaveLength(8);
  });

  it("제목이 파일 이름으로 쓸 수 없는 문자뿐이면 기본 이름을 쓴다", async () => {
    await downloadSlideDeckPptx(deck({ ...DECK, title: '/:*?"<>|' }));
    expect(saved.map((s) => s.download)).toEqual(["슬라이드 자료.pptx"]);
  });

  it("뷰어의 삽화 URL을 읽어 넣고, 읽지 못한 그림은 빼고 파일을 만든다", async () => {
    stubImageLoading({ "blob:ok": { width: 1024, height: 1536 }, "blob:broken": "broken", "blob:empty": { width: 0, height: 0 } });
    await downloadSlideDeckPptx(
      deck(DECK),
      new Map([
        [0, "blob:ok"],
        [2, "blob:broken"],
        [3, "blob:empty"],
      ]),
    );
    expect(saved).toHaveLength(1);
    const zip = await JSZip.loadAsync(await saved[0].blob.arrayBuffer());
    expect(names(zip, /^ppt\/media\/./)).toHaveLength(1);
    expect(await read(zip, "ppt/slides/slide1.xml")).toContain("<p:pic>");
    for (const n of [3, 4]) expect(await read(zip, `ppt/slides/slide${n}.xml`)).not.toContain("<p:pic>");
  });
});

// 2×3 RGB PNG — 세로로 긴 그림(코어 aspect_ratio 2:3과 같은 비율)
const PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAIAAAADCAIAAAA2iEnWAAAAEElEQVR4nGM4UaEBRAwoFABcbQhxorYfuAAAAABJRU5ErkJggg==";
const PNG: PptxImage = { data: `data:image/png;base64,${PNG_BASE64}`, width: 2, height: 3 };
const EMU = 914400;

/**
 * 브라우저 그림 디코드 경계 — jsdom에는 `HTMLImageElement.decode`·canvas 2D가 없다. `url → 결과`로 디코드 성공·크기를
 * 정하고, canvas는 받은 그림을 PNG data URL로 쓴다고 대역한다.
 */
function stubImageLoading(images: Record<string, { width: number; height: number } | "broken">) {
  Object.defineProperty(HTMLImageElement.prototype, "decode", {
    configurable: true,
    value(this: HTMLImageElement) {
      const found = images[this.getAttribute("src") ?? ""];
      if (!found || found === "broken") return Promise.reject(new Error("decode"));
      Object.defineProperty(this, "naturalWidth", { configurable: true, value: found.width });
      Object.defineProperty(this, "naturalHeight", { configurable: true, value: found.height });
      return Promise.resolve();
    },
  });
  const drawImage = vi.fn();
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
    () => ({ drawImage }) as unknown as CanvasRenderingContext2D,
  );
  vi.spyOn(HTMLCanvasElement.prototype, "toDataURL").mockImplementation(() => PNG.data);
  onTestFinished(() => {
    delete (HTMLImageElement.prototype as { decode?: unknown }).decode;
  });
  return { drawImage };
}

function shapes(xml: string, tag: "p:pic" | "p:sp"): string[] {
  return xml.split(`<${tag}>`).slice(1).map((part) => part.split(`</${tag}>`)[0]);
}

function frame(shape: string): { x: number; y: number; cx: number; cy: number } {
  const off = /<a:off x="(\d+)" y="(\d+)"\/>/.exec(shape);
  const ext = /<a:ext cx="(\d+)" cy="(\d+)"\/>/.exec(shape);
  if (!off || !ext) throw new Error("no xfrm");
  return { x: Number(off[1]), y: Number(off[2]), cx: Number(ext[1]), cy: Number(ext[2]) };
}

function headingWidth(xml: string, heading: string): number {
  const [sp] = shapes(xml, "p:sp").filter((s) => s.includes(`<a:t>${heading}</a:t>`));
  return frame(sp).cx / EMU;
}

describe("삽화(core-v0.23.0-upgrade AC-10·D3)", () => {
  it("삽화가 있는 장만 그림 하나를 오른쪽 40% 칸에 넣고 본문을 왼쪽 60%로 줄인다", async () => {
    const doc = deck(DECK);
    const zip = await unzip(
      doc,
      new Map([
        [0, PNG],
        [2, PNG],
      ]),
    );
    expect(names(zip, /^ppt\/media\/./)).toHaveLength(2);
    const slides = await Promise.all([1, 2, 3].map((n) => read(zip, `ppt/slides/slide${n}.xml`)));
    expect(shapes(slides[0], "p:pic")).toHaveLength(1);
    expect(shapes(slides[1], "p:pic")).toHaveLength(0);
    const [pic] = shapes(slides[2], "p:pic");
    const f = frame(pic);
    expect(f.x / EMU).toBeCloseTo(PPTX_IMAGE_BOX.x, 3);
    expect(f.y / EMU).toBeCloseTo(PPTX_IMAGE_BOX.y, 3);
    expect(f.cx / EMU).toBeCloseTo(PPTX_IMAGE_BOX.w, 3);
    expect(f.cy / EMU).toBeCloseTo(PPTX_IMAGE_BOX.h, 3);
    // 2:3(0.667)은 칸(약 0.73)보다 세로로 길다 — 좌우는 그대로, 위아래를 같은 비율로 잘라 칸을 채운다(cover)
    const crop = /<a:srcRect l="(-?\d+)" r="(-?\d+)" t="(-?\d+)" b="(-?\d+)"\/>/.exec(pic);
    expect(crop?.slice(1).map(Number)).toEqual([0, 0, expect.any(Number), expect.any(Number)]);
    expect(Number(crop?.[3])).toBeGreaterThan(0);
    expect(crop?.[3]).toBe(crop?.[4]);
    // 본문(제목 상자)은 칸 왼쪽(57cqw)에서 끝난다. 그림이 없는 장은 전체 너비다
    const cqw = 13.333 / 100;
    expect(headingWidth(slides[2], "붕괴의 법칙")).toBeCloseTo(51 * cqw, 2);
    expect(headingWidth(slides[1], "1부: 문제")).toBeGreaterThan(51 * cqw);
    const plain = await read(await unzip(doc), "ppt/slides/slide3.xml");
    expect(headingWidth(plain, "붕괴의 법칙")).toBeCloseTo(88 * cqw, 2);
    expect(shapes(plain, "p:pic")).toHaveLength(0);
  });

  it("layout 8종 모두 삽화와 함께 만들어지고 XML이 깨지지 않는다", async () => {
    const doc = deck(DECK);
    const zip = await unzip(doc, new Map(doc.slides.map((_, i) => [i, PNG] as const)));
    expect(names(zip, /^ppt\/media\/./)).toHaveLength(8);
    for (let n = 1; n <= 8; n++) {
      const xml = await read(zip, `ppt/slides/slide${n}.xml`);
      expect(parsesAsXml(xml)).toBe(true);
      expect(shapes(xml, "p:pic")).toHaveLength(1);
    }
  });
});

describe("loadPptxImage", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("blob URL을 fetch하지 않고 <img>로 디코드해 원본 크기와 PNG data URL로 읽는다(CSP connect-src)", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    onTestFinished(() => {
      vi.unstubAllGlobals();
    });
    const { drawImage } = stubImageLoading({ "blob:ok": { width: 1024, height: 1536 } });
    const img = await loadPptxImage("blob:ok");
    expect(img).toEqual({ data: PNG.data, width: 1024, height: 1536 });
    expect(drawImage).toHaveBeenCalledTimes(1);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("디코드 실패·크기 0·canvas 없음이면 null", async () => {
    stubImageLoading({ "blob:broken": "broken", "blob:empty": { width: 0, height: 0 }, "blob:ok": { width: 2, height: 3 } });
    expect(await loadPptxImage("blob:broken")).toBeNull();
    expect(await loadPptxImage("blob:empty")).toBeNull();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => null);
    expect(await loadPptxImage("blob:ok")).toBeNull();
  });
});
