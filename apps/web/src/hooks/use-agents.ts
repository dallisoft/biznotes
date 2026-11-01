"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api-client";
import { entryRunPollInterval } from "@/lib/chat-entry";
import type {
  Agent,
  AgentKind,
  AgentRun,
  AgentRunApproval,
  ExecutionLog,
  HiddenRuns,
} from "@/lib/agent-types";

export function useAgents(kind: AgentKind) {
  return useQuery<Agent[], ApiError>({
    queryKey: ["agents", kind],
    queryFn: () => api.get<Agent[]>(`/api/v1/agents?kind=${kind}`),
    staleTime: 30_000,
  });
}

// awaiting_approval 누락 시(CDX-D8) 승인 대기 중인 run의 폴링이 멈춰
// 승인 카드가 실시간으로 갱신되지 않는다.
const ACTIVE_RUN = new Set(["queued", "running", "awaiting_approval"]);

export function useAgentRuns(notebookId: string) {
  return useQuery<AgentRun[], ApiError>({
    queryKey: ["agent-runs", notebookId],
    queryFn: () => api.get<AgentRun[]>(`/api/v1/notebooks/${notebookId}/agent-runs`),
    refetchInterval: (query) =>
      query.state.data?.some((r) => ACTIVE_RUN.has(r.status)) ? 2000 : false,
  });
}

/**
 * run 하나 — 채팅 진입 run 카드(#66)가 쓴다. 끝나지 않은 동안 2초마다 다시 읽고, 끝나면 멈춘다.
 *
 * `knownStatus`는 호출측이 이미 아는 상태다(메시지의 `entry_run.status`). 첫 조회가 일시 오류로 실패해도
 * 그 상태가 끝나지 않았으면 계속 다시 본다(`entryRunPollInterval`). 채팅 run은 실행자와 admin만 읽을 수
 * 있다(그 밖은 404). 카드는 자기 대화의 run만 연다.
 */
export function useAgentRun(runId: string | null, knownStatus?: string) {
  return useQuery<AgentRun, ApiError>({
    queryKey: ["agent-run", runId],
    queryFn: () => api.get<AgentRun>(`/api/v1/agent-runs/${runId}`),
    enabled: !!runId,
    refetchInterval: (query) =>
      entryRunPollInterval(query.state.data?.status, knownStatus, query.state.error?.status),
  });
}

export function useCreateRun(notebookId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      agent_id: string;
      params: Record<string, unknown>;
      source_ids?: string[];
      /** run 단위 요청 모델(#216). 넣지 않으면 기본 모델 + 폴백 체인이다. */
      model_id?: string;
    }) => api.post<AgentRun>(`/api/v1/notebooks/${notebookId}/agent-runs`, body),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["agent-runs", notebookId] }),
  });
}

export function useCancelRun(notebookId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => api.post<AgentRun>(`/api/v1/agent-runs/${runId}/cancel`),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["agent-runs", notebookId] }),
  });
}

/**
 * 「숨긴 결과」(#221) — 숨긴 run만(최근 50개). 끝난 run뿐이라 폴링하지 않는다. 키가 목록 키의 하위라
 * 숨기기·되돌리기·실행의 목록 무효화(`["agent-runs", notebookId]`)가 이 목록도 다시 읽는다.
 */
export function useHiddenAgentRuns(notebookId: string) {
  return useQuery<AgentRun[], ApiError>({
    queryKey: ["agent-runs", notebookId, "hidden"],
    queryFn: () => api.get<AgentRun[]>(`/api/v1/notebooks/${notebookId}/agent-runs?hidden=true`),
  });
}

/** 실패·취소 run을 「최근 결과」에서 숨긴다(#220) — 노트북의 모든 사용자에게 숨겨진다. */
export function useHideRun(notebookId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => api.post<AgentRun>(`/api/v1/agent-runs/${runId}/hide`),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["agent-runs", notebookId] }),
  });
}

/** 숨길 수 있는 실패 run을 한 번에 숨긴다(#220) — 숨긴 run id를 돌려받아 「되돌리기」에 쓴다. */
export function useHideFailedRuns(notebookId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () =>
      api.post<HiddenRuns>(`/api/v1/notebooks/${notebookId}/agent-runs/hide-failed`),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["agent-runs", notebookId] }),
  });
}

