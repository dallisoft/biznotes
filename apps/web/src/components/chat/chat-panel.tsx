"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import {
  ChatComposer,
  type EntrySendResult,
  type EntryTarget,
} from "@/components/chat/chat-composer";
import { ChatEntryRunCard } from "@/components/chat/chat-entry-run-card";
import { ChatEmptyState } from "@/components/chat/chat-empty-state";
import {
  MessageList,
  STREAMING_ID,
  type DisplayMessage,
} from "@/components/chat/message-list";
import { SessionHistoryMenu } from "@/components/chat/session-history-menu";
import { AddSourceDialog } from "@/components/sources/add-source-dialog";
import { Skeleton } from "@/components/ui/skeleton";
import {
  useChatEntries,
  useChatSessions,
  useCreateEntryRun,
  useCreateSession,
  useMessages,
  useModels,
  useSendMessage,
  useTogglePinSession,
} from "@/hooks/use-chat";
import { useCreateNote } from "@/hooks/use-notes";
import { useOverview, useRegenerateOverview } from "@/hooks/use-overview";
import { useSources } from "@/hooks/use-sources";
import { ApiError } from "@/lib/api-client";
import { ambiguousCandidates, isActiveRunStatus } from "@/lib/chat-entry";
import type { Citation } from "@/lib/chat-types";
import { isDismissible, visiblePending } from "@/lib/pending-messages";
import { selectStreamingEvidence } from "@/lib/sse";
import { useChatPrompt, type ChatPromptRequest } from "@/stores/chat-prompt";
import { useSourceSelection } from "@/stores/source-selection";

