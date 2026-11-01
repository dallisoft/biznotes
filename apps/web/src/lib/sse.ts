import type {
  ChatEvidence,
  ChatProvenance,
  ChatStreamEvent,
  Citation,
  SkillInstallOffer,
} from "@/lib/chat-types";

/** SSE 버퍼에서 완성된 프레임을 이벤트로 파싱하고, 미완성 잔여분을 돌려준다.
 *  잘못된 JSON 프레임은 건너뛴다 (스트림 전체를 중단시키지 않음). */
export function drainSseBuffer(buffer: string): {
  events: ChatStreamEvent[];
  rest: string;
} {
  const frames = buffer.split("\n\n");
  const rest = frames.pop() ?? "";
  const events: ChatStreamEvent[] = [];
  for (const frame of frames) {
    const line = frame.split("\n").find((l) => l.startsWith("data: "));
    if (!line) continue;
    try {
      events.push(JSON.parse(line.slice(6)) as ChatStreamEvent);
    } catch {
      // malformed frame — skip
    }
  }
  return { events, rest };
}

export interface StreamingState {
  content: string;
  citations: Citation[];
  error: string | null;
  isStreaming: boolean;
  /** [Phase 18b] 검색 근거가 부족하다고 판정된 응답인지 — UI 배너 표시용 */
  insufficientContext: boolean;
  /** [BL-22] 라이브 배지의 단일 진실 공급원 — 영속화된 provenance와 같은 값. */
  provenance: ChatProvenance | null;
  /**
   * [BL-22 / HCI-P2-5] **`provenance` 키가 아예 없는** 구 API 응답(`done.evidence`)의
   * 1 릴리스 fallback 전용 보관함.
   *
   * [P4-03] 키가 있는데 값이 `null`인 경우(=서버 빌더 실패)는 fallback 대상이 **아니다**.
   * 그때 저장된 `ChatMessageOut.provenance`도 `null`이라, 라이브에만 배지를 띄우면
   * 새로고침 후 사라지는 이원화가 되살아난다(BL-22가 없애려던 바로 그 증상).
   * 그래서 이 배열은 키 부재일 때만 채워지고, 키가 있으면 항상 비어 있다.
   */
  legacyEvidence: ChatEvidence[];
  /** [#259] 설치 확인 카드 — 이번 전송이 GitHub 스킬 설치 요청이었을 때만 있다. */
  installOffer: SkillInstallOffer | null;
}

export const INITIAL_STREAM: StreamingState = {
  content: "",
  citations: [],
  error: null,
  isStreaming: false,
  insufficientContext: false,
  provenance: null,
  legacyEvidence: [],
  installOffer: null,
};

/** 채팅 스트림 이벤트 → 상태 전이 (순수 리듀서) */
export function applyChatEvent(
  state: StreamingState,
  event: ChatStreamEvent,
): StreamingState {
  switch (event.type) {
    case "retrieval":
      return {
        ...state,
        citations: event.citations,
        insufficientContext: event.insufficient_context ?? false,
      };
    case "delta":
      return { ...state, content: state.content + event.text };
    case "done":
      return {
        ...state,
        citations: event.citations,
        insufficientContext: event.insufficient_context ?? false,
        provenance: event.provenance ?? null,
        // [P4-03] `"provenance" in event`로 **키 존재**를 본다. `?? `로는 부재와 null을
        // 구분할 수 없다.
        legacyEvidence: "provenance" in event ? [] : (event.evidence ?? []),
      };
    case "skill_install_offer":
      return { ...state, installOffer: event.offer };
    case "error":
      return { ...state, error: event.detail };
    default:
      return state;
  }
}

/**
 * [BL-22 / P4-02] 스트리밍(라이브) 메시지의 배지 입력을 고르는 **단일 선택식**.
 *
 * `chat-panel.tsx`가 스트리밍 메시지를 만들 때 이 함수를 쓴다 — 같은 식을 호출부에
 * 인라인으로 두면 테스트가 그 코드를 고정하지 못하고 모양만 베끼게 된다(§3.3-3).
 *
 * 우선순위: `provenance.items`(단일 진실 공급원) → `legacyEvidence`(키 부재 구 API의
 * 1 릴리스 fallback, HCI-P2-5 · 제거는 BL-41).
 */
export function selectStreamingEvidence(state: StreamingState): ChatEvidence[] {
  return state.provenance?.items ?? state.legacyEvidence;
}
