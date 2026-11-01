"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api-client";
import type {
  ChatEntry,
  ChatEntryRunRequest,
  ChatEntryRunResult,
  ChatMessage,
  ChatSession,
  ChatStreamEvent,
  LLMModelInfo,
  RecentChat,
  SkillInstallOffer,
} from "@/lib/chat-types";
import type { PendingUserMessage } from "@/lib/pending-messages";
import { qk } from "@/lib/query-keys";
import {
  applyChatEvent,
  drainSseBuffer,
  INITIAL_STREAM,
  type StreamingState,
} from "@/lib/sse";

export type { StreamingState };
export type { PendingUserMessage };

export function useModels() {
  return useQuery<LLMModelInfo[], ApiError>({
    queryKey: ["models"],
    queryFn: () => api.get<LLMModelInfo[]>("/api/v1/models"),
    staleTime: 60_000,
  });
}

/**
 * 이미지 생성 모델 목록 — 슬라이드 자료 「이미지 넣기」를 켤 수 있는지 판단한다(core-v0.23.0-upgrade D4).
 * 비어 있으면 코어가 이미지 노드의 모델을 고를 수 없다.
 */
export function useImageModels() {
  return useQuery<LLMModelInfo[], ApiError>({
    queryKey: qk.models.image,
    queryFn: () => api.get<LLMModelInfo[]>("/api/v1/models?modality=image"),
    staleTime: 60_000,
  });
}

/** 홈 「최근 대화 이어가기」 — 내 대화 중 지금 읽을 수 있는 노트북의 것(ui-ux-improvements ⑥). */
export function useRecentChats(limit = 3) {
  return useQuery<RecentChat[], ApiError>({
    queryKey: ["chat-recent", limit],
    queryFn: () => api.get<RecentChat[]>(`/api/v1/chat/recent?limit=${limit}`),
    staleTime: 30_000,
  });
}

export function useChatSessions(notebookId: string) {
  return useQuery<ChatSession[], ApiError>({
    queryKey: ["chat-sessions", notebookId],
    queryFn: () =>
      api.get<ChatSession[]>(`/api/v1/notebooks/${notebookId}/chat/sessions`),
  });
}

export function useCreateSession(notebookId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () =>
      api.post<ChatSession>(`/api/v1/notebooks/${notebookId}/chat/sessions`),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["chat-sessions", notebookId] }),
  });
}

export function useTogglePinSession(notebookId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ sessionId, pinned }: { sessionId: string; pinned: boolean }) =>
      api.patch<ChatSession>(`/api/v1/chat/sessions/${sessionId}`, { pinned }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["chat-sessions", notebookId] }),
  });
}

/**
 * `/` 팔레트 항목(#66). 노트북과 무관한 사용자 단위 목록이다.
 *
 * 실패하면(옛 API는 404) 재시도하지 않는다 — 호출측은 데이터가 없으면 팔레트를 띄우지 않고 `/`로 시작하는
 * 입력을 일반 메시지로 보낸다(설계 §8 「새 web + 옛 api」).
 */
export function useChatEntries() {
  return useQuery<ChatEntry[], ApiError>({
    queryKey: ["chat-entries"],
    queryFn: () => api.get<ChatEntry[]>("/api/v1/chat/entries"),
    staleTime: 30_000,
    retry: false,
  });
}

/** 진입 실행 변수 — 보낸 대화를 요청과 함께 고정한다(응답 전에 대화를 바꿔도 그 대화의 캐시를 고친다). */
export interface EntryRunVariables {
  sessionId: string;
  body: ChatEntryRunRequest;
}

/**
 * 채팅 진입 실행(#66 D1) — 질문 행과 run을 만든다(202). 진행은 run 카드가 `useAgentRun`으로 폴링한다.
 *
 * 응답의 질문 행을 메시지 캐시 끝에 바로 붙여 run 카드가 곧바로 보이게 하고, 그 뒤 목록을 다시 읽는다.
 * 캐시 키는 렌더 시점의 대화가 아니라 **보낸 요청의 대화**(`variables.sessionId`)와 응답의 노트북으로
 * 정한다 — 요청 중 다른 대화로 옮겨도 그 대화에 남의 질문이 붙지 않는다(리뷰 High).
 * 오류(`ApiError` — 404 없음·409 여럿/활성 run·422)는 호출측이 해석한다.
 */
export function useCreateEntryRun() {
  const queryClient = useQueryClient();
  return useMutation<ChatEntryRunResult, ApiError, EntryRunVariables>({
    mutationFn: ({ sessionId, body }) =>
      api.post<ChatEntryRunResult>(`/api/v1/chat/sessions/${sessionId}/entry-runs`, body),
    onSuccess: (result, { sessionId }) => {
      queryClient.setQueryData<ChatMessage[]>(["chat-messages", sessionId], (old) =>
        old?.some((m) => m.id === result.user_message.id)
          ? old
          : [...(old ?? []), result.user_message],
      );
      void queryClient.invalidateQueries({ queryKey: ["chat-messages", sessionId] });
      void queryClient.invalidateQueries({ queryKey: ["chat-sessions", result.run.notebook_id] });
      void queryClient.invalidateQueries({ queryKey: ["chat-recent"] });
    },
  });
}

