"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type ApiError } from "@/lib/api-client";
import { notifyError, notifySuccess } from "@/lib/notify";
import { qk } from "@/lib/query-keys";
import type {
  AdminModel,
  AdminProvider,
  DiscoveredModel,
  EmbeddingGate,
  ModelSyncStatus,
  ProviderCircuit,
} from "@/lib/admin-types";

/**
 * admin/llm의 쿼리·뮤테이션 (R4-4 §5.1).
 *
 * **provider 축과 model 축을 나눈다.** 단일 훅으로 합치면 1,000줄 페이지를 1,000줄
 * 훅으로 옮기는 것에 불과하다.
 *
 * 모델·프로바이더 변경은 사용자향 `models` 셀렉터에도 반영돼야 하므로 대부분의
 * 뮤테이션이 `qk.models.all`을 함께 무효화한다.
 */

const PROVIDERS_PATH = "/api/v1/admin/llm/providers";
const MODELS_PATH = "/api/v1/admin/llm/models";

// ---------------------------------------------------------------- provider 축

export function useAdminProviders() {
  const qc = useQueryClient();

  const list = useQuery<AdminProvider[], ApiError>({
    queryKey: qk.adminProviders.all,
    queryFn: () => api.get<AdminProvider[]>(PROVIDERS_PATH),
  });

  /** 차단(circuit breaker) 상태 — 10초마다 폴링한다. */
  const circuits = useQuery<ProviderCircuit[], ApiError>({
    queryKey: qk.adminProviders.circuits,
    queryFn: () => api.get<ProviderCircuit[]>(`${PROVIDERS_PATH}/status`),
    refetchInterval: 10_000,
  });

  const invalidateProviders = () =>
    qc.invalidateQueries({ queryKey: qk.adminProviders.all });

  const create = useMutation<
    AdminProvider,
    ApiError,
    {
      name: string;
      type: string;
      base_url: string;
      api_key: string;
      embedding_max_concurrency: number | null;
    }
  >({
    mutationFn: (form) =>
      api.post<AdminProvider>(PROVIDERS_PATH, {
        ...form,
        base_url: form.base_url || null,
        api_key: form.api_key || null,
      }),
    onSuccess: () => {
      notifySuccess("프로바이더를 추가했습니다");
      invalidateProviders();
    },
    onError: (e) => notifyError("프로바이더를 추가", e),
  });

  const update = useMutation<
    AdminProvider,
    ApiError,
    {
      id: string;
      base_url: string;
      api_key: string;
      enabled: boolean;
      embedding_max_concurrency: number | null;
    }
  >({
    mutationFn: ({ id, base_url, api_key, enabled, embedding_max_concurrency }) =>
      api.patch<AdminProvider>(`${PROVIDERS_PATH}/${id}`, {
        base_url: base_url || null,
        // 비워두면 기존 키 유지 (undefined → 전송 안 됨)
        api_key: api_key ? api_key : undefined,
        enabled,
        // 항상 보낸다 — null이 「제한 해제」다(코어 model_fields_set, 계획 D-4)
        embedding_max_concurrency,
      }),
    onSuccess: () => {
      notifySuccess("프로바이더를 수정했습니다");
      invalidateProviders();
    },
    onError: (e) => notifyError("프로바이더를 수정", e),
  });

  const remove = useMutation<void, ApiError, string>({
    mutationFn: (id) => api.delete<void>(`${PROVIDERS_PATH}/${id}`),
    onSuccess: () => {
      // 프로바이더를 지우면 연결된 모델도 함께 사라진다.
      invalidateProviders();
      qc.invalidateQueries({ queryKey: qk.adminModels.all });
      qc.invalidateQueries({ queryKey: qk.models.all });
      notifySuccess("프로바이더를 삭제했습니다");
    },
    onError: (e) => notifyError("프로바이더를 삭제", e),
  });

  const test = useMutation<{ ok: boolean; detail: string }, ApiError, string>({
    mutationFn: (id) =>
      api.post<{ ok: boolean; detail: string }>(`${PROVIDERS_PATH}/${id}/test`),
    // 연결 테스트 결과 문구(`detail`)는 코어가 만든 진단 문장이다 — 성공·실패 모두 그대로 보인다.
    onSuccess: (r) =>
      r.ok
        ? notifySuccess("연결 테스트에 성공했습니다", { description: r.detail })
        : notifyError("연결을 테스트", new Error(r.detail)),
    onError: (e) => notifyError("연결을 테스트", e),
  });

  const resetCircuit = useMutation<void, ApiError, string>({
    mutationFn: (id) => api.post<void>(`${PROVIDERS_PATH}/${id}/circuit-reset`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.adminProviders.circuits });
      notifySuccess("차단을 해제했습니다");
    },
    onError: (e) => notifyError("차단을 해제", e),
  });

  return { list, circuits, create, update, remove, test, resetCircuit };
}

