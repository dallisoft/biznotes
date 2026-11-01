import type { components } from "@biznotes/shared-types";

type Schemas = components["schemas"];

export type AdminUser = Schemas["UserOut"];
export type AdminGroup = Schemas["GroupOut"];

export interface GroupMember {
  user_id: string;
  email: string;
  name: string;
  role: string;
}

export type AdminProvider = Schemas["ProviderOut"];
export type AdminModel = Schemas["ModelOut"];

export interface ModelSyncStatus {
  id: string;
  model_name: string;
  display_name: string;
  still_available: boolean;
}

export type Modality = AdminModel["capabilities"][number];

export type DiscoveredModel = Schemas["DiscoveredModel"];

export type ProviderCircuit = Schemas["ProviderCircuitOut"];

export type EmbeddingGate = Schemas["EmbeddingGateOut"];

export type RagSettings = Schemas["RagSettings"];

/** `GET /admin/limits` 응답 — 관리 › 사용 제한의 적용 중인 값. */
export type LimitSettings = Schemas["LimitSettingsOut"];

/** `GET /admin/rag/reranker` 응답 — 리랭커 선택지와 지금 쓰이는 리랭커(코어 v0.15.0 #209). */
export type RerankerStatus = Schemas["RerankerStatusOut"];

export type NotebookIndexStatus = Schemas["NotebookIndexStatus"];

/** `POST /admin/rag/reindex` 요청 본문 — 기본값 필드는 생략 가능하므로 Partial로 소비한다. */
export type ReindexRequest = Partial<Schemas["ReindexRequest"]>;

/** `POST /admin/rag/reindex` 응답 — `run_id`/`enqueued`/`skipped` (코어 v0.9.1). */
export type ReindexStartOut = Schemas["ReindexStartOut"];

/** `GET /admin/rag/reindex[/{run_id}]` 응답 — run 진행 카운트(코어 v0.10.6, `finished_at`은 합성값). */
export type ReindexRun = Schemas["ReindexRunOut"];

export type AdminAgent = Schemas["AgentAdminOut"];

export type AdminMCPServer = Schemas["MCPServerOut"];


export interface AuditLogRow {
  id: string;
  user_email: string | null;
  action: string;
  resource: string;
  detail: Record<string, unknown>;
  created_at: string;
}

/**
 * MCP 헬스체크(`POST /admin/mcp-servers/{id}/health-check`)의 도구 항목 — 정화 설명과 노출 판정
 * `spec`(`ok`·`sanitized`·`rejected` + 거부 사유). 원문 스키마는 응답에 없다(core-v0.20.0-upgrade §5.7).
 */
export type MCPTool = Schemas["MCPToolHealthOut"];

/** 사용자별 사용량 한도 + 당일 사용량 — admin/users 페이지 전용. */
export type UserLimits = Schemas["UsageLimitsOut"];

/** 사용자별 한도의 적용값과 출처(`user`·`default`·`unlimited`) — 코어 v0.26.0 #387. */
export type EffectiveUsageLimit = Schemas["EffectiveUsageLimit"];

// ---- 코어 「사용 제한」(코어 v0.26.0 #386·#387·#388) — 값은 `system_settings` 재정의, 지우면 env(기본값) ----

/** `GET /admin/rate-limits` — 키별 env 값·재정의·적용값과 읽기 전용 `enabled`·`cache_ttl_seconds`. */
export type RateLimits = Schemas["RateLimitsOut"];
export type RateLimitItem = Schemas["RateLimitItem"];
export type RateLimitKey = RateLimitItem["key"];
/** `PUT /admin/rate-limits` — 부분 갱신. 없는 키는 그대로, `null`은 재정의 삭제. */
export type RateLimitsUpdate = Schemas["RateLimitsUpdate"];

/** `GET`·`PUT /admin/usage-defaults` — 시스템 기본 일일 한도. 응답 null은 기본값 없음(무제한), 요청은 부분 갱신. */
export type UsageDefaults = Schemas["UsageDefaults"];
export type UsageLimitField = keyof UsageDefaults;

/** `GET /admin/source-upload` — 업로드 최대 크기(바이트)의 적용값·env 값·재정의·하드 상한. */
export type SourceUploadSettings = Schemas["SourceUploadSettingsOut"];
/** `PUT /admin/source-upload` — `null`은 재정의 삭제(env 값). */
export type SourceUploadSettingsUpdate = Schemas["SourceUploadSettingsUpdate"];
/** 노트북당 소스 상한(코어 v0.30.0 #445) — 적용값·env 값·재정의. */
export type NotebookSourceLimit = Schemas["NotebookSourceLimitOut"];
export type NotebookSourceLimitUpdate = Schemas["NotebookSourceLimitUpdate"];

/** 소스 요약 백필(코어 v0.30.0 #454) — 진행률·시작 응답. */
export type SourceDigestBackfillProgress = Schemas["SourceDigestBackfillProgress"];
export type SourceDigestBackfillStartOut = Schemas["SourceDigestBackfillStartOut"];
export type SourceDigestBackfillStatusOut = Schemas["SourceDigestBackfillStatusOut"];
