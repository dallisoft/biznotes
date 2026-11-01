"use client";

import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { api, ApiError } from "@/lib/api-client";
import { qk } from "@/lib/query-keys";
import type {
  Source,
  SourceDetail,
  SourceGuide,
  SourceUploadLimits,
  WebSearchResult,
} from "@/lib/source-types";

const ACTIVE = new Set(["pending", "parsing", "chunking", "embedding"]);

export function useSources(notebookId: string) {
  return useQuery<Source[], ApiError>({
    queryKey: ["sources", notebookId],
    queryFn: () => api.get<Source[]>(`/api/v1/notebooks/${notebookId}/sources`),
    // 인제스천 진행 중이면 2초 폴링 (R5-3: source SSE 엔드포인트 제거됨, 폴링이 유일 경로)
    refetchInterval: (query) =>
      query.state.data?.some((s) => ACTIVE.has(s.status)) ? 2000 : false,
  });
}

export function useSourceDetail(sourceId: string | null) {
  return useQuery<SourceDetail, ApiError>({
    queryKey: ["source", sourceId],
    queryFn: () => api.get<SourceDetail>(`/api/v1/sources/${sourceId}`),
    enabled: sourceId !== null,
  });
}

// 소스 가이드(요약+키워드) — 서버가 캐시 없으면 온디맨드 생성하므로 stale 유지
export function useSourceGuide(sourceId: string | null) {
  return useQuery<SourceGuide, ApiError>({
    queryKey: ["source-guide", sourceId],
    queryFn: () => api.get<SourceGuide>(`/api/v1/sources/${sourceId}/guide`),
    enabled: sourceId !== null,
    staleTime: Infinity,
    gcTime: 30 * 60 * 1000,
  });
}

export function useCreateSource(notebookId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      type: "web" | "text";
      url?: string;
      text?: string;
      title?: string;
    }) => api.post<Source>(`/api/v1/notebooks/${notebookId}/sources`, body),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["sources", notebookId] }),
  });
}

const uploadLimitsQuery = queryOptions<SourceUploadLimits, ApiError>({
  queryKey: qk.uploadLimits.all,
  queryFn: () => api.get<SourceUploadLimits>("/api/v1/sources/upload-limits"),
});

/**
 * 업로드 최대 크기(코어 v0.26.0 #388) — 업로드 판정과 같은 값이라 다이얼로그가 문구와 사전 검사에 쓴다.
 * 다이얼로그가 열려 있을 때만 읽는다. 관리자가 값을 바꾸면 `qk.uploadLimits`가 무효화된다.
 */
export function useUploadLimits(enabled = true) {
  return useQuery({ ...uploadLimitsQuery, enabled });
}

/**
 * 업로드 직전에 최대 크기를 확정한다 — 캐시가 있으면 그 값, 조회 중이면 그 요청을 기다린다(새로 부르지 않는다).
 * 못 읽으면 undefined다 — 호출부는 사전 검사 없이 보내고 서버 413이 판정한다.
 */
export function useResolveUploadLimit() {
  const queryClient = useQueryClient();
  return useCallback(async (): Promise<number | undefined> => {
    try {
      return (await queryClient.ensureQueryData(uploadLimitsQuery)).max_upload_bytes;
    } catch {
      return undefined;
    }
  }, [queryClient]);
}

export function useUploadSource(notebookId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ file, signal }: { file: File; signal?: AbortSignal }) => {
      const form = new FormData();
      form.append("file", file);
      return api.postForm<Source>(
        `/api/v1/notebooks/${notebookId}/sources/upload`,
        form,
        signal,
      );
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["sources", notebookId] }),
  });
}

export function useDeleteSource(notebookId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (sourceId: string) => api.delete<void>(`/api/v1/sources/${sourceId}`),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["sources", notebookId] }),
  });
}

export function useRetrySource(notebookId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (sourceId: string) =>
      api.post<Source>(`/api/v1/sources/${sourceId}/retry`),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["sources", notebookId] }),
  });
}

export function useWebSearch(notebookId: string) {
  return useMutation({
    mutationFn: (body: { query: string }) =>
      api.post<WebSearchResult[]>(
        `/api/v1/notebooks/${notebookId}/sources/web-search`,
        body,
      ),
  });
}
