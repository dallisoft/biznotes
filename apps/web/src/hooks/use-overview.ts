"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type ApiError } from "@/lib/api-client";

export interface NotebookOverview {
  summary: string | null;
  suggested_questions: string[];
  title_suggestions: string[];
  generated_at: string | null;
  stale: boolean;
  /**
   * 개요 생성 입력에 들어간 ready 소스 수(코어 v0.30.0 #449, #286). 개요는 소스를 빼지 않으므로 생성 시점의 ready
   * 소스 전체 수다. 개요가 없거나 옛 api면 0·undefined다.
   */
  source_count?: number;
}

export function useOverview(notebookId: string) {
  return useQuery<NotebookOverview, ApiError>({
    queryKey: ["overview", notebookId],
    queryFn: () =>
      api.get<NotebookOverview>(`/api/v1/notebooks/${notebookId}/overview`),
    // 요약이 아직 없거나 재생성 중(stale)이면 폴링 — 소스 ready 전이를 클라이언트가
    // 직접 알 수 없으므로 요약 확보 전까지는 주기 갱신한다
    refetchInterval: (query) =>
      query.state.data?.stale || !query.state.data?.summary ? 4000 : false,
  });
}

/** 개요 수동 재생성 — 성공 시 폴링이 갱신을 감지하도록 generated_at 기준 무효화 */
export function useRegenerateOverview(notebookId: string) {
  const queryClient = useQueryClient();
  return useMutation<{ status: string }, ApiError>({
    mutationFn: () =>
      api.post<{ status: string }>(
        `/api/v1/notebooks/${notebookId}/overview/regenerate`,
      ),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["overview", notebookId] }),
  });
}
