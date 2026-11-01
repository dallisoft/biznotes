"use client";

/**
 * admin/rag 데이터 훅 (reindex-progress PLAN D-1·D-2·D-10·D-14).
 *
 * 페이지 컴포넌트는 `api`를 직접 부르지 않고 이 훅들만 쓴다. 폴링 주기·포커스/온라인 복귀
 * refetch는 전부 `pollIntervalFor`(lib/reindex-progress) 한 함수의 결과로 결정해, 종결·
 * 404/401/403·429·5xx 백오프 정책을 interval·focus·reconnect 세 경로가 공유한다.
 */
import {
  type QueryFunctionContext,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useMemo } from "react";
import { api } from "@/lib/api-client";
import type {
  NotebookIndexStatus,
  RagSettings,
  ReindexRequest,
  ReindexRun,
  ReindexStartOut,
  RerankerStatus,
} from "@/lib/admin-types";
import { notifyError, notifySuccess } from "@/lib/notify";
import { qk } from "@/lib/query-keys";
import { historyPollIntervalFor, listState, pollIntervalFor } from "@/lib/reindex-progress";

export const REINDEX_RUNS_LIMIT = 100;

type RunsKey = ReturnType<typeof qk.adminRag.reindexRuns>;
type ActiveRunsKey = ReturnType<typeof qk.adminRag.activeReindexRuns>;
type RunKey = ReturnType<typeof qk.adminRag.reindexRun>;

// `retry: false`이면 React Query의 `state.fetchFailureCount`는 fetch 시작마다 0으로
// 초기화되어 실패 직후 항상 1이다(query-core 5.101 `fetchState`). 지수 백오프(D-2)에는
// "마지막 성공 이후 연속 실패 수"가 필요하므로 Query 인스턴스별로 직접 센다 — Query 객체를
// 키로 쓰는 WeakMap이라 QueryClient(테스트 포함) 사이에 값이 새지 않는다.
const consecutiveFailures = new WeakMap<object, number>();

function trackFailures<T>(fetcher: () => Promise<T>) {
  return async (context: Pick<QueryFunctionContext, "client" | "queryKey">): Promise<T> => {
    const query = context.client
      .getQueryCache()
      .find({ queryKey: context.queryKey, exact: true });
    try {
      const result = await fetcher();
      if (query) consecutiveFailures.set(query, 0);
      return result;
    } catch (error) {
      if (query) consecutiveFailures.set(query, (consecutiveFailures.get(query) ?? 0) + 1);
      throw error;
    }
  };
}

function pollState<T>(query: { state: { data: T | undefined; error: unknown } }) {
  return {
    data: query.state.data,
    error: query.state.error,
    fetchFailureCount: consecutiveFailures.get(query) ?? 0,
  };
}

export function useRagSettings() {
  return useQuery({
    queryKey: qk.adminRag.settings,
    queryFn: () => api.get<RagSettings>("/api/v1/admin/rag/settings"),
    // 화면에 들어올 때마다 현재 서버에 다시 묻는다 — 리랭커 선택은 이번 마운트 뒤 응답의 계약으로만
    // 연다(P2-09). 앱 기본 staleTime(10초)이면 fresh 캐시로 요청이 생략돼 계속 잠긴다(리뷰 P2-10)
    staleTime: 0,
  });
}

export function useSaveRagSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (settings: RagSettings) =>
      api.put<RagSettings>("/api/v1/admin/rag/settings", settings),
    onSuccess: () => {
      notifySuccess("RAG 설정을 저장했습니다", {
        description: "청킹 설정은 새로 인덱싱하는 소스부터 적용됩니다",
      });
      void queryClient.invalidateQueries({ queryKey: qk.adminRag.settings });
      // 저장한 리랭커로 해석 결과(active·error)가 바뀐다
      void queryClient.invalidateQueries({ queryKey: qk.adminRag.reranker });
    },
    onError: (error: Error) => notifyError("RAG 설정을 저장", error),
  });
}

