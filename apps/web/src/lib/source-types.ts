import { FileQuestion, FileText, Globe, NotepadText, type LucideIcon } from "lucide-react";
import type { components } from "@biznotes/shared-types";

type Schemas = components["schemas"];

export type Source = Schemas["SourceOut"];
export type SourceStatus = Source["status"];
export type SourceDetail = Schemas["SourceDetailOut"];
export type SourceGuide = Schemas["SourceGuideOut"];
export type WebSearchResult = Schemas["WebSearchResult"];
export type SearchHit = Schemas["SearchHit"];
/** `GET /sources/upload-limits` — 업로드 최대 크기(바이트). 서버 업로드 판정과 같은 값이다(코어 v0.26.0 #388). */
export type SourceUploadLimits = Schemas["SourceUploadLimitsOut"];

export const STATUS_LABEL: Record<SourceStatus, string> = {
  pending: "대기 중",
  parsing: "파싱 중",
  chunking: "청킹 중",
  embedding: "임베딩 중",
  ready: "준비됨",
  failed: "실패",
};

// 조회를 전역(total)으로 만들어 옛 판·새 판의 미지의 값이 와도 크래시하지 않게 한다.
// 인덱싱 대신 반드시 아래 헬퍼를 쓸 것.
/** 백엔드 SOURCE_TYPES(biznotes_core/models/source.py) 정본. */
type KnownSourceType = "file" | "web" | "text";

// `satisfies`로 정본 키 누락을 컴파일 타임에 잡되, 조회는 string으로 받아 런타임 전역성을 유지한다.
const TYPE_ICON_MAP = {
  file: "📄",
  web: "🌐",
  text: "📝",
} satisfies Record<KnownSourceType, string>;

const TYPE_LUCIDE_MAP = {
  file: FileText,
  web: Globe,
  text: NotepadText,
} satisfies Record<KnownSourceType, LucideIcon>;

const FALLBACK_TYPE_ICON = "📄";
const FALLBACK_TYPE_LUCIDE: LucideIcon = FileQuestion;

/** 알 수 없는 소스 타입에도 안전한 이모지 아이콘 조회. */
export function sourceTypeIcon(type: string): string {
  return (TYPE_ICON_MAP as Record<string, string>)[type] ?? FALLBACK_TYPE_ICON;
}

/** 알 수 없는 소스 타입에도 안전한 lucide 아이콘 조회 — undefined 컴포넌트 렌더 방지. */
export function sourceTypeLucide(type: string): LucideIcon {
  return (TYPE_LUCIDE_MAP as Record<string, LucideIcon>)[type] ?? FALLBACK_TYPE_LUCIDE;
}
