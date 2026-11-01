import type { StatusTone } from "@/components/ui/status-badge";
import type { ChatEntry, ChatEntryAmbiguous, ChatEntryRunRef } from "@/lib/chat-types";

/**
 * 채팅 `/` 팔레트 진입(#66) — 입력 해석·필터·라벨 순수 함수.
 *
 * 판정(무엇이 진입 항목인가)은 서버가 코어로 한다(`GET /chat/entries`). 여기서는 입력창 문자열만 다룬다.
 * 접두는 `/`만이다(결정 1) — `$`로 시작하는 입력은 일반 메시지다.
 */

/** 첫 토큰(첫 공백 문자 앞)의 끝 위치. 공백이 없으면 입력 길이다. */
function firstTokenEnd(input: string): number {
  const match = /\s/.exec(input);
  return match ? match.index : input.length;
}

/**
 * 팔레트를 열 조건 — 첫 글자가 `/`이고 커서가 첫 토큰 안에 있다. 열면 필터어(`/` 뒤 첫 토큰), 아니면 null.
 *
 * 커서가 `/` 앞(0)이거나 첫 공백 뒤로 넘어가면 닫는다 — 질문을 쓰는 동안 목록이 가리지 않게 한다.
 */
export function paletteQuery(input: string, caret: number): string | null {
  if (!input.startsWith("/")) return null;
  const end = firstTokenEnd(input);
  if (caret < 1 || caret > end) return null;
  return input.slice(1, end);
}

/** 필터 — `slash_name`·표시 이름·설명에 필터어가 들어 있는 항목. 순서는 서버 목록 그대로다. */
export function filterEntries(entries: ChatEntry[], query: string): ChatEntry[] {
  const q = query.trim().toLocaleLowerCase();
  if (!q) return entries;
  return entries.filter((e) =>
    [e.slash_name ?? "", e.display_name, e.description].some((text) =>
      text.toLocaleLowerCase().includes(q),
    ),
  );
}

/** 칩 없이 보낸 `/<이름> 질문`의 이름과 질문. `/`로 시작하지 않거나 이름이 비면 null. */
export function parseSlashInput(input: string): { name: string; question: string } | null {
  if (!input.startsWith("/")) return null;
  const end = firstTokenEnd(input);
  const name = input.slice(1, end);
  if (!name) return null;
  return { name, question: input.slice(end).trim() };
}

/** 항목을 고른 뒤 남는 질문 — 첫 토큰(`/필터어`)과 그 뒤 공백을 지운다. */
export function stripFirstToken(input: string): string {
  return input.slice(firstTokenEnd(input)).replace(/^\s+/, "");
}

/** 409 `chat_entry_ambiguous` 본문에서 후보를 꺼낸다. 모양이 다르면 빈 목록이다. */
export function ambiguousCandidates(body: unknown): ChatEntry[] {
  if (typeof body !== "object" || body === null) return [];
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail !== "object" || detail === null) return [];
  const { code, candidates } = detail as Partial<ChatEntryAmbiguous>;
  return code === "chat_entry_ambiguous" && Array.isArray(candidates) ? candidates : [];
}

export const ENTRY_SOURCE_LABEL: Record<ChatEntry["source"], string> = {
  own: "내 스킬",
  system: "시스템",
  org: "조직",
};

export const ENTRY_KIND_LABEL: Record<ChatEntry["kind"], string> = {
  skill: "스킬",
  workflow: "워크플로",
};

type RunStatus = ChatEntryRunRef["status"];

/** run 카드 상태 라벨(설계 §3 웹) — 취소는 사용자가 누른 「중지」로 보인다. */
export const ENTRY_RUN_STATUS_LABEL: Record<RunStatus, string> = {
  queued: "대기",
  running: "실행 중",
  awaiting_approval: "승인 대기",
  succeeded: "완료",
  failed: "실패",
  cancelled: "중지",
};

export const ENTRY_RUN_STATUS_TONE: Record<RunStatus, StatusTone> = {
  queued: "neutral",
  running: "info",
  awaiting_approval: "warning",
  succeeded: "success",
  failed: "danger",
  cancelled: "neutral",
};

const ACTIVE: ReadonlySet<string> = new Set(["queued", "running", "awaiting_approval"]);

/** 끝나지 않은 run인가 — 서버 `ACTIVE_RUN_STATUSES`와 같다. */
export function isActiveRunStatus(status: string): boolean {
  return ACTIVE.has(status);
}

/**
 * run 카드 폴링 간격(ms, 멈추면 false).
 *
 * - 읽은 상태가 있으면 그 상태로 정한다 — 끝나지 않았으면 2초, 끝났으면 멈춘다.
 * - 아직 못 읽었고 오류가 4xx(없음·권한)면 멈춘다 — 다시 물어도 바뀌지 않는다.
 * - 그 밖(일시 오류·첫 응답 전)은 메시지 목록이 아는 상태(`knownStatus`)가 끝나지 않았으면 5초마다 다시 본다.
 *   멈추면 카드가 「실행 중」에 머물고 전송 잠금(D3)이 풀리지 않는다(리뷰 Medium).
 */
export function entryRunPollInterval(
  status: string | undefined,
  knownStatus: string | undefined,
  errorStatus?: number,
): number | false {
  if (status) return isActiveRunStatus(status) ? 2000 : false;
  if (errorStatus !== undefined && errorStatus >= 400 && errorStatus < 500) return false;
  return knownStatus && isActiveRunStatus(knownStatus) ? 5000 : false;
}
