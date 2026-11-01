"use client";

import { ArrowUp, FileText, Square, X, Zap } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  ENTRY_KIND_LABEL,
  ENTRY_SOURCE_LABEL,
  filterEntries,
  paletteQuery,
  parseSlashInput,
  stripFirstToken,
} from "@/lib/chat-entry";
import type { ChatEntry } from "@/lib/chat-types";
import { cn } from "@/lib/utils";

export interface ModelOption {
  value: string;
  label: string;
}

/** 진입 실행 대상 — 팔레트에서 고른 항목(칩) 또는 칩 없이 쓴 `/<이름>`. */
export type EntryTarget = { agent_id: string } | { name: string };

/**
 * 진입 실행 결과 — `sent`면 입력을 비운다. `none`·`failed`는 입력을 그대로 둔다(알림은 호출측).
 * `many`면 그 후보로 팔레트를 연다(이름이 여러 항목에 맞으면 실행하지 않는다 — 코어 #410 D3).
 */
export type EntrySendResult =
  | { status: "sent" }
  | { status: "none" }
  | { status: "failed" }
  | { status: "many"; candidates: ChatEntry[] };

/**
 * 입력창 + 모델 셀렉터 + 전송 버튼 + `/` 팔레트(#66).
 *
 * `input`은 여기서만 쓰이므로 이 컴포넌트가 소유한다. 반면 선택된 소스(zustand)와
 * 실제 전송(`send`)은 ChatPanel이 계속 들고 있다 — 세션·소스 상태와 얽혀 있어서
 * 함께 내리면 결합만 옮기는 꼴이 된다.
 *
 * 팔레트는 입력의 첫 글자가 `/`이고 커서가 첫 토큰 안에 있을 때만 연다. 고르면 칩이 되고 남은 입력이 질문이다.
 * `entries`가 없으면(옛 API·불러오기 실패) 팔레트를 쓰지 않고 `/`로 시작하는 입력도 일반 메시지로 보낸다.
 */
