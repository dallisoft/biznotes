import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ADMIN_OPEN_STORAGE_KEY, readAdminOpen } from "@/components/shell/admin-nav-group";
import { AppShell, sidebarPanelSizes } from "@/components/shell/app-shell";
import { ADMIN_NAV, MAIN_NAV, isNavActive } from "@/components/shell/nav-items";
import {
  SIDEBAR_NOTEBOOK_LIMIT,
  SIDEBAR_SESSION_LIMIT,
  selectSidebarNotebooks,
} from "@/components/shell/notebook-nav-section";
import { revealWithin } from "@/components/shell/sidebar-content";
import type { Notebook, User } from "@/lib/types";
import { useCommandPalette } from "@/stores/command-palette";

// API 경계만 mock(§3.3-3 R6) — api 객체만 대체하고 ApiError 등은 실제 모듈.
const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
// 프레임워크 경계: App Router 대역
const nav = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn(), pathname: "/", search: "" }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: nav.push, replace: nav.replace, prefetch: vi.fn() }),
  usePathname: () => nav.pathname,
  useSearchParams: () => new URLSearchParams(nav.search),
}));

const ADMIN: User = {
  id: "u-admin",
  email: "admin@example.com",
  name: "관리자",
  role: "admin",
  is_active: true,
  must_change_password: false,
  created_at: "2026-10-10T00:00:00Z",
};
const MEMBER: User = { ...ADMIN, id: "u-member", email: "member@example.com", name: "일반", role: "user" };

function notebook(i: number, over: Partial<Notebook> = {}): Notebook {
  const day = String(1 + (i % 28)).padStart(2, "0");
  return {
    id: `nb-${i}`,
    owner_id: "u-admin",
    title: `노트북 ${i}`,
    emoji: "📓",
    description: "",
    visibility: "private",
    created_at: "2026-09-01T00:00:00Z",
    updated_at: `2026-09-${day}T00:00:00Z`,
    source_count: 0,
    is_owner: true,
    permission: "owner",
    tags: [],
    is_favorite: false,
    ...over,
  };
}

type Fixture = {
  notebooks?: Notebook[];
  sessions?: { id: string; title: string }[];
  /** true면 노트북 목록 조회가 실패한다 */
  notebooksFail?: boolean;
  /** true면 앱 버전 조회가 실패한다 */
  versionsFail?: boolean;
};

const VERSIONS = { app_version: "0.6.0" };

function mockApi({ notebooks = [], sessions = [], notebooksFail = false, versionsFail = false }: Fixture = {}) {
  api.get.mockImplementation((path: string) => {
    if (path === "/api/v1/notebooks?filter=all") {
      return notebooksFail ? Promise.reject(new Error("boom")) : Promise.resolve(notebooks);
    }
    if (path === "/api/v1/meta/versions") {
      return versionsFail ? Promise.reject(new Error("boom")) : Promise.resolve(VERSIONS);
    }
    if (/^\/api\/v1\/notebooks\/[^/]+\/chat\/sessions$/.test(path)) return Promise.resolve(sessions);
    return Promise.resolve([]);
  });
}

function stubViewport(desktop: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: query.includes("min-width: 768px") ? desktop : false,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
    })),
  );
}

function renderShell({
  me = ADMIN,
  pathname = "/",
  search = "",
  desktop = true,
  ...fixture
}: { me?: User; pathname?: string; search?: string; desktop?: boolean } & Fixture = {}) {
  nav.pathname = pathname;
  nav.search = search;
  stubViewport(desktop);
  mockApi(fixture);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AppShell me={me}>
        <div data-testid="page-body">본문</div>
      </AppShell>
    </QueryClientProvider>,
  );
}

const scrollIntoView = vi.fn();
// jsdom엔 레이아웃이 없다 — 스크롤 영역은 화면 y 100~400, 활성 항목은 y 600~632로 보고한다
const ACTIVE_TOP = 600;
const realRect = HTMLElement.prototype.getBoundingClientRect;

function rect(top: number, bottom: number): DOMRect {
  return { top, bottom, left: 0, right: 200, width: 200, height: bottom - top, x: 0, y: top, toJSON: () => ({}) };
}

beforeEach(() => {
  // 셸 코드가 아니라 jsdom에 없는 브라우저 환경(레이아웃·scrollIntoView)만 보충한다
  Element.prototype.scrollIntoView = scrollIntoView;
  HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
    if (this.dataset.slot === "sidebar-scroll") return rect(100, 400);
    if (this.getAttribute("aria-current") === "page") return rect(ACTIVE_TOP, ACTIVE_TOP + 32);
    return rect(0, 0);
  };
});

afterEach(() => {
  HTMLElement.prototype.getBoundingClientRect = realRect;
  vi.resetAllMocks();
  vi.unstubAllGlobals();
  window.localStorage.clear();
  useCommandPalette.setState({ open: false });
});

function primaryNavOf(container: HTMLElement) {
  return within(within(container).getByTestId("primary-nav"))
    .getAllByRole("link")
    .map((a) => [a.textContent?.trim(), a.getAttribute("href")]);
}

async function openDrawer() {
  await userEvent.click(screen.getByRole("button", { name: "메뉴 열기" }));
  return screen.findByTestId("nav-drawer");
}

