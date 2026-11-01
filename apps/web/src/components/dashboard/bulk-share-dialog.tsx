"use client";

import { useState } from "react";
import { toast } from "sonner";
import { BulkResultList } from "@/components/dashboard/bulk-result-list";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useMyGroups } from "@/hooks/use-my-groups";
import { type BulkRunReport, useBulkShare } from "@/hooks/use-notebook-bulk";
import { NOTEBOOK_PERMISSION_LABEL, SHARE_PERMISSION_OPTIONS } from "@/lib/labels";
import {
  BULK_MESSAGES,
  type BulkTarget,
  formatShareSummary,
  previewTitles,
  type SharePermission,
  STOP_NOTICES,
  summarizeResults,
} from "@/lib/notebook-bulk";

/**
 * 제출 전 요약 — 「노트북 N개 · {그룹 이름} 그룹 · {권한}」(AC-3.6, core-v0.20.0 §5.8 그룹판). 권한 라벨은 단건 공유·표
 * 보기와 같은 사전(`NOTEBOOK_PERMISSION_LABEL` — 「읽기 전용/편집 가능」, 괄호 영문 없음 — plan §6.3.2).
 */
function requestSummary(count: number, groupName: string, permission: SharePermission): string {
  return `노트북 ${count}개 · ${groupName} 그룹 · ${NOTEBOOK_PERMISSION_LABEL[permission]}`;
}

/** 입력 단계의 설명 — 「건너뜀」은 결정 시점 기준이라 그 범위로 쓴다(core-v0.20.0 §5.3·PR-16). */
const BULK_SHARE_DESCRIPTION =
  "선택한 그룹에 권한을 추가합니다. 이미 같거나 높은 권한으로 공유된 노트북은 건너뜁니다. 진행 중 다른 곳에서 바꾼 공유는 이 권한으로 바뀔 수 있습니다.";

/**
 * 일괄 공유 입력·진행·결과 다이얼로그(plan §5.5·§5.7, core-v0.20.0 §5.8). 내가 속한 그룹 하나(`GET /me/groups`)에
 * read/write를 **추가**한다 — 사용자(이메일) 대상·되돌리기는 없다. 그룹이 0개면 선택 대신 관리자에게 요청하라는 안내를
 * 보이고 제출할 수 없다. `targets`는 툴바에서 연 순간 캡처한 소유 노트북 목록이다.
 * 제출하면 항목·그룹·권한을 그 시점 값으로 고정하고(PR-01), 실행 중에는 닫히지 않는다(S-6 — Esc·바깥
 * 클릭 무시, 닫기 버튼 숨김).
 */
