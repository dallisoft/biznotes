"use client";

import { Plus } from "lucide-react";
import { useRouter } from "next/navigation";
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useCreateNotebook } from "@/hooks/use-notebooks";
import { notifyError, notifySuccess } from "@/lib/notify";
import type { Notebook } from "@/lib/types";
import { cn } from "@/lib/utils";

const EMOJI_CHOICES = ["📓", "📚", "🔬", "💡", "🧠", "📊", "🗂️", "✍️"];

/** 만든 노트북의 작업 화면 경로(HCI-3 — 생성 후 이동). */
export function notebookPath(notebook: Pick<Notebook, "id">): string {
  return `/notebooks/${notebook.id}`;
}

/**
 * 새 노트북 다이얼로그. 만들기에 성공하면 토스트 「노트북을 만들었습니다」를 띄우고 **새 노트북 작업 화면으로
 * 이동한다**(plan §6.10.1, HCI-3 — 바로 소스를 넣을 수 있게). 실패하면 다이얼로그와 입력을 그대로 두고
 * 실패 토스트를 띄운다. `open`·`onOpenChange`를 주면 제어형이다 — 빈 상태의 「새 노트북」처럼 트리거 밖에서 연다.
 */
export function CreateNotebookDialog({
  testId = "create-notebook",
  triggerClassName,
  triggerLabel = "새 노트북",
  iconOnly = false,
  open: controlledOpen,
  onOpenChange,
}: {
  testId?: string;
  triggerClassName?: string;
  triggerLabel?: string;
  /** true면 「+」 아이콘 버튼(ghost) — 접근 가능한 이름은 triggerLabel(aria-label). 사이드바 보조 진입점(plan §6.2.2 ⑤) */
  iconOnly?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
} = {}) {
  const router = useRouter();
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false);
  const open = controlledOpen ?? uncontrolledOpen;
  const [title, setTitle] = useState("");
  const [emoji, setEmoji] = useState(EMOJI_CHOICES[0]);
  const create = useCreateNotebook();
  const emojiLabelId = useId();
  const titleId = useId();

  function setOpen(next: boolean) {
    if (controlledOpen === undefined) setUncontrolledOpen(next);
    onOpenChange?.(next);
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (create.isPending || !title.trim()) return;
    create.mutateAsync({ title, emoji }).then(
      (created) => {
        notifySuccess("노트북을 만들었습니다");
        setOpen(false);
        setTitle("");
        setEmoji(EMOJI_CHOICES[0]);
        router.push(notebookPath(created));
      },
      (err: unknown) => notifyError("노트북을 생성", err),
    );
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={
          <Button
            data-testid={testId}
            className={triggerClassName}
            variant={iconOnly ? "ghost" : "default"}
            size={iconOnly ? "icon-sm" : "default"}
            aria-label={iconOnly ? triggerLabel : undefined}
          />
        }
      >
        <Plus className="size-4" aria-hidden={iconOnly || undefined} />
        {iconOnly ? null : triggerLabel}
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>새 노트북 만들기</DialogTitle>
          <DialogDescription>소스를 모으고 질문할 공간을 만듭니다.</DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <span id={emojiLabelId} className="text-sm leading-none font-medium">
              이모지
            </span>
            <div role="group" aria-labelledby={emojiLabelId} className="flex flex-wrap gap-1">
              {EMOJI_CHOICES.map((e) => (
                <button
                  key={e}
                  type="button"
                  aria-pressed={emoji === e}
                  onClick={() => setEmoji(e)}
                  className={cn(
                    "rounded-md p-2 text-xl outline-none hover:bg-accent focus-visible:ring-3 focus-visible:ring-ring/50 coarse:min-h-11 coarse:min-w-11",
                    emoji === e && "bg-accent ring-2 ring-ring",
                  )}
                >
                  {e}
                </button>
              ))}
            </div>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor={titleId}>제목</Label>
            <Input
              id={titleId}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="예: 시장 조사 자료"
              required
              autoFocus
            />
          </div>
          <DialogFooter>
            <Button type="submit" disabled={create.isPending || !title.trim()}>
              {create.isPending ? "만드는 중…" : "만들기"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
