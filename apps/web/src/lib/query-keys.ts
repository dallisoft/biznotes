/**
 * react-query 키 팩토리 (R4-1 §2.2).
 *
 * 리터럴 `queryKey: ["admin-models"]`을 직접 쓰면 오타가 **무음 캐시 미스**로 끝난다
 * (`"admin-models"` ↔ `"models"`처럼 비슷한 키가 실제로 공존한다). 여기를 단일
 * 출처로 삼으면 오타는 컴파일 에러가 된다.
 *
 * ## prefix invalidation 계약
 *
 * react-query의 `invalidateQueries({ queryKey })`는 **접두사 매칭**이다. 따라서
 * 파생 키는 반드시 루트 키를 앞에 두고 확장해야 한다:
 *
 *   qk.adminModels.all      → ["admin-models"]
 *   qk.adminModels.list(id) → ["admin-models", "list", id]
 *
 * `all`을 무효화하면 그 아래 `list(...)`가 전부 함께 무효화된다. 새 파생 키를
 * 추가할 때 이 규칙을 깨면(루트를 빼먹으면) 무효화가 조용히 그 키를 건너뛴다.
 *
 * HCI-R4-9(승인): 158개 리터럴을 일괄 치환하지 않는다. R4-2/R4-4가 손대는
 * 도메인부터 점진 치환한다 — 여기 없는 도메인은 아직 리터럴을 쓰고 있다.
 */

/** 파생 키가 없는 단순 리스트 도메인. */
function root<const K extends string>(key: K) {
  return { all: [key] as const };
}

export const qk = {
  // --- admin/rag 진행률·설정 ---
  adminRag: {
    all: ["admin-rag"] as const,
    settings: ["admin-rag", "settings"] as const,
    reranker: ["admin-rag", "reranker"] as const,
    indexStatus: ["admin-rag", "index-status"] as const,
    // 활성 run 전용 키는 목록 키의 뒤에 붙여, 목록 키 invalidate가 접두사로 둘 다 덮게 한다
    reindexRuns: (limit: number) => ["admin-rag", "reindex-runs", limit] as const,
    activeReindexRuns: (limit: number) =>
      ["admin-rag", "reindex-runs", limit, { activeOnly: true }] as const,
    reindexRun: (id: string) => ["admin-rag", "reindex-run", id] as const,
    // 소스 요약 백필 진행률(코어 v0.30.0 #454) — scope는 노트북 id 또는 "all"
    digestBackfill: (scope: string) => ["admin-rag", "digest-backfill", scope] as const,
  },

  // --- R4-2 대상 (admin CRUD) ---
  adminMcp: root("admin-mcp"),
  adminAgents: root("admin-agents"),
  adminToolPolicies: root("admin-tool-policies"),

  // --- R4-4 대상 (admin/llm 2축) ---
  adminProviders: {
    all: ["admin-providers"] as const,
    circuits: ["admin-provider-circuits"] as const,
    /** 특정 프로바이더에서 발견 가능한 모델(디스커버리 다이얼로그). */
    availableModels: (providerId: string) =>
      ["provider-available-models", providerId] as const,
    modelSync: (providerId: string) => ["provider-model-sync", providerId] as const,
    embeddingGate: (providerId: string) => ["provider-embedding-gate", providerId] as const,
  },
  adminModels: root("admin-models"),
  adminLimits: root("admin-limits"),

  // --- 노트북 공유 관리(core-v0.20.0 P3 — 그룹 공유·전체 공개, plan §5.8) ---
  /** `GET /api/v1/me/groups` — 요청자가 속한 그룹(코어가 이름순으로 준다). */
  myGroups: root("my-groups"),
  /** 노트북별 공유 목록(`GET /notebooks/{id}/shares`). `all`을 무효화하면 모든 노트북의 목록이 함께 무효화된다. */
  notebookShares: {
    all: ["notebook-shares"] as const,
    list: (notebookId: string) => ["notebook-shares", notebookId] as const,
  },

  // --- 코어 「사용 제한」(코어 v0.26.0 #386·#387·#388) — 관리 › 사용 제한의 레이트 리밋·일일 한도 기본값·업로드 최대 크기 ---
  adminCoreLimits: {
    all: ["admin-core-limits"] as const,
    rateLimits: ["admin-core-limits", "rate-limits"] as const,
    usageDefaults: ["admin-core-limits", "usage-defaults"] as const,
    sourceUpload: ["admin-core-limits", "source-upload"] as const,
    notebookSourceLimit: ["admin-core-limits", "notebook-source-limit"] as const,
  },
  /** 사용자별 일일 한도(`GET /admin/users/{id}/limits`) — 일일 한도 기본값을 바꾸면 적용값(`effective`)이 달라져 함께 무효화한다. */
  adminUserLimits: {
    all: ["admin-user-limits"] as const,
    user: (userId: string | undefined) => ["admin-user-limits", userId] as const,
  },

  // --- 비-admin: 위 도메인들이 함께 무효화하는 사용자향 키 ---
  /** `GET /sources/upload-limits` — 업로드 다이얼로그의 사전 검사. 관리자가 업로드 최대 크기를 바꾸면 함께 무효화한다. */
  uploadLimits: root("upload-limits"),
  /** `admin-agents` 변경 시 함께 무효화된다(에이전트 활성 토글 등). */
  agents: root("agents"),
  /** `admin-models` 변경 시 함께 무효화된다(채팅 모델 셀렉터·이미지 모델 유무 — 접두사로 함께 덮는다). */
  models: {
    all: ["models"] as const,
    image: ["models", "image"] as const,
  },
  /** 실행 산출물 본문 — 뷰어(ArtifactContent)와 스튜디오 항목 제목 조회가 같은 키·캐시를 쓴다(mindmap-agent-skill D-6). */
  artifact: {
    all: ["artifact"] as const,
    body: (runId: string, name: string) => ["artifact", runId, name] as const,
  },
  /** `GET /meta/versions` — 사이드바 브랜드 행 아래 앱·코어 버전. 배포 때만 바뀐다. */
  versions: root("versions"),
} as const;
