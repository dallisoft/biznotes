"use client";

import { BadgeCheck, Inbox } from "lucide-react";
import { Fragment, useCallback, useId, useState } from "react";
import { AdminTableFrame } from "@/components/admin/admin-table-frame";
import { PageHeader } from "@/components/shell/page-header";
import { ReviewVersionPanel } from "@/components/skills/review-version-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Label } from "@/components/ui/label";
import { LoadingState } from "@/components/ui/loading-state";
import { RowActions } from "@/components/ui/row-actions";
import { StatusBadge } from "@/components/ui/status-badge";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TablePagination } from "@/components/ui/table-pagination";
import { Textarea } from "@/components/ui/textarea";
import { usePagination } from "@/hooks/use-pagination";
import {
  useAdminReviewRequests,
  useAdminSetPublishedVersion,
  useAdminSkills,
  useAdminUpdateSkill,
  useApproveReviewRequest,
  useRejectReviewRequest,
} from "@/hooks/use-skills";
import { activeLabel, SKILL_STATUS_TONE, skillStatusLabel, type SkillDisplayStatus } from "@/lib/labels";
import { notifyError, notifySuccess } from "@/lib/notify";
import {
  REVIEW_KIND_LABEL,
  VISIBILITY_LABEL,
  type AdminSkill,
  type SkillReviewRequest,
} from "@/lib/skill-types";
import { useNarrowViewport } from "@/hooks/use-narrow-viewport";

/**
 * 관리 화면의 공개 상태(§6.3.2 스킬 상태) — 공개 버전(`published_version_id`)이 있으면 「공개 중」, 없고 관리자 철회 보류(`exposure_hold`)가
 * 서 있으면 「공개 철회됨」, 둘 다 아니면 「초안」. 검토 대기는 별도 배지(건수)로 함께 보인다.
 */
function publishStatus(skill: AdminSkill): SkillDisplayStatus {
  if (skill.published_version_id) return "published";
  if (skill.exposure_hold) return "withdrawn";
  return "draft";
}