describe("AC-2.1 한 셸 — 경로와 무관하게 주 메뉴가 같다 (DP-1)", () => {
  const EXPECTED = [
    ["노트북", "/"],
    ["스킬", "/skills"],
    ["워크플로우", "/workflows"],
  ];

  it.each(["/", "/skills", "/notebooks/x", "/admin/llm", "/workflows"])(
    "%s: 주 메뉴 항목(라벨·href·순서)이 같고, 사이드바 교체·「메인 메뉴로 돌아가기」가 없다",
    async (pathname) => {
      renderShell({ pathname });
      const sidebar = screen.getByTestId("app-sidebar");
      expect(primaryNavOf(sidebar)).toEqual(EXPECTED);
      expect(within(sidebar).getByRole("navigation", { name: "주 메뉴" })).toBeInTheDocument();
      expect(screen.queryByText("메인 메뉴로 돌아가기")).not.toBeInTheDocument();
      expect(screen.queryByText("어드민 콘솔")).not.toBeInTheDocument();
      // 관리 경로에서도 노트북 섹션과 사용자 메뉴가 그대로 있다(셸 교체 0회)
      expect(within(sidebar).getByRole("group", { name: "노트북" })).toBeInTheDocument();
      expect(within(sidebar).getByTestId("user-menu")).toBeInTheDocument();
    },
  );

  it("주 메뉴 정의는 3개다", () => {
    expect(MAIN_NAV.map((i) => [i.label, i.href])).toEqual(EXPECTED);
  });

  it("활성 항목만 aria-current=page — 접두어는 경계까지 본다", () => {
    renderShell({ pathname: "/skills/abc" });
    const sidebar = screen.getByTestId("app-sidebar");
    expect(within(sidebar).getByRole("link", { name: "스킬" })).toHaveAttribute("aria-current", "page");
    expect(within(sidebar).getByRole("link", { name: "노트북" })).not.toHaveAttribute("aria-current");
    expect(isNavActive("/skillsX", { href: "/skills" })).toBe(false);
    expect(isNavActive("/admin/llm", { href: "/admin", exact: true })).toBe(false);
    expect(isNavActive("/admin", { href: "/admin", exact: true })).toBe(true);
  });

  it("/admin(관리 개요)에서는 「개요」만 활성이고 다른 관리 항목은 자기 접두어에서만 활성이다", () => {
    renderShell({ pathname: "/admin" });
    const group = within(screen.getByTestId("app-sidebar")).getByTestId("admin-nav-group");
    const active = within(group)
      .getAllByRole("link")
      .filter((a) => a.getAttribute("aria-current") === "page")
      .map((a) => a.textContent?.trim());
    expect(active).toEqual(["개요"]);
  });
});

