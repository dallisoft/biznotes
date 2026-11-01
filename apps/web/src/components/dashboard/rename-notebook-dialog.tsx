"use client";

import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useOpenSession } from "@/hooks/use-dialog-slot";
import { useUpdateNotebook } from "@/hooks/use-notebooks";
import { notifyError, notifySuccess } from "@/lib/notify";
import type { Notebook } from "@/lib/types";

/**
 * 노트북 이름 변경(홈 카드·표의 소유자 메뉴, 노트북 화면 헤더가 함께 쓴다). 캐시 갱신은 `useUpdateNotebook`이 맡는다.
 *
 * 입력은 열린 회차마다 새 폼으로 시작하고(그때의 이름이 초깃값), 열려 있는 동안의 캐시 갱신으로 덮지 않는다. 사용자가 닫고
 * 다시 연 뒤 이전 요청이 끝나면 성공 토스트는 그대로 띄우되 새 창을 닫거나 새 입력을 바꾸지 않는다(P3 재리뷰 P3-R01).
 *
 * 성공·실패 알림은 요청마다 그 요청의 Promise(`mutateAsync`)로 붙인다 — `mutate(…, 콜백)`은 다시 연 창에서 두 번째 요청을
 * 보내면 같은 관찰자의 이전 요청 콜백이 떨어져 나가, 먼저 보낸 요청의 결과가 아무 알림 없이 사라졌다(P3 재리뷰 2 P3-R2-01).
 */
export function RenameNotebookDialog({
  notebook,
  open,
  onOpenChange,
}: {
  notebook: Notebook | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const update = useUpdateNotebook();
  const { session, isCurrent } = useOpenSession(open, notebook?.id);
  // 요청 중 표시는 그 요청을 보낸 회차의 폼에만 — 다시 연 창은 이전 요청을 기다리지 않는다
  const [pendingSession, setPendingSession] = useState<number | null>(null);

  async function submit(title: string) {
    if (!notebook) return;
    const submitted = session;
    setPendingSession(submitted);
    try {
      await update.mutateAsync({ id: notebook.id, title });
      notifySuccess("노트북 이름을 변경했습니다");
      if (isCurrent(submitted)) onOpenChange(false);
    } catch (err) {
      notifyError("이름을 변경", err);
    } finally {
      setPendingSession((cur) => (cur === submitted ? null : cur));
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* 처음 포커스는 다이얼로그 기본값(첫 포커스 가능 요소 = 이름 칸)에 맡긴다 — 입력 칸 `autoFocus`는 Base UI의 초기
          포커스·복귀 처리와 엇갈려, 더보기 메뉴에서 키보드로 열면 포커스가 모달 밖 ⋯에 남았다(P3 리뷰 수정 중 확인) */}
      <DialogContent>
        <DialogHeader>
          <DialogTitle>노트북 이름 변경</DialogTitle>
        </DialogHeader>
        <RenameForm
          key={session}
          initialTitle={notebook?.title ?? ""}
          pending={pendingSession === session}
          onSubmit={(title) => void submit(title)}
        />
      </DialogContent>
    </Dialog>
  );
}

/** 한 회차의 입력 — 초깃값은 처음 그릴 때만 쓴다(이후 `initialTitle`이 바뀌어도 입력 중인 초안을 덮지 않는다). */
function RenameForm({
  initialTitle,
  pending,
  onSubmit,
}: {
  initialTitle: string;
  pending: boolean;
  onSubmit: (title: string) => void;
}) {
  const [title, setTitle] = useState(initialTitle);
  const titleId = useId();

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim()) return;
    onSubmit(title);
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4">
      <div className="flex flex-col gap-2">
        <Label htmlFor={titleId}>이름</Label>
        <Input id={titleId} value={title} onChange={(e) => setTitle(e.target.value)} required />
      </div>
      <DialogFooter>
        <Button type="submit" disabled={pending || !title.trim()}>
          {pending ? "저장하는 중…" : "저장"}
        </Button>
      </DialogFooter>
    </form>
  );
}