/**
 * 리랭커 선택지와 지금 쓰이는 리랭커(코어 v0.15.0 #209) — 리랭킹이 켜졌을 때만 조회한다.
 * 구 API(v0.13.0)에는 라우트가 없어 404다 — 재시도하지 않고 화면이 안내로 처리한다.
 */
export function useRerankerStatus(enabled: boolean) {
  return useQuery({
    queryKey: qk.adminRag.reranker,
    queryFn: () => api.get<RerankerStatus>("/api/v1/admin/rag/reranker"),
    enabled,
    retry: false,
    // 마운트·리랭킹 켜기 때마다 다시 묻는다 — `useRagSettings`와 같은 이유(리뷰 P2-10)
    staleTime: 0,
  });
}

/** 노트북별 인덱스 현황 — 활성 run이 있을 때만 2초 폴링(D-1). */
export function useIndexStatus(active: boolean) {
  return useQuery({
    queryKey: qk.adminRag.indexStatus,
    queryFn: () => api.get<NotebookIndexStatus[]>("/api/v1/admin/rag/index-status"),
    refetchInterval: active ? 2_000 : false,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: active,
    refetchOnReconnect: active,
  });
}

/** 최근 run 이력(최근 `limit`건) — 유휴 30초 / 오류 백오프. 활성 run은 `useActiveReindexRuns`가 맡는다. */
export function useReindexRuns(limit = REINDEX_RUNS_LIMIT) {
  // 제네릭 명시: refetchInterval 등 콜백의 query.state.data 타입을 고정한다.
  return useQuery<ReindexRun[], Error, ReindexRun[], RunsKey>({
    queryKey: qk.adminRag.reindexRuns(limit),
    queryFn: trackFailures(() =>
      api.get<ReindexRun[]>(`/api/v1/admin/rag/reindex?limit=${limit}`),
    ),
    retry: false,
    staleTime: 0,
    refetchInterval: (query) => historyPollIntervalFor(pollState(query)),
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: (query) => query.state.error == null,
    refetchOnReconnect: (query) => query.state.error == null,
  });
}

/**
 * 활성 run 집합의 진실 공급원(`active_only=true`, 코어 v0.12.0) — 활성 2초 / 유휴 30초 /
 * 오류 백오프(D-1·D-2). 최근 100건 밖의 활성 run도 찾는다. 구 API는 파라미터를 무시하고 종결 run을
 * 돌려주므로 `queued > 0`만 남긴다.
 */
export function useActiveReindexRuns(limit = REINDEX_RUNS_LIMIT) {
  return useQuery<ReindexRun[], Error, ReindexRun[], ActiveRunsKey>({
    queryKey: qk.adminRag.activeReindexRuns(limit),
    queryFn: trackFailures(async () =>
      (
        await api.get<ReindexRun[]>(`/api/v1/admin/rag/reindex?active_only=true&limit=${limit}`)
      ).filter((run) => run.queued > 0),
    ),
    retry: false,
    staleTime: 0,
    refetchInterval: (query) => pollIntervalFor(listState(pollState(query))),
    refetchIntervalInBackground: false,
    // 백오프 중 포커스·온라인 복귀가 재시도 시각을 앞당기지 않는다(D-14).
    refetchOnWindowFocus: (query) => query.state.error == null,
    refetchOnReconnect: (query) => query.state.error == null,
  });
}