export function useMessages(sessionId: string | null) {
  return useQuery<ChatMessage[], ApiError>({
    queryKey: ["chat-messages", sessionId],
    queryFn: () => api.get<ChatMessage[]>(`/api/v1/chat/sessions/${sessionId}/messages`),
    enabled: sessionId !== null,
  });
}

/** 전송 결과 — `stopped`는 사용자가 `stop()`으로 끊었고 서버가 답변을 끝내지 못한 경우다. */
export type SendOutcome = "done" | "stopped" | "error" | "skipped";

export interface SendOptions {
  /** 마지막 질문·답변 한 쌍을 새 답변으로 바꾼다(ui-ux-improvements ③). 대기 user 메시지를 만들지 않는다. */
  regenerate?: boolean;
  /** 기본 true — 웹 채팅의 기존 동작. 마인드맵 질문은 false(서버도 정적 경로를 강제한다). */
  autonomous?: boolean;
  /** 질문 출처. 생략하면 요청 본문에 넣지 않는다(일반 질문의 본문 불변 — mindmap-agent-skill AC-2.5). */
  origin?: "chat" | "mindmap";
}

async function readStream(res: Response, onEvent: (event: ChatStreamEvent) => void): Promise<void> {
  if (!res.body) throw new Error(`HTTP ${res.status}: 응답 본문이 없습니다`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const { events, rest } = drainSseBuffer(buffer);
    buffer = rest;
    for (const event of events) onEvent(event);
  }
}