describe("AC-2.2 관리 진입점 노출 경계 (Tier 1 — 비인가 사용자 관점)", () => {
  const ADMIN_EXPECTED = [
    ["개요", "/admin"],
    ["사용자·그룹", "/admin/users"],
    ["LLM 게이트웨이", "/admin/llm"],
    ["RAG", "/admin/rag"],
    ["에이전트", "/admin/agents"],
    ["스킬 공개", "/admin/skills"],
    ["MCP 서버", "/admin/mcp"],
    ["사용 제한", "/admin/limits"],
    ["감사 로그", "/admin/audit"],
  ];

  function adminLinksIn(root: HTMLElement) {
    return Array.from(root.querySelectorAll<HTMLAnchorElement>("a[href]")).filter((a) => {
      const href = a.getAttribute("href") ?? "";
      return href === "/admin" || href.startsWith("/admin/");
    });
  }

  it.each(["/", "/admin/llm", "/admin"])(
    "role user(%s) — 데스크톱 사이드바에 「관리」 그룹도 /admin 링크도 0개",
    async (pathname) => {
      renderShell({ me: MEMBER, pathname });
      await screen.findByText("아직 노트북이 없습니다");
      expect(screen.queryByRole("button", { name: "관리" })).not.toBeInTheDocument();
      expect(screen.queryByTestId("admin-nav-group")).not.toBeInTheDocument();
      expect(adminLinksIn(document.body)).toHaveLength(0);
    },
  );

  it("role user — 모바일 드로어에도 「관리」 그룹·/admin 링크 0개", async () => {
    renderShell({ me: MEMBER, pathname: "/", desktop: false });
    const drawer = await openDrawer();
    await within(drawer).findByText("아직 노트북이 없습니다");
    expect(within(drawer).queryByRole("button", { name: "관리" })).not.toBeInTheDocument();
    expect(adminLinksIn(document.body)).toHaveLength(0);
  });

  it("role admin — 관리 경로에서는 기본 펼침이고 9개 하위 링크가 정해진 순서로 있다(현재 항목만 활성)", () => {
    renderShell({ pathname: "/admin/llm" });
    const sidebar = screen.getByTestId("app-sidebar");
    const toggle = within(sidebar).getByRole("button", { name: "관리" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const group = within(sidebar).getByTestId("admin-nav-group");
    const links = within(group).getAllByRole("link");
    expect(links.map((a) => [a.textContent?.trim(), a.getAttribute("href")])).toEqual(ADMIN_EXPECTED);
    expect(within(group).getByRole("link", { name: "LLM 게이트웨이" })).toHaveAttribute("aria-current", "page");
    expect(within(group).getByRole("link", { name: "개요" })).not.toHaveAttribute("aria-current");
    expect(ADMIN_NAV).toHaveLength(9);
  });

  it("role admin — 관리 밖 경로에서는 기본 접힘, 펼치면 9개가 보이고 선택이 localStorage에 남는다", async () => {
    renderShell({ pathname: "/skills" });
    const sidebar = screen.getByTestId("app-sidebar");
    const toggle = within(sidebar).getByRole("button", { name: "관리" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(adminLinksIn(sidebar)).toHaveLength(0);

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveAttribute("aria-controls");
    expect(adminLinksIn(sidebar).map((a) => a.getAttribute("href"))).toEqual(ADMIN_EXPECTED.map(([, h]) => h));
    expect(window.localStorage.getItem(ADMIN_OPEN_STORAGE_KEY)).toBe("true");

    await userEvent.click(toggle);
    expect(window.localStorage.getItem(ADMIN_OPEN_STORAGE_KEY)).toBe("false");
    expect(adminLinksIn(sidebar)).toHaveLength(0);
  });

  it("저장된 펼침 상태가 경로 기본값보다 우선한다(관리 경로에서 접어 둔 경우)", async () => {
    window.localStorage.setItem(ADMIN_OPEN_STORAGE_KEY, "false");
    renderShell({ pathname: "/admin/users" });
    const toggle = within(screen.getByTestId("app-sidebar")).getByRole("button", { name: "관리" });
    // 첫 렌더부터 저장값을 쓴다(펼쳤다 접히는 깜박임 없음)
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("link", { name: "사용자·그룹" })).not.toBeInTheDocument();
  });

  it("관리 밖 경로에서도 저장값(펼침)이 있으면 첫 렌더부터 펼친다", () => {
    window.localStorage.setItem(ADMIN_OPEN_STORAGE_KEY, "true");
    renderShell({ pathname: "/" });
    const toggle = within(screen.getByTestId("app-sidebar")).getByRole("button", { name: "관리" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(within(screen.getByTestId("admin-nav-group")).getAllByRole("link")).toHaveLength(9);
  });

  it("저장값이 손상됐거나 저장소 접근이 막혀도 기본값으로 동작한다(§11.1)", () => {
    window.localStorage.setItem(ADMIN_OPEN_STORAGE_KEY, "{broken");
    expect(readAdminOpen()).toBeNull();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    expect(readAdminOpen()).toBeNull();
    renderShell({ pathname: "/admin/rag" });
    const toggle = within(screen.getByTestId("app-sidebar")).getByRole("button", { name: "관리" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(toggle); // 저장 실패해도 화면 상태는 바뀐다
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("role admin — 드로어에서도 같은 9개(펼친 뒤)가 같은 순서로 있다", async () => {
    renderShell({ pathname: "/", desktop: false });
    const drawer = await openDrawer();
    const toggle = within(drawer).getByRole("button", { name: "관리" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(toggle);
    const links = within(within(drawer).getByTestId("admin-nav-group")).getAllByRole("link");
    expect(links.map((a) => [a.textContent?.trim(), a.getAttribute("href")])).toEqual(ADMIN_EXPECTED);
  });
});

describe("AC-2.6 사이드바 노트북 섹션", () => {
  // 12개: 3·7·11 즐겨찾기, updated_at은 i가 클수록 최근(1~12일)
  const MANY = Array.from({ length: 12 }, (_, k) =>
    notebook(k + 1, {
      updated_at: `2026-09-${String(k + 1).padStart(2, "0")}T00:00:00Z`,
      is_favorite: [3, 7, 11].includes(k + 1),
    }),
  );
  const ORDER = ["노트북 11", "노트북 7", "노트북 3", "노트북 12", "노트북 10", "노트북 9", "노트북 8", "노트북 6", "노트북 5", "노트북 4"];

  function notebookLinks() {
    const list = screen.getByTestId("sidebar-notebook-list");
    return within(list)
      .getAllByRole("link")
      .filter((a) => a.getAttribute("href")?.match(/^\/notebooks\/[^?]+$/));
  }

  it("즐겨찾기 먼저·최근 수정 순·최대 10개, 장식 화살표 없음, 「모든 노트북 보기 (N)」", async () => {
    renderShell({ pathname: "/", notebooks: [...MANY].reverse() });
    await screen.findByTestId("sidebar-notebook-list");
    const links = notebookLinks();
    expect(links.map((a) => a.textContent?.replace("📓", "").trim())).toEqual(ORDER);
    expect(links).toHaveLength(SIDEBAR_NOTEBOOK_LIMIT);
    // 장식용 > 화살표(svg)를 그리지 않는다 — 이름은 「이모지 제목」
    for (const a of links) expect(a.querySelector("svg")).toBeNull();
    expect(links[0]).toHaveAccessibleName("📓 노트북 11");
    const all = screen.getByRole("link", { name: "모든 노트북 보기 (12)" });
    expect(all).toHaveAttribute("href", "/");
    // 머리글 「노트북」은 heading이 아니다(e2e exact heading과 충돌 방지)
    expect(screen.queryByRole("heading", { name: "노트북" })).not.toBeInTheDocument();
  });

  it("현재 노트북이 10위 밖이면 맨 위에 추가로 보이고 활성이다(11개)", async () => {
    renderShell({ pathname: "/notebooks/nb-1", notebooks: MANY });
    await screen.findByTestId("sidebar-notebook-list");
    const links = notebookLinks();
    expect(links).toHaveLength(SIDEBAR_NOTEBOOK_LIMIT + 1);
    expect(links[0]).toHaveAttribute("href", "/notebooks/nb-1");
    expect(links[0]).toHaveAttribute("aria-current", "page");
    expect(links.slice(1).map((a) => a.textContent?.replace("📓", "").trim())).toEqual(ORDER);
  });

  it("현재 노트북이 10위 안이면 중복 없이 제자리에서 활성이고, 그 아래 대화는 최대 5개다(새 대화 아이콘 없음)", async () => {
    const sessions = Array.from({ length: 7 }, (_, k) => ({ id: `s${k}`, title: `대화 ${k}` }));
    renderShell({ pathname: "/notebooks/nb-12", search: "s=s1", notebooks: MANY, sessions });
    const sessionList = await screen.findByTestId("sidebar-session-list");
    const links = notebookLinks();
    expect(links).toHaveLength(SIDEBAR_NOTEBOOK_LIMIT);
    expect(links.filter((a) => a.getAttribute("aria-current") === "page").map((a) => a.getAttribute("href"))).toEqual([
      "/notebooks/nb-12",
    ]);
    const sessionLinks = within(sessionList).getAllByRole("link");
    expect(sessionLinks).toHaveLength(SIDEBAR_SESSION_LIMIT);
    expect(sessionLinks[1]).toHaveAttribute("href", "/notebooks/nb-12?s=s1");
    expect(sessionLinks[1]).toHaveAttribute("aria-current", "true");
    expect(screen.queryByRole("button", { name: "새 대화" })).not.toBeInTheDocument();
  });

  it("「새 노트북」(create-notebook-sidebar)은 「+」 아이콘 버튼이고 노트북 생성 다이얼로그를 연다", async () => {
    renderShell({ pathname: "/skills", notebooks: MANY.slice(0, 2) });
    const trigger = screen.getByTestId("create-notebook-sidebar");
    expect(trigger).toHaveAccessibleName("새 노트북");
    // §6.2.2 ⑤ — 보조 진입점은 「+」 아이콘 버튼: 보이는 글자 없이 aria-label로 이름을 갖고, 아이콘은 장식이다
    expect(trigger).toHaveAttribute("aria-label", "새 노트북");
    expect(trigger.textContent?.trim()).toBe("");
    expect(trigger.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    expect(trigger).toHaveClass("coarse:size-11");
    await userEvent.click(trigger);
    expect(await screen.findByRole("dialog", { name: "새 노트북 만들기" })).toBeInTheDocument();
    expect(screen.getByLabelText("제목")).toBeInTheDocument();
  });

  it("목록이 비면 빈 상태이고 「모든 노트북 보기」는 없다", async () => {
    renderShell({ pathname: "/" });
    expect(await screen.findByText("아직 노트북이 없습니다")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /모든 노트북 보기/ })).not.toBeInTheDocument();
  });

  it("조회 실패면 빈 상태(0개)로 단정하지 않고 오류 문구 + 「다시 시도」 → 재조회", async () => {
    renderShell({ pathname: "/", notebooksFail: true });
    expect(await screen.findByText("노트북 목록을 불러오지 못했습니다")).toBeInTheDocument();
    expect(screen.queryByText("아직 노트북이 없습니다")).not.toBeInTheDocument();
    const listCalls = () => api.get.mock.calls.filter(([p]) => p === "/api/v1/notebooks?filter=all").length;
    const before = listCalls();
    await userEvent.click(screen.getByRole("button", { name: "다시 시도" }));
    await waitFor(() => expect(listCalls()).toBe(before + 1));
  });

  // P2 리뷰 P2-03: 오류 분기의 「다시 시도」도 터치 기기(coarse)에서 44×44 — 사이드바·드로어 같은 컴포넌트
  it.each([true, false])("desktop=%s: 조회 실패의 「다시 시도」는 터치 기기에서 높이·폭 44px 이상이다", async (desktop) => {
    renderShell({ pathname: "/", notebooksFail: true, desktop });
    const root = desktop ? screen.getByTestId("app-sidebar") : await openDrawer();
    await within(root).findByText("노트북 목록을 불러오지 못했습니다");
    const retry = within(root).getByRole("button", { name: "다시 시도" });
    expect(retry.className).toMatch(/(^|\s)coarse:(min-h|h|size)-11(\s|$)/);
    expect(retry.className).toMatch(/(^|\s)coarse:(min-w|w|size)-11(\s|$)/);
  });

  describe("selectSidebarNotebooks (순수 함수)", () => {
    it("즐겨찾기 안에서도 최근 순, 잘못된 날짜는 가장 오래된 것으로 본다", () => {
      const list = [
        notebook(1, { updated_at: "not-a-date", is_favorite: true }),
        notebook(2, { updated_at: "2026-09-02T00:00:00Z", is_favorite: true }),
        notebook(3, { updated_at: "2026-09-20T00:00:00Z" }),
      ];
      expect(selectSidebarNotebooks(list, null).items.map((n) => n.id)).toEqual(["nb-2", "nb-1", "nb-3"]);
    });

    it("현재 노트북이 목록에 없으면(권한 없음·삭제) 고정 항목도 없다", () => {
      expect(selectSidebarNotebooks([notebook(1)], "gone").pinnedActive).toBeNull();
      // limit 1이면 최근 수정인 nb-2만 목록 — nb-1이 현재면 고정 항목
      expect(selectSidebarNotebooks([notebook(1), notebook(2)], "nb-1", 1).pinnedActive?.id).toBe("nb-1");
      expect(selectSidebarNotebooks([notebook(1), notebook(2)], "nb-2", 1).pinnedActive).toBeNull();
    });

    it("입력 배열을 바꾸지 않는다", () => {
      const list = [notebook(1), notebook(2)];
      const before = list.map((n) => n.id);
      selectSidebarNotebooks(list, null);
      expect(list.map((n) => n.id)).toEqual(before);
    });
  });
});

describe("AC-2.11 스크롤 구획 — 하나의 스크롤 영역 + 고정 머리·바닥", () => {
  const LONG = "가".repeat(60);
  const ELEVEN = Array.from({ length: 11 }, (_, k) =>
    notebook(k + 1, { title: `${LONG}${k + 1}`, updated_at: `2026-09-${String(k + 1).padStart(2, "0")}T00:00:00Z` }),
  );

  it("데스크톱: 주 메뉴·관리·노트북은 한 스크롤 영역(min-h-0 flex-1 overflow-y-auto) 안, 사용자 메뉴 트리거는 그 밖 고정 바닥", async () => {
    renderShell({ pathname: "/admin/llm", notebooks: ELEVEN });
    const sidebar = screen.getByTestId("app-sidebar");
    await within(sidebar).findByTestId("sidebar-notebook-list");
    const scrollAreas = sidebar.querySelectorAll('[data-slot="sidebar-scroll"]');
    expect(scrollAreas).toHaveLength(1);
    const scroll = scrollAreas[0] as HTMLElement;
    expect(scroll).toHaveClass("min-h-0", "flex-1", "overflow-y-auto");
    expect(scroll).toContainElement(within(sidebar).getByTestId("primary-nav"));
    expect(scroll).toContainElement(within(sidebar).getByTestId("admin-nav-group"));
    const links = within(within(sidebar).getByTestId("sidebar-notebook-list")).getAllByRole("link");
    expect(scroll).toContainElement(links[links.length - 1]);
    // 긴 이름은 한 줄로 자르고 전체 이름은 title로
    expect(links[0]).toHaveAttribute("title", `${LONG}11`);

    const footer = sidebar.querySelector('[data-slot="sidebar-footer"]') as HTMLElement;
    expect(footer).toContainElement(within(sidebar).getByTestId("user-menu"));
    expect(scroll).not.toContainElement(within(sidebar).getByTestId("user-menu"));
    const header = sidebar.querySelector('[data-slot="sidebar-header"]') as HTMLElement;
    expect(header).toContainElement(within(sidebar).getByTestId("open-search"));
    expect(header.parentElement).toHaveClass("flex", "h-full", "min-h-0", "flex-col");
  });

  it("데스크톱: 키보드(Tab)로 마지막 노트북 링크와 「모든 노트북 보기」까지 도달한다", async () => {
    renderShell({ pathname: "/admin/llm", notebooks: ELEVEN });
    const sidebar = screen.getByTestId("app-sidebar");
    const list = await within(sidebar).findByTestId("sidebar-notebook-list");
    const links = within(list).getAllByRole("link");
    const last = links[links.length - 1];
    within(sidebar).getByTestId("open-search").focus();
    let steps = 0;
    while (document.activeElement !== last && steps < 60) {
      await userEvent.tab();
      steps += 1;
    }
    expect(document.activeElement).toBe(last);
    await userEvent.tab();
    expect(document.activeElement).toHaveAccessibleName("모든 노트북 보기 (11)");
  });

  it("드로어: 같은 한 스크롤 영역 + 고정 바닥의 「로그아웃」(스크롤 영역 밖), 계정 섹션은 스크롤 안", async () => {
    renderShell({ pathname: "/notebooks/nb-1", desktop: false, notebooks: ELEVEN });
    const drawer = await openDrawer();
    await within(drawer).findByTestId("sidebar-notebook-list");
    const scroll = drawer.querySelector('[data-slot="sidebar-scroll"]') as HTMLElement;
    expect(scroll).toHaveClass("min-h-0", "flex-1", "overflow-y-auto");
    const logout = within(drawer).getByRole("button", { name: "로그아웃" });
    expect(scroll).not.toContainElement(logout);
    expect(drawer.querySelector('[data-slot="sidebar-footer"]')).toContainElement(logout);
    expect(scroll).toContainElement(within(drawer).getByTestId("drawer-account-section"));
    const links = within(within(drawer).getByTestId("sidebar-notebook-list")).getAllByRole("link");
    expect(scroll).toContainElement(links[links.length - 1]);
    // 사용자 표시(이름·이메일)도 고정 바닥
    expect(within(drawer).getByText("admin@example.com")).toBeInTheDocument();
  });

  it("스크롤 그림자 단서는 넘친 쪽에만 보인다", async () => {
    renderShell({ pathname: "/", notebooks: ELEVEN });
    const scroll = screen.getByTestId("app-sidebar").querySelector('[data-slot="sidebar-scroll"]') as HTMLElement;
    Object.defineProperty(scroll, "clientHeight", { configurable: true, value: 100 });
    Object.defineProperty(scroll, "scrollHeight", { configurable: true, value: 400 });
    scroll.scrollTop = 0;
    fireEvent.scroll(scroll);
    expect(scroll).toHaveAttribute("data-overflow-bottom", "true");
    expect(scroll).not.toHaveAttribute("data-overflow-top");
    scroll.scrollTop = 300;
    fireEvent.scroll(scroll);
    expect(scroll).toHaveAttribute("data-overflow-top", "true");
    expect(scroll).not.toHaveAttribute("data-overflow-bottom");
  });

  it("목록이 늦게 와도 내용 크기가 바뀔 때 현재 노트북을 nearest 규칙으로 보이게 하고, 그 뒤엔 다시 끌어오지 않는다", async () => {
    // jsdom엔 ResizeObserver가 없다 — 브라우저 API만 최소 대역으로 보충(셸 코드는 그대로 실행)
    const callbacks: Array<() => void> = [];
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(cb: () => void) {
          callbacks.push(cb);
        }
        observe() {}
        disconnect() {}
      },
    );
    renderShell({ pathname: "/notebooks/nb-1", notebooks: ELEVEN });
    const list = await screen.findByTestId("sidebar-notebook-list");
    const active = within(list).getAllByRole("link")[0];
    expect(active).toHaveAttribute("aria-current", "page");
    const scroll = screen.getByTestId("app-sidebar").querySelector('[data-slot="sidebar-scroll"]') as HTMLElement;
    // 첫 렌더엔 목록이 없어 활성 항목이 없었다 → 아직 스크롤하지 않음
    expect(scroll.scrollTop).toBe(0);

    act(() => callbacks.forEach((cb) => cb()));
    // 아래로 넘친 만큼(632 − (400 − 여유 12)) 스크롤 영역만 굴린다
    expect(scroll.scrollTop).toBe(244);

    act(() => callbacks.forEach((cb) => cb()));
    expect(scroll.scrollTop).toBe(244);
  });

  it("주 메뉴 활성 항목은 첫 렌더에서 바로 스크롤 영역 안에 보이게 한다", () => {
    renderShell({ pathname: "/workflows" });
    const scroll = screen.getByTestId("app-sidebar").querySelector('[data-slot="sidebar-scroll"]') as HTMLElement;
    expect(within(scroll).getByRole("link", { name: "워크플로우" })).toHaveAttribute("aria-current", "page");
    expect(scroll.scrollTop).toBe(244);
  });

  it("Element.scrollIntoView를 부르지 않는다 — Chromium이 순차 포커스 시작점을 옮겨 첫 Tab(AC-2.5)을 깨므로", async () => {
    renderShell({ pathname: "/notebooks/nb-1", notebooks: ELEVEN });
    await screen.findByTestId("sidebar-notebook-list");
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  describe("revealWithin (nearest 규칙)", () => {
    function box(scrollTop: number) {
      const container = document.createElement("div");
      container.scrollTop = scrollTop;
      container.getBoundingClientRect = () => rect(100, 400);
      return container;
    }
    function item(top: number) {
      const el = document.createElement("a");
      el.getBoundingClientRect = () => rect(top, top + 32);
      return el;
    }

    it("이미 보이면(여유 12px 안쪽) 그대로 둔다", () => {
      const c = box(50);
      expect(revealWithin(c, item(200))).toBe(false);
      expect(c.scrollTop).toBe(50);
    });

    it("위로 벗어나면 위쪽 여유까지 올린다", () => {
      const c = box(300);
      expect(revealWithin(c, item(40))).toBe(true);
      expect(c.scrollTop).toBe(300 - (112 - 40));
    });

    it("아래로 벗어나면 아래쪽 여유까지 내린다", () => {
      const c = box(0);
      expect(revealWithin(c, item(380))).toBe(true);
      expect(c.scrollTop).toBe(412 - 388);
    });
  });
});

describe("AC-2.5 랜드마크 — 셸 main 하나 + 「본문으로 건너뛰기」", () => {
  it.each([true, false])("desktop=%s: main#main-content(tabIndex -1) 하나이고 본문은 그 안", (desktop) => {
    renderShell({ desktop });
    const mains = document.querySelectorAll("main");
    expect(mains).toHaveLength(1);
    expect(mains[0]).toHaveAttribute("id", "main-content");
    expect(mains[0]).toHaveAttribute("tabindex", "-1");
    expect(mains[0]).toContainElement(screen.getByTestId("page-body"));
    // 높이 계약: 세로 flex 열의 min-h-0 flex-1 항목(확정 높이 — 페이지의 h-full 스크롤 div·bleed가 채운다)
    expect(mains[0]).toHaveClass("min-h-0", "flex-1");
    expect(mains[0].parentElement).toHaveClass("flex", "min-h-0", "flex-col");
    // 스크롤 주인은 페이지 하나(P2 통합 IR-S4) — main은 스크롤 영역이 아니다(겹친 스크롤 영역 0)
    expect(mains[0]).toHaveClass("overflow-hidden");
    expect(mains[0]).not.toHaveClass("overflow-y-auto");
    expect(mains[0]).not.toHaveClass("overflow-auto");
    if (!desktop) {
      // 모바일: 같은 열에서 상단 바(고정 높이) 다음
      expect(mains[0].previousElementSibling?.tagName).toBe("HEADER");
      expect(mains[0].previousElementSibling).toHaveClass("h-14", "shrink-0");
    }
  });

  it.each([true, false])("desktop=%s: 첫 Tab이 「본문으로 건너뛰기」이고 Enter로 main에 포커스", async (desktop) => {
    renderShell({ desktop });
    (document.activeElement as HTMLElement | null)?.blur();
    await userEvent.tab();
    const skip = screen.getByRole("link", { name: "본문으로 건너뛰기" });
    expect(document.activeElement).toBe(skip);
    expect(skip).toHaveAttribute("href", "#main-content");
    await userEvent.keyboard("{Enter}");
    expect(document.activeElement).toBe(document.getElementById("main-content"));
  });

  it("모바일 상단 바는 main 밖(banner)이고 「노트북 홈」 격자 링크가 없다", () => {
    renderShell({ desktop: false });
    const banner = screen.getByRole("banner");
    expect(document.querySelector("main")).not.toContainElement(banner);
    expect(within(banner).getByRole("button", { name: "메뉴 열기" })).toBeInTheDocument();
    expect(within(banner).getByRole("link", { name: "biznotes" })).toHaveAttribute("href", "/");
    expect(within(banner).queryByRole("link", { name: "노트북 홈" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("app-sidebar")).not.toBeInTheDocument();
  });
});

describe("검색 진입점 단일화 (D-02)", () => {
  it("데스크톱: 사이드바 검색 트리거(open-search) 하나가 팔레트를 연다", async () => {
    renderShell({ pathname: "/skills" });
    const triggers = screen.getAllByTestId("open-search");
    expect(triggers).toHaveLength(1);
    expect(triggers[0]).toHaveAccessibleName("검색");
    await userEvent.click(triggers[0]);
    expect(useCommandPalette.getState().open).toBe(true);
  });

  it("모바일: 상단 바 검색 아이콘(44px) 하나가 팔레트를 연다", async () => {
    renderShell({ pathname: "/skills", desktop: false });
    const triggers = screen.getAllByTestId("open-search");
    expect(triggers).toHaveLength(1);
    expect(triggers[0]).toHaveAccessibleName("검색");
    expect(triggers[0]).toHaveClass("size-11");
    await userEvent.click(triggers[0]);
    expect(useCommandPalette.getState().open).toBe(true);
  });

  it("터치 기기(coarse)에서 고정 바닥의 테마 버튼도 44px이다 — 사이드바·드로어(P2 통합 IR-S5, §6.4.4)", async () => {
    renderShell({ pathname: "/", desktop: true });
    expect(screen.getByTestId("theme-switcher")).toHaveClass("coarse:size-11");
    cleanup();
    renderShell({ pathname: "/", desktop: false });
    const drawer = await openDrawer();
    expect(within(drawer).getByTestId("theme-switcher")).toHaveClass("coarse:size-11");
  });
});

describe("AC-2.3 드로어 동작(단위) — 이동 시 닫힘, Esc 닫힘·포커스 복귀", () => {
  it("열면 주 메뉴 5개와 「로그아웃」, 「메뉴 닫기」가 보이고 heading은 없다", async () => {
    renderShell({ pathname: "/", desktop: false });
    const drawer = await openDrawer();
    expect(primaryNavOf(drawer).map(([l]) => l)).toEqual(["노트북", "스킬", "워크플로우"]);
    expect(within(drawer).getByRole("button", { name: "로그아웃" })).toBeInTheDocument();
    expect(within(drawer).getByRole("button", { name: "메뉴 닫기" })).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "biznotes" })).toBe(drawer);
    expect(within(drawer).queryByRole("heading")).not.toBeInTheDocument();
  });

  it("항목을 누르면 드로어가 닫히고 포커스가 메뉴 버튼으로 돌아온다", async () => {
    renderShell({ pathname: "/", desktop: false });
    const drawer = await openDrawer();
    const link = within(drawer).getByRole("link", { name: "스킬" });
    link.addEventListener("click", (e) => e.preventDefault()); // jsdom 탐색 대신 닫힘만 본다
    await userEvent.click(link);
    await waitFor(() => expect(screen.queryByTestId("nav-drawer")).not.toBeInTheDocument());
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "메뉴 열기" })));
  });

  it("Esc로 닫히고 포커스가 메뉴 버튼으로 돌아온다", async () => {
    renderShell({ pathname: "/", desktop: false });
    await openDrawer();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("nav-drawer")).not.toBeInTheDocument());
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "메뉴 열기" })));
  });

  it("「메뉴 닫기」로 닫힌다", async () => {
    renderShell({ pathname: "/", desktop: false });
    const drawer = await openDrawer();
    await userEvent.click(within(drawer).getByRole("button", { name: "메뉴 닫기" }));
    await waitFor(() => expect(screen.queryByTestId("nav-drawer")).not.toBeInTheDocument());
  });
});

