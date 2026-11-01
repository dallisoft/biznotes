import type { components } from "@biznotes/shared-types";

/**
 * R4-3ⓑ: 손으로 미러링하던 타입을 OpenAPI 생성 타입에서 파생한다.
 *
 * 이 세 타입은 드리프트 계측(`scripts/check-type-drift.py`)에서 **클린 판정**을 받은
 * 뒤에 교체했다. 백엔드가 `citations: list[Any]`를 `list[CitationOut]`으로,
 * `token_usage: dict[str, Any]`를 `dict[str, int]`로 좁혔기 때문에 생성 타입이
 * 더 이상 `unknown[]`이 아니다 — 좁히기 전에 교체했다면 `Citation[]`을 잃었다.
 */
type Schemas = components["schemas"];

/** 인용-청크 매칭 검증 결과(`verified`)는 과거 메시지엔 키가 없어 optional이다. */
export type Citation = Schemas["CitationOut"];
export type ChatMessage = Schemas["ChatMessageOut"];
export type ChatSession = Schemas["ChatSessionOut"];
export type RecentChat = Schemas["RecentChatOut"];

/** `/` 팔레트 항목(#66) — 코어 진입 판정을 서버가 직렬화한 값 그대로다. */
export type ChatEntry = Schemas["ChatEntryOut"];
/** 진입 질문 행에 붙는 run 요약(`ChatMessageOut.entry_run`). */
export type ChatEntryRunRef = Schemas["ChatEntryRunRefOut"];
/** 진입 실행 요청·응답(`POST /chat/sessions/{sid}/entry-runs`). */
export type ChatEntryRunRequest = Schemas["ChatEntryRunCreate"];
export type ChatEntryRunResult = Schemas["ChatEntryRunOut"];
/** 이름이 여러 항목에 맞을 때 409 본문의 `detail`. */
export type ChatEntryAmbiguous = Schemas["ChatEntryAmbiguousOut"];

export interface LLMModelInfo {
  id: string;
  display_name: string;
  provider_name: string;
  provider_type: string;
  is_default: boolean;
}

export interface ChatToolCall {
  tool: string;
  status: "succeeded" | "failed" | "blocked";
  arguments?: Record<string, unknown>;
}

/**
 * SSE `done.evidence`와 영속화된 `ChatMessageOut.provenance.items`는 같은 공개 계약
 * (`services/provenance.py::public_evidence_items` / `ProvenanceItemOut`)을 공유한다
 * — 생성 타입에서 파생해 두 경로의 shape이 갈라지지 않게 한다(note-provenance Phase 3).
 */
export type ChatEvidence = Schemas["ProvenanceItemOut"];

/**
 * SSE `done.provenance`와 영속화된 `ChatMessageOut.provenance`는 **같은 서버 헬퍼**
 * (`api/v1/chat.py::_sse_provenance` → `ProvenanceOut`)를 거친다 — 라이브 배지와
 * 새로고침 후 배지가 같은 데이터로 그려지게 하는 단일 진실 공급원이다(BL-22).
 */
export type ChatProvenance = Schemas["ProvenanceOut"];

/**
 * #259 GitHub 저장소 스킬 설치 확인 카드 — SSE `skill_install_offer` 이벤트의 `offer`.
 * HTTP 응답이 아니지만 서버가 이벤트 모델을 OpenAPI 컴포넌트로 내보낸다(`app/core/openapi_sse.py`).
 * 카드는 저장하지 않는다 — 새로고침하면 요약 답변만 남는다.
 */
export type SkillInstallOffer = Schemas["SkillInstallOfferOut"];
export type SkillInstallCandidate = Schemas["SkillInstallCandidateOut"];
export type SkillInstallOfferEvent = Schemas["SkillInstallOfferEvent"];

export type ChatStreamEvent =
  | { type: "retrieval"; citations: Citation[]; insufficient_context?: boolean }
  | { type: "delta"; text: string }
  | {
      type: "done";
      message_id: string;
      citations: Citation[];
      token_usage: Record<string, number>;
      model: string;
      insufficient_context?: boolean;
      /**
       * 배지의 단일 진실 공급원. 빌더 실패 시 `null`, 롤링 배포 중 구 API 응답에는
       * 키 자체가 없어 optional이다(HCI-P2-5).
       */
      provenance?: ChatProvenance | null;
      /**
       * @deprecated BL-41 — `provenance`로 대체됐다. 1 릴리스 뒤 서버에서 제거된다.
       * 정적 경로는 처음부터 보내지 않아 optional이다.
       */
      tool_calls?: ChatToolCall[];
      /**
       * @deprecated BL-41 — `provenance.items`로 대체됐다. `provenance`가 없는 구 API
       * 응답에 한해 1 릴리스 동안 fallback으로만 쓴다.
       */
      evidence?: ChatEvidence[];
    }
  | SkillInstallOfferEvent
  | { type: "error"; detail: string };
