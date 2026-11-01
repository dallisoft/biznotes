import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import type { ComponentType } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { APP_QUERY_DEFAULTS } from "@/lib/query-defaults";
import AdminAgentsPage from "./agents/page";
import AdminAuditPage from "./audit/page";
import AdminLayout from "./layout";
import AdminLimitsPage from "./limits/page";
import AdminLLMPage from "./llm/page";
import AdminMcpPage from "./mcp/page";
import AdminRagPage from "./rag/page";
import AdminSkillsPage from "./skills/page";
import AdminUsersPage from "./users/page";

// 비인가 관점(AGENTS.md 「보안·데이터」, plan §8.3) — P5가 바꾼 관리 8화면과 감사 로그 화면(#236)·「사용 제한」 각각을 실제 AdminLayout 가드 안에 마운트한다.
// 일반 사용자는 「/」로 replace되고, 그 화면의 제목·내용이 렌더되지 않으며, 관리 API(/api/v1/admin/…)와
// 비밀값을 돌려주는 요청(POST)이 0건이어야 한다. 관리자 양성 대조로 같은 하네스가 화면을 실제로 그림을 확인한다.
// 네트워크 경계(api)와 프레임워크 경계(next/navigation)만 바꾼다 — 가드·화면 코드는 실경로다.

const { api, replace } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
  replace: vi.fn(),
}));

// 에이전트 화면의 비주얼 빌더가 가져오는 React Flow 스타일(jsdom에서 처리 불가 — 에이전트 화면 테스트와 같은 처리)
vi.mock("@xyflow/react/dist/style.css", () => ({}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn(), back: vi.fn(), refresh: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/admin/users",
}));

const ME_PATH = "/api/v1/auth/me";
const me = (role: "admin" | "user") => ({
  id: `${role}-1`,
  email: `${role}@example.com`,
  name: role,
  role,
  is_active: true,
  must_change_password: false,
  created_at: "2026-10-01T00:00:00Z",
});

// RAG·사용 제한 설정 응답(객체) — 양성 대조에서 화면이 폼을 그릴 수 있게 한다. 나머지 관리 GET은 빈 배열이 유효한 모양이다.
const RAG_SETTINGS = {
  chunk_size: 800,
  chunk_overlap: 100,
  top_k: 8,
  rerank_enabled: false,
  rerank_model: null,
  citation_min_score: 0.5,
  insufficient_evidence_threshold: 0.3,
};

function mockApi(role: "admin" | "user") {
  api.get.mockImplementation((p: string) => {
    if (p === ME_PATH) return Promise.resolve(me(role));
    if (p === "/api/v1/admin/rag/settings") return Promise.resolve(RAG_SETTINGS);
    if (p === "/api/v1/admin/limits") return Promise.resolve({ chat_token_budget: 100_000 });
    return Promise.resolve([]);
  });
}

const adminGets = () =>
  api.get.mock.calls.map((c) => c[0] as string).filter((p) => p.startsWith("/api/v1/admin/"));
const writeCalls = () => [api.post, api.patch, api.put, api.delete].flatMap((fn) => fn.mock.calls);

const PAGES: Array<{ path: string; title: string; Page: ComponentType }> = [
  { path: "/admin/users", title: "사용자·그룹", Page: AdminUsersPage },
  { path: "/admin/llm", title: "LLM 게이트웨이", Page: AdminLLMPage },
  { path: "/admin/rag", title: "RAG 관리", Page: AdminRagPage },
  { path: "/admin/agents", title: "에이전트", Page: AdminAgentsPage },
  { path: "/admin/skills", title: "스킬 공개", Page: AdminSkillsPage },
  { path: "/admin/mcp", title: "MCP 서버", Page: AdminMcpPage },
  { path: "/admin/limits", title: "사용 제한", Page: AdminLimitsPage },
  { path: "/admin/audit", title: "감사 로그", Page: AdminAuditPage },
];

function renderRoute(Page: ComponentType) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { ...APP_QUERY_DEFAULTS, retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AdminLayout>
        <Page />
      </AdminLayout>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.resetAllMocks();
});

describe("관리 10화면 — 비인가 관점(§3.4-5, plan §8.3, #236)", () => {
  it.each(PAGES)(
    "$path: 일반 사용자(role=user)는 「/」로 이동하고 화면이 렌더되지 않으며 관리 API·쓰기 요청이 0건이다",
    async ({ title, Page }) => {
      mockApi("user");
      renderRoute(Page);
      await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
      // 늦게 시작하는 요청(폴링 등)이 없는지 — 30초를 진행해도 관리 API는 0건
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });
      expect(screen.queryByRole("heading", { level: 1, name: title })).toBeNull();
      expect(screen.queryByRole("navigation", { name: "위치" })).toBeNull();
      expect(adminGets()).toEqual([]);
      expect(writeCalls()).toEqual([]);
      expect(api.get.mock.calls.map((c) => c[0])).toEqual([ME_PATH]);
    },
  );

  it.each(PAGES)("$path: 관리자는 같은 하네스에서 화면 제목(h1)을 본다(양성 대조)", async ({ title, Page }) => {
    mockApi("admin");
    renderRoute(Page);
    expect(await screen.findByRole("heading", { level: 1, name: title })).toBeInTheDocument();
    await waitFor(() => expect(adminGets().length).toBeGreaterThan(0));
    expect(replace).not.toHaveBeenCalled();
  });
});
