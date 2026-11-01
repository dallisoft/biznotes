"use client";

import { Copy, MonitorSmartphone } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useLayoutEffect, useRef, useState } from "react";
import { PageContainer } from "@/components/layout/page-container";
import { PageHeader } from "@/components/shell/page-header";
import { Button, buttonVariants } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { WorkflowEditor } from "@/components/workflow/workflow-editor";
import { useMediaQuery } from "@/hooks/use-media-query";
import { useDuplicateWorkflow, useWorkflow } from "@/hooks/use-workflows";
import { retryFocusStatus, useRetryFocusLanding } from "@/hooks/use-retry-focus-landing";
import { ApiError } from "@/lib/api-client";
import { focusIsLost } from "@/lib/focus";
import { notifyError, notifySuccess, successMessage } from "@/lib/notify";
import type { Workflow } from "@/lib/workflow-types";

/** 편집기를 여는 최소 폭 — 셸의 데스크톱 판정(`app-shell.tsx`)과 같은 768px(plan §6.7, HCI-14). */
const WORKFLOW_EDITOR_MIN_WIDTH_QUERY = "(min-width: 768px)";

/** 편집 권한이 없을 때(§8.3 — 판정 무변경): 「복제해서 내 워크플로우로 편집」과 목록 링크(AC-4.5 계열). */
function WorkflowEditForbidden({ workflowId }: { workflowId: string }) {
  const router = useRouter();
  const duplicate = useDuplicateWorkflow();
  return (
    <PageContainer width="default">
      <ErrorState
        variant="forbidden"
        title="이 워크플로우를 편집할 권한이 없습니다"
        description="복제하면 내 워크플로우로 편집할 수 있습니다."
        actions={
          <Button
            className="coarse:h-11"
            disabled={duplicate.isPending}
            onClick={() =>
              duplicate.mutate(workflowId, {
                onSuccess: (clone) => {
                  notifySuccess(successMessage("워크플로우", "복제"));
                  router.push(`/workflows/${clone.id}/edit`);
                },
                onError: (err) => notifyError("워크플로우를 복제", err),
              })
            }
          >
            <Copy aria-hidden />
            복제해서 내 워크플로우로 편집
          </Button>
        }
        back={{ href: "/workflows", label: "워크플로우 목록으로" }}
      />
    </PageContainer>
  );
}

/** 768px 미만 — 캔버스·팔레트·속성 패널을 쓸 수 없으므로 편집기 대신 안내를 보인다(HCI-14, AC-4.11). */
function NarrowScreenNotice({
  workflow,
  titleRef,
}: {
  workflow: Workflow;
  /** 편집 중에 좁아지면 포커스를 받는 안내 제목(HCI-P4-6 ①) */
  titleRef?: React.Ref<HTMLParagraphElement>;
}) {
  return (
    <div className="h-full overflow-y-auto">
      <PageContainer width="narrow">
        <PageHeader
          breadcrumb={[{ label: "워크플로우", href: "/workflows" }, { label: workflow.name }]}
          title={workflow.name}
        />
        <div
          data-testid="workflow-edit-narrow-notice"
          className="flex flex-col items-center gap-3 rounded-xl border border-dashed px-6 py-12 text-center"
        >
          <div
            aria-hidden
            className="flex size-12 items-center justify-center rounded-xl bg-muted text-muted-foreground [&_svg]:size-6"
          >
            <MonitorSmartphone />
          </div>
          <p
            ref={titleRef}
            tabIndex={-1}
            className="rounded-sm text-base font-semibold outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            워크플로우 편집은 화면이 넓은 기기에서 할 수 있습니다
          </p>
          <p className="text-sm text-muted-foreground">
            노드를 배치하는 편집기는 화면 폭 768px 이상에서 열립니다. 태블릿 가로 화면이나 컴퓨터에서 다시 열어 주세요.
          </p>
          <Link href="/workflows" className={buttonVariants({ className: "coarse:h-11" })}>
            워크플로우 목록
          </Link>
        </div>
      </PageContainer>
    </div>
  );
}