/** 진입 실행 멱등 키(≤64자) — `randomUUID`가 없는 비보안 컨텍스트(http LAN 주소)에서도 만든다. */
function entryRequestKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function noteTitleFrom(content: string): string {
  const firstLine = content.split("\n").find((l) => l.trim()) ?? "채팅 메모";
  return firstLine.replace(/^#+\s*/, "").slice(0, 80);
}

export function ChatPanel({
  notebookId,
  sessionId,
  onSessionChange,
  canWrite,
}: {
  notebookId: string;
  sessionId: string | null;
  /**
   * 현재 대화 바꾸기(URL `?s=`). 화면이 열린 뒤 저절로 고른 대화(최신 대화 자동 선택·첫 대화 자동 생성)는
   * `{ auto: true }`로 알린다 — 그사이 사용자가 노트북을 떠나는 링크를 눌렀으면 페이지가 무시한다.
   */
  onSessionChange: (sessionId: string, options?: { auto?: boolean }) => void;
  /** [codex 18b-P3] 근거 부족 배너의 "소스 추가" 액션은 쓰기 권한이 있을 때만 노출한다
   * (읽기 전용 공유 노트북에서 소스 등록 API가 403을 반환하는 것과 일관되게). */
  canWrite: boolean;
}) {
  const sessions = useChatSessions(notebookId);
  const createSession = useCreateSession(notebookId);
  const togglePin = useTogglePinSession(notebookId);
  const models = useModels();
  const sources = useSources(notebookId);
  const [modelId, setModelId] = useState<string | null>(null);
  const [addSourceOpen, setAddSourceOpen] = useState(false);
  const { selected } = useSourceSelection();
  const messages = useMessages(sessionId);
  const { send, stop, streaming, errorAfterDone, pending, dismissPending, installOffers } =
    useSendMessage(sessionId, notebookId);
  // 중지한 질문을 입력창에 되돌린다(B3) — id로 같은 문장도 다시 적용되게 한다
  const [restoredInput, setRestoredInput] = useState<{ text: string; id: number } | null>(null);
  // 다시 생성 중인 답변 — 생성하는 동안 옛 답변 대신 새 답변 자리를 보인다(③)
  const [regeneratingId, setRegeneratingId] = useState<string | null>(null);
  const createNote = useCreateNote(notebookId);
  // `/` 팔레트(#66) — 목록을 못 읽으면(옛 API 404 등) 팔레트 없이 일반 채팅 그대로다
  const chatEntries = useChatEntries();
  const createEntryRun = useCreateEntryRun();
  // 끝나지 않은 진입 run이 있으면 전송을 잠근다(D3) — 서버도 일반 메시지를 409로 막는다
  const entryLocked = (messages.data ?? []).some(
    (m) => m.entry_run != null && isActiveRunStatus(m.entry_run.status),
  );
  const overview = useOverview(notebookId);
  const regenerate = useRegenerateOverview(notebookId);

  // Base UI Select의 `items`(닫힌 상태 라벨)와 children(팝업 목록)이 갈라지지 않게
  // 단일 배열에서 파생한다 — 자세한 사연은 ChatComposer 주석.
  const modelOptions = useMemo(
    () =>
      (models.data ?? []).map((m) => ({
        value: m.id,
        label: m.display_name + (m.is_default ? " (기본)" : ""),
      })),
    [models.data],
  );

  const selectedCount = selected[notebookId]?.length ?? 0;
  const currentSession = sessions.data?.find((s) => s.id === sessionId) ?? null;

  function startNewSession() {
    // 현재 대화가 이미 비어 있으면 빈 세션을 또 만들지 않음
    if (sessionId && messages.data && messages.data.length === 0) return;
    createSession.mutate(undefined, { onSuccess: (s) => onSessionChange(s.id) });
  }

  function sendQuestion(question: string) {
    if (streaming.isStreaming || !sessionId) return;
    const sourceIds = selected[notebookId];
    setRegeneratingId(null);
    void send(question, sourceIds && sourceIds.length > 0 ? sourceIds : null, modelId).then((outcome) => {
      if (outcome !== "stopped") return;
      setRestoredInput((prev) => ({ text: question, id: (prev?.id ?? 0) + 1 }));
      toast("답변 생성을 중지했습니다", { description: "질문을 입력창에 되돌렸습니다" });
    });
  }

  async function sendEntry(target: EntryTarget, content: string): Promise<EntrySendResult> {
    if (streaming.isStreaming || !sessionId) return { status: "failed" };
    const sourceIds = selected[notebookId];
    try {
      await createEntryRun.mutateAsync({
        sessionId,
        body: {
          ...target,
          content,
          source_ids: sourceIds && sourceIds.length > 0 ? sourceIds : null,
          model_id: modelId,
          // 같은 전송의 재시도가 run을 하나만 만들게 한다(서버 멱등 키)
          idempotency_key: entryRequestKey(),
        },
      });
      return { status: "sent" };
    } catch (err) {
      if (err instanceof ApiError && err.status === 409 && err.code === "chat_entry_ambiguous") {
        return { status: "many", candidates: ambiguousCandidates(err.body) };
      }
      if (err instanceof ApiError && err.status === 404 && "name" in target) {
        toast.error(`「/${target.name}」에 맞는 스킬·워크플로가 없습니다`);
        return { status: "none" };
      }
      const detail = err instanceof ApiError ? err.detail : err instanceof Error ? err.message : String(err);
      toast.error(`실행하지 못했습니다 — ${detail}`);
      return { status: "failed" };
    }
  }

  function regenerateAnswer(answer: DisplayMessage) {
    if (streaming.isStreaming || !sessionId) return;
    const list = messages.data ?? [];
    const index = list.findIndex((m) => m.id === answer.id);
    const question = index > 0 ? list[index - 1] : undefined;
    if (!question || question.role !== "user") return;
    const sourceIds = selected[notebookId];
    setRegeneratingId(answer.id);
    void send(question.content, sourceIds && sourceIds.length > 0 ? sourceIds : null, modelId, {
      regenerate: true,
    }).then((outcome) => {
      if (outcome === "stopped") toast("다시 생성을 중지했습니다", { description: "이전 답변을 그대로 둡니다" });
    });
  }

  function saveMessageAsNote(m: { id: string; content: string; citations: Citation[] }) {
    createNote.mutate(
      {
        title: noteTitleFrom(m.content),
        content: m.content,
        origin: "chat",
        origin_ref: m.id,
        citations: m.citations,
      },
      {
        onSuccess: () => toast.success("메모에 저장했습니다"),
        onError: (err) => toast.error(`저장 실패: ${err.message}`),
      },
    );
  }

  // 확정된 대기 행은 저장 목록에 서버 id가 한 번 보이면 지운다 — 뒤에 그 답변이 바뀌어도(다시 생성) 되살아나지 않게.
  useEffect(() => {
    const saved = new Set((messages.data ?? []).map((m) => m.id));
    for (const p of pending) {
      if (p.state === "confirmed" && p.assistantId && saved.has(p.assistantId)) dismissPending(p.nonce);
    }
  }, [messages.data, pending, dismissPending]);

  // ---- 교차 패널 질문 주입(마인드맵 노드 클릭 — mindmap-agent-skill AC-2.4) ----
  // 자기 노트북 요청만 집는다. 집은 nonce는 이 인스턴스가 끝까지 책임진다(전송 1회 → busy 해제·소비,
  // 실패·만료·노트북 이탈·언마운트 → 그 nonce만 취소). 다른 nonce·새 busy는 건드리지 않는다.
  const promptRequests = useChatPrompt((s) => s.requests);
  const ownedNonceRef = useRef<string | null>(null);
  const [waitingPrompt, setWaitingPrompt] = useState<ChatPromptRequest | null>(null);
  const streamingRef = useRef(streaming.isStreaming);
  streamingRef.current = streaming.isStreaming;
  // 끝나지 않은 진입 run이 있으면 마인드맵 질문도 보내지 않는다(D3 — 서버가 409로 막는다, 리뷰 Medium)
  const entryLockedRef = useRef(entryLocked);
  entryLockedRef.current = entryLocked;

  const finishPrompt = useCallback((nonce: string, consumed: boolean) => {
    const store = useChatPrompt.getState();
    store.releaseBusy(nonce);
    if (consumed) store.consume(nonce);
    else store.cancel(nonce);
    if (ownedNonceRef.current === nonce) ownedNonceRef.current = null;
    setWaitingPrompt((w) => (w?.nonce === nonce ? null : w));
  }, []);

  useEffect(() => {
    if (ownedNonceRef.current) return;
    const req = Object.values(promptRequests).find(
      (r) => r.notebookId === notebookId && !r.claimed && r.expiresAt > Date.now(),
    );
    if (!req) return;
    const store = useChatPrompt.getState();
    if (!store.claim(req.nonce)) return;
    ownedNonceRef.current = req.nonce;
    store.setBusy(req.nonce);
    if (streamingRef.current || entryLockedRef.current) {
      toast.error(entryLockedRef.current ? "실행이 끝난 뒤 다시 눌러 주세요" : "답변이 끝난 뒤 다시 눌러 주세요");
      finishPrompt(req.nonce, false);
      return;
    }
    setWaitingPrompt(req);
  }, [promptRequests, notebookId, finishPrompt]);

  // 세션이 준비되면 정확히 1회 보낸다. 세션 자동 생성이 만료 전에 끝나지 않으면 토스트 후 취소한다.
  useEffect(() => {
    if (!waitingPrompt || waitingPrompt.notebookId !== notebookId) return;
    const req = waitingPrompt;
    if (sessionId) {
      setWaitingPrompt(null);
      if (entryLockedRef.current) {
        toast.error("실행이 끝난 뒤 다시 눌러 주세요");
        finishPrompt(req.nonce, false);
        return;
      }
      setRegeneratingId(null);
      void send(req.text, req.sourceIds, modelId, { autonomous: false, origin: "mindmap" }).then(
        () => finishPrompt(req.nonce, true),
      );
      return;
    }
    const timer = setTimeout(() => {
      toast.error("대화를 준비하지 못해 질문을 보내지 못했습니다");
      finishPrompt(req.nonce, false);
    }, Math.max(0, req.expiresAt - Date.now()));
    return () => clearTimeout(timer);
  }, [waitingPrompt, sessionId, notebookId, modelId, send, finishPrompt]);

  // 노트북을 떠나거나 언마운트되면 자기가 집은 nonce만 취소한다(전송 중이면 그 전송의 완료가 소비를 맡는다).
  useEffect(
    () => () => {
      const nonce = ownedNonceRef.current;
      if (!nonce) return;
      ownedNonceRef.current = null;
      const store = useChatPrompt.getState();
      store.cancel(nonce);
      store.releaseBusy(nonce);
      setWaitingPrompt(null);
    },
    [notebookId],
  );

  // 최신 세션 자동 선택, 없으면 생성 (URL에 세션이 지정되지 않은 경우만)
  useEffect(() => {
    if (sessions.data && sessionId === null) {
      if (sessions.data.length > 0) {
        onSessionChange(sessions.data[0].id, { auto: true });
      } else if (!createSession.isPending) {
        createSession.mutate(undefined, {
          onSuccess: (s) => onSessionChange(s.id, { auto: true }),
        });
      }
    }
    // 정당: createSession/onSessionChange를 deps에 넣으면 세션 생성이 반복된다.
    // 세션 목록·현재 세션 변화에만 반응한다.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessions.data, sessionId]);

  // 기본 모델 선택
  useEffect(() => {
    if (models.data && modelId === null && models.data.length > 0) {
      const def = models.data.find((m) => m.is_default) ?? models.data[0];
      setModelId(def.id);
    }
  }, [models.data, modelId]);

  const allMessages: DisplayMessage[] = useMemo(() => {
    const generating = streaming.isStreaming || Boolean(streaming.content);
    const base: DisplayMessage[] = (messages.data ?? [])
      .filter((m) => !(generating && m.id === regeneratingId))
      .map((m) => ({
      id: m.id,
      role: m.role,
      content: m.content,
      citations: m.citations,
      // [Phase 18b/D3] 신규 컬럼 없이 기존 token_usage(JSONB)에 실은 플래그를 복원
      insufficientContext: m.token_usage?.insufficient_context === 1,
      // [note-provenance Phase 3] 새로고침/세션 재진입 후에도 과거 메시지의 스킬·MCP·도구
      // 배지를 복원한다 — provenance가 없는 legacy 행은 그대로 배지 없이 표시된다.
      evidence: m.provenance?.items,
      mindmapPrompt: m.role === "user" && m.token_usage?.mindmap_prompt === 1,
      entryRun: m.role === "user" ? (m.entry_run ?? undefined) : undefined,
      entryResult: m.role === "assistant" && m.run_id != null,
      // [#259] 이 화면에서 받은 설치 확인 카드 — 저장하지 않으므로 새로고침하면 요약 답변만 남는다
      installOffer: installOffers[m.id],
    }));
    // 대기 user 메시지(mindmap-agent-skill AC-2.12 — 서버는 답변이 끝나야 질문을 저장한다) — 전송 즉시 보이고,
    // 확정된 전송은 저장 목록에 서버 id가 보이면 숨긴다.
    const savedIds = new Set((messages.data ?? []).map((m) => m.id));
    for (const p of visiblePending(pending, sessionId, savedIds)) {
      base.push({
        id: `pending:${p.nonce}`,
        role: "user",
        content: p.content,
        citations: [],
        mindmapPrompt: p.origin === "mindmap",
        pendingState: p.state,
        onDismiss: isDismissible(p) ? () => dismissPending(p.nonce) : undefined,
      });
    }
    if (streaming.isStreaming || streaming.content) {
      base.push({
        id: STREAMING_ID,
        role: "assistant",
        content: streaming.content || "…",
        citations: streaming.citations,
        insufficientContext: streaming.insufficientContext,
        // [BL-22 / P4-02] 라이브 배지도 재조회와 같은 provenance.items로 그린다.
        // 선택식은 lib/sse.ts의 명명 함수 하나로 두어 테스트가 이 경로를 그대로 고정한다.
        evidence: selectStreamingEvidence(streaming),
        // [#259] 설치 카드는 스트리밍 자리에 띄우지 않는다 — 저장된 답변 id로 다시 그려지면 카드의 선택·설치
        // 상태가 사라진다. 저장(`done`) 뒤 `installOffers[message_id]`로만 그린다.
      });
    }
    return base;
  }, [messages.data, streaming, regeneratingId, pending, sessionId, dismissPending, installOffers]);

  return (
    <section className="flex min-w-0 flex-1 flex-col bg-background">
      <SessionHistoryMenu
        sessions={sessions.data ?? []}
        sessionId={sessionId}
        currentTitle={currentSession?.title}
        isStreaming={streaming.isStreaming}
        creating={createSession.isPending}
        onSessionChange={onSessionChange}
        onTogglePin={(id, pinned) => togglePin.mutate({ sessionId: id, pinned })}
        onNewSession={startNewSession}
      />

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-3xl px-4 pb-4">
          {messages.isPending && sessionId ? (
            <Skeleton className="h-20" />
          ) : allMessages.length === 0 ? (
            <ChatEmptyState
              notebookId={notebookId}
              overview={overview.data}
              isStreaming={streaming.isStreaming}
              regenerating={regenerate.isPending}
              onRegenerate={() => regenerate.mutate()}
              onAskQuestion={sendQuestion}
              canWrite={canWrite}
              sourceCount={sources.data?.length ?? 0}
            />
          ) : (
            <MessageList
              messages={allMessages}
              canWrite={canWrite}
              streamingContent={streaming.content}
              onAddSource={() => setAddSourceOpen(true)}
              onSaveNote={saveMessageAsNote}
              onRegenerate={regenerateAnswer}
              renderEntryRun={(entryRun) =>
                sessionId ? (
                  <ChatEntryRunCard
                    entryRun={entryRun}
                    sessionId={sessionId}
                    notebookId={notebookId}
                    canWrite={canWrite}
                  />
                ) : null
              }
            />
          )}
        </div>
      </div>
      <AddSourceDialog
        notebookId={notebookId}
        sourceCount={sources.data?.length ?? 0}
        open={addSourceOpen}
        onOpenChange={setAddSourceOpen}
        showTrigger={false}
      />

      {/* 스트림 오류는 스크롤 영역 밖, 입력창 바로 위에 둔다. 첫 질문이 실패하면 저장된 메시지가 없어 빈 화면
          (요약·추천 질문·소스 추가 카드)으로 돌아오는데, 목록 안에 두면 접힌 아래로 밀려 아무 일도 없던 것처럼 보인다. */}
      {streaming.error && (
        <div className="shrink-0 px-4 pb-2">
          <p
            role="alert"
            className="mx-auto w-full max-w-3xl rounded-md bg-destructive/10 p-2 text-sm text-destructive-text"
          >
            {errorAfterDone ? "응답 이후 처리 오류" : "답변을 받지 못했습니다"} — {streaming.error}
          </p>
        </div>
      )}
      <ChatComposer
        modelId={modelId}
        onModelChange={setModelId}
        modelOptions={modelOptions}
        selectedCount={selectedCount}
        isStreaming={streaming.isStreaming}
        disabled={!sessionId}
        onSend={sendQuestion}
        onStop={stop}
        restoredInput={restoredInput}
        entries={chatEntries.data ?? null}
        onSendEntry={sendEntry}
        entryLocked={entryLocked}
      />
    </section>
  );
}