export function BulkShareDialog({
  targets,
  onClose,
  onFinished,
}: {
  targets: readonly BulkTarget[] | null;
  onClose: () => void;
  onFinished: (report: BulkRunReport) => void;
}) {
  const bulk = useBulkShare();
  const open = targets !== null;
  const groups = useMyGroups(open);
  const [groupId, setGroupId] = useState<string | null>(null);
  const [permission, setPermission] = useState<SharePermission>("read");
  // 제출한 요청의 요약 — 진행·결과 화면은 입력값이 아니라 이 값을 보인다
  const [submitted, setSubmitted] = useState<string | null>(null);

  const total = targets?.length ?? 0;
  const running = bulk.phase === "running";
  const groupList = groups.data ?? [];
  const selected = groupList.find((g) => g.id === groupId) ?? null;
  // Base UI Select는 닫힌 상태의 값(<SelectValue>)을 `items`로 라벨화한다 — 없으면 원시값(그룹 id)이 보인다
  const groupItems = groupList.map((g) => ({ value: g.id, label: g.name }));
  const canSubmit = total > 0 && selected !== null;
  const { shown, rest } = previewTitles(targets ?? []);
  const summary = bulk.report ? summarizeResults(bulk.report.results) : null;

  function requestClose() {
    if (running) return;
    bulk.reset();
    setGroupId(null);
    setPermission("read");
    setSubmitted(null);
    onClose();
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!targets || !selected || !canSubmit || bulk.phase !== "idle") return;
    setSubmitted(requestSummary(targets.length, selected.name, permission));
    const report = await bulk.start({ targets, groupId: selected.id, permission });
    if (!report) return;
    onFinished(report);
    const s = summarizeResults(report.results);
    const text = formatShareSummary(s);
    if (s.succeeded + s.skipped === s.total) toast.success(text);
    else toast.error(text);
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) requestClose();
      }}
    >
      {/* X(「닫기」)는 입력 단계에만 — 실행 중에는 닫을 수 없고, 결과 단계는 푸터의 「닫기」 하나만 둔다(같은 이름 버튼 둘 방지, E-P5I-1) */}
      <DialogContent
        className="sm:max-w-lg"
        showCloseButton={bulk.phase === "idle"}
        data-testid="bulk-share-dialog"
      >
        <DialogHeader>
          <DialogTitle>
            {bulk.phase === "idle"
              ? `노트북 ${total}개 공유`
              : running
                ? "노트북 공유 중"
                : "노트북 공유 결과"}
          </DialogTitle>
          <DialogDescription>
            {bulk.phase === "idle"
              ? BULK_SHARE_DESCRIPTION
              : running
                ? "공유하는 중입니다. 끝날 때까지 이 창을 닫을 수 없습니다."
                : "공유가 끝났습니다. 실패하거나 실행하지 않은 노트북은 선택된 채로 남습니다."}
          </DialogDescription>
        </DialogHeader>

        {bulk.phase === "idle" ? (
          <form onSubmit={handleSubmit} className="flex flex-col gap-4" aria-label="일괄 공유">
            <div className="grid gap-1 text-sm">
              <ul aria-label="공유할 노트북" className="list-disc pl-5" data-testid="bulk-share-titles">
                {shown.map((title, i) => (
                  <li key={`${i}-${title}`} className="truncate">
                    {title}
                  </li>
                ))}
              </ul>
              {rest > 0 && <p className="text-muted-foreground">외 {rest}개</p>}
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="bulk-share-group">그룹</Label>
              {groups.isPending ? (
                <p role="status" className="text-sm text-muted-foreground">
                  그룹 목록을 불러오는 중…
                </p>
              ) : groups.isError ? (
                <div role="alert" className="flex items-center gap-2 text-sm" data-testid="bulk-share-groups-error">
                  <span className="text-destructive-text">그룹 목록을 불러오지 못했습니다</span>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="coarse:min-h-11"
                    onClick={() => void groups.refetch()}
                    disabled={groups.isFetching}
                  >
                    다시 시도
                  </Button>
                </div>
              ) : groupList.length === 0 ? (
                <p className="rounded-lg bg-muted px-3 py-2 text-sm" data-testid="bulk-share-no-groups">
                  {BULK_MESSAGES.noGroups}
                </p>
              ) : (
                <Select
                  value={groupId}
                  onValueChange={(v) => setGroupId((v as string | null) ?? null)}
                  items={groupItems}
                >
                  <SelectTrigger
                    id="bulk-share-group"
                    className="w-full coarse:min-h-11"
                    data-testid="bulk-share-group-select"
                  >
                    <SelectValue placeholder="그룹 선택" />
                  </SelectTrigger>
                  <SelectContent>
                    {groupList.map((g) => (
                      <SelectItem key={g.id} value={g.id}>
                        {g.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="bulk-share-permission">권한</Label>
              <Select
                value={permission}
                onValueChange={(v) => v && setPermission(v as SharePermission)}
                items={SHARE_PERMISSION_OPTIONS}
              >
                <SelectTrigger
                  id="bulk-share-permission"
                  className="w-full coarse:min-h-11"
                  data-testid="bulk-share-permission"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SHARE_PERMISSION_OPTIONS.map((o) => (
                    <SelectItem key={o.value} value={o.value}>
                      {o.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {selected && (
              <p className="rounded-lg bg-muted px-3 py-2 text-sm font-medium" data-testid="bulk-share-summary">
                {requestSummary(total, selected.name, permission)}
              </p>
            )}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={requestClose}>
                취소
              </Button>
              <Button type="submit" disabled={!canSubmit} data-testid="bulk-share-submit">
                노트북 {total}개 공유
              </Button>
            </DialogFooter>
          </form>
        ) : (
          submitted && (
            <p className="rounded-lg bg-muted px-3 py-2 text-sm font-medium" data-testid="bulk-share-summary">
              {submitted}
            </p>
          )
        )}

        {running && (
          <div className="grid gap-2">
            <p role="status" aria-live="polite" className="text-sm font-medium" data-testid="bulk-progress">
              {bulk.progress.done} / {bulk.progress.total}
            </p>
            <progress
              className="h-2 w-full"
              aria-label="공유 진행"
              max={Math.max(bulk.progress.total, 1)}
              value={bulk.progress.done}
            />
          </div>
        )}

        {bulk.phase === "done" && bulk.report && summary && (
          <BulkResultList
            results={bulk.report.results}
            summary={formatShareSummary(summary)}
            notice={bulk.report.stopReason ? STOP_NOTICES[bulk.report.stopReason] : null}
          />
        )}

        {bulk.phase !== "idle" && (
          <DialogFooter>
            {running ? (
              <Button variant="outline" onClick={bulk.stop} disabled={bulk.stopping}>
                {bulk.stopping ? "중지하는 중…" : "중지"}
              </Button>
            ) : (
              <Button onClick={requestClose}>닫기</Button>
            )}
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  );
}
