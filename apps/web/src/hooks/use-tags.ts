"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { notebookDetailKey } from "@/hooks/use-notebooks";
import { api, ApiError } from "@/lib/api-client";
import type { Tag } from "@/lib/types";

export function useTags() {
  return useQuery<Tag[], ApiError>({
    queryKey: ["tags"],
    queryFn: () => api.get<Tag[]>("/api/v1/tags"),
  });
}

export function useCreateTag() {
  const queryClient = useQueryClient();
  return useMutation<Tag, ApiError, { name: string }>({
    mutationFn: (body) => api.post<Tag>("/api/v1/tags", body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["tags"] }),
  });
}

export function useDeleteTag() {
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => api.delete<void>(`/api/v1/tags/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tags"] });
      queryClient.invalidateQueries({ queryKey: ["notebooks"] });
      // 태그 자체를 지우면 그 태그가 붙은 모든 노트북 상세가 바뀐다 — 상세 키 전체(접두 「notebook」)를 다시 읽는다
      queryClient.invalidateQueries({ queryKey: ["notebook"] });
    },
  });
}

/**
 * 태그 연결·해제 뒤 캐시 정책(plan §6.6.2, PR-09 · r2 보조 의견): 응답이 `void`라 응답으로 캐시를 쓰지 않고,
 * 그 노트북의 상세(`["notebook", id]` — 노트북 헤더·태그 관리 다이얼로그)와 목록(`["notebooks"]` — 홈·사이드바)을
 * 함께 무효화해 다시 읽는다.
 */
function invalidateNotebookTags(queryClient: ReturnType<typeof useQueryClient>, notebookId: string) {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ["notebooks"] }),
    queryClient.invalidateQueries({ queryKey: notebookDetailKey(notebookId) }),
  ]);
}

export function useAttachTag(notebookId: string) {
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (tagId) => api.post<void>(`/api/v1/notebooks/${notebookId}/tags/${tagId}`),
    onSuccess: () => invalidateNotebookTags(queryClient, notebookId),
  });
}

export function useDetachTag(notebookId: string) {
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (tagId) => api.delete<void>(`/api/v1/notebooks/${notebookId}/tags/${tagId}`),
    onSuccess: () => invalidateNotebookTags(queryClient, notebookId),
  });
}
