"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api-client";
import type { LimitSettings } from "@/lib/admin-types";
import { notifyError, notifySuccess } from "@/lib/notify";
import { qk } from "@/lib/query-keys";

/** 관리 › 사용 제한 — 적용 중인 값(저장값이 없으면 서버 기본값). */
export function useLimitSettings() {
  return useQuery({
    queryKey: qk.adminLimits.all,
    queryFn: () => api.get<LimitSettings>("/api/v1/admin/limits"),
  });
}

export function useSaveLimitSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    // 서버는 보낸 항목만 저장한다 — 바꾼 항목만 보낸다
    mutationFn: (changes: Partial<LimitSettings>) => api.put<LimitSettings>("/api/v1/admin/limits", changes),
    onSuccess: () => {
      notifySuccess("사용 제한을 저장했습니다", { description: "다음 질문부터 적용됩니다" });
      void queryClient.invalidateQueries({ queryKey: qk.adminLimits.all });
    },
    onError: (error: Error) => notifyError("사용 제한을 저장", error),
  });
}
