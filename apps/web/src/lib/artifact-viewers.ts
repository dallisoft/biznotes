/**
 * 산출물 뷰어 레지스트리(mindmap-agent-skill AC-1.8, D-9) — (이름, 타입) → 뷰어 종류.
 *
 * 전용 content type을 쓰지 않는다(서빙에서 octet-stream으로 강등된다). 이름 + JSON 타입으로 고른다.
 * 새 전용 뷰어는 `NAMED_JSON_VIEWERS`에 한 줄 더한다.
 */
import { SLIDE_DECK_ARTIFACT_NAME, SLIDE_OUTLINE_ARTIFACT_NAME } from "@/lib/slide-deck/parse";

export type ArtifactViewerKind =
  | "audio"
  | "image"
  | "mindmap"
  | "flashcards"
  | "quiz"
  | "infographic"
  | "slide-deck"
  | "slide-outline"
  | "markdown"
  | "json"
  | "text";

/** 스튜디오 마인드맵 산출물 이름 — 시드 스킬 매니페스트 `skills/mindmap/biznotes.json`의 `artifact.name`과 같다(test_mindmap_contract.py가 대조). */
export const MINDMAP_ARTIFACT_NAME = "mindmap.json";

const NAMED_JSON_VIEWERS: Record<string, ArtifactViewerKind> = {
  [MINDMAP_ARTIFACT_NAME]: "mindmap",
  "flashcards.json": "flashcards",
  "quiz.json": "quiz",
  "infographic.json": "infographic",
  [SLIDE_DECK_ARTIFACT_NAME]: "slide-deck",
  [SLIDE_OUTLINE_ARTIFACT_NAME]: "slide-outline",
};

export function isJsonType(contentType: string): boolean {
  return contentType.includes("json");
}

export function isMarkdownArtifact(name: string, contentType: string): boolean {
  return contentType.includes("markdown") || /\.(md|markdown)$/i.test(name);
}

export function resolveArtifactViewer(name: string, contentType: string): ArtifactViewerKind {
  if (contentType.startsWith("audio/")) return "audio";
  // 이미지 산출물(코어 image_generate — 슬라이드 삽화 등, PNG·JPEG)은 텍스트로 읽지 않는다
  if (contentType.startsWith("image/")) return "image";
  if (isJsonType(contentType)) return NAMED_JSON_VIEWERS[name] ?? "json";
  if (isMarkdownArtifact(name, contentType)) return "markdown";
  return "text";
}

/** 텍스트 계열(메모 저장 등 텍스트 동작을 붙일 수 있는) 산출물인가. */
export function isTextyArtifact(name: string, contentType: string): boolean {
  return contentType.includes("text") || isMarkdownArtifact(name, contentType) || isJsonType(contentType);
}
