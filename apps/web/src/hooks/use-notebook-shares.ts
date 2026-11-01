"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@biznotes/shared-types";
import { api, type ApiError } from "@/lib/api-client";
import { classifyError } from "@/lib/notebook-bulk";
import { qk } from "@/lib/query-keys";
import type { NotebookShare } from "@/lib/types";

type ShareRequest = components["schemas"]["NotebookShareRequest"];

/**
 * 그룹 공유 요청(공유·권한 변경 공통). 생성 타입에서 끌어온 별칭이다 — 요청 본문은 `group_id`·`permission` 두 필드뿐이고
 * 사용자 대상(사용자 id·이메일, deprecated)은 보내지 않는다(core-v0.20.0 AC-3.4).
 */
export interface GroupShareInput {
  notebookId: string;
  groupId: NonNullable<ShareRequest["group_id"]>;
  permission: ShareRequest["permission"];
}

export interface RevokeShareInput {
  notebookId: string;
  shareId: NotebookShare["id"];
}

const sharesPath = (notebookId: string) => `/api/v1/notebooks/${notebookId}/shares`;

/**
 * 공유 목록 캐시에 응답 행을 반영한다 — 같은 id면 바꾸고(같은 그룹 재공유는 처음 행의 id를 돌려준다, 코어 #222) 아니면
 * 뒤에 붙인다. 캐시가 없으면 만들지 않는다(다음 조회가 온전한 값을 읽는다).
 */
export function upsertShareRow(rows: NotebookShare[] | undefined, row: NotebookShare): NotebookShare[] | undefined {
  if (!rows) return undefined;
  return rows.some((r) => r.id === row.id) ? rows.map((r) => (r.id === row.id ? row : r)) : [...rows, row];
}

/** 노트북 하나의 현재 공유(`GET /notebooks/{id}/shares` — 소유자만). 대화상자가 열려 있을 때만 읽는다. */
export function useNotebookShares(notebookId: string | null, enabled = true) {
  return useQuery<NotebookShare[], ApiError>({
    queryKey: qk.notebookShares.list(notebookId ?? ""),
    queryFn: () => api.get<NotebookShare[]>(sharesPath(notebookId ?? "")),
    enabled: enabled && notebookId !== null,
  });
}

/**
 * 그룹에 공유하거나 그 그룹 행의 권한을 바꾼다(`POST /share {group_id, permission}` — 같은 그룹이면 같은 행이 요청
 * 권한으로 바뀐다, 낮추기 포함). 무효화 범위는 **그 노트북의 공유 목록만**이다 — 노트북 목록·상세 응답에는 공유
 * 필드가 없다. 성공하면 응답 행으로 목록 캐시를 곧바로 고친 뒤 다시 읽는다. 실패하면 목록을 다시 읽고(다른 곳의 변경
 * 반영), 그룹 404면 그룹 목록도 다시 읽는다(소속이 바뀌었을 수 있다).
 */
export function useShareWithGroup() {
  const queryClient = useQueryClient();
  return useMutation<NotebookShare, ApiError, GroupShareInput>({
    mutationFn: ({ notebookId, groupId, permission }) =>
      api.post<NotebookShare>(`/api/v1/notebooks/${notebookId}/share`, { group_id: groupId, permission }),
    onSuccess: (row, { notebookId }) => {
      queryClient.setQueryData<NotebookShare[]>(qk.notebookShares.list(notebookId), (rows) =>
        upsertShareRow(rows, row),
      );
      void queryClient.invalidateQueries({ queryKey: qk.notebookShares.list(notebookId) });
    },
    onError: (error, { notebookId }) => {
      void queryClient.invalidateQueries({ queryKey: qk.notebookShares.list(notebookId) });
      // 그룹 404 — 노트북 실제 소유자가 속하지 않은(또는 없는) 그룹. 분류는 `classifyError` 한 곳(같은 문구 대조)
      if (classifyError(error).kind === "target_group_missing") {
        void queryClient.invalidateQueries({ queryKey: qk.myGroups.all });
      }
    },
  });
}

/**
 * 공유 행 하나를 해제한다(`DELETE /notebooks/{id}/shares/{share_id}` — 고른 행의 id로만). 성공하면 그 행만 목록
 * 캐시에서 빼고 다시 읽는다. 실패해도 목록을 다시 읽는다(이미 해제된 행이면 사라진다). 무효화 범위는 공유 목록만이다.
 */
export function useRevokeNotebookShare() {
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, RevokeShareInput>({
    mutationFn: ({ notebookId, shareId }) => api.delete<void>(`${sharesPath(notebookId)}/${shareId}`),
    onSuccess: (_data, { notebookId, shareId }) => {
      queryClient.setQueryData<NotebookShare[]>(qk.notebookShares.list(notebookId), (rows) =>
        rows?.filter((r) => r.id !== shareId),
      );
      void queryClient.invalidateQueries({ queryKey: qk.notebookShares.list(notebookId) });
    },
    onError: (_error, { notebookId }) => {
      void queryClient.invalidateQueries({ queryKey: qk.notebookShares.list(notebookId) });
    },
  });
}
