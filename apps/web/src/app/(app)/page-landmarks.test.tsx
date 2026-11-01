/**
 * 셸 안 페이지의 랜드마크·폭 계약(plan §9 Phase 2 작업 2, AC-2.5의 페이지 부분, §6.6.1 템플릿 폭).
 *
 * 랜드마크 `main`은 셸의 `main#main-content` 하나뿐이어야 한다. 그래서 셸 안 페이지는 본문·로딩·오류 분기
 * 어디에서도 `<main>`을 그리지 않고, 최상위 래퍼로 `PageContainer`(div)를 쓴다. 폭 변형은 페이지 템플릿을
 * 따른다(목록·대시보드 wide / 상세 default / 설정·안내 narrow / 편집기·노트북 작업 화면 bleed). 편집기·작업
 * 화면의 로딩·오류 분기는 짧은 문구·스켈레톤이므로 패딩이 있는 default를 쓴다.
 *
 * 경계(`api` 클라이언트·App Router·토스트·`matchMedia`)만 대체하고 페이지·하위 컴포넌트·훅은 실제 코드다.
 * 응답을 정하지 않은 GET은 끝나지 않는 요청으로 두어 하위 패널이 로딩 상태에 머물게 한다.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ComponentType } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PageContainerWidth } from "@/components/layout/page-container";

const nav = vi.hoisted(() => ({
  search: "",
  router: { push: vi.fn(), replace: vi.fn(), back: vi.fn(), refresh: vi.fn(), prefetch: vi.fn() },
}));
vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "x1" }),
  useRouter: () => nav.router,
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(nav.search),
}));
vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));
// 정적 자산 경계: react-flow 스타일시트는 Vitest의 PostCSS 변환 대상이 아니다(기존 테스트와 같은 처리).
vi.mock("@xyflow/react/dist/style.css", () => ({}));

const { api } = vi.hoisted(() => ({
  api: {
    get: vi.fn(),
    getText: vi.fn(),
    getBlob: vi.fn(),
    post: vi.fn(),
    postForm: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    putBytes: vi.fn(),
    delete: vi.fn(),
    stream: vi.fn(),
  },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));

import NotebookPage from "@/app/(app)/notebooks/[id]/page";
import DashboardPage from "@/app/(app)/page";
import SkillEditPage from "@/app/(app)/skills/[id]/edit/page";
import SkillDetailPage from "@/app/(app)/skills/[id]/page";
import SkillsPage from "@/app/(app)/skills/page";
import WorkflowEditPage from "@/app/(app)/workflows/[id]/edit/page";
import WorkflowsPage from "@/app/(app)/workflows/page";
import { ApiError } from "@/lib/api-client";

const never = () => new Promise<never>(() => {});

/** 경로 → 응답(값) 또는 실패(ApiError). 표에 없는 GET은 끝나지 않는다. */
let routes: Record<string, unknown> = {};

beforeEach(() => {
  nav.search = "";
  routes = {};
  for (const fn of Object.values(api)) fn.mockImplementation(never);
  api.get.mockImplementation((path: string) => {
    if (!(path in routes)) return never();
    const value = routes[path];
    return value instanceof ApiError ? Promise.reject(value) : Promise.resolve(value);
  });
  // jsdom에는 matchMedia가 없다 — 노트북 작업 화면의 useMediaQuery(데스크톱 판정)가 쓴다.
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: true,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetAllMocks();
});

function renderPage(Page: ComponentType) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <Page />
    </QueryClientProvider>,
  );
}

/** 페이지가 `main`을 그리지 않고, 최상위 `PageContainer`의 폭이 기대값인지 본다. */
function expectPageWrapper(container: HTMLElement, width: PageContainerWidth) {
  expect(container.querySelector("main")).toBeNull();
  expect(screen.queryByRole("main")).toBeNull();
  const wrapper = container.querySelector<HTMLElement>("[data-slot=page-container]");
  expect(wrapper).not.toBeNull();
  expect(wrapper).toHaveAttribute("data-width", width);
  // 페이지 래퍼는 하나다(중첩 PageContainer 없음).
  expect(container.querySelectorAll("[data-slot=page-container]")).toHaveLength(1);
}

