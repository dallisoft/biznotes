"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api-client";
import type {
  NotebookSourceLimit,
  NotebookSourceLimitUpdate,
  RateLimits,
  RateLimitsUpdate,
  SourceUploadSettings,
  SourceUploadSettingsUpdate,
  UsageDefaults,
} from "@/lib/admin-types";
import { notifyError, notifySuccess } from "@/lib/notify";
import { qk } from "@/lib/query-keys";

/**
 * 관리 › 사용 제한의 코어 항목(코어 v0.26.0 #386·#387·#388). PUT은 저장 뒤 전체 상태를 돌려주므로 그 응답으로
 * 캐시를 바꾼다. 검증 422는 섹션이 항목별 문구로 보이므로(`limitValidationMessages`) 토스트는 그 밖의 실패만 띄운다.
 */
function notifySaveError(action: string, error: Error) {
  if (error instanceof ApiError && error.status === 422) return;
  notifyError(action, error);
}

const RATE_LIMITS_PATH = "/api/v1/admin/rate-limits";
const USAGE_DEFAULTS_PATH = "/api/v1/admin/usage-defaults";
const SOURCE_UPLOAD_PATH = "/api/v1/admin/source-upload";
const NOTEBOOK_SOURCE_LIMIT_PATH = "/api/v1/admin/notebook-source-limit";

/** 분당 레이트 리밋 — 키별 env 값·재정의·적용값. */
export function useRateLimits() {
  return useQuery({
    queryKey: qk.adminCoreLimits.rateLimits,
    queryFn: () => api.get<RateLimits>(RATE_LIMITS_PATH),
  });
}

/** 부분 갱신 — 바꾼 키만 보낸다. `null`은 재정의 삭제(env 값). */
export function useSaveRateLimits() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (changes: RateLimitsUpdate) => api.put<RateLimits>(RATE_LIMITS_PATH, changes),
    onSuccess: (saved) => {
      queryClient.setQueryData(qk.adminCoreLimits.rateLimits, saved);
      notifySuccess("분당 레이트 리밋을 저장했습니다", {
        description: `다른 API 프로세스에는 최대 ${saved.cache_ttl_seconds}초 뒤에 반영됩니다`,
      });
    },
    onError: (error: Error) => notifySaveError("분당 레이트 리밋을 저장", error),
  });
}

/** 일일 한도 기본값 — null은 기본값 없음(무제한). */
export function useUsageDefaults() {
  return useQuery({
    queryKey: qk.adminCoreLimits.usageDefaults,
    queryFn: () => api.get<UsageDefaults>(USAGE_DEFAULTS_PATH),
  });
}

/** 부분 갱신 — 사용자별 한도의 적용값(`effective`)이 달라지므로 사용자 한도 조회도 무효화한다. */
export function useSaveUsageDefaults() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (changes: UsageDefaults) => api.put<UsageDefaults>(USAGE_DEFAULTS_PATH, changes),
    onSuccess: (saved) => {
      queryClient.setQueryData(qk.adminCoreLimits.usageDefaults, saved);
      void queryClient.invalidateQueries({ queryKey: qk.adminUserLimits.all });
      notifySuccess("일일 한도 기본값을 저장했습니다", { description: "다음 한도 검사부터 적용됩니다" });
    },
    onError: (error: Error) => notifySaveError("일일 한도 기본값을 저장", error),
  });
}

/** 업로드 최대 크기(바이트) — 적용값·env 값·재정의·하드 상한. */
export function useSourceUploadSettings() {
  return useQuery({
    queryKey: qk.adminCoreLimits.sourceUpload,
    queryFn: () => api.get<SourceUploadSettings>(SOURCE_UPLOAD_PATH),
  });
}

/** 업로드 다이얼로그의 사전 검사 값(`GET /sources/upload-limits`)도 무효화한다. */
export function useSaveSourceUploadSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: SourceUploadSettingsUpdate) => api.put<SourceUploadSettings>(SOURCE_UPLOAD_PATH, body),
    onSuccess: (saved) => {
      queryClient.setQueryData(qk.adminCoreLimits.sourceUpload, saved);
      void queryClient.invalidateQueries({ queryKey: qk.uploadLimits.all });
      notifySuccess("업로드 최대 크기를 저장했습니다", { description: "다음 업로드부터 적용됩니다" });
    },
    onError: (error: Error) => notifySaveError("업로드 최대 크기를 저장", error),
  });
}

/** 노트북당 소스 상한(코어 v0.30.0 #445) — 적용값·env 값·재정의. */
export function useNotebookSourceLimit() {
  return useQuery({
    queryKey: qk.adminCoreLimits.notebookSourceLimit,
    queryFn: () => api.get<NotebookSourceLimit>(NOTEBOOK_SOURCE_LIMIT_PATH),
  });
}

/** 소스 추가 다이얼로그의 상한 표시(`GET /sources/upload-limits`)도 무효화한다. */
export function useSaveNotebookSourceLimit() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: NotebookSourceLimitUpdate) =>
      api.put<NotebookSourceLimit>(NOTEBOOK_SOURCE_LIMIT_PATH, body),
    onSuccess: (saved) => {
      queryClient.setQueryData(qk.adminCoreLimits.notebookSourceLimit, saved);
      void queryClient.invalidateQueries({ queryKey: qk.uploadLimits.all });
      notifySuccess("노트북당 소스 상한을 저장했습니다", { description: "다음 소스 추가부터 적용됩니다" });
    },
    onError: (error: Error) => notifySaveError("노트북당 소스 상한을 저장", error),
  });
}