export default function WorkflowEditPage() {
  const { id } = useParams<{ id: string }>();
  const workflow = useWorkflow(id);
  // 서버·첫 렌더는 데스크톱으로 가정하고(셸과 같은 기본값) 마운트 뒤 실제 폭으로 보정한다.
  const wide = useMediaQuery(WORKFLOW_EDITOR_MIN_WIDTH_QUERY, true);
  // 편집기를 한 번 연 뒤에는 폭이 768 아래로 내려가도 편집기를 언마운트하지 않고 숨긴다 — 이름·노드·엣지·파라미터의
  // 미저장 편집은 편집기 안 상태라, 창을 좁히거나 기기를 돌렸다가 돌아와도 그대로 남아야 한다(P4 리뷰 P4-01).
  // 처음부터 좁으면 편집기를 만들지 않는다(AC-4.11). 안내 경계 768은 그대로다(HCI-14).
  const [editorOpened, setEditorOpened] = useState(false);
  // 「다시 시도」는 오류 → 스켈레톤 → 결과로 화면 전체가 두 번 바뀐다 — 포커스가 body로 떨어지지 않게 로딩 동안은 셸 본문에,
  // 결과가 오면 본문 첫 포커스 가능 요소(브레드크럼·뒤로 링크)에 둔다(P2 회고 HCI-P2-2).
  const pageFocus = useRetryFocusLanding(retryFocusStatus(workflow), "page");
  // 편집 중에 좁아지면 숨겨지는 편집기 안의 포커스가 body로 떨어진다 — 안내 제목으로 옮기고, 다시 넓어져 안내가
  // 사라지면 편집기 안 원래 자리로 돌려준다(P4 회고 HCI-P4-6 ①). 편집기 밖의 포커스는 건드리지 않는다.
  const editorRef = useRef<HTMLDivElement>(null);
  const noticeTitleRef = useRef<HTMLParagraphElement>(null);
  const lastEditorFocus = useRef<HTMLElement | null>(null);
  const restoreOnWide = useRef<HTMLElement | null>(null);
  const prevWide = useRef(wide);
  useLayoutEffect(() => {
    const was = prevWide.current;
    prevWide.current = wide;
    if (!editorOpened || was === wide) return;
    if (!wide) {
      const active = document.activeElement;
      const inEditor = active instanceof HTMLElement && editorRef.current?.contains(active) === true;
      if (!inEditor && !focusIsLost()) return;
      restoreOnWide.current = inEditor ? active : lastEditorFocus.current;
      noticeTitleRef.current?.focus();
      return;
    }
    const back = restoreOnWide.current;
    restoreOnWide.current = null;
    if (back?.isConnected && focusIsLost()) back.focus();
  }, [wide, editorOpened]);

  if (workflow.isPending) {
    return (
      <PageContainer width="default">
        <Skeleton className="h-96" />
      </PageContainer>
    );
  }

  if (workflow.isError || !workflow.data) {
    const notFound =
      workflow.error instanceof ApiError &&
      (workflow.error.status === 404 || workflow.error.status === 403);
    return (
      <PageContainer width="default">
        <ErrorState
          title={notFound ? "워크플로우를 찾을 수 없습니다" : "워크플로우를 불러오지 못했습니다"}
          description={notFound ? "삭제되었거나 볼 수 없는 워크플로우입니다." : undefined}
          onRetry={
            notFound
              ? undefined
              : () => {
                  pageFocus.markRetry();
                  void workflow.refetch();
                }
          }
          retrying={workflow.isFetching}
          back={{ href: "/workflows", label: "워크플로우 목록으로" }}
        />
      </PageContainer>
    );
  }

  if (!workflow.data.can_edit) {
    return <WorkflowEditForbidden workflowId={workflow.data.id} />;
  }

  if (wide && !editorOpened) {
    // 렌더 중 상태 보정(React 권장 패턴) — 편집기를 처음 보이는 이 렌더에서 「열었음」으로 고정한다
    setEditorOpened(true);
  }
  if (!wide && !editorOpened) {
    return <NarrowScreenNotice workflow={workflow.data} />;
  }

  return (
    <>
      {!wide && <NarrowScreenNotice workflow={workflow.data} titleRef={noticeTitleRef} />}
      {/* 좁아지면 `hidden`(display:none)으로 숨긴다 — 탭 순서·접근성 트리에서 빠지고 상태는 남는다 */}
      <PageContainer
        ref={editorRef}
        width="bleed"
        hidden={!wide}
        data-testid="workflow-editor-container"
        onFocus={(event) => {
          lastEditorFocus.current = event.target;
        }}
      >
        <WorkflowEditor key={workflow.data.id} workflow={workflow.data} />
      </PageContainer>
    </>
  );
}
