/**
 * 슬라이드 자료 PPTX 내보내기(docs/plans/slide-deck-studio.md D-12·AC-11) — `pptxgenjs`로 덱을 텍스트 상자·도형으로
 * 옮긴다. 와이드(13.333in × 7.5in) 한 장에 슬라이드 하나, `notes`는 발표자 노트다. 크기·색은 웹 슬라이드
 * (`slide-view.tsx`, `.slide-theme`)와 같은 비율이다 — 웹의 1cqw = 0.1333in = 9.6pt.
 *
 * - 라이브러리는 다운로드를 누를 때 동적으로 import한다(초기 번들 무영향).
 * - 저장은 `write({ outputType: "blob" })` + `downloadBlob`이다. `writeFile`은 Node 환경을 감지하면 디스크에
 *   쓰므로 쓰지 않는다.
 * - 본문은 라이브러리가 XML 이스케이프한다. XML 1.0이 허용하지 않는 제어 문자는 여기서 지운다(파일 손상 방지).
 */
import type PptxGenJS from "pptxgenjs";
import { downloadBlob, safeFileName } from "@/lib/mindmap/export";
import type { Slide, SlideDeckDoc } from "@/lib/slide-deck/parse";

type Pres = InstanceType<typeof PptxGenJS>;
type PptxSlide = ReturnType<Pres["addSlide"]>;
type TextProps = Parameters<PptxSlide["addText"]>[1];

/** `.slide-theme` oklch 팔레트의 sRGB 값. */
export const PPTX_PALETTE = {
  paper: "FAF6EE",
  panel: "F1EBDE",
  rule: "D2CAB9",
  ink: "171F2E",
  muted: "4B5361",
  accent: "1A57AD",
  warm: "B95115",
} as const;

const W = 13.333;
const H = 7.5;
const CQW = W / 100;
const PT = 9.6; // 1cqw의 pt
const PAD_X = 6 * CQW;
const PAD_TOP = 5 * CQW;
const BOTTOM = H - 5 * CQW;
const INNER_W = W - PAD_X * 2;
// PowerPoint에서 흔한 한글 글꼴 — 없으면 Office가 대체 글꼴로 그린다
const FONT = "Malgun Gothic";

// XML 1.0 금지 문자(탭·줄바꿈·CR 제외 C0 제어, U+FFFE·FFFF)
const XML_BAD = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\uFFFE\uFFFF]/g;
// 짝 없는 서로게이트 — 브라우저 JSZip의 UTF-8 인코더는 이것을 잘못된 바이트로 쓴다
const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g;

/** XML·UTF-8로 안전한 문자열 — 금지 제어 문자는 지우고 짝 없는 서로게이트는 U+FFFD로 바꾼다. */
export function xmlSafe(text: string): string {
  return text.replace(XML_BAD, "").replace(LONE_SURROGATE, "\uFFFD");
}

const pt = (cqw: number) => Math.round(cqw * PT * 10) / 10;

const MIN_FONT_PT = 8;
const LINE_HEIGHT = 1.2;

/** 글자 폭 어림(em) — 한글·CJK·전각·그림 문자는 1, 그 밖은 0.55. */
function charEm(ch: string): number {
  const c = ch.codePointAt(0) ?? 0;
  const wide =
    (c >= 0x1100 && c <= 0x115f) ||
    (c >= 0x2e80 && c <= 0xa4cf) ||
    (c >= 0xac00 && c <= 0xd7a3) ||
    (c >= 0xf900 && c <= 0xfaff) ||
    (c >= 0xfe30 && c <= 0xfe4f) ||
    (c >= 0xff00 && c <= 0xff60) ||
    (c >= 0xffe0 && c <= 0xffe6) ||
    c >= 0x1f300;
  return wide ? 1 : 0.55;
}

