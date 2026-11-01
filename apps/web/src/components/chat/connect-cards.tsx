"use client";

import { FolderPlus } from "lucide-react";
import { useState } from "react";
import { AddSourceDialog } from "@/components/sources/add-source-dialog";
import { useSources } from "@/hooks/use-sources";

/**
 * 빈 대화 화면의 「소스 추가하기」 카드. 기존 AddSourceDialog 흐름을 재사용한다.
 * 카드로 연 다이얼로그 설명의 사용량은 실제 소스 수다.
 */
export function ConnectCards({ notebookId }: { notebookId: string }) {
  const sources = useSources(notebookId);
  const [open, setOpen] = useState(false);

  return (
    <div className="flex flex-col gap-2" data-testid="connect-cards">
      <h4 className="text-xs font-medium text-muted-foreground">소스 추가하기</h4>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="flex flex-col gap-2 rounded-xl border bg-card p-4 text-left shadow-xs transition-colors hover:border-primary/40 hover:bg-accent focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
          data-testid="connect-card-upload"
        >
          <FolderPlus className="size-5 text-primary" aria-hidden />
          <span className="text-sm font-medium">소스 추가</span>
          <span className="text-xs text-muted-foreground">파일 업로드 · 웹 · 텍스트 직접 추가</span>
        </button>
      </div>
      <AddSourceDialog
        notebookId={notebookId}
        sourceCount={sources.data?.length ?? 0}
        open={open}
        onOpenChange={setOpen}
        defaultTab="file"
        showTrigger={false}
      />
    </div>
  );
}
