/**
 * 마인드맵 내보내기(mindmap-agent-skill AC-1.4, D-8, T1) — 신규 의존성 없음.
 *
 * - JSON: 정규화본(`serializeMindmap`)을 `application/json`으로.
 * - PNG: 레이아웃에서 SVG 문자열을 만들고(라벨 XML 이스케이프) `<img>`로 읽어 canvas 2배(최대 변 16,384 px)에
 *   그린 뒤 `image/png`로. 배경은 밝은 고정 팔레트. object URL은 다운로드 뒤 해제한다.
 * - 실패(이미지 로드·canvas)는 오류 토스트로 끝낸다 — 예외를 위로 던지지 않는다.
 */
import { toast } from "sonner";
import type { MindmapLayout } from "@/lib/mindmap/layout";
import { NODE_LINE_HEIGHT, NODE_MAX_LINES } from "@/lib/mindmap/layout";
import { serializeMindmap, truncateChars, type MindmapDoc } from "@/lib/mindmap/parse";

export const PNG_SCALE = 2;
export const PNG_MAX_SIDE = 16_384;
const MARGIN = 24;

/** 내보내기 고정 팔레트(밝은 배경) — 깊이 0·1·2 이상. */
export const EXPORT_PALETTE = {
  background: "#ffffff",
  edge: "#94a3b8",
  text: "#0f172a",
  depth: ["#c7d2fe", "#bae6fd", "#d9f99d"],
  border: "#64748b",
} as const;

export function escapeXml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

const FILE_NAME_BAD = /[\p{Cc}\p{Cf}/\\:*?"<>|]/gu;

/** 다운로드 파일 이름 — 제어·형식·경로 문자 제거 + 80자 + 고정 확장자. 이름이 비면 `fallback`. */
export function safeFileName(title: string, ext: "json" | "png" | "pptx", fallback = "마인드맵"): string {
  const base = truncateChars(title.replace(FILE_NAME_BAD, "").replace(/\s+/g, " ").trim(), 80);
  return `${base || fallback}.${ext}`;
}

function wrapLabel(label: string, width: number): string[] {
  // 레이아웃 어림(글자 폭)과 같은 기준으로 줄을 나눈다.
  const maxChars = Math.max(4, Math.floor((width - 24) / 10));
  const chars = Array.from(label);
  const lines: string[] = [];
  for (let i = 0; i < chars.length && lines.length < NODE_MAX_LINES; i += maxChars) {
    lines.push(chars.slice(i, i + maxChars).join(""));
  }
  if (lines.length === NODE_MAX_LINES && chars.length > maxChars * NODE_MAX_LINES) {
    lines[NODE_MAX_LINES - 1] = truncateChars(lines[NODE_MAX_LINES - 1], maxChars);
  }
  return lines;
}

/** 현재 펼침 상태의 레이아웃을 독립 SVG 문서 문자열로. */
export function buildMindmapSvg(layout: MindmapLayout): { svg: string; width: number; height: number } {
  const width = Math.ceil(layout.width + MARGIN * 2);
  const height = Math.ceil(layout.height + MARGIN * 2);
  const byId = new Map(layout.nodes.map((n) => [n.id, n]));
  const parts: string[] = [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">`,
    `<rect width="100%" height="100%" fill="${EXPORT_PALETTE.background}"/>`,
    `<g transform="translate(${MARGIN} ${MARGIN})">`,
  ];
  for (const e of layout.edges) {
    const s = byId.get(e.source);
    const t = byId.get(e.target);
    if (!s || !t) continue;
    const x1 = s.x + s.width;
    const y1 = s.y + s.height / 2;
    const x2 = t.x;
    const y2 = t.y + t.height / 2;
    const mx = (x1 + x2) / 2;
    parts.push(
      `<path d="M${x1} ${y1} C${mx} ${y1} ${mx} ${y2} ${x2} ${y2}" fill="none" stroke="${EXPORT_PALETTE.edge}" stroke-width="1.5"/>`,
    );
  }
  for (const n of layout.nodes) {
    const fill = EXPORT_PALETTE.depth[Math.min(n.depth, EXPORT_PALETTE.depth.length - 1)];
    parts.push(
      `<rect x="${n.x}" y="${n.y}" width="${n.width}" height="${n.height}" rx="8" fill="${fill}" stroke="${EXPORT_PALETTE.border}" stroke-width="1"/>`,
    );
    const lines = wrapLabel(n.node.label, n.width);
    const startY = n.y + n.height / 2 - ((lines.length - 1) * NODE_LINE_HEIGHT) / 2 + 5;
    parts.push(
      `<text font-family="sans-serif" font-size="14" fill="${EXPORT_PALETTE.text}">` +
        lines
          .map(
            (line, i) =>
              `<tspan x="${n.x + 12}" y="${startY + i * NODE_LINE_HEIGHT}">${escapeXml(line)}</tspan>`,
          )
          .join("") +
        `</text>`,
    );
  }
  parts.push("</g></svg>");
  return { svg: parts.join(""), width, height };
}

export function downloadBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = url;
    a.download = fileName;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    // 클릭이 다운로드를 시작한 뒤 해제한다(같은 틱 해제는 일부 브라우저에서 다운로드를 끊는다).
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}

export function exportMindmapJson(doc: MindmapDoc, title: string): void {
  const blob = new Blob([serializeMindmap(doc)], { type: "application/json" });
  downloadBlob(blob, safeFileName(title, "json"));
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("image load failed"));
    img.src = src;
  });
}

function canvasToBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("toBlob failed"))), "image/png");
  });
}

/** PNG 내보내기. 성공하면 true, 실패하면 오류 토스트 후 false(예외 전파 없음). */
export async function exportMindmapPng(layout: MindmapLayout, title: string): Promise<boolean> {
  const { svg, width, height } = buildMindmapSvg(layout);
  const svgUrl = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
  try {
    const img = await loadImage(svgUrl);
    const scale = Math.min(PNG_SCALE, PNG_MAX_SIDE / Math.max(width, height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.floor(width * scale));
    canvas.height = Math.max(1, Math.floor(height * scale));
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("canvas unavailable");
    ctx.fillStyle = EXPORT_PALETTE.background;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    const blob = await canvasToBlob(canvas);
    downloadBlob(blob, safeFileName(title, "png"));
    return true;
  } catch {
    toast.error("이미지를 만들지 못했습니다");
    return false;
  } finally {
    URL.revokeObjectURL(svgUrl);
  }
}