function newNonce(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

/**
 * POST 기반 SSE 스트림 전송 — fetch ReadableStream 파싱.
 *
 * 전송 즉시 대기 user 메시지를 만든다(mindmap-agent-skill AC-2.12, D-19 — 다시 생성 밖의 모든 채팅 전송).
 * 상태는 그 전송의 nonce로만 바꾼다: `sending` → SSE `done` 수신 시 `confirmed(assistantId)` / 4xx 거절이면
 * `not_saved` / 서버 쪽 종료인데 `done`이 없으면 종료 뒤 재조회에 전송 시점 밖 메시지가 없을 때만 `not_saved`,
 * 있으면 `uncertain` / 그 밖(abort·연결 단절·5xx·네트워크)은 `uncertain`. 사용자가 중지했고 재조회에 새 메시지가
 * 없으면 대기 행을 지운다(질문은 입력창으로 돌아간다). 내용 일치로 저장 메시지에 대응시키지 않는다.
 */
export function useSendMessage(sessionId: string | null, notebookId: string) {
  const queryClient = useQueryClient();
  const [streaming, setStreaming] = useState<StreamingState>(INITIAL_STREAM);
  // `done` 뒤에 온 오류 — 전송은 끝났으므로 「답변을 받지 못했습니다」가 아니라 따로 보인다.
  const [errorAfterDone, setErrorAfterDone] = useState(false);
  // 오류가 난 전송의 대화 — 다른 대화로 옮기면 그 오류를 보이지 않는다.
  const [sentSessionId, setSentSessionId] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingUserMessage[]>([]);
  // [#259] 설치 확인 카드 — 저장된 답변 id별로 이 화면에서만 들고 있다(서버는 카드를 저장하지 않는다).
  const [installOffers, setInstallOffers] = useState<Record<string, SkillInstallOffer>>({});
  const abortRef = useRef<AbortController | null>(null);
  // 사용자가 누른 중지인지(다음 전송이 앞 요청을 끊는 abort와 구분한다)
  const stoppedRef = useRef<AbortController | null>(null);
  // 지금 화면 스트림을 소유한 전송 — 늦게 끝난 이전 전송이 새 전송의 표시를 덮지 않게 한다.
  const activeNonceRef = useRef<string | null>(null);

  const stop = useCallback(() => {
    const controller = abortRef.current;
    if (!controller || controller.signal.aborted) return;
    stoppedRef.current = controller;
    controller.abort();
  }, []);

  const updatePending = useCallback(
    (nonce: string, patch: (p: PendingUserMessage) => PendingUserMessage) =>
      setPending((list) => list.map((p) => (p.nonce === nonce ? patch(p) : p))),
    [],
  );

  const dismissPending = useCallback(
    (nonce: string) => setPending((list) => list.filter((p) => p.nonce !== nonce)),
    [],
  );

  const send = useCallback(
    async (
      content: string,
      sourceIds: string[] | null,
      modelId: string | null,
      options: SendOptions = {},
    ): Promise<SendOutcome> => {
      if (!sessionId) return "skipped";
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      const nonce = newNonce();
      activeNonceRef.current = nonce;
      const isActive = () => activeNonceRef.current === nonce;
      let outcome: SendOutcome = "done";
      let finished = false;
      // 서버가 스트림을 끝냈는데 done이 없었나(SSE error 이벤트 또는 본문 정상 종료)
      let serverEndedWithoutDone = false;
      let userStopped = false;
      const trackPending = !options.regenerate;
      setSentSessionId(sessionId);
      setErrorAfterDone(false);

      const known = queryClient.getQueryData<ChatMessage[]>(["chat-messages", sessionId]) ?? [];
      if (trackPending) {
        setPending((list) => [
          ...list,
          {
            nonce,
            sessionId,
            content,
            knownIds: known.map((m) => m.id),
            state: "sending",
            origin: options.origin ?? "chat",
          },
        ]);
      }

      setStreaming({
        content: "",
        citations: [],
        error: null,
        isStreaming: true,
        insufficientContext: false,
        provenance: null,
        legacyEvidence: [],
        installOffer: null,
      });

      try {
        // api.stream(): 401 갱신은 헤더 수신 시점까지만 시도한다 —
        // 본문을 읽기 시작하면 부분 소비된 스트림을 재생할 수 없다(HCI-R4-4).
        const res = await api.stream(`/api/v1/chat/sessions/${sessionId}/messages`, {
          method: "POST",
          body: JSON.stringify({
            content,
            source_ids: sourceIds,
            model_id: modelId,
            autonomous: options.autonomous ?? true,
            ...(options.regenerate ? { regenerate: true } : {}),
            ...(options.origin ? { origin: options.origin } : {}),
          }),
          signal: controller.signal,
        });
        let offer: SkillInstallOffer | null = null;
        await readStream(res, (event) => {
          if (event.type === "skill_install_offer") offer = event.offer;
          if (event.type === "done") {
            finished = true;
            const saved = offer;
            if (saved) setInstallOffers((m) => ({ ...m, [event.message_id]: saved }));
            updatePending(nonce, (p) => ({ ...p, state: "confirmed", assistantId: event.message_id }));
          }
          if (event.type === "error") {
            outcome = "error";
            if (finished && isActive()) setErrorAfterDone(true);
          }
          if (isActive()) setStreaming((s) => applyChatEvent(s, event));
        });
        serverEndedWithoutDone = !finished;
      } catch (e) {
        const aborted = e instanceof DOMException && e.name === "AbortError";
        if (!aborted) {
          outcome = "error";
          if (isActive()) {
            if (finished) setErrorAfterDone(true);
            setStreaming((s) => ({
              ...s,
              error: e instanceof Error ? e.message : "스트림 오류",
            }));
          }
        } else if (stoppedRef.current === controller && !finished) {
          // 서버가 답변을 저장하기 전에 끊었다 — 질문·답변 모두 저장되지 않는다
          outcome = "stopped";
          userStopped = true;
        }
        if (!finished && !userStopped) {
          const rejected = e instanceof ApiError && e.status >= 400 && e.status < 500;
          updatePending(nonce, (p) => ({ ...p, state: rejected ? "not_saved" : "uncertain" }));
        }
      } finally {
        if (isActive()) setStreaming((s) => ({ ...s, isStreaming: false }));
        // 서버에 저장된 메시지로 동기화
        await queryClient.invalidateQueries({ queryKey: ["chat-messages", sessionId] });
        await queryClient.invalidateQueries({ queryKey: ["chat-sessions", notebookId] });
        // 홈 「최근 대화 이어가기」도 이 대화를 맨 앞으로 바꿔야 한다
        void queryClient.invalidateQueries({ queryKey: ["chat-recent"] });
        if (serverEndedWithoutDone || userStopped) {
          // 서버 생성기는 끝났고 commit은 done 전에만 일어난다 — 전송 시점 밖 메시지가 없으면 저장되지 않았다.
          const after = queryClient.getQueryData<ChatMessage[]>(["chat-messages", sessionId]) ?? [];
          const knownIds = new Set(known.map((m) => m.id));
          const anyNew = after.some((m) => !knownIds.has(m.id));
          if (userStopped && !anyNew) {
            dismissPending(nonce);
          } else {
            updatePending(nonce, (p) =>
              p.state === "sending" ? { ...p, state: anyNew ? "uncertain" : "not_saved" } : p,
            );
          }
        }
        // 저장된 메시지로 바뀌었으니 스트림 내용은 비우되, 오류는 남긴다. 실패하면 서버에 남는 메시지가
        // 없을 수 있어(예: LLM 호출 400) 오류까지 비우면 화면이 깜박이고 아무 일도 없던 것처럼 보인다.
        if (isActive()) setStreaming((s) => ({ ...INITIAL_STREAM, error: s.error }));
        if (abortRef.current === controller) abortRef.current = null;
      }
      return outcome;
    },
    [sessionId, notebookId, queryClient, updatePending, dismissPending],
  );

  const hideError = streaming.error !== null && sentSessionId !== sessionId;
  const visible = hideError ? { ...streaming, error: null } : streaming;
  return {
    send,
    stop,
    streaming: visible,
    errorAfterDone: !hideError && errorAfterDone,
    pending,
    dismissPending,
    installOffers,
  };
}
