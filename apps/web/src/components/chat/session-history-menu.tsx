"use client";

import { Check, ChevronDown, Pin, PinOff, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { ChatSession } from "@/lib/chat-types";
import { formatDate, formatTime } from "@/lib/format";

function sessionDateLabel(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return formatTime(d);
  }
  return formatDate(d);
}

/** 대화 기록 트리거 라벨(AC-3.3) — 「대화: {제목}」. 현재 대화를 아직 모르면(불러오는 중) 「대화 기록」. */
export function sessionTriggerLabel(currentTitle?: string): string {
  return currentTitle ? `대화: ${currentTitle}` : "대화 기록";
}

/**
 * 대화 바(plan §6.6.2): 왼쪽 「대화: {제목} ▾」 드롭다운(대화 기록·고정), 오른쪽 「새 대화」 하나.
 * 「새 대화」 실행 컨트롤은 화면 전체에서 이 버튼 하나다(D-03 — 사이드바 연필 아이콘은 P2에서 없앴다).
 * 좁은 화면에서는 「새 대화」 글자를 숨기고 아이콘만 보이되 접근 가능한 이름은 그대로 「새 대화」다.
 */
export function SessionHistoryMenu({
  sessions,
  sessionId,
  currentTitle,
  isStreaming,
  creating,
  onSessionChange,
  onTogglePin,
  onNewSession,
}: {
  sessions: ChatSession[];
  sessionId: string | null;
  currentTitle?: string;
  isStreaming: boolean;
  creating: boolean;
  onSessionChange: (sessionId: string) => void;
  onTogglePin: (sessionId: string, pinned: boolean) => void;
  onNewSession: () => void;
}) {
  return (
    <div
      className="flex shrink-0 items-center justify-between gap-2 px-3 py-1.5 md:px-4"
      data-testid="conversation-bar"
    >
      <DropdownMenu>
        <DropdownMenuTrigger
          disabled={isStreaming}
          render={
            <Button
              variant="ghost"
              size="sm"
              className="min-w-0 max-w-80 text-muted-foreground coarse:min-h-11"
              data-testid="session-history"
            >
              <span className="truncate">{sessionTriggerLabel(currentTitle)}</span>
              <ChevronDown className="size-4 shrink-0" aria-hidden />
            </Button>
          }
        />
        <DropdownMenuContent align="start" className="w-80" data-testid="session-list">
          <DropdownMenuGroup>
            <DropdownMenuLabel>대화 기록</DropdownMenuLabel>
            {sessions.map((s) => (
              <DropdownMenuItem
                key={s.id}
                onClick={() => onSessionChange(s.id)}
                data-testid="session-item"
              >
                {s.id === sessionId ? (
                  <Check className="size-3.5 shrink-0 text-primary" />
                ) : (
                  <span className="size-3.5 shrink-0" />
                )}
                <span className="truncate">{s.title}</span>
                <button
                  type="button"
                  aria-label={s.pinned ? "고정 해제" : "대화 고정"}
                  data-testid="session-pin-toggle"
                  className="ml-auto shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground coarse:min-h-11 coarse:min-w-11"
                  onClick={(e) => {
                    e.stopPropagation();
                    onTogglePin(s.id, !s.pinned);
                  }}
                >
                  {s.pinned ? (
                    <Pin className="size-3 fill-current text-primary" />
                  ) : (
                    <PinOff className="size-3" />
                  )}
                </button>
                <span className="shrink-0 pl-1 text-xs text-muted-foreground">
                  {sessionDateLabel(s.updated_at)}
                </span>
              </DropdownMenuItem>
            ))}
          </DropdownMenuGroup>
        </DropdownMenuContent>
      </DropdownMenu>
      <Button
        variant="outline"
        size="sm"
        className="shrink-0 coarse:min-h-11 coarse:min-w-11"
        disabled={isStreaming || creating}
        onClick={onNewSession}
        data-testid="new-session"
      >
        <Plus aria-hidden />
        <span className="max-sm:sr-only">새 대화</span>
      </Button>
    </div>
  );
}