/** 포커스 run 1건 상세 — 활성 1초, 종결·종결 오류에서 정지(D-1·D-2·D-14). */
export function useReindexRun(runId: string | null) {
  return useQuery<ReindexRun, Error, ReindexRun, RunKey>({
    queryKey: qk.adminRag.reindexRun(runId ?? ""),
    queryFn: trackFailures(() => api.get<ReindexRun>(`/api/v1/admin/rag/reindex/${runId}`)),
    enabled: runId !== null,
    retry: false,
    staleTime: 0,
    refetchInterval: (query) => pollIntervalFor(pollState(query)),
    refetchIntervalInBackground: false,
    // 활성 폴링 중(또는 첫 응답 전)일 때만 복귀 refetch — 종결·404/401/403·429/5xx 백오프 중 0건.
    refetchOnWindowFocus: (query) => pollIntervalFor(pollState(query)) === 1_000,
    refetchOnReconnect: (query) => pollIntervalFor(pollState(query)) === 1_000,
  });
}

/**
 * 페이지가 run 수명주기 이벤트에 맞춰 부르는 캐시 조작(D-2 404·D-6 종결). 페이지는
 * QueryClient를 직접 만지지 않고 이 함수들만 쓴다. 반환 객체는 QueryClient가 같으면 동일하다.
 */
export function useReindexCacheActions() {
  const queryClient = useQueryClient();
  return useMemo(() => {
    const runsKey = qk.adminRag.reindexRuns(REINDEX_RUNS_LIMIT);
    const activeKey = qk.adminRag.activeReindexRuns(REINDEX_RUNS_LIMIT);
    return {
      /** D-6: 포커스 run 종결 전이 뒤 index-status·runs 목록을 다시 받는다. */
      refreshAfterRunFinished() {
        void queryClient.invalidateQueries({ queryKey: qk.adminRag.indexStatus });
        void queryClient.invalidateQueries({ queryKey: runsKey });
      },
      /**
       * D-2 404: 사라진 run을 목록 캐시에서 빼고 목록을 다시 받는다. 오류 상태의 목록에
       * setQueryData를 하면 status가 success로 바뀌어 degraded 잠금이 풀리므로 성공 캐시일 때만 제거.
       */
      forgetRun(runId: string) {
        for (const key of [runsKey, activeKey]) {
          const state = queryClient.getQueryState(key);
          if (state?.data !== undefined && state.error == null) {
            queryClient.setQueryData<ReindexRun[]>(key, (current) =>
              current?.filter((run) => run.id !== runId),
            );
          }
        }
        void queryClient.invalidateQueries({ queryKey: runsKey });
      },
    };
  }, [queryClient]);
}

export interface StartReindexCallbacks {
  /** 요청 직전 — 페이지는 직전 결과 줄을 지운다. */
  onMutate?: (request: ReindexRequest) => void;
  /** 모든 성공 응답(`run_id` null 포함). */
  onResult?: (result: ReindexStartOut, request: ReindexRequest) => void;
  /** `run_id`가 있을 때만 — 진행 패널의 포커스 run이 된다. */
  onStarted?: (runId: string, counts: Pick<ReindexStartOut, "enqueued" | "skipped">) => void;
}

export function useStartReindex(callbacks: StartReindexCallbacks = {}) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: ReindexRequest) =>
      api.post<ReindexStartOut>("/api/v1/admin/rag/reindex", request),
    onMutate: (request) => {
      callbacks.onMutate?.(request);
    },
    onSuccess: (result, request) => {
      callbacks.onResult?.(result, request);
      notifySuccess(
        result.skipped > 0
          ? `재인덱싱 작업 ${result.enqueued}건을 등록했습니다 — ${result.skipped}건은 이미 대기·처리 중이라 건너뛰었습니다`
          : `재인덱싱 작업 ${result.enqueued}건을 등록했습니다`,
      );
      if (result.run_id) {
        callbacks.onStarted?.(result.run_id, {
          enqueued: result.enqueued,
          skipped: result.skipped,
        });
        void queryClient.invalidateQueries({ queryKey: qk.adminRag.indexStatus });
      }
      void queryClient.invalidateQueries({
        queryKey: qk.adminRag.reindexRuns(REINDEX_RUNS_LIMIT),
      });
    },
    onError: (error: Error) => notifyError("재인덱싱을 요청", error),
  });
}
