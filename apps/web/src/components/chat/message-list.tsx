"use client";

import { Fragment, useEffect, useRef, type ReactNode } from "react";
import { MessageBubble } from "@/components/chat/message-bubble";
import { SkillInstallCard } from "@/components/chat/skill-install-card";
import { notifyError, notifySuccess } from "@/lib/notify";
import type { ChatEntryRunRef, ChatEvidence, Citation, SkillInstallOffer } from "@/lib/chat-types";
import type { PendingState } from "@/lib/pending-messages";

export interface DisplayMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  insufficientContext?: boolean;
  /** [BL-22] 라이브·재조회 모두 `provenance.items` — 호출부에서 같은 형태로 채운다. */
  evidence?: ChatEvidence[];
  /** 마인드맵 노드 질문(저장 표식 `token_usage.mindmap_prompt === 1` 또는 대기 행의 origin) */
  mindmapPrompt?: boolean;
  /** 대기 user 메시지 상태(AC-2.12) — 저장 메시지는 없음 */
  pendingState?: PendingState;
  onDismiss?: () => void;
  /** 진입 질문 행의 run 요약(#66) — 있으면 메시지 아래에 run 카드를 붙인다. */
  entryRun?: ChatEntryRunRef;
  /** 진입 run의 결과 행(`run_id`가 있다) — 「다시 생성」을 두지 않는다(서버도 409). */
  entryResult?: boolean;
  /** [#259] 이 답변이 띄운 스킬 설치 확인 카드 — 이 화면에서만 있다(새로고침하면 없다). */
  installOffer?: SkillInstallOffer;
}

/** 스트리밍 중인 어시스턴트 메시지의 임시 id — 저장 버튼을 숨기는 기준이다. */
export const STREAMING_ID = "__streaming__";

async function copyMessage(content: string) {
  try {
    if (!navigator.clipboard?.writeText) throw new Error("이 브라우저는 클립보드 복사를 지원하지 않습니다");
    await navigator.clipboard.writeText(content);
    notifySuccess("답변을 복사했습니다");
  } catch (error) {
    notifyError("답변을 복사", error);
  }
}

export function MessageList({
  messages,
  canWrite,
  streamingContent,
  onAddSource,
  onSaveNote,
  onRegenerate,
  renderEntryRun,
}: {
  messages: DisplayMessage[];
  /** 읽기 전용 공유 노트북이면 "소스 추가" 액션을 숨긴다. */
  canWrite: boolean;
  /** 새 토큰이 도착할 때마다 하단으로 스크롤하기 위한 트리거. */
  streamingContent: string;
  onAddSource: () => void;
  onSaveNote: (m: DisplayMessage) => void;
  /** 마지막 답변 다시 생성 — 없으면 버튼을 두지 않는다. */
  onRegenerate?: (m: DisplayMessage) => void;
  /** 진입 질문 행 아래 run 카드(#66). */
  renderEntryRun?: (entryRun: ChatEntryRunRef) => ReactNode;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length, streamingContent]);

  const streaming = streamingContent !== "" || messages.some((m) => m.id === STREAMING_ID);
  // 다시 생성은 마지막 답변에만(그 앞 답변을 바꾸면 뒤 대화의 맥락이 어긋난다)
  const last = messages[messages.length - 1];
  const regenerableId =
    onRegenerate && !streaming && last?.role === "assistant" && last.id !== STREAMING_ID && !last.entryResult
      ? last.id
      : null;

  return (
    // 새 답변을 화면 읽기 프로그램에 알린다. 스트리밍 중에는 aria-busy로 델타마다 읽지 않게 한다(ui-ux-improvements ③)
    <div
      role="log"
      aria-label="대화 내용"
      aria-live="polite"
      aria-busy={streaming}
      className="flex flex-col gap-5 pt-2"
      data-testid="chat-messages"
    >
      {messages.map((m) => (
        <Fragment key={m.id}>
          <MessageBubble
            role={m.role}
            content={m.content}
            citations={m.citations}
            insufficientContext={m.insufficientContext}
            evidence={m.evidence}
            mindmapPrompt={m.mindmapPrompt}
            pendingState={m.pendingState}
            onDismiss={m.onDismiss}
            onAddSource={m.role === "assistant" && canWrite ? onAddSource : undefined}
            onSaveNote={
              // 읽기 전용 공유 노트북은 메모를 만들 수 없다 — 눌러서 403을 보게 하지 않는다
              m.role === "assistant" && m.id !== STREAMING_ID && canWrite ? () => onSaveNote(m) : undefined
            }
            onRegenerate={m.id === regenerableId && onRegenerate ? () => onRegenerate(m) : undefined}
            onCopy={
              m.role === "assistant" && m.id !== STREAMING_ID ? () => void copyMessage(m.content) : undefined
            }
          />
          {m.entryRun && renderEntryRun?.(m.entryRun)}
          {m.installOffer && <SkillInstallCard offer={m.installOffer} />}
        </Fragment>
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
