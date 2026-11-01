"use client";

import { ArrowLeft, Plus } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { MarkdownContent } from "@/components/chat/markdown-content";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import {
  useConvertNoteToSource,
  useCreateNote,
  useDeleteNote,
  useNotes,
  useUpdateNote,
} from "@/hooks/use-notes";
import { formatRelative } from "@/lib/format";
import { NOTE_ORIGIN_LABEL, type Note } from "@/lib/note-types";
import { getNoteProvenanceView, NOTE_PROVENANCE_COPY } from "@/lib/provenance";

/** 노트 뷰어 "생성 근거" 섹션 — origin!=manual 노트에서만 호출부가 렌더한다(AC-12). */
function NoteProvenanceSection({ note }: { note: Note }) {
  const view = getNoteProvenanceView(note);
  return (
    <div
      className="rounded-md border bg-muted/40 p-2 text-xs"
      data-testid="note-provenance"
    >
      <p className="mb-1 font-medium text-muted-foreground">생성 근거</p>
      {view.edited && (
        <p className="mb-1 text-warning-text" data-testid="note-provenance-edited">
          {NOTE_PROVENANCE_COPY.edited}
        </p>
      )}
      {view.state === "none" && (
        <p className="text-muted-foreground">{NOTE_PROVENANCE_COPY.none}</p>
      )}
      {view.state === "unrecorded" && (
        <p className="text-muted-foreground">{NOTE_PROVENANCE_COPY.unrecorded}</p>
      )}
      {view.state === "empty" && (
        <>
          <p className="mb-1 text-muted-foreground">{view.modeLabel}</p>
          <p className="text-muted-foreground">{NOTE_PROVENANCE_COPY.empty}</p>
        </>
      )}
      {view.state === "items" && (
        <>
          <p className="mb-1 text-muted-foreground">{view.modeLabel}</p>
          <ul className="flex flex-wrap gap-1">
            {view.badges.map((badge) => (
              <li key={badge.key}>
                <Badge
                  variant={badge.failed ? "destructive" : "secondary"}
                  className="text-xs"
                >
                  {badge.label}
                </Badge>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

/**
 * 메모 보기·편집 — 모달 대신 메모 패널(우측 패널) 안에 인라인으로 표시한다(에이전트 실행 상세와 같은 방식).
 * 상단 「← 목록」으로 목록에 돌아가고, 본문은 패널 높이에 맞춰 세로 스크롤하며, 동작은 아래에 고정한다.
 * 기존 메모는 렌더링된 뷰어로 먼저 열고 「편집」으로 같은 자리에서 편집한다. 새 메모는 바로 편집으로 연다.
 * 저장·삭제·소스 변환이 끝나면 목록으로 돌아간다(모달을 닫던 것과 같은 흐름). 돌아갈 때의 포커스 복귀는
 * NotePanel이 맡는다.
 */
function NoteDetailView({
  notebookId,
  note,
  canWrite,
  onClose,
}: {
  notebookId: string;
  note: Note | null; // null = 새 메모
  canWrite: boolean;
  onClose: () => void;
}) {
  const [title, setTitle] = useState(note?.title ?? "");
  const [content, setContent] = useState(note?.content ?? "");
  const [editing, setEditing] = useState(note === null);
  const create = useCreateNote(notebookId);
  const update = useUpdateNote(notebookId);
  const remove = useDeleteNote(notebookId);
  const convert = useConvertNoteToSource(notebookId);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const titleInputRef = useRef<HTMLInputElement>(null);

  // 화면이 바뀐 것을 알린다 — 보기는 제목(머리글)으로, 편집은 제목 입력으로 포커스를 옮긴다.
  useEffect(() => {
    (editing ? titleInputRef.current : headingRef.current)?.focus();
  }, [editing]);

  function cancelEdit() {
    if (!note) {
      onClose();
      return;
    }
    setTitle(note.title);
    setContent(note.content);
    setEditing(false);
  }

  function handleSave() {
    if (!title.trim()) {
      toast.error("제목을 입력하세요");
      return;
    }
    const onError = (err: Error) => toast.error(`저장 실패: ${err.message}`);
    if (note) {
      update.mutate(
        { noteId: note.id, title, content },
        {
          onSuccess: () => {
            toast.success("메모를 수정했습니다");
            onClose();
          },
          onError,
        },
      );
    } else {
      create.mutate(
        { title, content },
        {
          onSuccess: () => {
            toast.success("메모를 저장했습니다");
            onClose();
          },
          onError,
        },
      );
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="note-detail-view">
      <div className="flex shrink-0 items-center gap-2 border-b px-3 py-2">
        <Button
          variant="ghost"
          size="sm"
          onClick={onClose}
          className="text-muted-foreground"
          data-testid="note-detail-back"
        >
          <ArrowLeft className="size-4" /> 목록
        </Button>
        <div className="ml-1 min-w-0">
          <h2
            ref={headingRef}
            tabIndex={-1}
            className="truncate text-sm font-semibold outline-none"
          >
            {editing ? (note ? "메모 편집" : "새 메모") : (note?.title ?? "메모")}
          </h2>
          {note && (
            <p className="truncate text-xs text-muted-foreground">
              {NOTE_ORIGIN_LABEL[note.origin]} · {formatRelative(note.updated_at)}
            </p>
          )}
        </div>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3">
        {editing ? (
          <>
            <Input
              ref={titleInputRef}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="제목"
              aria-label="제목"
              disabled={!canWrite}
              data-testid="note-title-input"
            />
            <Textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              placeholder="내용 (markdown)"
              aria-label="내용"
              rows={12}
              disabled={!canWrite}
              data-testid="note-content-input"
            />
          </>
        ) : (
          <div data-testid="note-viewer">
            {content ? (
              <MarkdownContent content={content} citations={note?.citations ?? []} />
            ) : (
              <p className="text-sm text-muted-foreground">(내용 없음)</p>
            )}
          </div>
        )}
        {note && note.citations.length > 0 && (
          <div className="rounded-md border bg-muted/40 p-2 text-xs">
            <p className="mb-1 font-medium text-muted-foreground">인용 출처</p>
            <ul className="flex flex-col gap-1">
              {note.citations.map((c) => (
                <li key={c.n} className="truncate">
                  [{c.n}] {c.source_title}
                </li>
              ))}
            </ul>
          </div>
        )}
        {note && note.origin !== "manual" && <NoteProvenanceSection note={note} />}
      </div>

      {canWrite && (
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t px-3 py-2">
          <div className="flex gap-2">
            {note && !editing && (
              <>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={convert.isPending}
                  onClick={() =>
                    convert.mutate(note.id, {
                      onSuccess: () => {
                        toast.success("소스로 변환했습니다 — 인제스천이 시작됩니다");
                        onClose();
                      },
                      onError: (err) => toast.error(`변환 실패: ${err.message}`),
                    })
                  }
                >
                  소스로 변환
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="text-destructive"
                  onClick={() =>
                    remove.mutate(note.id, {
                      onSuccess: () => {
                        toast.success("메모를 삭제했습니다");
                        onClose();
                      },
                      onError: (err) => toast.error(`삭제 실패: ${err.message}`),
                    })
                  }
                >
                  삭제
                </Button>
              </>
            )}
          </div>
          {editing ? (
            <div className="flex gap-2">
              <Button size="sm" variant="ghost" onClick={cancelEdit} data-testid="note-cancel">
                취소
              </Button>
              <Button
                size="sm"
                onClick={handleSave}
                disabled={create.isPending || update.isPending}
                data-testid="note-save"
              >
                저장
              </Button>
            </div>
          ) : (
            <Button
              size="sm"
              variant="outline"
              onClick={() => setEditing(true)}
              data-testid="note-edit"
            >
              편집
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

/** 상세를 연 요소의 식별자 — 상세를 닫으면 그 요소로 포커스를 돌린다(모달이 하던 일). */
const NEW_NOTE_OPENER = "new";
const noteOpenerKey = (noteId: string) => `note:${noteId}`;

export function NotePanel({
  notebookId,
  canWrite,
}: {
  notebookId: string;
  canWrite: boolean;
}) {
  const notes = useNotes(notebookId);
  // detail: null=목록, { noteId: null }=새 메모, { noteId }=메모 보기·편집
  const [detail, setDetail] = useState<{ noteId: string | null } | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const restoreFocusRef = useRef<string | null>(null);

  useEffect(() => {
    const key = restoreFocusRef.current;
    if (detail || !key) return;
    restoreFocusRef.current = null;
    const openers = listRef.current?.querySelectorAll<HTMLElement>("[data-note-opener]") ?? [];
    // 연 요소가 없으면(삭제·소스 변환으로 목록에서 빠짐) 패널 머리글로 보낸다
    const target = Array.from(openers).find((el) => el.dataset.noteOpener === key);
    (target ?? headingRef.current)?.focus();
  }, [detail]);

  // 폴링·무효화된 목록의 최신 메모를 쓴다(다른 곳에서 지워졌으면 목록으로 돌아간다).
  const detailNote =
    detail?.noteId != null ? ((notes.data ?? []).find((n) => n.id === detail.noteId) ?? null) : null;

  if (detail && (detail.noteId === null || detailNote)) {
    return (
      <NoteDetailView
        key={detail.noteId ?? NEW_NOTE_OPENER}
        notebookId={notebookId}
        note={detailNote}
        canWrite={canWrite}
        onClose={() => {
          restoreFocusRef.current =
            detail.noteId === null ? NEW_NOTE_OPENER : noteOpenerKey(detail.noteId);
          setDetail(null);
        }}
      />
    );
  }

  return (
    <div ref={listRef} className="flex h-full min-h-0 flex-col">
      <div className="flex items-center justify-between border-b px-3 py-2">
        <h2 ref={headingRef} tabIndex={-1} className="text-sm font-semibold outline-none">
          메모
        </h2>
        {canWrite && (
          <Button
            variant="outline"
            size="sm"
            className="coarse:min-h-11"
            onClick={() => setDetail({ noteId: null })}
            data-testid="new-note"
            data-note-opener={NEW_NOTE_OPENER}
          >
            <Plus aria-hidden />
            새 메모
          </Button>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {notes.isPending ? (
          <div className="flex flex-col gap-2 p-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-20" />
            ))}
          </div>
        ) : (notes.data ?? []).length === 0 ? (
          <p className="px-4 py-10 text-center text-sm text-muted-foreground">
            아직 메모가 없습니다.
            <br />
            채팅 답변을 저장하거나 직접 작성해 보세요.
          </p>
        ) : (
          <ul className="flex flex-col gap-2 p-3" data-testid="note-list">
            {(notes.data ?? []).map((note) => (
              <li key={note.id}>
                <button
                  type="button"
                  onClick={() => setDetail({ noteId: note.id })}
                  data-note-opener={noteOpenerKey(note.id)}
                  className="w-full rounded-lg border p-3 text-left transition-colors hover:bg-accent/60"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-medium">{note.title}</span>
                    {note.origin !== "manual" && (
                      <Badge variant="secondary" className="shrink-0 text-xs">
                        {NOTE_ORIGIN_LABEL[note.origin]}
                      </Badge>
                    )}
                  </div>
                  <p className="mt-1 line-clamp-2 whitespace-pre-wrap text-xs text-muted-foreground">
                    {note.content || "(내용 없음)"}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {formatRelative(note.updated_at)}
                  </p>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
