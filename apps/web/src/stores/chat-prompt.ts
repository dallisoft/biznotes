import { create } from "zustand";

/** 요청 수명 — 소비자(ChatPanel)가 이 안에 집어 가지 않으면 만료로 정리된다(mindmap-agent-skill AC-2.1, T5). */
export const CHAT_PROMPT_TTL_MS = 15_000;

export interface ChatPromptRequest {
  nonce: string;
  notebookId: string;
  text: string;
  /** 검색 범위 — 마인드맵 run의 `input_source_ids`(null = 노트북 전체) */
  sourceIds: string[] | null;
  origin: "mindmap";
  expiresAt: number;
  claimed: boolean;
}

interface ChatPromptState {
  requests: Record<string, ChatPromptRequest>;
  /** 전송 중인 요청의 nonce — 그 nonce로만 해제한다 */
  busyNonce: string | null;
  /** 새 요청. 같은 노트북의 만료 전 미소비 요청이 있으면 null(거부). */
  request: (input: {
    notebookId: string;
    text: string;
    sourceIds: string[] | null;
    origin: "mindmap";
  }) => string | null;
  /** 소비자가 집어 간다. 이미 집었거나 만료·없으면 false. */
  claim: (nonce: string) => boolean;
  consume: (nonce: string) => void;
  cancel: (nonce: string) => void;
  setBusy: (nonce: string) => void;
  releaseBusy: (nonce: string) => void;
  /** 이 노트북에 만료 전 미소비 요청이 있는가 */
  hasPending: (notebookId: string) => boolean;
}

function newNonce(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function withoutExpired(
  requests: Record<string, ChatPromptRequest>,
  now: number,
): Record<string, ChatPromptRequest> {
  const next: Record<string, ChatPromptRequest> = {};
  for (const [k, r] of Object.entries(requests)) {
    // 집어 간 요청은 소비자가 consume·cancel로 정리한다(전송 중 만료로 사라지지 않게).
    if (r.claimed || r.expiresAt > now) next[k] = r;
  }
  return next;
}

function without(
  requests: Record<string, ChatPromptRequest>,
  nonce: string,
): Record<string, ChatPromptRequest> {
  const next = { ...requests };
  delete next[nonce];
  return next;
}

/**
 * 교차 패널 채팅 질문 주입 — 마인드맵 노드 클릭 → ChatPanel 전송(mindmap-agent-skill §7.3).
 * 요청은 nonce·노트북·만료를 갖고, 소비자는 자기 노트북 요청만 집는다(T6).
 */
export const useChatPrompt = create<ChatPromptState>((set, get) => ({
  requests: {},
  busyNonce: null,
  request: ({ notebookId, text, sourceIds, origin }) => {
    const now = Date.now();
    const live = withoutExpired(get().requests, now);
    if (Object.values(live).some((r) => r.notebookId === notebookId)) {
      set({ requests: live });
      return null;
    }
    const nonce = newNonce();
    live[nonce] = {
      nonce,
      notebookId,
      text,
      sourceIds,
      origin,
      expiresAt: now + CHAT_PROMPT_TTL_MS,
      claimed: false,
    };
    set({ requests: live });
    return nonce;
  },
  claim: (nonce) => {
    const r = get().requests[nonce];
    if (!r || r.claimed || r.expiresAt <= Date.now()) return false;
    set({ requests: { ...get().requests, [nonce]: { ...r, claimed: true } } });
    return true;
  },
  consume: (nonce) => set({ requests: without(get().requests, nonce) }),
  cancel: (nonce) => set({ requests: without(get().requests, nonce) }),
  setBusy: (nonce) => set({ busyNonce: nonce }),
  releaseBusy: (nonce) => {
    if (get().busyNonce === nonce) set({ busyNonce: null });
  },
  hasPending: (notebookId) => {
    const now = Date.now();
    return Object.values(get().requests).some(
      (r) => r.notebookId === notebookId && (r.claimed || r.expiresAt > now),
    );
  },
}));