export function ChatComposer({
  modelId,
  onModelChange,
  modelOptions,
  selectedCount,
  isStreaming,
  disabled,
  onSend,
  onStop,
  restoredInput = null,
  entries = null,
  onSendEntry,
  entryLocked = false,
}: {
  modelId: string | null;
  /** Base UI Select는 선택 해제 시 null을 흘린다 — 원본처럼 setState를 그대로 받는다. */
  onModelChange: (id: string | null) => void;
  modelOptions: ModelOption[];
  /** 선택한 소스 개수 — 0이면 칩을 숨긴다. */
  selectedCount: number;
  isStreaming: boolean;
  /** 세션이 아직 없으면 전송할 수 없다. */
  disabled: boolean;
  onSend: (content: string) => void;
  /** 생성 중인 답변을 중지한다(B3). */
  onStop: () => void;
  /** 중지한 질문을 입력창에 되돌린다. `id`가 바뀔 때마다 한 번 적용한다(같은 문장을 두 번 되돌릴 수 있게). */
  restoredInput?: { text: string; id: number } | null;
  /** `/` 팔레트 항목 — null이면 팔레트를 쓰지 않는다. */
  entries?: ChatEntry[] | null;
  onSendEntry?: (target: EntryTarget, content: string) => Promise<EntrySendResult>;
  /** 진입 run이 끝나기 전이면 전송을 잠근다(D3 — 결과가 나중 메시지 뒤에 끼지 않게). */
  entryLocked?: boolean;
}) {
  const [input, setInput] = useState("");
  const [caret, setCaret] = useState(0);
  const [chip, setChip] = useState<ChatEntry | null>(null);
  // 이름이 여러 항목에 맞았을 때의 후보 — 있으면 필터 대신 이 목록을 보인다
  const [candidates, setCandidates] = useState<ChatEntry[] | null>(null);
  // Esc로 닫은(또는 포커스를 잃은) 팔레트는 입력이 바뀌거나 다시 포커스할 때까지 열지 않는다
  const [dismissed, setDismissed] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [sending, setSending] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const hintId = useId();
  const listId = useId();

  useEffect(() => {
    if (restoredInput) setInput(restoredInput.text);
  }, [restoredInput]);

  const paletteEnabled = entries !== null && onSendEntry !== undefined;
  const query = paletteEnabled && !chip ? paletteQuery(input, caret) : null;
  const items = candidates ?? (query !== null && entries ? filterEntries(entries, query) : []);
  const paletteOpen = paletteEnabled && !chip && !dismissed && (candidates !== null || query !== null);
  const active = items.length ? Math.min(activeIndex, items.length - 1) : -1;
  const blocked = isStreaming || disabled || entryLocked || sending;

  function changeInput(value: string, nextCaret: number) {
    setInput(value);
    setCaret(nextCaret);
    setDismissed(false);
    setCandidates(null);
    setActiveIndex(0);
  }

  function choose(entry: ChatEntry) {
    const rest = stripFirstToken(input);
    setChip(entry);
    setInput(rest);
    setCaret(rest.length);
    setCandidates(null);
    setDismissed(false);
    setActiveIndex(0);
    textareaRef.current?.focus();
  }

  function removeChip() {
    setChip(null);
    textareaRef.current?.focus();
  }

  async function runEntry(target: EntryTarget, question: string) {
    if (!onSendEntry) return;
    setSending(true);
    try {
      const result = await onSendEntry(target, question);
      if (result.status === "sent") {
        setInput("");
        setCaret(0);
        setChip(null);
      } else if (result.status === "many") {
        setCandidates(result.candidates);
        setDismissed(false);
        setActiveIndex(0);
      }
    } finally {
      setSending(false);
    }
  }

  function handleSend(e?: React.FormEvent) {
    e?.preventDefault();
    const content = input.trim();
    if (blocked) return;
    if (chip) {
      if (content) void runEntry({ agent_id: chip.agent_id }, content);
      return;
    }
    const slash = paletteEnabled ? parseSlashInput(content) : null;
    if (slash) {
      if (!slash.question) {
        toast(`/${slash.name} 뒤에 질문을 함께 입력하세요`);
        return;
      }
      void runEntry({ name: slash.name }, slash.question);
      return;
    }
    if (!content) return;
    setInput("");
    setCaret(0);
    onSend(content);
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.nativeEvent.isComposing) return;
    // 진입 요청 중에는 입력을 바꾸지 않는다 — 성공하면 보낸 입력을 비우므로 그사이 고친 내용이 사라진다
    if (sending) {
      if (e.key === "Enter" || e.key === "Backspace" || e.key === "Tab") e.preventDefault();
      return;
    }
    if (paletteOpen) {
      if (e.key === "Escape") {
        e.preventDefault();
        setDismissed(true);
        setCandidates(null);
        return;
      }
      if (items.length && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
        e.preventDefault();
        const step = e.key === "ArrowDown" ? 1 : -1;
        setActiveIndex((active + step + items.length) % items.length);
        return;
      }
      if (items.length && (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey))) {
        e.preventDefault();
        choose(items[active]);
        return;
      }
    }
    if (e.key === "Backspace" && chip && input === "") {
      e.preventDefault();
      removeChip();
      return;
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  const placeholder = chip
    ? `${chip.display_name}에 보낼 질문을 입력하세요…`
    : paletteEnabled
      ? "소스에 대해 질문하세요… (/ 로 스킬 실행)"
      : "소스에 대해 질문하세요…";

  return (
    <div className="shrink-0 px-4 pb-4">
      <form
        onSubmit={handleSend}
        className="relative mx-auto w-full max-w-3xl rounded-2xl border bg-card shadow-sm transition-shadow focus-within:border-ring/60 focus-within:shadow-md"
      >
        {paletteOpen && (
          <div
            id={listId}
            role="listbox"
            aria-label="실행할 스킬·워크플로"
            className="absolute inset-x-0 bottom-full z-20 mb-2 max-h-72 overflow-y-auto rounded-xl border bg-popover p-1 text-popover-foreground shadow-lg"
            data-testid="entry-palette"
          >
            {items.length === 0 ? (
              <p className="px-3 py-2 text-xs text-muted-foreground">일치하는 스킬·워크플로가 없습니다</p>
            ) : (
              items.map((entry, i) => (
                <div
                  key={entry.agent_id}
                  id={`${listId}-${i}`}
                  role="option"
                  aria-selected={i === active}
                  // 입력창 포커스를 잃지 않게 mousedown에서 고른다
                  onMouseDown={(e) => {
                    e.preventDefault();
                    choose(entry);
                  }}
                  onMouseEnter={() => setActiveIndex(i)}
                  className={cn(
                    "flex cursor-pointer items-start gap-2 rounded-lg px-3 py-2 text-sm",
                    i === active && "bg-accent text-accent-foreground",
                  )}
                  data-testid="entry-option"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="truncate font-medium">{entry.display_name}</span>
                      {entry.slash_name && (
                        <code className="shrink-0 font-mono text-xs text-muted-foreground">
                          /{entry.slash_name}
                        </code>
                      )}
                    </div>
                    {entry.description && (
                      <p className="truncate text-xs text-muted-foreground">{entry.description}</p>
                    )}
                  </div>
                  <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                    {entry.kind === "workflow" ? ENTRY_KIND_LABEL.workflow : ENTRY_SOURCE_LABEL[entry.source]}
                  </span>
                </div>
              ))
            )}
          </div>
        )}
        {chip && (
          <div className="px-3 pt-2.5">
            <span
              className="inline-flex max-w-full items-center gap-1 rounded-full bg-primary/10 py-0.5 pr-1 pl-2 text-xs font-medium text-primary"
              data-testid="entry-chip"
            >
              <Zap className="size-3 shrink-0" aria-hidden />
              <span className="truncate">{chip.slash_name ? `/${chip.slash_name}` : chip.display_name}</span>
              <button
                type="button"
                onClick={removeChip}
                disabled={sending}
                aria-label={`${chip.display_name} 실행 취소`}
                className="rounded-full p-0.5 hover:bg-primary/15"
              >
                <X className="size-3" aria-hidden />
              </button>
            </span>
          </div>
        )}
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(e) => changeInput(e.target.value, e.target.selectionStart ?? e.target.value.length)}
          onSelect={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
          onKeyDown={handleKeyDown}
          onBlur={() => setDismissed(true)}
          onFocus={() => setDismissed(false)}
          placeholder={placeholder}
          readOnly={sending}
          aria-busy={sending}
          aria-describedby={hintId}
          aria-controls={paletteOpen ? listId : undefined}
          aria-activedescendant={paletteOpen && active >= 0 ? `${listId}-${active}` : undefined}
          rows={2}
          enterKeyHint="send"
          className="w-full resize-none bg-transparent px-4 pt-3 text-sm outline-none placeholder:text-muted-foreground"
          data-testid="chat-input"
        />
        <div className="flex items-center gap-2 px-2.5 pb-2.5">
          {/* 단축키 안내 — placeholder에서 빼 입력 설명으로 둔다. 키보드 없는 좁은 화면에서는 숨긴다(⑧) */}
          <span id={hintId} className="sr-only">
            Enter 전송 · Shift+Enter 줄바꿈{paletteEnabled ? " · / 로 스킬·워크플로 실행" : ""}
          </span>
          {/* Base UI Select는 두 경로를 쓴다: `items`가 닫힌 상태의 라벨(<SelectValue>)을 해석하고,
              children <SelectItem>이 열린 팝업 목록을 그린다. 둘 다 필요하므로(제거하면 모델 UUID가
              노출된다 — ui-redesign-phase-c 회고) 호출부가 단일 배열에서 파생해 넘긴다. */}
          <Select value={modelId ?? ""} onValueChange={onModelChange} items={modelOptions}>
            <SelectTrigger
              size="sm"
              className="h-7 max-w-48 border-none bg-transparent text-xs text-muted-foreground shadow-none hover:bg-accent"
              data-testid="model-select"
            >
              <SelectValue placeholder="모델 선택" />
            </SelectTrigger>
            <SelectContent>
              {modelOptions.map((o) => (
                <SelectItem key={o.value} value={o.value}>
                  {o.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {selectedCount > 0 && (
            <span className="flex items-center gap-1 rounded-full bg-accent px-2 py-0.5 text-xs text-accent-foreground">
              <FileText className="size-3" />
              소스 {selectedCount}
            </span>
          )}
          {entryLocked && (
            <span className="text-xs text-muted-foreground" data-testid="entry-locked-hint">
              실행이 끝나면 보낼 수 있습니다
            </span>
          )}
          {isStreaming ? (
            <Button
              type="button"
              size="icon"
              variant="secondary"
              aria-label="답변 중지"
              title="답변 중지"
              onClick={onStop}
              className="ml-auto size-8 rounded-full coarse:size-11"
            >
              <Square className="size-3.5 fill-current" aria-hidden />
            </Button>
          ) : (
            <Button
              type="submit"
              size="icon"
              aria-label="전송"
              disabled={!input.trim() || disabled || entryLocked || sending}
              className="ml-auto size-8 rounded-full"
            >
              <ArrowUp className="size-4" />
            </Button>
          )}
        </div>
      </form>
    </div>
  );
}
