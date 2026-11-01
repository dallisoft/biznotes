"use client";

import { useQuery } from "@tanstack/react-query";
import { Copy, Download, FileText, MessageSquarePlus, Pencil, TerminalSquare } from "lucide-react";
import { useParams, useRouter } from "next/navigation";
import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/shell/page-header";
import { Button, buttonVariants } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge, type StatusTone } from "@/components/ui/status-badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  encodeSkillFilePath,
  fetchSkillFile,
  skillExportUrl,
  useCloneSkill,
  useRequestSkillReview,
  useSkill,
  useSkillCatalog,
  useSkillReviewRequests,
  useSkillVersions,
} from "@/hooks/use-skills";
import { retryFocusStatus, useRetryFocusLanding } from "@/hooks/use-retry-focus-landing";
import { api, ApiError } from "@/lib/api-client";
import { SKILL_STATUS_TONE, skillStatusLabel, skillVisibilityLabel } from "@/lib/labels";
import { notifyError, notifySuccess } from "@/lib/notify";
import { hasRunnableScripts } from "@/lib/skill-catalog";

async function fetchVersionFile(versionId: string, path: string): Promise<string> {
  return api.getText(`/api/v1/skills/versions/${versionId}/files/${encodeSkillFilePath(path)}`);
}

type ReviewState = "pending" | "approved" | "rejected";

const REVIEW_TONE: Record<ReviewState, StatusTone> = {
  pending: "warning",
  approved: "success",
  rejected: "danger",
};

const CATALOG_REQUEST_LABEL: Record<ReviewState, string> = {
  pending: "등재 검토 대기",
  approved: "채팅 카탈로그 등재됨",
  rejected: "등재 거절됨 — 새 버전으로 다시 요청",
};

const SCRIPTS_REQUEST_LABEL: Record<ReviewState, string> = {
  pending: "스크립트 검토 대기",
  approved: "스크립트 실행 허가됨",
  rejected: "스크립트 거절됨 — 새 버전으로 다시 요청",
};

function isReviewState(status: string): status is ReviewState {
  return status === "pending" || status === "approved" || status === "rejected";
}