/**
 * 임베딩 동시성 게이트 상태 — **관리자가 요청할 때만 1회** 조회한다(`refetch()`).
 *
 * 코어 엔드포인트가 동기 Redis로 API 이벤트 루프를 최대 ~15초 막을 수 있다(v0.13.0 알려진 한계).
 * 그래서 폴링·포커스 재조회를 끄고, 전역 기본 `retry: 1`도 덮어써 실패 시 재요청하지 않는다.
 */
export function useProviderEmbeddingGate(providerId: string) {
  return useQuery<EmbeddingGate, ApiError>({
    queryKey: qk.adminProviders.embeddingGate(providerId),
    queryFn: () => api.get<EmbeddingGate>(`${PROVIDERS_PATH}/${providerId}/embedding-gate`),
    enabled: false,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
}

/** 프로바이더가 제공하는 모델 목록. 다이얼로그가 열렸을 때만 조회한다. */
export function useProviderAvailableModels(providerId: string, enabled: boolean) {
  return useQuery<DiscoveredModel[], ApiError>({
    queryKey: qk.adminProviders.availableModels(providerId),
    queryFn: () =>
      api.get<DiscoveredModel[]>(`${PROVIDERS_PATH}/${providerId}/available-models`),
    enabled: enabled && !!providerId,
    // 목록 조회를 지원하지 않는 프로바이더가 있다 — 재시도하지 않고 수동 입력으로 넘긴다.
    retry: false,
  });
}

/** 등록된 모델이 프로바이더에 여전히 존재하는지 확인. */
export function useProviderModelSync(providerId: string, enabled: boolean) {
  return useQuery<ModelSyncStatus[], ApiError>({
    queryKey: qk.adminProviders.modelSync(providerId),
    queryFn: () =>
      api.get<ModelSyncStatus[]>(`${PROVIDERS_PATH}/${providerId}/models/sync-check`),
    enabled,
    retry: false,
  });
}

/** 디스커버리 목록에서 고른 모델들을 일괄 등록. */
export function useImportModels(providerId: string) {
  const qc = useQueryClient();
  return useMutation<
    { created: number; skipped: string[] },
    ApiError,
    { model_name: string; display_name: string; capabilities: string[] }[]
  >({
    mutationFn: (items) =>
      api.post<{ created: number; skipped: string[] }>(
        `${PROVIDERS_PATH}/${providerId}/models/import`,
        { items },
      ),
    onSuccess: (r) => {
      notifySuccess(
        `모델 ${r.created}개를 등록했습니다` +
          (r.skipped.length ? ` (${r.skipped.length}개 건너뜀)` : ""),
      );
      qc.invalidateQueries({ queryKey: qk.adminModels.all });
      qc.invalidateQueries({ queryKey: qk.adminProviders.all });
      qc.invalidateQueries({ queryKey: qk.models.all });
      qc.invalidateQueries({
        queryKey: qk.adminProviders.availableModels(providerId),
      });
    },
    onError: (e) => notifyError("모델을 등록", e),
  });
}

// ------------------------------------------------------------------- model 축

/** 모델 변경은 사용자향 셀렉터(`models`)에도 즉시 반영돼야 한다. */
function useInvalidateModels() {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: qk.adminModels.all });
    qc.invalidateQueries({ queryKey: qk.models.all });
  };
}

/** 모델 사고 양 기본값(코어 v0.29.0 #429) — `REASONING_EFFORTS`와 같다. 빈 값은 기본값 없음(null). */
export const REASONING_EFFORT_OPTIONS = [
  { value: "low", label: "낮음 (low)" },
  { value: "medium", label: "보통 (medium)" },
  { value: "high", label: "높음 (high)" },
] as const;
export type ReasoningEffort = (typeof REASONING_EFFORT_OPTIONS)[number]["value"];

/** 모델 최대 출력 토큰 상한 — 코어 관리자 스키마(PostgreSQL Integer 최댓값, #428·#437). */
export const MAX_OUTPUT_TOKENS_LIMIT = 2_147_483_647;

export interface ModelForm {
  display_name: string;
  context_window: string;
  /** 빈 문자열 = 기본값 없음(null) */
  reasoning_effort: "" | ReasoningEffort;
  /** 빈 문자열 = 모른다(null, 상한을 줄이지 않는다) */
  max_output_tokens: string;
  capabilities: string[];
  cost_input: string;
  cost_output: string;
  cost_per_image: string;
}

