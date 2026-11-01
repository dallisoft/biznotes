"use client";

/**
 * 관리 개요(`/admin`) 전용 쿼리와 집계(plan §6.2.6, D-05·D-23, HCI-13).
 *
 * - 기존 관리 GET 10개만 쓴다(새 API 없음). 카드마다 독립 쿼리라 하나가 실패해도 나머지는 보인다.
 * - **스냅샷**: 모든 쿼리가 {@link ADMIN_OVERVIEW_QUERY_OPTIONS}를 쓴다 — 폴링·포커스·재연결 refetch가 없고,
 *   다시 읽는 길은 화면의 「새로고침」(전체 refetch)과 staleTime이 지난 뒤의 재진입뿐이다.
 * - 기존 훅(`use-admin-llm`의 10초 차단 상태 폴링, `use-reindex`의 2초·상태별 폴링)을 가져오지 않는다.
 *   query key도 `admin-overview` 아래에 따로 둔다 — 기존 화면의 폴링 관찰자·다른 응답 모양(예: 활성
 *   재인덱싱 목록의 필터 결과)과 캐시 항목을 공유하지 않아, 개요가 폴링을 물려받는 길이 없다.
 * - 집계는 응답 배열 전체로 계산한다(페이지 단위 아님). 집계 함수는 순수 함수로 내보내 단위 테스트한다.
 */
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { MODALITIES } from "@/components/admin/llm/capability-picker";
import { REINDEX_RUNS_LIMIT } from "@/hooks/use-reindex";
import type {
  AdminMCPServer,
  AdminModel,
  AdminProvider,
  AdminUser,
  AuditLogRow,
  NotebookIndexStatus,
  ProviderCircuit,
  ReindexRun,
} from "@/lib/admin-types";
import { api, type ApiError } from "@/lib/api-client";
import type { SkillReviewRequest } from "@/lib/skill-types";

/** 개요 전용 쿼리 옵션(§6.2.6) — 폴링·포커스·재연결 refetch 없음, 1분 동안 fresh, 실패 시 1회 재시도. */
export const ADMIN_OVERVIEW_QUERY_OPTIONS = {
  staleTime: 60_000,
  refetchInterval: false,
  refetchOnWindowFocus: false,
  refetchOnReconnect: false,
  retry: 1,
} as const;

/**
 * 첫 진입 시 보내는 GET 9개(§6.2.6 — 7개 카드가 8개, 최근 감사 로그 1개).
 * 활성 재인덱싱은 기존 RAG 화면과 같은 `limit`을 붙인다(API 기본 20건에서 잘리지 않게).
 */
export const ADMIN_OVERVIEW_PATHS = {
  users: "/api/v1/admin/users",
  providers: "/api/v1/admin/llm/providers",
  providerStatus: "/api/v1/admin/llm/providers/status",
  models: "/api/v1/admin/llm/models",
  mcpServers: "/api/v1/admin/mcp-servers",
  indexStatus: "/api/v1/admin/rag/index-status",
  activeReindex: `/api/v1/admin/rag/reindex?active_only=true&limit=${REINDEX_RUNS_LIMIT}`,
  pendingReviews: "/api/v1/admin/skills/review-requests?status=pending",
  auditLogs: "/api/v1/admin/audit-logs?limit=100",
} as const;

export type AdminOverviewQueryName = keyof typeof ADMIN_OVERVIEW_PATHS;

const ROOT = "admin-overview";

/** 개요 쿼리 키 — 모두 `["admin-overview", …]` 아래(기존 관리 화면 키와 겹치지 않는다). */
export const adminOverviewKeys = {
  all: [ROOT] as const,
  query: (name: AdminOverviewQueryName) => [ROOT, name] as const,
};

/** 최근 감사 로그 카드에 보이는 행 수. */
export const RECENT_AUDIT_LOG_COUNT = 5;

function useOverviewQuery<T>(name: AdminOverviewQueryName): UseQueryResult<T, ApiError> {
  return useQuery<T, ApiError>({
    queryKey: adminOverviewKeys.query(name),
    queryFn: ({ signal }) => api.get<T>(ADMIN_OVERVIEW_PATHS[name], { signal }),
    ...ADMIN_OVERVIEW_QUERY_OPTIONS,
  });
}

export interface AdminOverviewQueries {
  users: UseQueryResult<AdminUser[], ApiError>;
  providers: UseQueryResult<AdminProvider[], ApiError>;
  providerStatus: UseQueryResult<ProviderCircuit[], ApiError>;
  models: UseQueryResult<AdminModel[], ApiError>;
  mcpServers: UseQueryResult<AdminMCPServer[], ApiError>;
  indexStatus: UseQueryResult<NotebookIndexStatus[], ApiError>;
  activeReindex: UseQueryResult<ReindexRun[], ApiError>;
  pendingReviews: UseQueryResult<SkillReviewRequest[], ApiError>;
  auditLogs: UseQueryResult<AuditLogRow[], ApiError>;
}

