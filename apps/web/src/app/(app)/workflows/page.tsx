"use client";

import { Workflow as WorkflowIcon } from "lucide-react";
import { useState } from "react";
import { PageContainer } from "@/components/layout/page-container";
import { Toolbar, ToolbarSearch } from "@/components/layout/toolbar";
import { PageHeader } from "@/components/shell/page-header";
import { EmptyState, FilteredEmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { LoadingState } from "@/components/ui/loading-state";
import { CreateWorkflowDialog } from "@/components/workflow/create-workflow-dialog";
import { LockedWorkflowDialog } from "@/components/workflow/locked-workflow-dialog";
import { filterWorkflows, WorkflowCard } from "@/components/workflow/workflow-card";
import { useWorkflows } from "@/hooks/use-workflows";
import type { Workflow } from "@/lib/workflow-types";

export default function WorkflowsPage() {
  const workflows = useWorkflows();
  const [q, setQ] = useState("");
  const [locked, setLocked] = useState<Workflow | null>(null);

  const all = workflows.data ?? [];
  const list = filterWorkflows(all, q);

  return (
    <div className="h-full overflow-y-auto">
      <PageContainer width="wide">
        <PageHeader
          title="워크플로우"
          description="노드를 연결해 나만의 에이전트 워크플로우를 만들고 실행합니다"
          actions={<CreateWorkflowDialog />}
        />

        <Toolbar>
          <ToolbarSearch
            label="워크플로우 검색"
            placeholder="이름·설명으로 검색"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </Toolbar>

        {workflows.isPending ? (
          <LoadingState variant="cards" label="워크플로우 목록을 불러오는 중" />
        ) : workflows.isError ? (
          <ErrorState
            title="워크플로우 목록을 불러오지 못했습니다"
            onRetry={() => void workflows.refetch()}
            retrying={workflows.isFetching}
          />
        ) : all.length === 0 ? (
          <EmptyState
            icon={<WorkflowIcon />}
            title="아직 워크플로우가 없습니다"
            description="「새 워크플로우」로 노드를 연결해 첫 워크플로우를 만드세요."
          />
        ) : list.length === 0 ? (
          <FilteredEmptyState onClearFilters={() => setQ("")} />
        ) : (
          <ul
            data-testid="workflow-grid"
            className="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(260px,1fr))]"
          >
            {list.map((wf) => (
              <li key={wf.id}>
                <WorkflowCard wf={wf} onLocked={setLocked} />
              </li>
            ))}
          </ul>
        )}
      </PageContainer>
      <LockedWorkflowDialog wf={locked} onClose={() => setLocked(null)} />
    </div>
  );
}
