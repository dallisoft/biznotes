"use client";

import { Copy } from "lucide-react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useDuplicateWorkflow } from "@/hooks/use-workflows";
import { notifyError, notifySuccess, successMessage } from "@/lib/notify";
import type { Workflow } from "@/lib/workflow-types";

/** 편집할 수 없는 워크플로우를 열었을 때 — 제자리 링크 대신 「복제해서 편집」으로 안내한다(P-09 ⑤). */
export function LockedWorkflowDialog({ wf, onClose }: { wf: Workflow | null; onClose: () => void }) {
  const router = useRouter();
  const duplicate = useDuplicateWorkflow();
  return (
    <Dialog open={wf !== null} onOpenChange={(open) => !open && onClose()}>
      {/* 푸터에 「닫기」가 있으므로 X는 두지 않는다 — 같은 이름 버튼 둘 방지(E-P5I-1) */}
      <DialogContent showCloseButton={false}>
        <DialogHeader>
          <DialogTitle>이 워크플로우는 편집할 수 없습니다</DialogTitle>
          <DialogDescription>
            {wf ? `‘${wf.name}’은(는) 다른 사람이 만들었거나 시스템이 제공하는 워크플로우입니다. ` : ""}
            복제하면 내 워크플로우로 편집할 수 있습니다.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose render={<Button variant="outline" className="coarse:h-11" />}>닫기</DialogClose>
          <Button
            className="coarse:h-11"
            disabled={!wf || duplicate.isPending}
            onClick={() => {
              if (!wf) return;
              duplicate.mutate(wf.id, {
                onSuccess: (clone) => {
                  notifySuccess(successMessage("워크플로우", "복제"));
                  onClose();
                  router.push(`/workflows/${clone.id}/edit`);
                },
                onError: (err) => notifyError("워크플로우를 복제", err),
              });
            }}
          >
            <Copy aria-hidden />
            복제해서 편집
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
