"use client";

import { Check, Plus } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  useAttachTag,
  useCreateTag,
  useDetachTag,
  useTags,
} from "@/hooks/use-tags";
import { notifyError } from "@/lib/notify";
import type { Notebook } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 노트북 태그 붙이기·떼기·새로 만들기(홈 카드·표의 소유자 메뉴, 노트북 화면 헤더가 함께 쓴다).
 * 태그 항목은 눌림 상태(`aria-pressed`)를 가진 버튼이다 — 키보드로도 붙이고 뗄 수 있다.
 * 캐시 갱신은 태그 훅(`use-tags`)이 맡는다.
 */
export function ManageTagsDialog({
  notebook,
  open,
  onOpenChange,
}: {
  notebook: Notebook | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [name, setName] = useState("");
  const tags = useTags();
  const createTag = useCreateTag();
  const attach = useAttachTag(notebook?.id ?? "");
  const detach = useDetachTag(notebook?.id ?? "");

  if (!notebook) return null;

  const attachedNames = new Set(notebook.tags);

  function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return;
    createTag.mutate(
      { name: trimmed },
      {
        onSuccess: (tag) => {
          setName("");
          attach.mutate(tag.id, {
            onError: (err) => notifyError("태그를 추가", err),
          });
        },
        onError: (err) => notifyError("태그를 생성", err),
      },
    );
  }

  function handleToggle(tagId: string, tagName: string) {
    if (attachedNames.has(tagName)) {
      detach.mutate(tagId, { onError: (err) => notifyError("태그를 제거", err) });
    } else {
      attach.mutate(tagId, { onError: (err) => notifyError("태그를 추가", err) });
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>태그 관리</DialogTitle>
          <DialogDescription>
            “{notebook.title}”에 붙일 태그를 고르거나 새로 만드세요. 붙은 태그를 다시 누르면 뗍니다.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleCreate} className="flex gap-2">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="새 태그 이름"
            aria-label="새 태그 이름"
            data-testid="new-tag-input"
          />
          <Button
            type="submit"
            size="icon"
            aria-label="태그 만들기"
            className="coarse:size-11"
            disabled={!name.trim() || createTag.isPending}
          >
            <Plus className="size-4" aria-hidden />
          </Button>
        </form>
        <div role="group" aria-label="태그" className="flex flex-wrap gap-2" data-testid="tag-manage-list">
          {(tags.data ?? []).map((tag) => {
            const attached = attachedNames.has(tag.name);
            return (
              <button
                key={tag.id}
                type="button"
                aria-pressed={attached}
                data-testid="tag-manage-item"
                onClick={() => handleToggle(tag.id, tag.name)}
                className={cn(
                  "inline-flex h-6 items-center gap-1 rounded-full border px-2.5 text-xs font-medium outline-none transition-colors focus-visible:ring-3 focus-visible:ring-ring/50 coarse:min-h-11",
                  attached
                    ? "border-transparent bg-primary text-primary-foreground hover:bg-primary-hover"
                    : "border-border hover:bg-muted",
                )}
              >
                {attached && <Check className="size-3" aria-hidden />}
                {tag.name}
              </button>
            );
          })}
          {tags.data?.length === 0 && (
            <p className="text-sm text-muted-foreground">아직 만든 태그가 없습니다</p>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