/** 단가 키 — `per_image`는 이미지 생성 모델의 장당 가격(USD, 코어 v0.23.0 #338). */
export type CostPer1k = { input?: number; output?: number; per_image?: number };

/**
 * 빈 문자열은 "미설정"이므로 키 자체를 빼서 비용 미집계로 남긴다. 장당 가격은 `image` 능력일 때만
 * 보낸다 — 능력을 바꾼 뒤 남은 입력값이 다른 모델의 단가로 저장되지 않게 한다.
 */
export function costPer1k(
  input: string,
  output: string,
  perImage = "",
  capabilities: readonly string[] = [],
): CostPer1k {
  return {
    ...(input !== "" ? { input: Number(input) } : {}),
    ...(output !== "" ? { output: Number(output) } : {}),
    ...(perImage !== "" && capabilities.includes("image") ? { per_image: Number(perImage) } : {}),
  };
}

/** PATCH /models/{id} 본문 — 모든 필드가 선택적이다(부분 갱신). */
export interface ModelUpdate {
  id: string;
  display_name?: string;
  context_window?: number | null;
  reasoning_effort?: ReasoningEffort | null;
  max_output_tokens?: number | null;
  capabilities?: string[];
  cost_per_1k?: CostPer1k;
}

export function useAdminModels() {
  const qc = useQueryClient();
  const invalidate = useInvalidateModels();

  const list = useQuery<AdminModel[], ApiError>({
    queryKey: qk.adminModels.all,
    queryFn: () => api.get<AdminModel[]>(MODELS_PATH),
  });

  const create = useMutation<
    AdminModel,
    ApiError,
    {
      provider_id: string;
      model_name: string;
      display_name: string;
      capabilities: string[];
      cost_per_1k: CostPer1k;
    }
  >({
    mutationFn: (body) => api.post<AdminModel>(MODELS_PATH, body),
    onSuccess: () => {
      notifySuccess("모델을 추가했습니다", { description: "사용자 화면에 바로 반영됩니다" });
      invalidate();
    },
    onError: (e) => notifyError("모델을 추가", e),
  });

  const update = useMutation<AdminModel, ApiError, ModelUpdate>({
    mutationFn: ({ id, ...body }) => api.patch<AdminModel>(`${MODELS_PATH}/${id}`, body),
    onSuccess: () => {
      notifySuccess("모델을 수정했습니다");
      invalidate();
    },
    onError: (e) => notifyError("모델을 수정", e),
  });

  /** 활성 토글 — 스위치가 상태를 보여주므로 성공은 토스트 없이 조용히 반영한다. 실패는 알린다(스위치가 그대로라 실패를 모른다). */
  const toggle = useMutation<AdminModel, ApiError, { id: string; enabled: boolean }>({
    mutationFn: ({ id, enabled }) => api.patch<AdminModel>(`${MODELS_PATH}/${id}`, { enabled }),
    onSuccess: invalidate,
    onError: (e, { enabled }) => notifyError(enabled ? "모델을 활성화" : "모델을 비활성화", e),
  });

  const remove = useMutation<void, ApiError, string>({
    mutationFn: (id) => api.delete<void>(`${MODELS_PATH}/${id}`),
    onSuccess: () => {
      invalidate();
      notifySuccess("모델을 삭제했습니다");
    },
    onError: (e) => notifyError("모델을 삭제", e),
  });

  const setDefault = useMutation<void, ApiError, string>({
    mutationFn: (id) => api.post<void>(`${MODELS_PATH}/${id}/set-default`),
    onSuccess: () => {
      invalidate();
      notifySuccess("기본 모델을 지정했습니다");
    },
    onError: (e) => notifyError("기본 모델을 지정", e),
  });

  const setFallback = useMutation<
    AdminModel,
    ApiError,
    { id: string; fallback_priority: number | null }
  >({
    mutationFn: ({ id, fallback_priority }) =>
      api.patch<AdminModel>(`${MODELS_PATH}/${id}`, { fallback_priority }),
    onSuccess: () => {
      // 폴백 순서는 사용자 셀렉터에 노출되지 않는다 — admin 목록만 갱신한다
      // (기존 동작 보존: 여기서만 `models`를 무효화하지 않는다).
      qc.invalidateQueries({ queryKey: qk.adminModels.all });
      notifySuccess("폴백 순서를 저장했습니다");
    },
    onError: (e) => notifyError("폴백 순서를 저장", e),
  });

  return { list, create, update, toggle, remove, setDefault, setFallback };
}
