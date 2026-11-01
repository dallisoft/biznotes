"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type ApiError } from "@/lib/api-client";
import type { SourceDigestBackfillStartOut, SourceDigestBackfillStatusOut } from "@/lib/admin-types";
import { notifyError, notifySuccess } from "@/lib/notify";
import { qk } from "@/lib/query-keys";

/**
 * 소스 요약 백필(코어 v0.30.0 #454, #285). 노트북 하나(`notebookId`) 또는 모든 노트북(null)의 최신 요약이 없는
 * ready 소스를 만든다. 진행률은 Redis에 있고(TTL 7일), 대기·진행 중이면 5초마다 다시 읽는다.
 */
const PATH = "/api/v1/admin/source-digests/backfill";
const POLL_MS = 5000;

export const ALL_NOTEBOOKS = "all";

function scopeKey(notebookId: string | null): string {
  return notebookId ?? ALL_NOTEBOOKS;
}

export function useSourceDigestBackfill(notebookId: string | null) {
  return useQuery<SourceDigestBackfillStatusOut, ApiError>({
    queryKey: qk.adminRag.digestBackfill(scopeKey(notebookId)),
    queryFn: () =>
      api.get<SourceDigestBackfillStatusOut>(
        notebookId ? `${PATH}?notebook_id=${encodeURIComponent(notebookId)}` : PATH,
      ),
    refetchInterval: (query) => {
      const status = query.state.data?.progress?.status;
      return status === "queued" || status === "running" ? POLL_MS : false;
    },
  });
}

export function useStartSourceDigestBackfill() {
  const queryClient = useQueryClient();
  return useMutation<SourceDigestBackfillStartOut, ApiError, { notebookId: string | null }>({
    mutationFn: ({ notebookId }) =>
      api.post<SourceDigestBackfillStartOut>(PATH, notebookId ? { notebook_id: notebookId } : {}),
    onSuccess: (out, { notebookId }) => {
      queryClient.setQueryData<SourceDigestBackfillStatusOut>(
        qk.adminRag.digestBackfill(scopeKey(notebookId)),
        { scope: out.scope, progress: out.progress ?? null },
      );
      notifySuccess(
        out.scheduled ? "소스 요약 만들기를 시작했습니다" : "이미 대기·진행 중인 요약 만들기가 있습니다",
        { description: out.scheduled ? "진행률은 이 화면에서 볼 수 있습니다" : "끝난 뒤 다시 시작할 수 있습니다" },
      );
    },
    onError: (error) => notifyError("소스 요약 만들기를 시작", error),
  });
}
