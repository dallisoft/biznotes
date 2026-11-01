"use client";

import { Plus } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
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
import { useCreateWorkflow } from "@/hooks/use-workflows";
import { notifyError, notifySuccess } from "@/lib/notify";

/** 서버 `WorkflowCreate.name` 제약(OpenAPI): 1~100자. */
export const WORKFLOW_NAME_MAX = 100;

/**
 * 「새 워크플로우」 — 이름을 받아 만든 뒤 편집 화면으로 간다(plan §6.6.2 워크플로우 목록, HCI-3 ②).
 * 즉시 「제목 없는 워크플로우」를 만들던 흐름을 대체한다. 트리거의 `data-testid="create-workflow"`는 유지한다(D-17).
 */
export function CreateWorkflowDialog() {
  const router = useRouter();
  const create = useCreateWorkflow();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const trimmed = name.trim();

  function onOpenChange(next: boolean) {
    setOpen(next);
    if (!next) setName("");
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!trimmed || create.isPending) return;
    create.mutate(
      { name: trimmed },
      {
        onSuccess: (workflow) => {
          notifySuccess("워크플로우를 만들었습니다");
          onOpenChange(false);
          router.push(`/workflows/${workflow.id}/edit`);
        },
        onError: (err) => notifyError("워크플로우를 생성", err),
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger render={<Button data-testid="create-workflow" className="coarse:h-11" />}>
        <Plus aria-hidden />새 워크플로우
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>새 워크플로우</DialogTitle>
          <DialogDescription>이름을 정하면 편집 화면에서 노드를 연결합니다.</DialogDescription>
        </DialogHeader>
        <form onSubmit={onSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="new-workflow-name">이름</Label>
            <Input
              id="new-workflow-name"
              value={name}
              maxLength={WORKFLOW_NAME_MAX}
              placeholder="예: 주간 보고서 초안"
              autoFocus
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <DialogFooter>
            <Button type="submit" disabled={!trimmed || create.isPending} className="coarse:h-11">
              {create.isPending ? "만드는 중…" : "만들기"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
