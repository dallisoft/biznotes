"use client";

import { toast } from "sonner";
import { BulkResultList } from "@/components/dashboard/bulk-result-list";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { type BulkRunReport, useBulkDelete } from "@/hooks/use-notebook-bulk";
import {
  type BulkTarget,
  formatDeleteSummary,
  previewTitles,
  STOP_NOTICES,
  summarizeResults,
} from "@/lib/notebook-bulk";

/**
 * 일괄 삭제 확인·진행·결과 다이얼로그(plan §5.6, D-12). `targets`는 툴바에서 연 순간 캡처한 소유 노트북
 * 목록이고(표시 순서), 확인하면 정확히 이 목록만 지운다. 실행 중에는 닫히지 않는다(S-6 — Esc·바깥 클릭
 * 무시, 취소 버튼 없음). 「중지」는 새 항목 시작만 막고 이미 보낸 DELETE는 응답을 기다린다(D-06).
 */
export function BulkDeleteDialog({
  targets,
  onClose,
  onFinished,
}: {
  targets: readonly BulkTarget[] | null;
  onClose: () => void;
  onFinished: (report: BulkRunReport) => void;
}) {
  const bulk = useBulkDelete();
  const open = targets !== null;
  const total = targets?.length ?? 0;
  const running = bulk.phase === "running";
  const { shown, rest } = previewTitles(targets ?? []);
  const summary = bulk.report ? summarizeResults(bulk.report.results) : null;

  function requestClose() {
    if (running) return;
    bulk.reset();
    onClose();
  }

  async function handleConfirm() {
    if (!targets || targets.length === 0) return;
    const report = await bulk.start(targets);
    if (!report) return;
    onFinished(report);
    const s = summarizeResults(report.results);
    const text = formatDeleteSummary(s);
    if (s.succeeded === s.total) toast.success(text);
    else toast.error(text);
  }

  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        if (!next) requestClose();
      }}
    >
      <AlertDialogContent className="sm:max-w-lg" data-testid="bulk-delete-dialog">
        <AlertDialogHeader>
          <AlertDialogTitle>
            {bulk.phase === "idle"
              ? `노트북 ${total}개를 영구 삭제할까요?`
              : running
                ? "노트북 삭제 중"
                : "노트북 삭제 결과"}
          </AlertDialogTitle>
          <AlertDialogDescription>
            {bulk.phase === "idle"
              ? `선택한 노트북 ${total}개와 그 안의 소스·메모·대화·에이전트 실행 기록·공유 설정이 모두 영구 삭제되며 되돌릴 수 없습니다. 공유받은 사용자도 더 이상 접근할 수 없습니다.`
              : running
                ? "삭제하는 중입니다. 끝날 때까지 이 창을 닫을 수 없습니다."
                : "삭제가 끝났습니다. 실패하거나 실행하지 않은 노트북은 선택된 채로 남습니다."}
          </AlertDialogDescription>
        </AlertDialogHeader>

        {bulk.phase === "idle" && (
          <div className="grid gap-1 text-sm">
            <ul aria-label="삭제할 노트북" className="list-disc pl-5" data-testid="bulk-delete-titles">
              {shown.map((title, i) => (
                <li key={`${i}-${title}`} className="truncate">
                  {title}
                </li>
              ))}
            </ul>
            {rest > 0 && (
              <p className="text-muted-foreground" data-testid="bulk-delete-rest">
                외 {rest}개
              </p>
            )}
          </div>
        )}

        {running && (
          <div className="grid gap-2">
            <p role="status" aria-live="polite" className="text-sm font-medium" data-testid="bulk-progress">
              {bulk.progress.done} / {bulk.progress.total}
            </p>
            <progress
              className="h-2 w-full"
              aria-label="삭제 진행"
              max={Math.max(bulk.progress.total, 1)}
              value={bulk.progress.done}
            />
          </div>
        )}

        {bulk.phase === "done" && bulk.report && summary && (
          <BulkResultList
            results={bulk.report.results}
            summary={formatDeleteSummary(summary)}
            notice={bulk.report.stopReason ? STOP_NOTICES[bulk.report.stopReason] : null}
          />
        )}

        <AlertDialogFooter>
          {bulk.phase === "idle" && (
            <>
              <AlertDialogCancel>취소</AlertDialogCancel>
              <Button
                className="bg-destructive text-destructive-foreground hover:bg-destructive-hover"
                onClick={handleConfirm}
                disabled={total === 0}
              >
                노트북 {total}개 영구 삭제
              </Button>
            </>
          )}
          {running && (
            <Button variant="outline" onClick={bulk.stop} disabled={bulk.stopping}>
              {bulk.stopping ? "중지하는 중…" : "중지"}
            </Button>
          )}
          {bulk.phase === "done" && <Button onClick={requestClose}>닫기</Button>}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
