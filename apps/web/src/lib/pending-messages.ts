/**
 * 대기 user 메시지(mindmap-agent-skill AC-2.12, D-19) — 전송 즉시 질문을 보이고, 저장 여부를 아는 만큼만 말한다.
 *
 * - `sending`: 전송 중.
 * - `confirmed`: SSE `done`을 받았다(`assistantId = done.message_id`). 저장 목록에 그 id가 보이면 숨긴다 —
 *   서버 id로만 확정하므로 다른 전송과 섞이지 않는다. 그 뒤의 오류로 되돌리지 않는다.
 * - `not_saved`: 서버가 거절(4xx)했거나, 서버 쪽 종료 뒤 재조회에 전송 시점 밖 메시지가 하나도 없다.
 * - `uncertain`: abort·연결 단절·5xx·네트워크 오류, 또는 종료 뒤 새 메시지가 있어 소유를 가릴 수 없다.
 * `not_saved`·`uncertain`은 사용자가 닫을 때까지 그대로 둔다(다른 재조회로 바꾸지 않는다).
 */

export type PendingState = "sending" | "confirmed" | "uncertain" | "not_saved";

export interface PendingUserMessage {
  nonce: string;
  sessionId: string;
  content: string;
  /** 전송 시점에 이미 저장돼 있던 메시지 id */
  knownIds: string[];
  state: PendingState;
  assistantId?: string;
  origin: "chat" | "mindmap";
}

export const PENDING_STATE_LABEL: Partial<Record<PendingState, string>> = {
  uncertain: "응답 중단 — 전송 결과 확인 필요",
  not_saved: "전송되지 않음",
};

/** 이 대화에 보일 대기 행 — 확정된 전송은 저장 목록에 그 assistant id가 보이면 숨긴다. */
export function visiblePending(
  pending: PendingUserMessage[],
  sessionId: string | null,
  savedIds: ReadonlySet<string>,
): PendingUserMessage[] {
  return pending.filter(
    (p) =>
      p.sessionId === sessionId &&
      !(p.state === "confirmed" && p.assistantId !== undefined && savedIds.has(p.assistantId)),
  );
}

export function isDismissible(p: PendingUserMessage): boolean {
  return p.state === "uncertain" || p.state === "not_saved";
}