describe("브랜드 행 아래 앱 버전 표기", () => {
  it("데스크톱: 「biznotes」 아래에 앱 버전을 작게 적는다", async () => {
    renderShell({ pathname: "/" });
    const line = await screen.findByTestId("app-versions");
    expect(line).toHaveTextContent("v0.6.0");
    expect(line).toHaveAttribute("title", "biznotes v0.6.0");
    expect(line).toHaveClass("text-[11px]");
    expect(api.get).toHaveBeenCalledWith("/api/v1/meta/versions");
    const brand = line.closest("a");
    expect(brand).toHaveAttribute("href", "/");
    expect(brand).toHaveTextContent(/^biznotes/);
  });

  it("모바일 드로어 머리에도 같은 줄이 있고 다이얼로그 이름은 「biznotes」 그대로다", async () => {
    renderShell({ pathname: "/", desktop: false });
    const drawer = await openDrawer();
    expect(await within(drawer).findByTestId("app-versions")).toHaveTextContent("v0.6.0");
    expect(screen.getByRole("dialog", { name: "biznotes" })).toBe(drawer);
  });

  it("조회가 실패하면 줄을 그리지 않는다(빈 줄·「vundefined」 금지)", async () => {
    renderShell({ pathname: "/", versionsFail: true });
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/api/v1/meta/versions"));
    await waitFor(() => expect(screen.queryByTestId("app-versions")).not.toBeInTheDocument());
    expect(screen.getByTestId("app-sidebar")).not.toHaveTextContent("undefined");
  });
});