export default function SkillDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const skill = useSkill(id);
  const versions = useSkillVersions(id);
  const clone = useCloneSkill();
  const reviews = useSkillReviewRequests(id, !!skill.data?.can_edit);
  const requestReview = useRequestSkillReview(id);
  const catalog = useSkillCatalog();

  // SKILL.md 미리보기 — owner는 draft, 그 외는 **공개 version**(biznotes-core D8).
  // 비소유자에게 current_version_id는 노출 version의 별칭이므로 published를 우선 읽는다.
  const viewVersionId = skill.data?.published_version_id ?? skill.data?.current_version_id ?? null;
  const preview = useQuery<string, ApiError>({
    queryKey: ["skill-preview", id, skill.data?.can_edit, viewVersionId],
    queryFn: async () =>
      skill.data?.can_edit
        ? ((await fetchSkillFile(id, "SKILL.md")) ?? "")
        : fetchVersionFile(viewVersionId!, "SKILL.md"),
    enabled: !!skill.data && (skill.data.can_edit || !!viewVersionId),
  });
  // 「다시 시도」는 오류 → 스켈레톤 → 결과로 화면 전체가 두 번 바뀐다 — 포커스가 body로 떨어지지 않게 로딩 동안은 셸 본문에,
  // 결과가 오면 본문 첫 포커스 가능 요소(브레드크럼·뒤로 링크)에 둔다(P2 회고 HCI-P2-2).
  const pageFocus = useRetryFocusLanding(retryFocusStatus(skill), "page");

  if (skill.isPending) {
    return (
      <PageContainer width="default">
        <Skeleton className="h-40" />
      </PageContainer>
    );
  }
  if (skill.isError || !skill.data) {
    // 없는 스킬·볼 수 없는 스킬(서버 404·403)은 다시 시도해도 같다 — 목록으로만 안내한다.
    const notFound =
      skill.error instanceof ApiError && (skill.error.status === 404 || skill.error.status === 403);
    return (
      <PageContainer width="default">
        <ErrorState
          title={notFound ? "스킬을 찾을 수 없습니다" : "스킬을 불러오지 못했습니다"}
          description={notFound ? "삭제되었거나 볼 수 없는 스킬입니다." : undefined}
          onRetry={
            notFound
              ? undefined
              : () => {
                  pageFocus.markRetry();
                  void skill.refetch();
                }
          }
          retrying={skill.isFetching}
          back={{ href: "/skills", label: "스킬 목록으로" }}
        />
      </PageContainer>
    );
  }

  const s = skill.data;
  const fileTree = versions.data?.[0]?.file_tree ?? [];
  // 직접 작성해 org로 공개한 스킬은 다른 사용자가 목록에서 실행할 수 있지만, 그들의 채팅
  // 카탈로그에는 관리자가 그 version을 승인해야 들어간다(biznotes-core S-HCI-12).
  // 같은 version의 거절은 다시 요청할 수 없다(새 version이 필요하다) — 버튼 대신 상태를 보인다.
  const catalogRequest = (reviews.data ?? []).find(
    (r) => r.kind === "expose_org" && r.version_id === s.published_version_id && isReviewState(r.status),
  );
  const publishedVersion = (versions.data ?? []).find((v) => v.id === s.published_version_id);
  // 스크립트 실행 허가는 version 단위다 — 소유자가 직접 실행하는 현재 version에 요청한다.
  // 승인이 있어야 직접 실행에서 run_skill_script가 보인다(채팅에는 없다).
  const currentVersion = (versions.data ?? []).find((v) => v.id === s.current_version_id);
  const scriptsRequest = (reviews.data ?? []).find(
    (r) => r.kind === "scripts" && r.version_id === s.current_version_id && isReviewState(r.status),
  );
  const canRequestScripts =
    s.can_edit &&
    !!catalog.data?.scripts_enabled &&
    currentVersion?.runtime_profile === "standard_agent_v1" &&
    hasRunnableScripts((currentVersion?.file_tree ?? []).map((f) => f.path));
  // 카탈로그에는 표준 런타임 version만 들어간다 — 런타임이 꺼져 있거나 legacy면 요청할 이유가 없다.
  const canRequestCatalog =
    s.can_edit &&
    s.visibility === "org" &&
    s.origin === "authored" &&
    !!catalog.data?.enabled &&
    publishedVersion?.runtime_profile === "standard_agent_v1";

  function request(kind: "expose_org" | "scripts", versionId: string, done: string) {
    requestReview.mutate(
      { versionId, kind },
      {
        onSuccess: () => notifySuccess(done),
        onError: (err) => notifyError("검토를 요청", err),
      },
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <PageContainer width="default">
        <PageHeader
          breadcrumb={[{ label: "스킬", href: "/skills" }, { label: s.name }]}
          title={s.name}
          description={s.description || undefined}
          actions={
            <>
              <Button
                variant="outline"
                className="coarse:h-11"
                disabled={clone.isPending}
                onClick={() =>
                  clone.mutate(s.id, {
                    onSuccess: (r) => {
                      notifySuccess("스킬을 복제했습니다");
                      router.push(`/skills/${r.skill_id}/edit`);
                    },
                    onError: (err) => notifyError("스킬을 복제", err),
                  })
                }
              >
                <Copy aria-hidden /> 복제
              </Button>
              {canRequestCatalog && !catalogRequest && (
                <Button
                  variant="outline"
                  className="coarse:h-11"
                  disabled={requestReview.isPending}
                  onClick={() =>
                    request("expose_org", s.published_version_id!, "채팅 카탈로그 등재를 요청했습니다")
                  }
                >
                  <MessageSquarePlus aria-hidden /> 채팅 카탈로그 등재 요청
                </Button>
              )}
              {canRequestScripts && !scriptsRequest && (
                <Button
                  variant="outline"
                  className="coarse:h-11"
                  disabled={requestReview.isPending}
                  onClick={() =>
                    request("scripts", s.current_version_id!, "스크립트 실행 허가를 요청했습니다")
                  }
                >
                  <TerminalSquare aria-hidden /> 스크립트 실행 허가 요청
                </Button>
              )}
              {s.can_edit && (
                <Button className="coarse:h-11" onClick={() => router.push(`/skills/${s.id}/edit`)}>
                  <Pencil aria-hidden /> 편집
                </Button>
              )}
            </>
          }
        />

        <div className="mb-6 flex flex-wrap items-center gap-2" data-testid="skill-meta">
          <StatusBadge>{skillVisibilityLabel(s.visibility)}</StatusBadge>
          {s.published_version_id ? (
            <StatusBadge tone={SKILL_STATUS_TONE.published}>{skillStatusLabel("published")}</StatusBadge>
          ) : (
            s.can_edit && <StatusBadge tone={SKILL_STATUS_TONE.draft}>{skillStatusLabel("draft")}</StatusBadge>
          )}
          {canRequestCatalog && catalogRequest && isReviewState(catalogRequest.status) && (
            <StatusBadge tone={REVIEW_TONE[catalogRequest.status]}>
              {CATALOG_REQUEST_LABEL[catalogRequest.status]}
            </StatusBadge>
          )}
          {canRequestScripts && scriptsRequest && isReviewState(scriptsRequest.status) && (
            <StatusBadge tone={REVIEW_TONE[scriptsRequest.status]}>
              {SCRIPTS_REQUEST_LABEL[scriptsRequest.status]}
            </StatusBadge>
          )}
          <span className="font-mono text-xs text-muted-foreground">{s.slug}</span>
        </div>

        <div className="grid gap-6 lg:grid-cols-3">
          <section className="min-w-0 lg:col-span-2">
            <h2 className="mb-2 text-sm font-semibold text-muted-foreground">SKILL.md</h2>
            {preview.isPending ? (
              <Skeleton className="h-64" />
            ) : preview.isError ? (
              <p className="text-sm text-muted-foreground">미리보기를 불러오지 못했습니다.</p>
            ) : (
              <pre className="max-h-[28rem] overflow-auto rounded-lg bg-muted p-4 text-xs whitespace-pre-wrap">
                {preview.data}
              </pre>
            )}
          </section>

          <aside className="flex min-w-0 flex-col gap-6">
            {s.can_run && (
              <p className="rounded-lg bg-muted/50 p-3 text-sm text-muted-foreground" data-testid="skill-run-hint">
                실행은 노트북 오른쪽 <strong className="text-foreground">에이전트</strong> 탭 →{" "}
                <strong className="text-foreground">스킬</strong>에서 합니다.
              </p>
            )}

            <section>
              <h2 className="mb-2 text-sm font-semibold text-muted-foreground">파일</h2>
              {fileTree.length === 0 ? (
                <p className="text-xs text-muted-foreground">
                  {viewVersionId ? "파일 없음" : "아직 동결한 버전이 없습니다"}
                </p>
              ) : (
                <ul className="flex flex-col gap-1 text-xs">
                  {fileTree.map((f) => (
                    <li key={f.path} className="flex items-center gap-1.5">
                      <FileText aria-hidden className="size-3 shrink-0 text-muted-foreground" />
                      <span className="truncate font-mono">{f.path}</span>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section>
              <h2 className="mb-2 text-sm font-semibold text-muted-foreground">버전</h2>
              {versions.isPending ? (
                <Skeleton className="h-16" />
              ) : (versions.data ?? []).length === 0 ? (
                <p className="text-xs text-muted-foreground">동결한 버전 없음</p>
              ) : (
                <ul className="flex flex-col gap-1 text-xs">
                  {(versions.data ?? []).map((v) => (
                    <li key={v.id} className="flex items-center justify-between gap-2">
                      <span>v{v.version}</span>
                      <span className="flex items-center gap-1">
                        {v.id === s.published_version_id && (
                          <StatusBadge tone={SKILL_STATUS_TONE.published}>{skillStatusLabel("published")}</StatusBadge>
                        )}
                        <StatusBadge>{v.validation_profile === "standard_v1" ? "표준" : "비표준"}</StatusBadge>
                        <Tooltip>
                          <TooltipTrigger
                            render={
                              <a
                                href={skillExportUrl(v.id)}
                                aria-label={`v${v.version} 내보내기`}
                                className={buttonVariants({
                                  variant: "ghost",
                                  size: "icon-sm",
                                  className: "text-muted-foreground coarse:size-11",
                                })}
                              />
                            }
                          >
                            <Download aria-hidden />
                          </TooltipTrigger>
                          <TooltipContent>v{v.version} ZIP으로 내보내기</TooltipContent>
                        </Tooltip>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </aside>
        </div>
      </PageContainer>
    </div>
  );
}