/**
 * 상자(w × h in)에 문단들이 들어가는 가장 큰 글꼴 크기(pt) — `size`에서 0.5pt씩 줄이고 `MIN_FONT_PT`에서 멈춘다.
 * `fit: "shrink"`는 PowerPoint가 편집·리사이즈 뒤에야 적용하므로 처음 열 때의 넘침은 여기서 막는다.
 * 단어 단위 줄바꿈을 글자 단위로 어림하므로 한 줄 폭을 10% 줄여 보수적으로 잰다.
 */
export function fitFontSize(paragraphs: string[], w: number, h: number, size: number, paraGapPt = 0): number {
  for (let s = size; s > MIN_FONT_PT; s = Math.round((s - 0.5) * 10) / 10) {
    const lineEm = ((w * 72) / s) * 0.9;
    const lines = paragraphs.reduce(
      (n, p) => n + Math.max(1, Math.ceil(Array.from(p).reduce((a, ch) => a + charEm(ch), 0) / lineEm)),
      0,
    );
    const height = (lines * s * LINE_HEIGHT + paraGapPt * Math.max(0, paragraphs.length - 1)) / 72;
    if (height <= h) return s;
  }
  return MIN_FONT_PT;
}

type Box = { x: number; y: number; w: number; h: number; fontSize: number };

function text(s: PptxSlide, value: string, opts: TextProps & Box) {
  const safe = xmlSafe(value);
  const fontSize = fitFontSize(safe.split(/\r?\n/), opts.w, opts.h, opts.fontSize);
  s.addText(safe, { fontFace: FONT, color: PPTX_PALETTE.ink, margin: 0, valign: "top", fit: "shrink", ...opts, fontSize });
}

const BULLET_INDENT = 0.3; // 글머리표 들여쓰기(in) — 맞춤 계산에서 폭을 뺀다
const BULLET_GAP_PT = 6;

function bullets(s: PptxSlide, items: string[], y: number, h: number, opts: { check?: boolean; x?: number; w?: number; size?: number } = {}) {
  if (items.length === 0 || h <= 0) return;
  const safe = items.map(xmlSafe);
  const w = opts.w ?? INNER_W;
  s.addText(
    safe.map((b) => ({
      text: b,
      options: { bullet: opts.check ? { code: "2713", indent: BULLET_INDENT * 72 } : { indent: BULLET_INDENT * 72 }, breakLine: true },
    })),
    {
      x: opts.x ?? PAD_X,
      y,
      w,
      h,
      fontFace: FONT,
      fontSize: fitFontSize(safe, w - BULLET_INDENT, h, opts.size ?? pt(1.9), BULLET_GAP_PT),
      color: PPTX_PALETTE.ink,
      valign: "top",
      margin: 0,
      paraSpaceAfter: BULLET_GAP_PT,
      fit: "shrink",
    },
  );
}

/** 제목·부제·본문을 위에서부터 놓고 다음 내용이 시작할 y를 돌려준다. `cw`는 본문 칸 너비다. */
function header(s: PptxSlide, slide: Slide, cw: number, opts: { headingColor?: string; subColor?: string } = {}): number {
  let y = PAD_TOP;
  text(s, slide.heading, { x: PAD_X, y, w: cw, h: 1.05, fontSize: pt(3.6), bold: true, color: opts.headingColor });
  y += 1.1;
  if (slide.subheading) {
    text(s, slide.subheading, { x: PAD_X, y, w: cw, h: 0.45, fontSize: pt(2.1), color: opts.subColor ?? PPTX_PALETTE.warm });
    y += 0.5;
  }
  if (slide.body) {
    text(s, slide.body, { x: PAD_X, y, w: cw, h: 0.85, fontSize: pt(1.75), color: PPTX_PALETTE.muted });
    y += 0.9;
  }
  return y + 0.15;
}

function panel(pres: Pres, s: PptxSlide, x: number, y: number, w: number, h: number) {
  s.addShape(pres.ShapeType.roundRect, { x, y, w, h, fill: { color: PPTX_PALETTE.panel }, line: { color: PPTX_PALETTE.panel }, rectRadius: 0.12 });
}

