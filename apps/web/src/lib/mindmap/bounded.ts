/**
 * 산출물 소비 경계(mindmap-agent-skill AC-1.1 (c), T4).
 *
 * 제목 조회·뷰어·원본 보기·JSON 내보내기는 본문을 이 함수로 먼저 거른다. UTF-8 바이트가 1 MiB를 넘거나
 * 문자열 밖 괄호 깊이가 64를 넘으면 `JSON.parse`를 부르지 않고 앞부분 텍스트 미리보기만 돌려준다.
 * 보장 범위는 파싱·렌더 비용이다 — 전송·메모리 크기는 제한하지 않는다.
 */

export const MINDMAP_MAX_BYTES = 1_048_576;
export const MINDMAP_MAX_DEPTH = 64;
export const MINDMAP_PREVIEW_CHARS = 65_536;

export type BoundedText =
  | { kind: "ok"; text: string }
  | { kind: "too_large" | "too_deep"; preview: string };

export function utf8ByteLength(text: string): number {
  return new TextEncoder().encode(text).length;
}

/** 문자열 밖의 `{`·`[` 중첩 깊이가 `limit`를 넘는지 선형 스캔으로 판정한다(재귀 없음). */
export function exceedsBracketDepth(text: string, limit: number): boolean {
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text.charCodeAt(i);
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === 0x5c /* \ */) escaped = true;
      else if (ch === 0x22 /* " */) inString = false;
      continue;
    }
    if (ch === 0x22) inString = true;
    else if (ch === 0x7b /* { */ || ch === 0x5b /* [ */) {
      depth += 1;
      if (depth > limit) return true;
    } else if (ch === 0x7d /* } */ || ch === 0x5d /* ] */) {
      if (depth > 0) depth -= 1;
    }
  }
  return false;
}

export function bounded(text: string): BoundedText {
  if (utf8ByteLength(text) > MINDMAP_MAX_BYTES) {
    return { kind: "too_large", preview: text.slice(0, MINDMAP_PREVIEW_CHARS) };
  }
  if (exceedsBracketDepth(text, MINDMAP_MAX_DEPTH)) {
    return { kind: "too_deep", preview: text.slice(0, MINDMAP_PREVIEW_CHARS) };
  }
  return { kind: "ok", text };
}

/** 경계를 통과한 본문만 pretty JSON으로 바꾼다. 거부된 본문은 미리보기, JSON이 아니면 원문. */
export function boundedPrettyJson(text: string): string {
  const b = bounded(text);
  if (b.kind !== "ok") return b.preview;
  try {
    return JSON.stringify(JSON.parse(b.text), null, 2);
  } catch {
    return b.text;
  }
}