const NOTEBOOK = {
  id: "x1",
  owner_id: "me",
  title: "랜드마크 노트북",
  emoji: "📓",
  description: "",
  visibility: "private",
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
  source_count: 0,
  is_owner: true,
  permission: "owner",
  tags: [],
  is_favorite: false,
};

const SKILL = {
  id: "x1",
  name: "보고서 스킬",
  description: "설명",
  slug: "report",
  visibility: "private",
  origin: "authored",
  can_edit: true,
  can_run: true,
  current_version_id: "v1",
  published_version_id: null,
};

const WORKFLOW = {
  id: "x1",
  name: "요약 워크플로우",
  description: "",
  icon: "",
  kind: "workflow",
  definition: { nodes: [], edges: [] },
  params_schema: {},
  scope: "user",
  owner_id: "me",
  enabled: false,
  version: 1,
  can_edit: true,
};

describe("셸 안 페이지는 main 대신 PageContainer를 최상위 래퍼로 쓴다(AC-2.5 페이지 부분)", () => {
  describe("목록·대시보드(T1·T5) — wide", () => {
    it("노트북 홈 /", async () => {
      routes = { "/api/v1/notebooks?filter=all": [], "/api/v1/tags": [] };
      const { container } = renderPage(DashboardPage);
      await screen.findByText("아직 노트북이 없습니다");
      expectPageWrapper(container, "wide");
    });

    it("스킬 목록 /skills", () => {
      const { container } = renderPage(SkillsPage);
      expect(screen.getByRole("heading", { name: "스킬" })).toBeInTheDocument();
      expectPageWrapper(container, "wide");
    });

    it("워크플로우 목록 /workflows", () => {
      const { container } = renderPage(WorkflowsPage);
      expect(screen.getByRole("heading", { name: "워크플로우" })).toBeInTheDocument();
      expectPageWrapper(container, "wide");
    });
  });

  describe("스킬 상세 /skills/[id] — default, 로딩·오류·본문", () => {
    it("로딩 분기", () => {
      const { container } = renderPage(SkillDetailPage);
      expectPageWrapper(container, "default");
    });

    it("오류 분기", async () => {
      routes = { "/api/v1/skills/x1": new ApiError(404, "not found") };
      const { container } = renderPage(SkillDetailPage);
      await screen.findByText("스킬을 찾을 수 없습니다");
      expectPageWrapper(container, "default");
    });

    it("본문 분기", async () => {
      routes = { "/api/v1/skills/x1": SKILL };
      const { container } = renderPage(SkillDetailPage);
      await screen.findByRole("heading", { name: "보고서 스킬" });
      expectPageWrapper(container, "default");
    });
  });

  describe("스킬 편집 /skills/[id]/edit — 편집기 bleed, 로딩·권한 없음은 default", () => {
    it("로딩 분기", () => {
      const { container } = renderPage(SkillEditPage);
      expectPageWrapper(container, "default");
    });

    it("권한 없음 분기", async () => {
      routes = { "/api/v1/skills/x1": { ...SKILL, can_edit: false } };
      const { container } = renderPage(SkillEditPage);
      await screen.findByText("이 스킬을 편집할 권한이 없습니다");
      expectPageWrapper(container, "default");
    });

    it("편집기 분기", async () => {
      routes = { "/api/v1/skills/x1": SKILL };
      const { container } = renderPage(SkillEditPage);
      await waitFor(() =>
        expect(container.querySelector("[data-slot=page-container]")).toHaveAttribute(
          "data-width",
          "bleed",
        ),
      );
      expectPageWrapper(container, "bleed");
    });
  });

  describe("워크플로우 편집 /workflows/[id]/edit — 편집기 bleed, 로딩·오류·권한 없음은 default", () => {
    it("로딩 분기", () => {
      const { container } = renderPage(WorkflowEditPage);
      expectPageWrapper(container, "default");
    });

    it("오류 분기", async () => {
      routes = { "/api/v1/workflows/x1": new ApiError(404, "not found") };
      const { container } = renderPage(WorkflowEditPage);
      await screen.findByText("워크플로우를 찾을 수 없습니다");
      expectPageWrapper(container, "default");
    });

    it("권한 없음 분기", async () => {
      routes = { "/api/v1/workflows/x1": { ...WORKFLOW, can_edit: false } };
      const { container } = renderPage(WorkflowEditPage);
      await screen.findByText("이 워크플로우를 편집할 권한이 없습니다");
      expectPageWrapper(container, "default");
    });

    it("편집기 분기", async () => {
      routes = { "/api/v1/workflows/x1": WORKFLOW };
      const { container } = renderPage(WorkflowEditPage);
      await screen.findByDisplayValue("요약 워크플로우");
      expectPageWrapper(container, "bleed");
    });
  });

  describe("노트북 작업 화면 /notebooks/[id] — bleed, 로딩·오류는 default", () => {
    it("로딩 분기", () => {
      const { container } = renderPage(NotebookPage);
      expectPageWrapper(container, "default");
    });

    it("오류 분기", async () => {
      routes = { "/api/v1/notebooks/x1": new ApiError(404, "not found") };
      const { container } = renderPage(NotebookPage);
      await screen.findByText("노트북을 찾을 수 없습니다.");
      expectPageWrapper(container, "default");
    });

    it("작업 화면 분기", async () => {
      routes = { "/api/v1/notebooks/x1": NOTEBOOK };
      const { container } = renderPage(NotebookPage);
      await waitFor(() =>
        expect(container.querySelector("[data-slot=page-container]")).toHaveAttribute(
          "data-width",
          "bleed",
        ),
      );
      expectPageWrapper(container, "bleed");
    });
  });
});