/** layout별 본문 — `cw`는 본문 칸 너비(삽화가 있으면 왼쪽 60%). */
function drawBody(pres: Pres, s: PptxSlide, slide: Slide, cw: number) {
  switch (slide.layout) {
    case "title": {
      s.addShape(pres.ShapeType.roundRect, { x: PAD_X, y: 2.0, w: 9 * CQW, h: 0.6 * CQW, fill: { color: PPTX_PALETTE.accent }, line: { color: PPTX_PALETTE.accent }, rectRadius: 0.04 });
      text(s, slide.heading, { x: PAD_X, y: 2.35, w: cw, h: 1.9, fontSize: pt(5.4), bold: true });
      if (slide.subheading) text(s, slide.subheading, { x: PAD_X, y: 4.35, w: cw, h: 0.6, fontSize: pt(2.4), bold: true, color: PPTX_PALETTE.warm });
      if (slide.body) text(s, slide.body, { x: PAD_X, y: 5.0, w: cw, h: 1.0, fontSize: pt(1.75), color: PPTX_PALETTE.muted });
      return;
    }
    case "section": {
      const barX = PAD_X;
      const textX = barX + 1 * CQW + 4 * CQW;
      const textW = PAD_X + cw - textX;
      s.addShape(pres.ShapeType.roundRect, { x: barX, y: (H - 22 * CQW) / 2, w: 1 * CQW, h: 22 * CQW, fill: { color: PPTX_PALETTE.warm }, line: { color: PPTX_PALETTE.warm }, rectRadius: 0.06 });
      text(s, slide.heading, { x: textX, y: 2.2, w: textW, h: 1.5, fontSize: pt(4.6), bold: true, valign: "bottom" });
      if (slide.subheading) text(s, slide.subheading, { x: textX, y: 3.8, w: textW, h: 0.55, fontSize: pt(2.2), color: PPTX_PALETTE.muted });
      if (slide.body) text(s, slide.body, { x: textX, y: 4.4, w: textW, h: 0.9, fontSize: pt(1.75), color: PPTX_PALETTE.muted });
      return;
    }
    case "two_column": {
      const y = header(s, { ...slide, subheading: undefined }, cw);
      const gap = 2.4 * CQW;
      const colW = (cw - gap) / 2;
      const h = BOTTOM - y;
      slide.columns.forEach((c, i) => {
        const x = PAD_X + i * (colW + gap);
        panel(pres, s, x, y, colW, h);
        const inset = 2.4 * CQW;
        let cy = y + inset;
        if (c.heading) {
          text(s, c.heading, { x: x + inset, y: cy, w: colW - inset * 2, h: 0.45, fontSize: pt(2.3), bold: true, color: i === 0 ? PPTX_PALETTE.accent : PPTX_PALETTE.warm });
          cy += 0.55;
        }
        bullets(s, c.bullets, cy, y + h - inset - cy, { x: x + inset, w: colW - inset * 2 });
      });
      return;
    }
    case "steps": {
      const y = header(s, { ...slide, subheading: undefined }, cw) + 0.1;
      const n = slide.steps.length;
      const gap = 1.6 * CQW;
      const boxW = (cw - gap * (n - 1)) / n;
      const h = Math.min(BOTTOM - y, 3.6);
      const inset = 1.8 * CQW;
      const dot = 3.6 * CQW;
      slide.steps.forEach((st, i) => {
        const x = PAD_X + i * (boxW + gap);
        panel(pres, s, x, y, boxW, h);
        s.addText(String(i + 1), {
          shape: pres.ShapeType.ellipse,
          x: x + inset,
          y: y + inset,
          w: dot,
          h: dot,
          fill: { color: PPTX_PALETTE.accent },
          color: PPTX_PALETTE.paper,
          fontFace: FONT,
          fontSize: pt(1.8),
          bold: true,
          align: "center",
          valign: "middle",
          margin: 0,
        });
        const ty = y + inset + dot + 1.4 * CQW;
        text(s, st.label, { x: x + inset, y: ty, w: boxW - inset * 2, h: 0.75, fontSize: pt(2), bold: true });
        if (st.detail) text(s, st.detail, { x: x + inset, y: ty + 0.8, w: boxW - inset * 2, h: y + h - inset - (ty + 0.8), fontSize: pt(1.5), color: PPTX_PALETTE.muted });
      });
      return;
    }
    case "stats": {
      const y = header(s, { ...slide, subheading: undefined, body: undefined }, cw) + 0.15;
      const n = slide.stats.length;
      const gap = 2 * CQW;
      const boxW = (cw - gap * (n - 1)) / n;
      const h = 2.2;
      const inset = 2.4 * CQW;
      slide.stats.forEach((st, i) => {
        const x = PAD_X + i * (boxW + gap);
        panel(pres, s, x, y, boxW, h);
        text(s, st.value, { x: x + inset, y: y + inset, w: boxW - inset * 2, h: 0.85, fontSize: pt(5.2), bold: true, color: PPTX_PALETTE.accent });
        text(s, st.label, { x: x + inset, y: y + inset + 0.95, w: boxW - inset * 2, h: h - inset * 2 - 0.95, fontSize: pt(1.8) });
      });
      if (slide.body) text(s, slide.body, { x: PAD_X, y: y + h + 0.3, w: cw, h: BOTTOM - (y + h + 0.3), fontSize: pt(1.75), color: PPTX_PALETTE.muted });
      return;
    }
    case "quote": {
      text(s, slide.heading, { x: PAD_X, y: 1.6, w: cw, h: 0.4, fontSize: pt(2), bold: true, color: PPTX_PALETTE.muted });
      s.addShape(pres.ShapeType.rect, { x: PAD_X, y: 2.25, w: 0.8 * CQW, h: 3.0, fill: { color: PPTX_PALETTE.warm }, line: { color: PPTX_PALETTE.warm } });
      const qx = PAD_X + 0.8 * CQW + 3 * CQW;
      text(s, `“${slide.quote?.text ?? ""}”`, { x: qx, y: 2.25, w: PAD_X + cw - qx, h: 2.3, fontSize: pt(3.4), bold: true });
      if (slide.quote?.attribution) text(s, `— ${slide.quote.attribution}`, { x: qx, y: 4.7, w: PAD_X + cw - qx, h: 0.5, fontSize: pt(1.9), color: PPTX_PALETTE.muted });
      return;
    }
    case "closing": {
      const y = header(s, slide, cw, { headingColor: PPTX_PALETTE.accent, subColor: PPTX_PALETTE.muted });
      bullets(s, slide.bullets, y, BOTTOM - y, { check: true, w: cw });
      return;
    }
    default: {
      const y = header(s, slide, cw);
      bullets(s, slide.bullets, y, BOTTOM - y, { w: cw });
    }
  }
}