/**
 * 숨김 「되돌리기」(#220) — 항목 하나 또는 일괄로 숨긴 run들. 결과는 되돌리지 못한 요청의 오류 목록이다(던지지 않는다).
 *
 * 요청이 **모두** 끝난 뒤에 목록을 한 번 다시 읽는다. 첫 실패에서 끝내면(`Promise.all`) 아직 진행 중인 되돌리기가
 * 재조회 뒤에 반영돼 화면에 돌아오지 않는다 — 끝난 run만 있는 목록은 폴링하지 않는다.
 */
export function useUnhideRuns(notebookId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (runIds: string[]): Promise<unknown[]> => {
      const results = await Promise.allSettled(
        runIds.map((id) => api.post<AgentRun>(`/api/v1/agent-runs/${id}/unhide`)),
      );
      return results.flatMap((r) => (r.status === "rejected" ? [r.reason as unknown] : []));
    },
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: ["agent-runs", notebookId] }),
  });
}

/** HITL 승인 이력 — run 상세/승인 카드용(phase-19-design.md §1.1)
 *
 * 종료된 run의 승인 이력은 더 이상 바뀌지 않으므로, 최신 응답에 pending
 * 항목이 없으면 폴링을 멈춘다(useAgentRuns의 조건부 폴링과 동일 패턴).
 * RunDetailView가 모든 상태의 run에 대해 이 훅을 호출하면서 도입된
 * 낭비(코드 리뷰 지적)를 막는다. */
export function useAgentRunApprovals(runId: string | null) {
  return useQuery<AgentRunApproval[], ApiError>({
    queryKey: ["agent-run-approvals", runId],
    queryFn: () => api.get<AgentRunApproval[]>(`/api/v1/agent-runs/${runId}/approvals`),
    enabled: !!runId,
    refetchInterval: (query) =>
      query.state.data?.some((a) => a.status === "pending") ? 2000 : false,
  });
}

export function useResolveApproval(notebookId: string, runId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ approvalId, outcome }: { approvalId: string; outcome: "approve" | "reject" }) =>
      api.post<AgentRun>(`/api/v1/agent-runs/${runId}/approvals/${approvalId}/${outcome}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agent-run-approvals", runId] });
      queryClient.invalidateQueries({ queryKey: ["agent-runs", notebookId] });
      // 채팅 진입 run 카드(#66)는 노트북 목록이 아니라 단건을 본다
      queryClient.invalidateQueries({ queryKey: ["agent-run", runId] });
    },
  });
}

/** 노드별 실행 로그(react_agent tool-call 보조행 포함) — run 상세 뷰용(19b)
 *
 * `active`가 true인 동안(run이 queued/running/awaiting_approval) 2초 간격으로
 * 갱신해 진행 중인 react_agent의 새 로그·tool-call 보조행이 다이얼로그를 닫지
 * 않고도 반영되도록 한다. run이 끝나면 호출측에서 false를 넘겨 폴링을 멈춘다. */
export function useAgentRunLogs(runId: string | null, active: boolean) {
  return useQuery<ExecutionLog[], ApiError>({
    queryKey: ["agent-run-logs", runId],
    queryFn: () => api.get<ExecutionLog[]>(`/api/v1/agent-runs/${runId}/logs`),
    enabled: !!runId,
    refetchInterval: active ? 2000 : false,
  });
}

export async function fetchArtifact(runId: string, name: string): Promise<string> {
  return api.getText(`/api/v1/agent-runs/${runId}/artifacts/${encodeURIComponent(name)}`);
}

/** 오디오 등 바이너리 아티팩트 — blob object URL 반환 (호출측에서 revoke) */
export async function fetchArtifactObjectUrl(runId: string, name: string): Promise<string> {
  const blob = await api.getBlob(
    `/api/v1/agent-runs/${runId}/artifacts/${encodeURIComponent(name)}`,
  );
  return URL.createObjectURL(blob);
}
