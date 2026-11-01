/**
 * 마인드맵 노드 → 채팅 질문 문구(mindmap-agent-skill AC-2.2, D-11, HCI-5).
 *
 * 정화는 표시 안전용이며 격리 근거가 아니다(T2) — 격리는 서버 `origin=mindmap` 정책이 맡는다.
 */
import { sanitizeLabel, truncateChars } from "@/lib/mindmap/parse";

export const DISCUSS_LABEL_CHARS = 80;
export const DISCUSS_PROMPT_MAX = 400;

function quoteSafe(label: string): string {
  return truncateChars(sanitizeLabel(label), DISCUSS_LABEL_CHARS)
    .replace(/「/g, "『")
    .replace(/」/g, "』");
}

export function discussPrompt(nodeLabel: string, parentLabel: string | null): string {
  const node = quoteSafe(nodeLabel);
  const text =
    parentLabel === null
      ? `이 소스들이 「${node}」에 대해 무엇을 말하는지 설명해 주세요.`
      : `이 소스들이 「${node}」에 대해 무엇을 말하는지 「${quoteSafe(parentLabel)}」의 맥락에서 설명해 주세요.`;
  return truncateChars(text, DISCUSS_PROMPT_MAX);
}
