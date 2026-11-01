import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ADMIN_OVERVIEW_PATHS } from "@/hooks/use-admin-overview";
import { APP_QUERY_DEFAULTS } from "@/lib/query-defaults";
import AdminLayout from "./layout";
import AdminOverviewPage from "./page";

const { api, replace } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
  replace: vi.fn(),
}));

// 네트워크 경계(api)와 프레임워크 경계(next/navigation)만 바꾼다 — 가드·개요 코드는 실제로 실행한다.
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn(), back: vi.fn(), refresh: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/admin",
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

/** `me`는 주어진 역할로, 관리 API는 모두 빈 배열로 응답한다(빈 배열은 10개 응답 모두에 유효한 모양). */
function mockApi(meResponse: Promise<unknown>) {
  api.get.mockImplementation((p: string) => (p === ME_PATH ? meResponse : Promise.resolve([])));
}

const adminCalls = () =>
  api.get.mock.calls.map((c) => c[0] as string).filter((p) => p.startsWith("/api/v1/admin/"));

function renderOverviewRoute() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { ...APP_QUERY_DEFAULTS } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <AdminLayout>
        <AdminOverviewPage />
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

describe("AdminLayout 가드 — 비인가 관점(§3.4-5, plan §8.3)", () => {
  it("일반 사용자(role=user)는 「/」로 replace되고 관리 개요가 렌더되지 않으며 관리 API 요청이 0건이다", async () => {
    mockApi(Promise.resolve(me("user")));
    renderOverviewRoute();
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
    // 지연된 요청이 없는지 — 60초를 진행해도 관리 API는 0건
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(screen.queryByRole("heading", { name: "관리 개요" })).toBeNull();
    expect(screen.queryByTestId("overview-attention")).toBeNull();
    expect(adminCalls()).toEqual([]);
    expect(api.get.mock.calls.map((c) => c[0])).toEqual([ME_PATH]);
  });

  it("사용자 확인 전(me 응답 대기)에도 개요를 마운트하지 않는다 — 관리 API 0건, 이동 없음", async () => {
    mockApi(new Promise(() => {}));
    renderOverviewRoute();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(screen.queryByRole("heading", { name: "관리 개요" })).toBeNull();
    expect(adminCalls()).toEqual([]);
    expect(replace).not.toHaveBeenCalled();
  });

  it("관리자는 개요를 렌더하고(양성 대조) 개요의 GET 9건을 보낸다 — 이동 없음", async () => {
    mockApi(Promise.resolve(me("admin")));
    renderOverviewRoute();
    expect(await screen.findByRole("heading", { level: 1, name: "관리 개요" })).toBeInTheDocument();
    await waitFor(() => expect(adminCalls()).toHaveLength(9));
    expect(new Set(adminCalls())).toEqual(new Set(Object.values(ADMIN_OVERVIEW_PATHS)));
    expect(replace).not.toHaveBeenCalled();
  });
});

describe("AdminLayout 랜드마크·폭(AC-2.5 페이지 부분, §6.5)", () => {
  it("관리 레이아웃은 main을 만들지 않고 PageContainer(wide) 안에 관리 화면을 둔다", async () => {
    mockApi(Promise.resolve(me("admin")));
    const { container } = renderOverviewRoute();
    const heading = await screen.findByRole("heading", { level: 1, name: "관리 개요" });
    expect(container.querySelector("main")).toBeNull();
    const pageContainer = heading.closest('[data-slot="page-container"]');
    expect(pageContainer).not.toBeNull();
    expect(pageContainer).toHaveAttribute("data-width", "wide");
    // 셸 안에서 세로 스크롤은 이 영역이 맡는다(셸의 콘텐츠 칸은 overflow-hidden)
    expect(pageContainer!.parentElement).toHaveAttribute("data-slot", "admin-scroll");
    expect(pageContainer!.parentElement).toHaveClass("overflow-y-auto");
  });
});

describe("라우트 구조 — 관리 개요는 AdminLayout 밖에서 마운트되지 않는다(plan §8.3 ③)", () => {
  const SRC = path.resolve(__dirname, "../../..");
  const ADMIN_DIR = __dirname;

  function walk(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const full = path.join(dir, name);
      if (statSync(full).isDirectory()) return walk(full);
      return /\.(ts|tsx)$/.test(name) ? [full] : [];
    });
  }

  const sources = walk(SRC).filter((f) => !/\.test\.(ts|tsx)$/.test(f));
  const rel = (f: string) => path.relative(SRC, f);

  it("개요 페이지는 app/(app)/admin/page.tsx이고 같은 폴더에 가드 레이아웃이 있다", () => {
    expect(rel(path.join(ADMIN_DIR, "page.tsx"))).toBe(path.join("app", "(app)", "admin", "page.tsx"));
    expect(existsSync(path.join(ADMIN_DIR, "layout.tsx"))).toBe(true);
    const layout = readFileSync(path.join(ADMIN_DIR, "layout.tsx"), "utf8");
    expect(layout).toMatch(/me\.data\.role !== "admin"\) router\.replace\("\/"\)/);
    expect(layout).toMatch(/if \(!me\.data \|\| me\.data\.role !== "admin"\) \{/);
  });

  it("개요 페이지 모듈과 개요 쿼리 훅을 가져오는 소스는 개요 페이지 하나뿐이다", () => {
    const importsPage = sources.filter((f) =>
      /from\s+["'](@\/app\/\(app\)\/admin\/page|\.\.?\/(admin\/)?page)["']/.test(readFileSync(f, "utf8")),
    );
    expect(importsPage.map(rel)).toEqual([]);
    const importsHook = sources.filter((f) =>
      readFileSync(f, "utf8").includes("@/hooks/use-admin-overview"),
    );
    expect(importsHook.map(rel)).toEqual([path.join("app", "(app)", "admin", "page.tsx")]);
  });
});