/** 개요의 GET 9개를 병렬로 건다. 반환 객체의 각 값은 독립 쿼리 결과다. */
export function useAdminOverview(): AdminOverviewQueries {
  return {
    users: useOverviewQuery<AdminUser[]>("users"),
    providers: useOverviewQuery<AdminProvider[]>("providers"),
    providerStatus: useOverviewQuery<ProviderCircuit[]>("providerStatus"),
    models: useOverviewQuery<AdminModel[]>("models"),
    mcpServers: useOverviewQuery<AdminMCPServer[]>("mcpServers"),
    indexStatus: useOverviewQuery<NotebookIndexStatus[]>("indexStatus"),
    activeReindex: useOverviewQuery<ReindexRun[]>("activeReindex"),
    pendingReviews: useOverviewQuery<SkillReviewRequest[]>("pendingReviews"),
    auditLogs: useOverviewQuery<AuditLogRow[]>("auditLogs"),
  };
}

// ------------------------------------------------------------------ 집계(순수 함수)

export interface UserSummary {
  total: number;
  active: number;
}

export function summarizeUsers(users: readonly AdminUser[]): UserSummary {
  return { total: users.length, active: users.filter((u) => u.is_active).length };
}

export interface ProviderSummary {
  total: number;
  /** 활성이고 차단되지 않은 프로바이더 */
  healthy: number;
  /** 차단(circuit open)된 프로바이더 — 이름은 「주의가 필요한 항목」에 쓴다 */
  blocked: string[];
  /** 관리자가 끈 프로바이더(정상·차단 어느 쪽도 아니다) */
  disabled: number;
}

/** 프로바이더 목록 + 차단 상태 — 차단 판정은 상태 응답의 `open`만 본다(목록에 없는 상태 행은 무시). */
export function summarizeProviders(
  providers: readonly AdminProvider[],
  circuits: readonly ProviderCircuit[],
): ProviderSummary {
  const openIds = new Set(circuits.filter((c) => c.open).map((c) => c.id));
  const blocked = providers.filter((p) => openIds.has(p.id));
  const disabled = providers.filter((p) => !p.enabled && !openIds.has(p.id)).length;
  return {
    total: providers.length,
    healthy: providers.length - blocked.length - disabled,
    blocked: blocked.map((p) => p.name),
    disabled,
  };
}

export interface ModelSummary {
  total: number;
  enabled: number;
  /** 활성 모델이 있는데 그 능력의 기본 모델이 활성 모델 중에 없는 능력(정해진 능력 순서, 그 밖은 이름순) */
  missingDefaults: string[];
}

/**
 * 활성 모델 수와 「기본 모델 미지정 능력」. 기본값은 능력별로 1개다(코어 부분 유니크 인덱스). 게이트웨이는
 * 활성 모델 중 기본값을 고르고 없으면 첫 모델로 넘어가므로(`resolve_model`), 비활성 기본값은 미지정으로 본다.
 */
export function summarizeModels(models: readonly AdminModel[]): ModelSummary {
  const enabled = models.filter((m) => m.enabled);
  const present = new Set(enabled.flatMap((m) => m.capabilities));
  const withDefault = new Set(enabled.filter((m) => m.is_default).flatMap((m) => m.capabilities));
  const known: readonly string[] = MODALITIES;
  // `string[]`로 넓힌다 — 생성 타입(6값)과 달리 새 코어가 보낸 모르는 능력도 아래에서 뒤에 붙인다
  const missing: string[] = [...present].filter((c) => !withDefault.has(c));
  const ordered = [
    ...known.filter((c) => missing.includes(c)),
    ...missing.filter((c) => !known.includes(c)).sort(),
  ];
  return { total: models.length, enabled: enabled.length, missingDefaults: ordered };
}

export interface McpSummary {
  total: number;
  healthy: number;
  error: number;
  unknown: number;
}

/** 마지막 상태 확인 결과로 나눈다 — `true` 정상, `false` 오류, `null`(아직 확인 안 함) 미확인. */
export function summarizeMcpServers(servers: readonly AdminMCPServer[]): McpSummary {
  let healthy = 0;
  let error = 0;
  for (const s of servers) {
    if (s.last_health_ok === true) healthy += 1;
    else if (s.last_health_ok === false) error += 1;
  }
  return { total: servers.length, healthy, error, unknown: servers.length - healthy - error };
}

export interface IndexSummary {
  notebooks: number;
  /** 인덱싱에 실패한 소스 수(전체 노트북 합) */
  failed: number;
  /** 처리 중인 소스 수(전체 노트북 합) */
  processing: number;
  /** 실패한 소스가 있는 노트북 수 */
  notebooksWithFailures: number;
}

export function summarizeIndex(rows: readonly NotebookIndexStatus[]): IndexSummary {
  let failed = 0;
  let processing = 0;
  let notebooksWithFailures = 0;
  for (const r of rows) {
    failed += r.failed;
    processing += r.processing;
    if (r.failed > 0) notebooksWithFailures += 1;
  }
  return { notebooks: rows.length, failed, processing, notebooksWithFailures };
}

/**
 * 진행 중 재인덱싱 run — `queued > 0`만 센다. 구 API는 `active_only`를 무시하고 종결 run도 돌려주므로
 * 기존 RAG 화면(`useActiveReindexRuns`)과 같은 기준으로 거른다.
 */
export function countActiveReindexRuns(runs: readonly ReindexRun[]): number {
  return runs.filter((r) => r.queued > 0).length;
}

