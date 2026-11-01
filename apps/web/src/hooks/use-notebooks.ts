"use client";

import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
  type QueryKey,
} from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api-client";
import type { ChatSession } from "@/lib/chat-types";
import type { Notebook, NotebookFilter } from "@/lib/types";

/** 노트북 상세 쿼리 키 — 노트북 작업 화면(`/notebooks/[id]`)이 이 키로 읽는다. */
export function notebookDetailKey(id: string) {
  return ["notebook", id] as const;
}

/**
 * 노트북 하나에 딸린 쿼리 키(상세 + 하위 — 소스·메모·대화·요약·실행·일정). 삭제 뒤 캐시에서 지운다
 * (plan §6.6.2 노트북 작업 화면 행, r1 PR-09). `removeQueries`는 접두 일치라 하위 키도
 * 함께 지워진다. 대화별 메시지(`["chat-messages", sessionId]`)는 대화 목록 캐시에서 세션 id를 읽어 따로 지운다.
 */
export function notebookScopedQueryKeys(id: string): QueryKey[] {
  return [
    notebookDetailKey(id),
    ["sources", id],
    ["notes", id],
    ["chat-sessions", id],
    ["overview", id],
    ["agent-runs", id],
  ];
}

/** 삭제된 노트북의 상세·하위 쿼리를 캐시에서 지운다(관찰자가 있어도 다시 요청하지 않도록 먼저 취소한다). */
export function evictNotebookQueries(queryClient: QueryClient, id: string) {
  const sessions = queryClient.getQueryData<ChatSession[]>(["chat-sessions", id]) ?? [];
  const keys: QueryKey[] = [
    ...notebookScopedQueryKeys(id),
    ...sessions.map((s) => ["chat-messages", s.id] as QueryKey),
  ];
  for (const queryKey of keys) {
    void queryClient.cancelQueries({ queryKey });
    queryClient.removeQueries({ queryKey });
  }
}

/**
 * PATCH 응답을 상세 캐시에 합친다. 코어 PATCH 응답은 사용자별 계산 필드(`source_count`·`tags`·`is_favorite`)를
 * 채우지 않고 기본값(0·[]·false)으로 돌려주므로(`biznotes_core/api/v1/notebooks.py` `update` → `_to_out(nb, perm, user.id)`),
 * 바뀐 필드만 덮고 나머지는 기존 캐시 값을 둔다. 캐시가 없으면 새로 만들지 않는다(다음 조회가 온전한 값을 읽는다).
 */
export function mergeNotebookPatch(prev: Notebook | undefined, updated: Notebook): Notebook | undefined {
  if (!prev) return undefined;
  return {
    ...prev,
    title: updated.title,
    emoji: updated.emoji,
    description: updated.description,
    visibility: updated.visibility,
    updated_at: updated.updated_at,
  };
}

export function useNotebooks(filter: NotebookFilter) {
  return useQuery<Notebook[], ApiError>({
    queryKey: ["notebooks", filter],
    queryFn: () => api.get<Notebook[]>(`/api/v1/notebooks?filter=${filter}`),
  });
}

export function useCreateNotebook() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { title: string; emoji?: string; description?: string }) =>
      api.post<Notebook>("/api/v1/notebooks", body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notebooks"] }),
  });
}

/**
 * 웹이 보내는 공개 범위 — `private`·`public`만이다. `group`은 코어 v0.20.0에서 deprecated(접근 판정상 `private`과
 * 같다)라 보내지 않는다(core-v0.20.0 §5.8 ⑤).
 */
export type NotebookVisibilityChoice = Exclude<Notebook["visibility"], "group">;

export interface UpdateNotebookInput {
  id: string;
  title?: string;
  emoji?: string;
  description?: string;
  /** 전체 공개 토글(소유자만 — 코어가 owner 권한만 값 변경을 허용한다). */
  visibility?: NotebookVisibilityChoice;
}

/**
 * 노트북 이름·이모지·설명·공개 범위 변경. 본문은 넘긴 필드 그대로다(토글이면 정확히 `{visibility}`).
 * 성공하면 PATCH 응답으로 상세 캐시와 목록 캐시의 그 항목을 곧바로 고치고
 * (헤더 `h1`·사이드바·홈 카드가 같은 값을 보이게), 상세·목록을 무효화해 서버 값으로 다시 읽는다(PR-09).
 * 캐시는 이미 고쳤으므로 재조회는 기다리지 않는다 — 이름 변경 다이얼로그가 응답 직후 닫힌다.
 */
export function useUpdateNotebook() {
  const queryClient = useQueryClient();
  return useMutation<Notebook, ApiError, UpdateNotebookInput>({
    mutationFn: ({ id, ...body }) => api.patch<Notebook>(`/api/v1/notebooks/${id}`, body),
    onSuccess: (updated, { id }) => {
      queryClient.setQueryData<Notebook>(notebookDetailKey(id), (prev) =>
        mergeNotebookPatch(prev, updated),
      );
      queryClient.setQueriesData<Notebook[]>({ queryKey: ["notebooks"] }, (list) =>
        list?.map((nb) => (nb.id === id ? (mergeNotebookPatch(nb, updated) ?? nb) : nb)),
      );
      void queryClient.invalidateQueries({ queryKey: notebookDetailKey(id) });
      void queryClient.invalidateQueries({ queryKey: ["notebooks"] });
    },
  });
}

/**
 * 노트북 삭제 — 노트북 홈 카드 메뉴와 노트북 헤더가 같은 훅을 쓴다(PR-09). 성공하면 목록 캐시에서 그 항목을 바로
 * 빼고(사이드바의 현재 노트북 행과 그 아래 대화 목록이 먼저 사라져 지워진 노트북을 다시 요청하지 않게), 상세·하위
 * 쿼리를 지운 뒤 목록을 무효화한다. 목록 재조회는 기다리지 않는다 — 헤더가 곧바로 노트북 홈으로 이동한다.
 * 화면 이동은 호출자가 정한다(헤더는 `router.replace("/")`, 홈은 이동 없음).
 */
export function useDeleteNotebook() {
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => api.delete<void>(`/api/v1/notebooks/${id}`),
    onSuccess: (_data, id) => {
      queryClient.setQueriesData<Notebook[]>({ queryKey: ["notebooks"] }, (list) =>
        list?.filter((nb) => nb.id !== id),
      );
      evictNotebookQueries(queryClient, id);
      void queryClient.invalidateQueries({ queryKey: ["notebooks"] });
    },
  });
}

export function useToggleFavorite() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, favorite }: { id: string; favorite: boolean }) =>
      favorite
        ? api.post<void>(`/api/v1/notebooks/${id}/favorite`)
        : api.delete<void>(`/api/v1/notebooks/${id}/favorite`),
    onSuccess: (_data, { id }) =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: ["notebooks"] }),
        queryClient.invalidateQueries({ queryKey: notebookDetailKey(id) }),
      ]),
  });
}
