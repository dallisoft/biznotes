import { describe, expect, it } from "vitest";
import type {
  AdminMCPServer,
  AdminModel,
  AdminProvider,
  AdminUser,
  NotebookIndexStatus,
  ProviderCircuit,
  ReindexRun,
} from "@/lib/admin-types";
import {
  ADMIN_OVERVIEW_PATHS,
  ADMIN_OVERVIEW_QUERY_OPTIONS,
  adminOverviewKeys,
  countActiveReindexRuns,
  summarizeIndex,
  summarizeMcpServers,
  summarizeModels,
  summarizeProviders,
  summarizeUsers,
} from "./use-admin-overview";

const user = (id: number, is_active: boolean): AdminUser => ({
  id: `u${id}`,
  email: `u${id}@example.com`,
  name: `U${id}`,
  role: "user",
  is_active,
  must_change_password: false,
  created_at: "2026-10-01T00:00:00Z",
});

const provider = (id: string, enabled = true): AdminProvider => ({
  id,
  name: id,
  type: "mock",
  base_url: null,
  has_api_key: false,
  enabled,
  model_count: 0,
});

const circuit = (id: string, open: boolean): ProviderCircuit => ({
  id,
  name: id,
  open,
  open_remaining_seconds: open ? 30 : 0,
  recent_failures: open ? 5 : 0,
});

const model = (
  id: string,
  capabilities: string[],
  { enabled = true, is_default = false }: { enabled?: boolean; is_default?: boolean } = {},
): AdminModel => ({
  id,
  provider_id: "p",
  provider_name: "p",
  model_name: id,
  display_name: id,
  // 이 빌드가 모르는 능력(새 코어가 보낸 값)도 흉내 내도록 넓은 입력을 생성 타입으로 단언한다
  capabilities: capabilities as AdminModel["capabilities"],
  is_default,
  enabled,
  context_window: null,
  cost_per_1k: {},
});

const mcp = (id: string, last_health_ok: boolean | null): AdminMCPServer => ({
  id,
  name: id,
  transport: "stdio",
  endpoint_or_command: "x",
  has_env: false,
  enabled: true,
  last_health_check: null,
  last_health_ok,
});

const index = (id: number, failed: number, processing: number): NotebookIndexStatus => ({
  notebook_id: `n${id}`,
  title: `N${id}`,
  source_count: 3,
  ready: 3 - failed - processing,
  failed,
  processing,
  chunk_total: 10,
  reindex_pending: 0,
  stale: 0,
});

const run = (id: string, queued: number): ReindexRun => ({
  id,
  notebook_id: null,
  failed_only: false,
  refetch: false,
  requested: 10,
  queued,
  done: 10 - queued,
  failed: 0,
  created_at: "2026-10-15T00:00:00Z",
  finished_at: null,
  removed: 0,
  retrying: 0,
  max_attempts: 3,
  stale_only: false,
});

describe("관리 개요 쿼리 계약(§6.2.6)", () => {
  it("개요 전용 옵션은 폴링·포커스·재연결 refetch가 없고 staleTime 60초·재시도 1회다", () => {
    expect(ADMIN_OVERVIEW_QUERY_OPTIONS).toEqual({
      staleTime: 60_000,
      refetchInterval: false,
      refetchOnWindowFocus: false,
      refetchOnReconnect: false,
      retry: 1,
    });
  });

  it("GET 9개 — 7개 카드(프로바이더 2개) + 최근 감사 로그 1개, 기존 관리 API 경로만", () => {
    const paths = Object.values(ADMIN_OVERVIEW_PATHS);
    expect(paths).toHaveLength(9);
    expect(new Set(paths).size).toBe(9);
    for (const p of paths) expect(p.startsWith("/api/v1/admin/")).toBe(true);
    expect(ADMIN_OVERVIEW_PATHS.activeReindex).toBe("/api/v1/admin/rag/reindex?active_only=true&limit=100");
    expect(ADMIN_OVERVIEW_PATHS.pendingReviews).toBe(
      "/api/v1/admin/skills/review-requests?status=pending",
    );
    expect(ADMIN_OVERVIEW_PATHS.auditLogs).toBe("/api/v1/admin/audit-logs?limit=100");
  });

  it("쿼리 키는 모두 admin-overview 아래 — 기존 관리 화면(폴링 훅)의 키와 겹치지 않는다", () => {
    expect(adminOverviewKeys.query("providerStatus")).toEqual(["admin-overview", "providerStatus"]);
    expect(adminOverviewKeys.query("indexStatus")[0]).toBe("admin-overview");
    expect(adminOverviewKeys.all).toEqual(["admin-overview"]);
  });
});

describe("관리 개요 집계 — 응답 배열 전체 기준", () => {
  it("사용자: 전체·활성", () => {
    expect(summarizeUsers([user(1, true), user(2, false), user(3, true)])).toEqual({
      total: 3,
      active: 2,
    });
    expect(summarizeUsers([])).toEqual({ total: 0, active: 0 });
  });

  it("프로바이더: 차단은 상태 응답의 open, 비활성은 정상·차단 어느 쪽도 아니다", () => {
    const s = summarizeProviders(
      [provider("a"), provider("b"), provider("c", false), provider("d", false)],
      [circuit("a", false), circuit("b", true), circuit("d", true), circuit("ghost", true)],
    );
    expect(s).toEqual({ total: 4, healthy: 1, blocked: ["b", "d"], disabled: 1 });
  });

  it("모델: 활성 수, 활성 모델이 있는데 활성 기본 모델이 없는 능력만 미지정(정해진 순서 → 그 밖 이름순)", () => {
    const s = summarizeModels([
      model("chat", ["llm"], { is_default: true }),
      model("chat2", ["llm", "vlm"]),
      model("emb-old", ["embedding"], { enabled: false, is_default: true }),
      model("emb-new", ["embedding"]),
      model("rr", ["reranker"], { enabled: false }),
      model("img", ["image"]),
      model("aaa", ["zeta"]),
    ]);
    expect(s.total).toBe(7);
    expect(s.enabled).toBe(5);
    // vlm·embedding·image는 MODALITIES 순서, zeta는 뒤에 이름순. reranker는 활성 모델이 없어 제외.
    expect(s.missingDefaults).toEqual(["vlm", "embedding", "image", "zeta"]);
  });

  it("MCP: true 정상 · false 오류 · null 미확인", () => {
    expect(
      summarizeMcpServers([mcp("a", true), mcp("b", false), mcp("c", null), mcp("d", false)]),
    ).toEqual({ total: 4, healthy: 1, error: 2, unknown: 1 });
  });

  it("인덱스: 노트북 수 · 실패/처리 중 소스 합 · 실패가 있는 노트북 수", () => {
    expect(summarizeIndex([index(1, 0, 0), index(2, 2, 1), index(3, 1, 0)])).toEqual({
      notebooks: 3,
      failed: 3,
      processing: 1,
      notebooksWithFailures: 2,
    });
  });

  it("진행 중 재인덱싱: queued > 0만 센다(구 API의 종결 run 제외)", () => {
    expect(countActiveReindexRuns([run("a", 3), run("b", 0), run("c", 1)])).toBe(2);
    expect(countActiveReindexRuns([])).toBe(0);
  });
});