describe("사이드바 폭 — 기본 264px, 리사이즈 220–360px (§6.2.2)", () => {
  it("창 폭으로 px를 %로 바꾼다", () => {
    expect(sidebarPanelSizes(1280)).toEqual({ defaultSize: 20.625, minSize: 17.1875, maxSize: 28.125 });
    const narrow = sidebarPanelSizes(800);
    expect(narrow.minSize).toBeCloseTo(27.5);
    expect(narrow.maxSize).toBeCloseTo(45);
    expect(sidebarPanelSizes(0).defaultSize).toBe(90);
  });
});

describe("포커스 표시 — 사이드바·드로어 면의 컨트롤은 대비를 검증한 sidebar-ring을 쓴다(P2 리뷰 P2-02)", () => {
  // theme-contrast.test.ts X6이 sidebar-ring × sidebar·sidebar-accent·검색 트리거 면의 3:1을 6조합으로 검사한다.
  // 여기서는 그 토큰이 실제 사용처 전부에 쓰이는지 — 다른 링 토큰(ring: 틸 라이트 사이드바 위 2.97:1)이 섞이지 않는지 본다.
  const FOCUSABLE = 'a[href], button, input, [tabindex]:not([tabindex="-1"])';
  const SIDEBAR_RING = /(^|\s)focus-visible:ring-sidebar-ring(\/\d+)?(\s|$)/;
  const OTHER_RING = /(^|\s)focus-visible:(ring|border)-ring(\/\d+)?(\s|$)/;
  const offenders = (root: HTMLElement) =>
    [...root.querySelectorAll<HTMLElement>(FOCUSABLE)]
      .filter((el) => !SIDEBAR_RING.test(el.className) || OTHER_RING.test(el.className))
      .map((el) => el.getAttribute("aria-label") ?? el.textContent?.trim() ?? el.tagName);
  const NBS = [notebook(1), notebook(2), notebook(3)];

  it("데스크톱 사이드바 — 브랜드·검색·주 메뉴·관리(펼침)·「새 노트북」·노트북·대화·모든 노트북 보기·사용자 메뉴·테마", async () => {
    window.localStorage.setItem("sidebar-admin-open", "true");
    renderShell({ pathname: "/notebooks/nb-2", notebooks: NBS, sessions: [{ id: "s1", title: "대화 1" }] });
    const sidebar = screen.getByTestId("app-sidebar");
    await within(sidebar).findByTestId("sidebar-session-list");
    expect(within(sidebar).getAllByRole("link", { name: /사용자|LLM 게이트웨이/ }).length).toBeGreaterThan(0);
    const all = sidebar.querySelectorAll(FOCUSABLE).length;
    expect(all, "검사 대상 컨트롤 수").toBeGreaterThan(20);
    expect(offenders(sidebar)).toEqual([]);
  });

  it("노트북 목록 조회 실패의 「다시 시도」도 같다", async () => {
    renderShell({ pathname: "/", notebooksFail: true });
    const sidebar = screen.getByTestId("app-sidebar");
    await within(sidebar).findByText("노트북 목록을 불러오지 못했습니다");
    expect(offenders(sidebar)).toEqual([]);
  });

  it("모바일 드로어 — 「메뉴 닫기」·계정 섹션·「로그아웃」·테마까지", async () => {
    window.localStorage.setItem("sidebar-admin-open", "true");
    renderShell({ pathname: "/notebooks/nb-2", notebooks: NBS, desktop: false });
    const drawer = await openDrawer();
    await within(drawer).findByTestId("sidebar-notebook-list");
    expect(within(drawer).getByRole("button", { name: "로그아웃" })).toBeInTheDocument();
    expect(offenders(drawer)).toEqual([]);
  });
});