/** 삽화 한 장 — base64 data URL과 원본 크기(px). 크기는 칸을 채우며 잘라 낼(cover) 비율 계산에 쓴다. */
export interface PptxImage {
  data: string;
  width: number;
  height: number;
}

/** 삽화 칸(D3) — 웹 `slide-view.tsx`와 같다: 테두리 안쪽 오른쪽 40%(left 60cqw), 본문은 57cqw까지. */
export const PPTX_IMAGE_BOX = { x: 60 * CQW, y: 2.2 * CQW, w: 37.8 * CQW, h: H - 4.4 * CQW } as const;
const BODY_W_WITH_IMAGE = (57 - 6) * CQW;

function image(s: PptxSlide, img: PptxImage) {
  const box = PPTX_IMAGE_BOX;
  // cover는 넘긴 w·h를 원본 비율로 읽고 칸(sizing)에 맞춰 가운데를 잘라 낸다
  const w = box.w;
  const h = (box.w * img.height) / img.width;
  s.addImage({ data: img.data, x: box.x, y: box.y, w, h, sizing: { type: "cover", w: box.w, h: box.h }, altText: "" });
}

/**
 * 덱 → PPTX 문서(쓰기 전). 생성자는 호출하는 쪽이 넘긴다 — 다운로드는 동적 import, 테스트는 정적 import.
 * `images`(장 위치 → 삽화)가 있는 장은 오른쪽 40%에 그림을 넣고 본문을 왼쪽 60%에 그린다.
 */
