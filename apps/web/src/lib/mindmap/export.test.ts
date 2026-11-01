/**
 * 내보내기(AC-1.4). jsdom에 없는 canvas·Image·object URL만 대역으로 두고(§12 R-2) 우리 코드가 그 경계에 넘긴
 * 인자를 단언한다. 실제 PNG 바이트는 e2e(mindmap-studio.spec.ts S1)가 검증한다.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { toastError } = vi.hoisted(() => ({ toastError: vi.fn() }));
vi.mock("sonner", () => ({ toast: { error: toastError, success: vi.fn() } }));

import {
  buildMindmapSvg,
  escapeXml,
  exportMindmapJson,
  exportMindmapPng,
  PNG_MAX_SIDE,
  safeFileName,
} from "@/lib/mindmap/export";
import { layoutMindmap, type MindmapLayout } from "@/lib/mindmap/layout";
import { parseMindmap, type MindmapDoc } from "@/lib/mindmap/parse";

const SENTINEL = "</text><script>alert(1)</script>";

function doc(): MindmapDoc {
  const r = parseMindmap(
    JSON.stringify({
      version: 1,
      root: { id: "r", label: "루트 & \"따옴표\" 'x'", children: [{ id: "a", label: SENTINEL }] },
    }),
  );
  if (!r.ok) throw new Error("fixture");
  return r.doc;
}

let created: string[];
let revoked: string[];
let clicked: { href: string; download: string }[];

beforeEach(() => {
  vi.useFakeTimers();
  created = [];
  revoked = [];
  clicked = [];
  let i = 0;
  vi.stubGlobal("URL", {
    ...URL,
    createObjectURL: vi.fn(() => {
      const u = `blob:mock/${(i += 1)}`;
      created.push(u);
      return u;
    }),
    revokeObjectURL: vi.fn((u: string) => revoked.push(u)),
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    clicked.push({ href: this.href, download: this.download });
  });
  toastError.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("SVG 생성·이스케이프 (T1)", () => {
  it("escapeXml은 &<>\"'를 모두 바꾼다", () => {
    expect(escapeXml(`&<>"'`)).toBe("&amp;&lt;&gt;&quot;&apos;");
  });

  it("라벨 sentinel이 SVG에 태그로 들어가지 않는다(<script 0)", () => {
    const d = doc();
    const { svg, width, height } = buildMindmapSvg(layoutMindmap(d.root, new Set(["r"])));
    expect(svg).not.toContain("<script");
    expect(svg).toContain("&lt;/text&gt;&lt;script&gt;");
    expect(svg.startsWith("<svg")).toBe(true);
    expect(width).toBeGreaterThan(0);
    expect(height).toBeGreaterThan(0);
  });
});

describe("파일 이름 정화", () => {
  it("제어·형식·경로 문자를 지우고 80자로 자른다, 비면 「마인드맵」", () => {
    expect(safeFileName('a/b\\c:d*e?f"g<h>i|j\u0007\u202e', "png")).toBe("abcdefghij.png");
    expect(Array.from(safeFileName("가".repeat(100), "json").replace(/\.json$/, ""))).toHaveLength(80);
    expect(safeFileName("///", "json")).toBe("마인드맵.json");
  });
});

describe("JSON 다운로드", () => {
  it("정규화본을 application/json으로 내려받고 object URL을 해제한다", async () => {
    const blobs: Blob[] = [];
    (URL.createObjectURL as ReturnType<typeof vi.fn>).mockImplementation((b: Blob) => {
      blobs.push(b);
      created.push("blob:json");
      return "blob:json";
    });
    exportMindmapJson(doc(), "제목/이름");
    expect(clicked).toEqual([{ href: "blob:json", download: "제목이름.json" }]);
    expect(blobs[0].type).toBe("application/json");
    const text = await blobs[0].text();
    expect(JSON.parse(text).root.children[0].label).toBe(SENTINEL);
    vi.runAllTimers();
    expect(revoked).toEqual(["blob:json"]);
  });
});

describe("PNG 다운로드", () => {
  function stubImage(fail: boolean) {
    class FakeImage {
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      set src(_v: string) {
        queueMicrotask(() => (fail ? this.onerror?.() : this.onload?.()));
      }
    }
    vi.stubGlobal("Image", FakeImage);
  }

  function stubCanvas(sizes: { w: number; h: number }[], ok = true) {
    const drawImage = vi.fn();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(function (
      this: HTMLCanvasElement,
    ) {
      sizes.push({ w: this.width, h: this.height });
      return ok ? ({ fillRect: vi.fn(), drawImage, fillStyle: "" } as unknown as CanvasRenderingContext2D) : null;
    } as unknown as HTMLCanvasElement["getContext"]);
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((cb: BlobCallback) =>
      cb(new Blob(["png"], { type: "image/png" })),
    );
    return drawImage;
  }

  it("canvas를 2배로 그려 image/png를 내려받고, SVG·PNG object URL을 모두 해제한다", async () => {
    vi.useRealTimers();
    stubImage(false);
    const sizes: { w: number; h: number }[] = [];
    const drawImage = stubCanvas(sizes);
    const d = doc();
    const layout = layoutMindmap(d.root, new Set(["r"]));
    const { width, height } = buildMindmapSvg(layout);
    await expect(exportMindmapPng(layout, "제목")).resolves.toBe(true);
    expect(sizes[0]).toEqual({ w: width * 2, h: height * 2 });
    expect(drawImage).toHaveBeenCalledTimes(1);
    expect(clicked).toHaveLength(1);
    expect(clicked[0].download).toBe("제목.png");
    await new Promise((r) => setTimeout(r, 5));
    expect(new Set(revoked)).toEqual(new Set(created));
    expect(toastError).not.toHaveBeenCalled();
  });

  it("최대 변 16,384 px를 넘으면 축소한다", async () => {
    vi.useRealTimers();
    stubImage(false);
    const sizes: { w: number; h: number }[] = [];
    stubCanvas(sizes);
    const huge: MindmapLayout = { ...layoutMindmap(doc().root, new Set()), height: 20_000 };
    await exportMindmapPng(huge, "큰 그림");
    expect(Math.max(sizes[0].w, sizes[0].h)).toBeLessThanOrEqual(PNG_MAX_SIDE);
  });

  it("이미지 로드 실패는 오류 토스트로 끝나고 예외를 던지지 않는다", async () => {
    vi.useRealTimers();
    stubImage(true);
    stubCanvas([]);
    await expect(exportMindmapPng(layoutMindmap(doc().root, new Set()), "x")).resolves.toBe(false);
    expect(toastError).toHaveBeenCalledWith("이미지를 만들지 못했습니다");
    expect(clicked).toHaveLength(0);
    expect(revoked).toEqual(created);
  });

  it("canvas를 못 얻으면 오류 토스트", async () => {
    vi.useRealTimers();
    stubImage(false);
    stubCanvas([], false);
    await expect(exportMindmapPng(layoutMindmap(doc().root, new Set()), "x")).resolves.toBe(false);
    expect(toastError).toHaveBeenCalled();
  });
});

describe("렌더 경로에 dangerouslySetInnerHTML이 없다(T1)", () => {
  it.each(["export.ts", "parse.ts", "layout.ts", "../../components/agents/mindmap-viewer.tsx"])(
    "%s",
    (f) => {
      const src = readFileSync(path.resolve(__dirname, f), "utf8");
      expect(src).not.toContain("dangerouslySetInnerHTML");
    },
  );
});