/**
 * 세로 스크롤의 주인은 페이지 하나다(P2 통합 — shell IR-S4·admin IR-2). 셸 `main`은 확정 높이만 주고
 * 스크롤 영역이 아니므로(`overflow-hidden`), 길어질 수 있는 본문 분기는 최상위에 `div.h-full.overflow-y-auto`를 두고
 * 그 안에 `PageContainer`를 둔다. 관리 화면은 `admin/layout.tsx`의 스크롤 div(layout.test.tsx), bleed는 내부 스크롤.
 */
function expectScrollOwner(container: HTMLElement) {
  const wrapper = container.querySelector<HTMLElement>("[data-slot=page-container]");
  expect(wrapper).not.toBeNull();
  const owner = wrapper?.parentElement ?? null;
  expect(owner).not.toBeNull();
  expect(owner).toHaveClass("h-full", "overflow-y-auto");
  // 스크롤 div가 페이지의 최상위 요소다(그 위에 또 다른 높이·스크롤 래퍼가 없다)
  expect(owner?.parentElement).toBe(container);
}

describe("본문 분기는 세로 스크롤을 스스로 소유한다(셸 main은 스크롤 영역이 아님 — P2 통합 IR-S4)", () => {
  it.each([
    ["노트북 홈 /", DashboardPage],
    ["스킬 목록 /skills", SkillsPage],
    ["워크플로우 목록 /workflows", WorkflowsPage],
  ] as const)("%s", (_name, Page) => {
    const { container } = renderPage(Page);
    expectScrollOwner(container);
  });

  it("스킬 상세 /skills/[id] 본문 분기", async () => {
    routes = { "/api/v1/skills/x1": SKILL };
    const { container } = renderPage(SkillDetailPage);
    await screen.findByRole("heading", { name: "보고서 스킬" });
    expectScrollOwner(container);
  });

});
