import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api-client";
import type { User } from "@/lib/types";
import AppLayout from "./layout";

// API 경계만 mock(AC-3.7) — api 객체만 대체하고 ApiError는 실제 모듈.
const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
// 프레임워크 경계: App Router 대역
const nav = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn(), pathname: "/workflows", search: "" }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: nav.replace, push: nav.push }),
  usePathname: () => nav.pathname,
  useSearchParams: () => new URLSearchParams(nav.search),
}));

const ME: User = {
  id: "u1",
  email: "user@example.com",
  name: "사용자",
  role: "user",
  is_active: true,
  must_change_password: false,
  created_at: "2026-10-10T00:00:00Z",
};

function renderLayout(me: unknown | Error) {
  api.get.mockImplementation((path: string) => {
    if (path === "/api/v1/auth/me") {
      return me instanceof Error ? Promise.reject(me) : Promise.resolve(me);
    }
    return Promise.resolve([]); // 셸의 목록 조회(노트북·세션 등)
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AppLayout>
        <div data-testid="page-body">본문</div>
      </AppLayout>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  nav.pathname = "/workflows";
  nav.search = "tab=mine";
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
    })),
  );
});

afterEach(() => {
  vi.resetAllMocks();
  vi.unstubAllGlobals();
});

describe("(app) 레이아웃 — 비밀번호 강제 변경 유도 (AC-3.2)", () => {
  it("me.must_change_password=true면 앱 셸을 렌더하지 않고 변경 화면으로 replace한다(현재 경로를 redirect로)", async () => {
    renderLayout({ ...ME, must_change_password: true });

    await waitFor(() =>
      expect(nav.replace).toHaveBeenCalledWith(
        `/change-password?redirect=${encodeURIComponent("/workflows?tab=mine")}`,
      ),
    );
    expect(screen.queryByTestId("page-body")).not.toBeInTheDocument();
    expect(screen.queryByTestId("user-menu")).not.toBeInTheDocument();
  });

  it("must_change_password=false면 앱 셸과 본문을 렌더하고 이동하지 않는다", async () => {
    renderLayout(ME);

    expect(await screen.findByTestId("page-body")).toBeInTheDocument();
    expect(screen.getByTestId("user-menu")).toBeInTheDocument();
    expect(nav.replace).not.toHaveBeenCalled();
  });

  it("구 API 응답(키 없음)도 강제 변경으로 읽지 않는다", async () => {
    const legacy: Record<string, unknown> = { ...ME };
    delete legacy.must_change_password;
    renderLayout(legacy);

    expect(await screen.findByTestId("page-body")).toBeInTheDocument();
    expect(nav.replace).not.toHaveBeenCalled();
  });

  it("401은 기존처럼 로그인으로 보낸다(redirect 유지)", async () => {
    renderLayout(new ApiError(401, "Unauthorized"));

    await waitFor(() =>
      expect(nav.replace).toHaveBeenCalledWith(
        `/login?redirect=${encodeURIComponent("/workflows?tab=mine")}`,
      ),
    );
    expect(screen.queryByTestId("page-body")).not.toBeInTheDocument();
  });

  it("홈(/)에서 강제 변경이면 redirect 쿼리 없이 변경 화면으로 간다", async () => {
    nav.pathname = "/";
    nav.search = "";
    renderLayout({ ...ME, must_change_password: true });

    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/change-password"));
  });
});

// AC-2.5 · §10.2 「중첩 main」 허용 목록 근거(E-P2P-1 판정 — P2 통합): 셸 밖 화면의 `main`(인증 확인 실패 폴백
// `components/auth-error.tsx`, 인증 스켈레톤)은 셸과 함께 렌더되지 않는다. 어느 분기에서든 랜드마크 main은 하나다.
describe("(app) 레이아웃 — 랜드마크 main은 어느 분기에서든 하나다(AC-2.5, E-P2P-1)", () => {
  it("인증 확인이 401 외 사유로 실패하면 폴백 화면의 main 하나만 — 셸(main#main-content·사이드바·사용자 메뉴)과 본문은 없다", async () => {
    renderLayout(new ApiError(503, "Service Unavailable"));

    expect(await screen.findByText(/서버에 연결하지 못했습니다/)).toBeInTheDocument();
    const mains = document.querySelectorAll("main");
    expect(mains).toHaveLength(1);
    expect(mains[0]).toContainElement(screen.getByTestId("auth-retry"));
    expect(document.getElementById("main-content")).toBeNull();
    expect(screen.queryByTestId("app-sidebar")).not.toBeInTheDocument();
    expect(screen.queryByTestId("user-menu")).not.toBeInTheDocument();
    expect(screen.queryByTestId("page-body")).not.toBeInTheDocument();
    expect(nav.replace).not.toHaveBeenCalled();
  });

  it("인증 확인 중(스켈레톤)에도 main은 하나이고 셸·본문은 없다", () => {
    api.get.mockImplementation(() => new Promise(() => {}));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <AppLayout>
          <div data-testid="page-body">본문</div>
        </AppLayout>
      </QueryClientProvider>,
    );
    expect(document.querySelectorAll("main")).toHaveLength(1);
    expect(document.getElementById("main-content")).toBeNull();
    expect(screen.queryByTestId("page-body")).not.toBeInTheDocument();
  });

  it("셸이 그려지면 main은 셸의 main#main-content 하나이고 폴백 화면은 없다", async () => {
    renderLayout(ME);

    expect(await screen.findByTestId("page-body")).toBeInTheDocument();
    const mains = document.querySelectorAll("main");
    expect(mains).toHaveLength(1);
    expect(mains[0]).toHaveAttribute("id", "main-content");
    expect(mains[0]).toContainElement(screen.getByTestId("page-body"));
    expect(screen.queryByTestId("auth-retry")).not.toBeInTheDocument();
  });
});