export function buildSlideDeckPptx(
  Pptx: typeof PptxGenJS,
  doc: SlideDeckDoc,
  images: ReadonlyMap<number, PptxImage> = new Map(),
): Pres {
  const pres = new Pptx();
  pres.layout = "LAYOUT_WIDE";
  pres.title = xmlSafe(doc.title);
  const total = doc.slides.length;
  doc.slides.forEach((slide, i) => {
    const s = pres.addSlide();
    s.background = { color: PPTX_PALETTE.paper };
    s.addShape(pres.ShapeType.roundRect, {
      x: 2.2 * CQW,
      y: 2.2 * CQW,
      w: W - 4.4 * CQW,
      h: H - 4.4 * CQW,
      fill: { color: PPTX_PALETTE.paper },
      line: { color: PPTX_PALETTE.rule, width: 1.25 },
      rectRadius: 0.08,
    });
    const img = images.get(i);
    if (img) image(s, img);
    drawBody(pres, s, slide, img ? BODY_W_WITH_IMAGE : INNER_W);
    // 쪽 번호가 삽화 위에 오면 웹처럼 종이색 바탕을 깐다
    const numW = img ? 0.8 : 1.2;
    text(s, `${i + 1} / ${total}`, {
      x: W - 3.2 * CQW - numW,
      y: H - 2.8 * CQW - 0.25,
      w: numW,
      h: 0.25,
      fontSize: pt(1.3),
      color: PPTX_PALETTE.muted,
      align: img ? "center" : "right",
      valign: img ? "middle" : "bottom",
      ...(img ? { fill: { color: PPTX_PALETTE.paper, transparency: 15 } } : {}),
    });
    if (slide.notes) s.addNotes(xmlSafe(slide.notes));
  });
  return pres;
}

/**
 * 화면에 보이는 삽화 URL → PPTX 그림. 읽거나 디코드하지 못한 그림은 null이고, 그 장은 이미지 없이 넣는다.
 * blob URL을 `fetch`하지 않는다. CSP `connect-src`가 `blob:`을 허용하지 않기 때문이다(`img-src`는 허용).
 * 대신 `<img>`로 디코드하고 canvas에서 PNG로 다시 쓴다. 같은 출처 blob이라 canvas가 오염되지 않는다.
 */
export async function loadPptxImage(url: string): Promise<PptxImage | null> {
  try {
    const img = new Image();
    img.src = url;
    await img.decode();
    const width = img.naturalWidth;
    const height = img.naturalHeight;
    if (!width || !height) return null;
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    ctx.drawImage(img, 0, 0);
    const data = canvas.toDataURL("image/png");
    return data.startsWith("data:image/png;base64,") ? { data, width, height } : null;
  } catch {
    return null;
  }
}

/**
 * 다운로드 — 라이브러리를 이때 불러와 Blob으로 쓰고 저장한다. 실패는 호출하는 쪽에 그대로 던진다.
 * 삽화는 뷰어가 이미 받은 object URL에서 읽는다(서버 재요청 없음). 그림 하나를 못 읽어도 파일은 만든다.
 */
export async function downloadSlideDeckPptx(doc: SlideDeckDoc, imageUrls: ReadonlyMap<number, string> = new Map()): Promise<void> {
  const [{ default: Pptx }, loaded] = await Promise.all([
    import("pptxgenjs"),
    Promise.all([...imageUrls].map(async ([i, url]) => [i, await loadPptxImage(url)] as const)),
  ]);
  const images = new Map<number, PptxImage>();
  for (const [i, img] of loaded) if (img) images.set(i, img);
  const blob = (await buildSlideDeckPptx(Pptx, doc, images).write({ outputType: "blob" })) as Blob;
  downloadBlob(blob, safeFileName(doc.title, "pptx", "슬라이드 자료"));
}