/** 거절 확인 — 사유는 요청자에게 남는다(`note`). 빈 사유도 허용하지만 확인은 거친다(P0-17). */
function RejectDialog({
  request,
  pending,
  onClose,
  onReject,
}: {
  request: SkillReviewRequest | null;
  pending: boolean;
  onClose: () => void;
  onReject: (note: string) => void;
}) {
  const [note, setNote] = useState("");
  const noteId = useId();
  return (
    <Dialog
      open={request !== null}
      onOpenChange={(open) => {
        if (!open) {
          setNote("");
          onClose();
        }
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>검토 요청을 거절할까요?</DialogTitle>
          <DialogDescription>
            {request?.skill_name ?? "—"} v{request?.version ?? "?"} —{" "}
            {request ? (REVIEW_KIND_LABEL[request.kind] ?? request.kind) : ""}. 거절 사유는 요청자에게
            표시됩니다.
          </DialogDescription>
        </DialogHeader>
        <Label htmlFor={noteId}>거절 사유(선택)</Label>
        <Textarea
          id={noteId}
          rows={3}
          value={note}
          placeholder="거절 사유(요청자에게 표시됩니다)"
          onChange={(e) => setNote(e.target.value)}
          data-testid="reject-note"
        />
        <DialogFooter>
          <Button variant="outline" className="coarse:min-h-11" onClick={onClose}>
            취소
          </Button>
          <Button
            variant="destructive"
            className="coarse:min-h-11"
            disabled={pending}
            onClick={() => {
              onReject(note.trim());
              setNote("");
            }}
          >
            거절
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** 승인 잠금 사유 — 필수 파일(지시문·스크립트 원문)을 연 뒤에만 승인할 수 있다(P0-03). 넓은 화면·좁은 화면 메뉴 공통. */
const APPROVE_LOCKED_REASON = "내용을 확인한 뒤 승인할 수 있습니다";

function ReviewQueue() {
  const reviews = useAdminReviewRequests();
  const approve = useApproveReviewRequest();
  const reject = useRejectReviewRequest();
  // 검토 상세를 연 요청과, 필수 파일을 모두 확인한 요청 — 승인은 확인한 요청만(P0-03)
  const [expanded, setExpanded] = useState<string | null>(null);
  const [reviewed, setReviewed] = useState<Record<string, boolean>>({});
  const [rejecting, setRejecting] = useState<SkillReviewRequest | null>(null);
  // 좁은 화면(< 768)에서는 행 동작을 ⋯ 하나로 접는다(plan §6.7 — P5 재리뷰 P5-R01). 같은 상태·다이얼로그·단계를 쓴다.
  const narrow = useNarrowViewport();
  const markReviewed = useCallback(
    (requestId: string, ok: boolean) =>
      setReviewed((prev) => (prev[requestId] === ok ? prev : { ...prev, [requestId]: ok })),
    [],
  );

  const approveRequest = (id: string) =>
    approve.mutate(id, {
      onSuccess: () => notifySuccess("검토 요청을 승인했습니다"),
      onError: (err) => notifyError("검토 요청을 승인", err),
    });

  let body: React.ReactNode;
  if (reviews.isPending) {
    body = <LoadingState variant="table" count={2} />;
  } else if (reviews.isError) {
    body = (
      <ErrorState
        title="검토 요청을 불러오지 못했습니다"
        onRetry={() => reviews.refetch()}
        retrying={reviews.isFetching}
      />
    );
  } else if (reviews.data.length === 0) {
    body = (
      <EmptyState
        icon={<Inbox />}
        title="대기 중인 검토 요청이 없습니다"
        description="스킬 소유자가 공개나 스크립트 실행 허가를 요청하면 여기에 보입니다."
      />
    );
  } else {
    body = (
      <AdminTableFrame>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>스킬</TableHead>
              <TableHead>요청</TableHead>
              <TableHead className="text-right">버전</TableHead>
              <TableHead className="text-right">처리</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {reviews.data.map((r) => (
              <Fragment key={r.id}>
                <TableRow>
                  <TableCell>
                    <div className="font-medium">{r.skill_name ?? "—"}</div>
                    {r.note && <div className="text-xs text-muted-foreground">{r.note}</div>}
                  </TableCell>
                  <TableCell>
                    <Badge variant="secondary">{REVIEW_KIND_LABEL[r.kind] ?? r.kind}</Badge>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">v{r.version ?? "?"}</TableCell>
                  <TableCell>
                    {narrow ? (
                      <RowActions
                        rowLabel={`‘${r.skill_name ?? "스킬"}’ 검토 요청`}
                        items={[
                          {
                            label: expanded === r.id ? "내용 닫기" : "내용 보기",
                            onSelect: () => setExpanded((cur) => (cur === r.id ? null : r.id)),
                          },
                          {
                            label: "승인",
                            disabled: approve.isPending || !reviewed[r.id],
                            disabledReason: reviewed[r.id] ? undefined : APPROVE_LOCKED_REASON,
                            onSelect: () => approveRequest(r.id),
                          },
                          { label: "거절", disabled: reject.isPending, onSelect: () => setRejecting(r) },
                        ]}
                      />
                    ) : (
                      <div className="flex flex-col items-end gap-1">
                        <div className="flex flex-wrap justify-end gap-2">
                          <Button
                            size="sm"
                            variant="ghost"
                            className="coarse:min-h-11"
                            onClick={() => setExpanded((cur) => (cur === r.id ? null : r.id))}
                            aria-expanded={expanded === r.id}
                          >
                            {expanded === r.id ? "내용 닫기" : "내용 보기"}
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            className="text-destructive-text coarse:min-h-11"
                            disabled={reject.isPending}
                            onClick={() => setRejecting(r)}
                          >
                            거절
                          </Button>
                          <Button
                            size="sm"
                            className="coarse:min-h-11"
                            disabled={approve.isPending || !reviewed[r.id]}
                            onClick={() => approveRequest(r.id)}
                          >
                            승인
                          </Button>
                        </div>
                        {!reviewed[r.id] && (
                          <p className="text-xs text-muted-foreground">{APPROVE_LOCKED_REASON}</p>
                        )}
                      </div>
                    )}
                  </TableCell>
                </TableRow>
                {expanded === r.id && (
                  <TableRow>
                    <TableCell colSpan={4}>
                      <ReviewVersionPanel
                        versionId={r.version_id}
                        kind={r.kind}
                        onReviewed={(ok) => markReviewed(r.id, ok)}
                      />
                    </TableCell>
                  </TableRow>
                )}
              </Fragment>
            ))}
          </TableBody>
        </Table>
      </AdminTableFrame>
    );
  }

  return (
    <section className="mb-8" aria-labelledby="admin-skills-reviews">
      <h2 id="admin-skills-reviews" className="mb-3 text-lg font-semibold">
        검토 요청
      </h2>
      {body}
      <RejectDialog
        request={rejecting}
        pending={reject.isPending}
        onClose={() => setRejecting(null)}
        onReject={(note) => {
          if (!rejecting) return;
          reject.mutate(
            { requestId: rejecting.id, note: note || undefined },
            {
              onSuccess: () => notifySuccess("검토 요청을 거절했습니다"),
              onError: (err) => notifyError("검토 요청을 거절", err),
            },
          );
          setRejecting(null);
        }}
      />
    </section>
  );
}

function SkillTable() {
  const skills = useAdminSkills();
  const update = useAdminUpdateSkill();
  const setPublished = useAdminSetPublishedVersion();
  const pg = usePagination(skills.data ?? []);

  let body: React.ReactNode;
  if (skills.isPending) {
    body = <LoadingState variant="table" />;
  } else if (skills.isError) {
    body = (
      <ErrorState
        title="스킬 목록을 불러오지 못했습니다"
        onRetry={() => skills.refetch()}
        retrying={skills.isFetching}
      />
    );
  } else if (pg.total === 0) {
    body = (
      <EmptyState
        icon={<BadgeCheck />}
        title="아직 스킬이 없습니다"
        description="사용자가 스킬을 만들거나 가져오면 여기에서 공개와 활성 상태를 관리합니다."
      />
    );
  } else {
    body = (
      <>
        <AdminTableFrame>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>이름</TableHead>
                <TableHead>공개 범위</TableHead>
                <TableHead>공개 상태</TableHead>
                <TableHead className="text-right">실행</TableHead>
                <TableHead className="text-right">복제</TableHead>
                <TableHead>활성</TableHead>
                <TableHead className="w-16 text-right">동작</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {pg.pageItems.map((s) => {
                const status = publishStatus(s);
                return (
                  <TableRow key={s.id}>
                    <TableCell>
                      <div className="font-medium">{s.name}</div>
                      <div className="font-mono text-xs text-muted-foreground">{s.slug}</div>
                    </TableCell>
                    <TableCell>
                      <Badge variant={s.visibility === "private" ? "outline" : "secondary"}>
                        {VISIBILITY_LABEL[s.visibility] ?? s.visibility}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        <StatusBadge tone={SKILL_STATUS_TONE[status]}>{skillStatusLabel(status)}</StatusBadge>
                        {s.pending_review_count > 0 && (
                          <StatusBadge tone={SKILL_STATUS_TONE.review_pending}>
                            {`${skillStatusLabel("review_pending")} ${s.pending_review_count}건`}
                          </StatusBadge>
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{s.run_count}</TableCell>
                    <TableCell className="text-right tabular-nums">{s.clone_count}</TableCell>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <Switch
                          checked={s.enabled}
                          aria-label={`${s.name} 활성`}
                          onCheckedChange={(enabled) =>
                            update.mutate(
                              { skillId: s.id, body: { enabled } },
                              {
                                onSuccess: () =>
                                  notifySuccess(
                                    `‘${s.name}’ 스킬을 ${enabled ? "활성화" : "비활성화"}했습니다`,
                                  ),
                                onError: (err) =>
                                  notifyError(
                                    `‘${s.name}’ 스킬을 ${enabled ? "활성화" : "비활성화"}`,
                                    err,
                                  ),
                              },
                            )
                          }
                        />
                        <span aria-hidden className="text-xs text-muted-foreground">
                          {activeLabel(s.enabled)}
                        </span>
                      </div>
                    </TableCell>
                    <TableCell>
                      <RowActions
                        rowLabel={s.name}
                        items={
                          s.published_version_id
                            ? [
                                {
                                  label: "공개 철회",
                                  destructive: true,
                                  disabled: setPublished.isPending,
                                  confirm: {
                                    title: `‘${s.name}’ 공개를 철회할까요?`,
                                    description:
                                      "사용자에게 더 이상 보이지 않고 보류가 설정됩니다. 다시 공개하려면 관리자 승인이 필요합니다.",
                                    actionLabel: "공개 철회",
                                  },
                                  onConfirm: () =>
                                    setPublished.mutate(
                                      { skillId: s.id, versionId: null },
                                      {
                                        onSuccess: () =>
                                          notifySuccess(`‘${s.name}’ 스킬 공개를 철회했습니다`, {
                                            description: "보류가 설정되어 다시 공개하려면 관리자 승인이 필요합니다",
                                          }),
                                        onError: (err) => notifyError("공개를 철회", err),
                                      },
                                    ),
                                },
                              ]
                            : []
                        }
                      />
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </AdminTableFrame>
        <TablePagination pagination={pg} />
      </>
    );
  }

  return (
    <section aria-labelledby="admin-skills-list">
      {/* 공개 철회 뒤 그 행의 ⋯가 사라지면 확인 다이얼로그가 이 제목으로 포커스를 옮긴다(§6.5 계약 ③) */}
      <h2 id="admin-skills-list" tabIndex={-1} data-focus-fallback="" className="mb-3 text-lg font-semibold">
        스킬 목록
      </h2>
      {body}
    </section>
  );
}

export default function AdminSkillsPage() {
  return (
    <div>
      <PageHeader
        breadcrumb={[{ label: "관리", href: "/admin" }, { label: "스킬 공개" }]}
        title="스킬 공개"
        description="공개 버전과 활성 상태를 관리합니다. 공개를 철회하면 보류가 설정되어 다시 공개하려면 관리자 승인이 필요하고, 즉시 차단하려면 비활성화합니다."
      />
      <ReviewQueue />
      <SkillTable />
    </div>
  );
}
