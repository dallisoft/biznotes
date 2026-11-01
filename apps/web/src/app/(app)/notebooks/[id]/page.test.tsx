/**
 * 노트북 작업 화면(plan §6.6.2 노트북 작업 화면 행, Phase 3 — AC-3.1·3.2·3.3·3.4·3.10).
 *
 * 경계(`api` 클라이언트·App Router·토스트·`matchMedia`)만 대체하고 페이지·헤더·다이얼로그·훅은 실제 코드다
 * (mock 대체 금지 — AGENTS.md 「테스트 존재」). 사이드바 동기화는 셸의 실제 `NotebookNavSection`을 같은 QueryClient에 함께
 * 그려 확인한다. 응답을 정하지 않은 GET은 끝나지 않는 요청으로 두어 하위 패널이 로딩 상태에 머물게 한다.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({
  search: "",
  router: { push: vi.fn(), replace: vi.fn(), back: vi.fn(), refresh: vi.fn(), prefetch: vi.fn() },
}));
vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "x1" }),
  useRouter: () => nav.router,
  usePathname: () => "/notebooks/x1",
  useSearchParams: () => new URLSearchParams(nav.search),
}));
const { toast } = vi.hoisted(() => ({
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  }),
}));
vi.mock("sonner", () => ({ toast }));
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
import { notebookAccessLabel } from "@/components/notebooks/notebook-header";
import { NotebookNavSection } from "@/components/shell/notebook-nav-section";
import { ApiError } from "@/lib/api-client";
import type { Notebook } from "@/lib/types";
import { useChatPrompt } from "@/stores/chat-prompt";
import { useSourceFocus } from "@/stores/source-focus";

const never = () => new Promise<never>(() => {});

/** 응답 시점을 테스트가 정하는 요청(지연 응답 재현 — P3 리뷰 P3-01·P3-02). */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** 닫힘 애니메이션·지연 콜백이 끝날 시간을 준다 — 퇴장 중 남은 DOM을 「유지」로 오판하지 않게 기다린 뒤 단언한다. */
async function settle(ms = 250) {
  await act(async () => {
    await new Promise((r) => setTimeout(r, ms));
  });
}

function nb(overrides: Partial<Notebook> = {}): Notebook {
  return {
    id: "x1",
    owner_id: "me",
    title: "커피 연구",
    emoji: "☕",
    description: "",
    visibility: "private",
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    source_count: 3,
    is_owner: true,
    permission: "owner",
    tags: ["연구"],
    is_favorite: true,
    ...overrides,
  };
}

/** 경로 → 응답(값·함수) 또는 실패(ApiError). 표에 없는 GET은 끝나지 않는다. */
let routes: Record<string, unknown>;
let detail: Notebook;
let list: Notebook[];

function detailGets() {
  return api.get.mock.calls.filter(([path]) => path === "/api/v1/notebooks/x1").length;
}

beforeEach(() => {
  nav.search = "";
  detail = nb();
  list = [detail];
  routes = {};
  for (const fn of Object.values(api)) fn.mockImplementation(never);
  api.get.mockImplementation((path: string) => {
    if (path === "/api/v1/notebooks/x1") return Promise.resolve(detail);
    if (path === "/api/v1/notebooks?filter=all") return Promise.resolve(list);
    if (!(path in routes)) return never();
    const value = routes[path];
    if (value instanceof ApiError) return Promise.reject(value);
    return Promise.resolve(typeof value === "function" ? (value as () => unknown)() : value);
  });
  stubMedia(true);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetAllMocks();
});

/** 데스크톱(≥768) 여부 — 노트북 작업 화면의 `useMediaQuery("(min-width: 768px)")`가 읽는다. */
function stubMedia(desktop: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: desktop,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
}

function renderPage({ withSidebar = false } = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      {withSidebar && <NotebookNavSection activeNotebookId="x1" />}
      <NotebookPage />
    </QueryClientProvider>,
  );
  return client;
}

async function openMoreMenu(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "노트북 동작 더보기" }));
  return screen.findByRole("menu");
}

/**
 * 다이얼로그가 열리고 포커스가 그 안에 자리 잡을 때까지 기다린다. 예전에는 메뉴 항목이 다이얼로그를 즉시 열어, 닫히는
 * 메뉴의 포커스 복귀(⋯)가 다이얼로그의 초기 포커스를 덮었다 — 포커스가 모달 밖에 남고, 이 파일만 따로 돌리면 ①의 입력이
 * 간헐적으로 새었다(P3 리뷰 수정 중 발견 → P3 `opensDialog`로 우회, P6에서 RowActions 불리언 메뉴 복귀로 원인 제거).
 */
async function dialogReady(name: string) {
  const dialog = await screen.findByRole("dialog", { name });
  await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
  await settle(50);
  expect(dialog.contains(document.activeElement)).toBe(true);
  return dialog;
}

/** 확인 다이얼로그가 초기 포커스(「취소」)를 잡을 때까지 기다린다 — 그 전에 누른 「삭제」의 포커스를 초기 포커스가 덮지 않게. */
async function confirmReady() {
  const confirm = await screen.findByRole("alertdialog");
  await waitFor(() => expect(within(confirm).getByRole("button", { name: "취소" })).toHaveFocus());
  return confirm;
}

describe("노트북 헤더 — 제목·브레드크럼(AC-3.1)", () => {
  it("노트북 제목이 h1이고, 브레드크럼 「노트북」 링크는 노트북 홈(/)으로 간다 — heading이 아니다", async () => {
    renderPage();
    const h1 = await screen.findByRole("heading", { level: 1, name: "커피 연구" });
    expect(h1).toHaveTextContent(/^커피 연구$/);
    const crumbs = screen.getByRole("navigation", { name: "위치" });
    expect(within(crumbs).getByRole("link", { name: "노트북" })).toHaveAttribute("href", "/");
    // e2e 공용 헬퍼가 홈의 heading 「노트북」(exact)을 단언하므로 같은 이름의 heading을 만들지 않는다(§9 공통 규칙)
    expect(screen.queryByRole("heading", { name: "노트북" })).toBeNull();
    // h1은 하나다
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });
});

describe("노트북 헤더 — 소유자 전용 동작(AC-3.2, 비인가 관점 is_owner·permission 4조합)", () => {
  it.each([
    { name: "소유자(is_owner, owner)", is_owner: true, permission: "owner" as const, badge: null },
    { name: "편집 공유(write)", is_owner: false, permission: "write" as const, badge: "편집 가능" },
    { name: "읽기 공유(read)", is_owner: false, permission: "read" as const, badge: "읽기 전용" },
    {
      name: "관리자 승격(is_owner=false, owner) — 웹 소유자 전용 UI는 is_owner로만 판정",
      is_owner: false,
      permission: "owner" as const,
      badge: "편집 가능",
    },
  ])("$name", async ({ is_owner, permission, badge }) => {
    detail = nb({ is_owner, permission, owner_id: is_owner ? "me" : "someone" });
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { level: 1, name: "커피 연구" });

    const share = screen.queryByRole("button", { name: "공유" });
    const accessBadge = screen.queryByTestId("notebook-access-badge");
    if (badge === null) expect(accessBadge).toBeNull();
    else expect(accessBadge).toHaveTextContent(badge);

    if (is_owner) {
      const menu = await openMoreMenu(user);
      const items = within(menu).getAllByRole("menuitem").map((i) => i.textContent);
      expect(share).toBeInTheDocument();
      expect(items).toEqual(["이름 변경", "태그 관리", "노트북 삭제"]);
      expect(within(menu).getByRole("menuitem", { name: "노트북 삭제" })).toHaveAttribute(
        "data-variant",
        "destructive",
      );
    } else {
      // 비소유자: 더보기가 없다 — 공유·이름 변경·태그·삭제는 렌더되지 않는다
      expect(share).toBeNull();
      expect(screen.queryByRole("button", { name: "노트북 동작 더보기" })).toBeNull();
      for (const owned of ["이름 변경", "태그 관리", "노트북 삭제", "공유"]) {
        expect(screen.queryByRole("menuitem", { name: owned })).toBeNull();
      }
    }
  });

  it("notebookAccessLabel: 소유자는 배지 없음, 읽기 전용만 「읽기 전용」, 그 밖의 비소유자는 「편집 가능」", () => {
    expect(notebookAccessLabel({ is_owner: true, permission: "owner" })).toBeNull();
    expect(notebookAccessLabel({ is_owner: false, permission: "read" })).toBe("읽기 전용");
    expect(notebookAccessLabel({ is_owner: false, permission: "write" })).toBe("편집 가능");
    expect(notebookAccessLabel({ is_owner: false, permission: "owner" })).toBe("편집 가능");
  });

  it("소유자의 「공유」는 공유 다이얼로그를 연다", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "공유" }));
    expect(await screen.findByRole("dialog", { name: "노트북 공유" })).toBeInTheDocument();
  });
});

describe("노트북 헤더 동작의 캐시·이동(AC-3.10, 실제 훅 경로 — api만 대체)", () => {
  it("① 이름 변경 성공 → 헤더 h1·사이드바 항목이 곧바로 새 이름이고, PATCH 응답에 없는 계산 필드(태그·소스 수·즐겨찾기)는 지우지 않는다", async () => {
    const user = userEvent.setup();
    // 코어 PATCH 응답은 source_count·tags·is_favorite를 기본값으로 돌려준다(`_to_out(nb, perm, user.id)`)
    api.patch.mockImplementation((_path: string, body: { title: string }) =>
      Promise.resolve(nb({ title: body.title, source_count: 0, tags: [], is_favorite: false, updated_at: "2026-10-15T00:00:00Z" })),
    );
    const client = renderPage({ withSidebar: true });
    await screen.findByRole("heading", { level: 1, name: "커피 연구" });
    expect(await screen.findByRole("link", { name: "☕ 커피 연구" })).toBeInTheDocument();

    // 무효화 뒤 다시 읽는 상세·목록은 끝나지 않게 두어, setQueryData만으로 바뀐 중간 상태를 본다
    const detailBefore = detailGets();
    api.get.mockImplementation((path: string) =>
      path === "/api/v1/notebooks/x1" || path === "/api/v1/notebooks?filter=all" ? never() : never(),
    );

    const menu = await openMoreMenu(user);
    await user.click(within(menu).getByRole("menuitem", { name: "이름 변경" }));
    const dialog = await dialogReady("노트북 이름 변경");
    const input = within(dialog).getByRole("textbox");
    await user.clear(input);
    await user.type(input, "에스프레소 연구");
    await user.click(within(dialog).getByRole("button", { name: "저장" }));

    expect(api.patch).toHaveBeenCalledWith("/api/v1/notebooks/x1", { title: "에스프레소 연구" });
    expect(await screen.findByRole("heading", { level: 1, name: "에스프레소 연구" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "☕ 에스프레소 연구" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "☕ 커피 연구" })).toBeNull();

    const cached = client.getQueryData<Notebook>(["notebook", "x1"]);
    expect(cached).toMatchObject({
      title: "에스프레소 연구",
      updated_at: "2026-10-15T00:00:00Z",
      tags: ["연구"],
      source_count: 3,
      is_favorite: true,
      is_owner: true,
    });
    expect(client.getQueryData<Notebook[]>(["notebooks", "all"])?.[0]).toMatchObject({
      title: "에스프레소 연구",
      tags: ["연구"],
    });
    // 상세·목록을 무효화해 서버 값으로 다시 읽는다
    await waitFor(() => expect(detailGets()).toBeGreaterThan(detailBefore));
    expect(api.get).toHaveBeenCalledWith("/api/v1/notebooks?filter=all");
  });

  it("② 태그 연결 성공 → 상세 캐시를 다시 읽어 헤더의 태그 관리 다이얼로그가 새 태그를 연결 상태로 보인다", async () => {
    const user = userEvent.setup();
    routes["/api/v1/tags"] = [
      { id: "t1", name: "연구" },
      { id: "t2", name: "보관" },
    ];
    api.post.mockImplementation((path: string) => {
      if (path === "/api/v1/notebooks/x1/tags/t2") {
        detail = nb({ tags: ["연구", "보관"] });
        list = [detail];
        return Promise.resolve(undefined);
      }
      return never();
    });
    const client = renderPage({ withSidebar: true });
    const menu = await openMoreMenu(user);
    await user.click(within(menu).getByRole("menuitem", { name: "태그 관리" }));
    const dialog = await screen.findByRole("dialog", { name: "태그 관리" });
    const before = detailGets();
    await user.click(await within(dialog).findByText("보관"));

    expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/x1/tags/t2");
    await waitFor(() => expect(detailGets()).toBeGreaterThan(before));
    await waitFor(() =>
      expect(client.getQueryData<Notebook>(["notebook", "x1"])?.tags).toEqual(["연구", "보관"]),
    );
    // 응답이 void라 응답으로 캐시를 쓰지 않는다 — 목록도 함께 다시 읽는다
    expect(api.get).toHaveBeenCalledWith("/api/v1/notebooks?filter=all");
  });

  it("③ 삭제 확인을 취소하면 요청 0건이고 화면·URL이 그대로다", async () => {
    const user = userEvent.setup();
    renderPage();
    const menu = await openMoreMenu(user);
    await user.click(within(menu).getByRole("menuitem", { name: "노트북 삭제" }));
    const confirm = await screen.findByRole("alertdialog", { name: "‘커피 연구’ 노트북을 삭제할까요?" });
    expect(confirm).toHaveTextContent("소스·메모·대화가 모두 삭제되며 되돌릴 수 없습니다.");
    // 기본 포커스는 「취소」(§6.5)
    await waitFor(() => expect(within(confirm).getByRole("button", { name: "취소" })).toHaveFocus());
    await user.click(within(confirm).getByRole("button", { name: "취소" }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(api.delete).not.toHaveBeenCalled();
    expect(nav.router.replace).not.toHaveBeenCalled();
    expect(nav.router.push).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { level: 1, name: "커피 연구" })).toBeInTheDocument();
  });

  it("④ 삭제 성공 → 노트북 홈으로 replace, 상세·하위 쿼리 캐시 제거, 목록에서 빠짐, 토스트 — 지워진 노트북을 다시 읽지 않는다", async () => {
    const user = userEvent.setup();
    nav.search = "s=s1";
    routes["/api/v1/notebooks/x1/chat/sessions"] = [
      { id: "s1", notebook_id: "x1", title: "첫 대화", pinned: false, created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z" },
    ];
    api.delete.mockImplementation((path: string) => {
      if (path !== "/api/v1/notebooks/x1") return never();
      list = [];
      return Promise.resolve(undefined);
    });
    const client = renderPage({ withSidebar: true });
    await screen.findByRole("heading", { level: 1, name: "커피 연구" });
    await screen.findByRole("button", { name: "대화: 첫 대화" });
    // 하위 쿼리 캐시가 있는 상태를 만든다(소스·메모·요약·실행 등은 진행 중 요청이라도 캐시 항목이 있다)
    act(() => {
      client.setQueryData(["chat-messages", "s1"], []);
    });
    for (const key of [["sources", "x1"], ["notes", "x1"], ["overview", "x1"], ["agent-runs", "x1"]]) {
      expect(client.getQueryCache().find({ queryKey: key })).toBeDefined();
    }

    const menu = await openMoreMenu(user);
    await user.click(within(menu).getByRole("menuitem", { name: "노트북 삭제" }));
    const confirm = await screen.findByRole("alertdialog");
    const getsBefore = detailGets();
    await user.click(within(confirm).getByRole("button", { name: "삭제" }));

    await waitFor(() => expect(nav.router.replace).toHaveBeenCalledWith("/"));
    expect(api.delete).toHaveBeenCalledWith("/api/v1/notebooks/x1");
    expect(toast.success).toHaveBeenCalledWith("노트북을 삭제했습니다", expect.anything());
    expect(screen.getByRole("status")).toHaveTextContent("노트북을 삭제했습니다");
    // 상세·하위 쿼리(소스·메모·대화·요약·실행·대화별 메시지)가 캐시에서 빠졌다. 이동하는 동안 화면에 남은
    // 상세 관찰자는 조회를 멈춘 빈 항목만 다시 만들 뿐이다(데이터 없음·요청 없음)
    for (const key of [
      ["sources", "x1"],
      ["notes", "x1"],
      ["chat-sessions", "x1"],
      ["overview", "x1"],
      ["agent-runs", "x1"],
      ["chat-messages", "s1"],
    ]) {
      expect(client.getQueryCache().findAll({ queryKey: key }), JSON.stringify(key)).toHaveLength(0);
    }
    const detailQueries = client.getQueryCache().findAll({ queryKey: ["notebook", "x1"] });
    expect(detailQueries.every((q) => q.state.data === undefined && q.state.fetchStatus === "idle")).toBe(true);
    // 목록 캐시에서 바로 빠져 사이드바 행이 사라진다
    expect(client.getQueryData<Notebook[]>(["notebooks", "all"])).toEqual([]);
    await waitFor(() => expect(screen.queryByRole("link", { name: "☕ 커피 연구" })).toBeNull());
    // 지워진 노트북 상세를 다시 요청하지 않는다(이동하는 동안 404 화면이 번쩍이지 않음)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
    expect(detailGets()).toBe(getsBefore);
    expect(screen.queryByText("노트북을 찾을 수 없습니다.")).toBeNull();
  });

  it("⑤ 삭제 실패(500) → 확인 다이얼로그·화면·URL 유지, 실패 토스트, 같은 다이얼로그에서 다시 시도하면 삭제된다(§6.6.2 — P3-01)", async () => {
    const user = userEvent.setup();
    let calls = 0;
    api.delete.mockImplementation((path: string) => {
      if (path !== "/api/v1/notebooks/x1") return never();
      calls += 1;
      return calls === 1 ? Promise.reject(new ApiError(500, "Internal Server Error")) : Promise.resolve(undefined);
    });
    const client = renderPage();
    await screen.findByRole("heading", { level: 1, name: "커피 연구" });
    const menu = await openMoreMenu(user);
    await user.click(within(menu).getByRole("menuitem", { name: "노트북 삭제" }));
    const confirm = await confirmReady();
    expect(confirm).toHaveAccessibleName("‘커피 연구’ 노트북을 삭제할까요?");
    await user.click(within(confirm).getByRole("button", { name: "삭제" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("노트북을 삭제하지 못했습니다", expect.anything()),
    );
    await settle();
    // 확인 다이얼로그가 그대로 열려 있고(퇴장 애니메이션 뒤에도), 다시 시도할 수 있다 — 포커스도 다이얼로그 안에 있다
    const still = screen.getByRole("alertdialog", { name: "‘커피 연구’ 노트북을 삭제할까요?" });
    const retry = within(still).getByRole("button", { name: "삭제" });
    expect(retry).not.toHaveAttribute("aria-disabled");
    expect(still.contains(document.activeElement)).toBe(true);
    expect(nav.router.replace).not.toHaveBeenCalled();
    // 작업 화면도 그대로다(모달이 열려 있어 바깥은 접근성 트리에서 숨겨져 있다)
    expect(screen.getByRole("heading", { level: 1, name: "커피 연구", hidden: true })).toBeInTheDocument();
    expect(client.getQueryData<Notebook>(["notebook", "x1"])).toMatchObject({ id: "x1" });

    // 같은 다이얼로그에서 다시 시도 → 성공하면 닫히고 노트북 홈으로
    await user.click(retry);
    await waitFor(() => expect(nav.router.replace).toHaveBeenCalledWith("/"));
    expect(api.delete).toHaveBeenCalledTimes(2);
    expect(toast.success).toHaveBeenCalledWith("노트북을 삭제했습니다", expect.anything());
  });

  it("⑤-b 삭제 실패 뒤 「취소」로 닫으면 요청을 더 보내지 않고 포커스는 헤더 더보기(⋯)로 돌아온다", async () => {
    const user = userEvent.setup();
    api.delete.mockRejectedValue(new ApiError(500, "Internal Server Error"));
    renderPage();
    await screen.findByRole("heading", { level: 1, name: "커피 연구" });
    const menu = await openMoreMenu(user);
    await user.click(within(menu).getByRole("menuitem", { name: "노트북 삭제" }));
    const confirm = await confirmReady();
    await user.click(within(confirm).getByRole("button", { name: "삭제" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "취소" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(api.delete).toHaveBeenCalledTimes(1);
    // ConfirmDialog 계약 ②
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "노트북 동작 더보기" })).toHaveFocus(),
    );
  });

  it("⑤-c 삭제 요청 중에는 「삭제하는 중…」으로 비활성이고 두 번째 클릭·Enter·Esc·취소가 먹지 않는다(중복 제출 차단 — P3-01)", async () => {
    const user = userEvent.setup();
    const deleting = deferred<undefined>();
    api.delete.mockImplementation((path: string) => (path === "/api/v1/notebooks/x1" ? deleting.promise : never()));
    renderPage();
    await screen.findByRole("heading", { level: 1, name: "커피 연구" });
    const menu = await openMoreMenu(user);
    await user.click(within(menu).getByRole("menuitem", { name: "노트북 삭제" }));
    const confirm = await confirmReady();
    // 키보드: 「취소」 → Tab → 「삭제」 → Enter
    await user.tab();
    expect(within(confirm).getByRole("button", { name: "삭제" })).toHaveFocus();
    await user.keyboard("{Enter}");

    const busy = await within(confirm).findByRole("button", { name: "삭제하는 중…" });
    expect(busy).toHaveAttribute("aria-disabled", "true");
    // 누른 버튼이 대기 중에도 포커스를 잃지 않는다(focusableWhenDisabled)
    expect(busy).toHaveFocus();
    expect(within(confirm).getByRole("button", { name: "취소" })).toBeDisabled();
    await user.click(busy);
    await user.keyboard("{Enter}");
    await user.keyboard("{Escape}");
    await settle(50);
    expect(api.delete).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("alertdialog")).toBe(confirm);
    expect(nav.router.replace).not.toHaveBeenCalled();

    await act(async () => deleting.resolve(undefined));
    await waitFor(() => expect(nav.router.replace).toHaveBeenCalledTimes(1));
    expect(api.delete).toHaveBeenCalledTimes(1);
  });

  it("⑤-d 키보드로 더보기 → 「이름 변경」을 고르면 포커스가 이름 칸에 남고(닫히는 메뉴가 ⋯로 덮지 않음) 입력이 그 칸에 들어가며, 닫으면 ⋯로 돌아온다", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { level: 1, name: "커피 연구" });
    const more = screen.getByRole("button", { name: "노트북 동작 더보기" });
    act(() => more.focus());
    await user.keyboard("{Enter}");
    const menu = await screen.findByRole("menu");
    const first = within(menu).getAllByRole("menuitem")[0];
    expect(first).toHaveAccessibleName("이름 변경");
    await waitFor(() => expect(first).toHaveFocus());
    await user.keyboard("{Enter}");
    const dialog = await screen.findByRole("dialog", { name: "노트북 이름 변경" });
    const input = within(dialog).getByRole("textbox");
    await waitFor(() => expect(input).toHaveFocus());
    await settle(100);
    expect(input).toHaveFocus();
    await user.keyboard("{End}!");
    expect(input).toHaveValue("커피 연구!");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "노트북 이름 변경" })).toBeNull());
    await waitFor(() => expect(more).toHaveFocus());
  });

  it.each([
    ["태그 관리", "태그 관리"],
  ])("⑤-e 키보드로 더보기 → 「%s」도 포커스가 다이얼로그 안에 남고 닫으면 ⋯로 돌아온다", async (item, dialogName) => {
    const user = userEvent.setup();
    routes["/api/v1/tags"] = [];
    renderPage();
    await screen.findByRole("heading", { level: 1, name: "커피 연구" });
    const more = screen.getByRole("button", { name: "노트북 동작 더보기" });
    act(() => more.focus());
    await user.keyboard("{Enter}");
    const target = await screen.findByRole("menuitem", { name: item });
    act(() => target.focus());
    await user.keyboard("{Enter}");
    await dialogReady(dialogName);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: dialogName })).toBeNull());
    await waitFor(() => expect(more).toHaveFocus());
  });

  it("⑥ 공유 요청 중에 닫고 이름 변경을 열면, 공유 응답이 늦게 와도 이름 변경 창은 그대로다(P3-02)", async () => {
    const user = userEvent.setup();
    // 공유 관리 대화상자(core-v0.20.0 P3)가 읽는 그룹·공유 목록
    routes["/api/v1/me/groups"] = [{ id: "g-research", name: "연구팀" }];
    routes["/api/v1/notebooks/x1/shares"] = [];
    const shared = deferred<unknown>();
    api.post.mockImplementation((path: string) => (path === "/api/v1/notebooks/x1/share" ? shared.promise : never()));
    renderPage();
    await screen.findByRole("heading", { level: 1, name: "커피 연구" });
    await user.click(screen.getByTestId("notebook-share"));
    const share = await dialogReady("노트북 공유");
    await user.click(await within(share).findByTestId("share-group-select"));
    await screen.findByRole("option", { name: "연구팀" });
    // 이 파일 환경에서는 Select 항목을 클릭하면 포커스만 옮겨지고 선택되지 않는다(실측) — 키보드(Enter)로 고른다
    act(() => screen.getByRole("option", { name: "연구팀" }).focus());
    await user.keyboard("{Enter}");
    await user.click(within(share).getByTestId("share-submit"));
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/x1/share", {
        group_id: "g-research",
        permission: "read",
      }),
    );
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "노트북 공유" })).toBeNull());

    const menu = await openMoreMenu(user);
    await user.click(within(menu).getByRole("menuitem", { name: "이름 변경" }));
    const rename = await dialogReady("노트북 이름 변경");
    const input = within(rename).getByRole("textbox");
    await user.clear(input);
    await user.type(input, "입력 중인 새 이름");

    await act(async () =>
      shared.resolve({ id: "s1", notebook_id: "x1", user_id: null, group_id: "g-research", permission: "read" }),
    );
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("「연구팀」 그룹에 읽기 전용 권한으로 공유했습니다", expect.anything()),
    );
    await settle();
    const stillOpen = screen.getByRole("dialog", { name: "노트북 이름 변경" });
    expect(within(stillOpen).getByRole("textbox")).toHaveValue("입력 중인 새 이름");
  });

  it("⑦ 이름 변경을 다시 열어 입력하는 중에 이전 PATCH가 늦게 성공해도 새 창과 새 입력이 그대로다(열기 세대·회차 — P3-02·P3-R01)", async () => {
    const user = userEvent.setup();
    const patched = deferred<Notebook>();
    api.patch.mockImplementation(() => patched.promise);
    renderPage();
    await screen.findByRole("heading", { level: 1, name: "커피 연구" });

    let menu = await openMoreMenu(user);
    await user.click(within(menu).getByRole("menuitem", { name: "이름 변경" }));
    let rename = await dialogReady("노트북 이름 변경");
    await user.clear(within(rename).getByRole("textbox"));
    await user.type(within(rename).getByRole("textbox"), "첫 이름");
    await user.click(within(rename).getByRole("button", { name: "저장" }));
    expect(api.patch).toHaveBeenCalledTimes(1);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "노트북 이름 변경" })).toBeNull());

    // 같은 「이름 변경」을 다시 연다(새 창) — 지금 이름으로 시작하고, 이전 요청을 기다리는 표시가 없다
    menu = await openMoreMenu(user);
    await user.click(within(menu).getByRole("menuitem", { name: "이름 변경" }));
    rename = await dialogReady("노트북 이름 변경");
    const input = within(rename).getByRole("textbox");
    expect(input).toHaveValue("커피 연구");
    expect(within(rename).queryByRole("button", { name: "저장하는 중…" })).toBeNull();
    await user.clear(input);
    await user.type(input, "새 창에서 입력 중");

    // 이전 요청이 성공 — 서버도 이제 「첫 이름」을 돌려준다(상세 캐시가 바뀐다)
    detail = nb({ title: "첫 이름" });
    list = [detail];
    await act(async () => patched.resolve(nb({ title: "첫 이름" })));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("노트북 이름을 변경했습니다", expect.anything()),
    );
    // 서버 성공에 따른 캐시 갱신은 그대로 — 헤더 h1이 새 이름이다
    expect(await screen.findByRole("heading", { level: 1, name: "첫 이름", hidden: true })).toBeInTheDocument();
    await settle();
    const still = screen.getByRole("dialog", { name: "노트북 이름 변경" });
    expect(within(still).getByRole("textbox")).toHaveValue("새 창에서 입력 중");
  });

  it("⑧ 공유를 다시 열어 새 그룹을 고르는 중에 이전 공유가 늦게 성공해도 새 창과 새 입력이 그대로다(P3-R01)", async () => {
    const user = userEvent.setup();
    routes["/api/v1/me/groups"] = [
      { id: "g-alpha", name: "가나 그룹" },
      { id: "g-research", name: "연구팀" },
    ];
    routes["/api/v1/notebooks/x1/shares"] = [];
    const shared = deferred<unknown>();
    api.post.mockImplementation((path: string) => (path === "/api/v1/notebooks/x1/share" ? shared.promise : never()));
    renderPage();
    await screen.findByRole("heading", { level: 1, name: "커피 연구" });

    await user.click(screen.getByTestId("notebook-share"));
    let share = await dialogReady("노트북 공유");
    await user.click(await within(share).findByTestId("share-group-select"));
    await screen.findByRole("option", { name: "가나 그룹" });
    act(() => screen.getByRole("option", { name: "가나 그룹" }).focus());
    await user.keyboard("{Enter}");
    await user.click(within(share).getByTestId("share-submit"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "노트북 공유" })).toBeNull());

    await user.click(screen.getByTestId("notebook-share"));
    share = await dialogReady("노트북 공유");
    const group = await within(share).findByTestId("share-group-select");
    // 새 창은 빈 입력이고, 이전 요청을 기다리는 표시가 없다
    expect(group).toHaveTextContent("그룹 선택");
    expect(within(share).getByTestId("share-submit")).toHaveTextContent(/^공유$/);
    await user.click(group);
    await screen.findByRole("option", { name: "연구팀" });
    act(() => screen.getByRole("option", { name: "연구팀" }).focus());
    await user.keyboard("{Enter}");

    await act(async () =>
      shared.resolve({ id: "s1", notebook_id: "x1", user_id: null, group_id: "g-alpha", permission: "read" }),
    );
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("「가나 그룹」 그룹에 읽기 전용 권한으로 공유했습니다", expect.anything()),
    );
    await settle();
    const still = screen.getByRole("dialog", { name: "노트북 공유" });
    expect(within(still).getByTestId("share-group-select")).toHaveTextContent("연구팀");
  });

  it("⑨ 이전 공유가 다시 연 뒤 「그룹 404」로 끝나도 그 인라인 오류가 새 창에 새지 않고 토스트로 알린다(P3-R01)", async () => {
    const user = userEvent.setup();
    routes["/api/v1/me/groups"] = [
      { id: "g-alpha", name: "가나 그룹" },
      { id: "g-research", name: "연구팀" },
    ];
    routes["/api/v1/notebooks/x1/shares"] = [];
    const shared = deferred<unknown>();
    api.post.mockImplementation((path: string) => (path === "/api/v1/notebooks/x1/share" ? shared.promise : never()));
    renderPage();
    await screen.findByRole("heading", { level: 1, name: "커피 연구" });

    await user.click(screen.getByTestId("notebook-share"));
    let share = await dialogReady("노트북 공유");
    await user.click(await within(share).findByTestId("share-group-select"));
    await screen.findByRole("option", { name: "연구팀" });
    act(() => screen.getByRole("option", { name: "연구팀" }).focus());
    await user.keyboard("{Enter}");
    await user.click(within(share).getByTestId("share-submit"));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "노트북 공유" })).toBeNull());

    await user.click(screen.getByTestId("notebook-share"));
    share = await dialogReady("노트북 공유");
    await user.click(await within(share).findByTestId("share-group-select"));
    await screen.findByRole("option", { name: "가나 그룹" });
    act(() => screen.getByRole("option", { name: "가나 그룹" }).focus());
    await user.keyboard("{Enter}");

    await act(async () => shared.reject(new ApiError(404, "Target group not found")));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("노트북을 공유하지 못했습니다", expect.anything()),
    );
    await settle();
    const still = screen.getByRole("dialog", { name: "노트북 공유" });
    expect(within(still).queryByRole("alert")).toBeNull();
    expect(within(still).getByTestId("share-group-select")).not.toHaveAttribute("aria-invalid");
    expect(within(still).getByTestId("share-group-select")).toHaveTextContent("가나 그룹");
    expect(still).not.toHaveTextContent("이 그룹에는 공유할 수 없습니다");
  });
});

describe("헤더 공유 — 겹친 요청의 알림(P3 재리뷰 2 P3-R2-01)", () => {
  it("공유 요청 중 닫고 다시 열어 두 번째로 공유한 뒤 첫 요청이 500으로 실패하면, 그 실패 알림이 한 번 뜨고 두 번째 창은 그대로이며 두 번째 성공은 따로 알리고 창은 열린 채 목록에 새 행이 보인다", async () => {
    const user = userEvent.setup();
    const rows: unknown[] = [];
    routes["/api/v1/me/groups"] = [
      { id: "g-alpha", name: "가나 그룹" },
      { id: "g-research", name: "연구팀" },
    ];
    routes["/api/v1/notebooks/x1/shares"] = () => [...rows];
    const first = deferred<unknown>();
    const second = deferred<unknown>();
    let calls = 0;
    api.post.mockImplementation((path: string) => {
      if (path !== "/api/v1/notebooks/x1/share") return never();
      calls += 1;
      return calls === 1 ? first.promise : second.promise;
    });
    renderPage();
    await screen.findByRole("heading", { level: 1, name: "커피 연구" });

    await user.click(screen.getByTestId("notebook-share"));
    let share = await dialogReady("노트북 공유");
    await user.click(await within(share).findByTestId("share-group-select"));
    await screen.findByRole("option", { name: "가나 그룹" });
    act(() => screen.getByRole("option", { name: "가나 그룹" }).focus());
    await user.keyboard("{Enter}");
    await user.click(within(share).getByTestId("share-submit"));
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "노트북 공유" })).toBeNull());
    await user.click(screen.getByTestId("notebook-share"));
    share = await dialogReady("노트북 공유");
    await user.click(await within(share).findByTestId("share-group-select"));
    await screen.findByRole("option", { name: "연구팀" });
    act(() => screen.getByRole("option", { name: "연구팀" }).focus());
    await user.keyboard("{Enter}");
    await user.click(within(share).getByTestId("share-submit"));
    expect(calls).toBe(2);
    expect(within(share).getByTestId("share-submit")).toHaveTextContent("공유하는 중…");
    expect(within(share).getByTestId("share-submit")).toBeDisabled();

    await act(async () => first.reject(new ApiError(500, "boom")));
    await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(1));
    expect(toast.error).toHaveBeenCalledWith("노트북을 공유하지 못했습니다", expect.anything());
    await settle();
    expect(screen.getByRole("dialog", { name: "노트북 공유" })).toBe(share);
    expect(within(share).getByTestId("share-group-select")).toHaveTextContent("연구팀");
    expect(within(share).getByTestId("share-submit")).toHaveTextContent("공유하는 중…");

    const created = { id: "s2", notebook_id: "x1", user_id: null, group_id: "g-research", permission: "read" };
    rows.push(created);
    await act(async () => second.resolve(created));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("「연구팀」 그룹에 읽기 전용 권한으로 공유했습니다", expect.anything()),
    );
    // 관리 대화상자 — 성공 뒤에도 열린 채 목록에 새 행이 보인다(core-v0.20.0 §5.8)
    await settle();
    expect(screen.getByRole("dialog", { name: "노트북 공유" })).toBe(share);
    const newRow = await within(share).findByTestId("share-row");
    expect(newRow).toHaveAttribute("data-share-id", "s2");
    expect(within(newRow).getByTestId("share-row-target")).toHaveTextContent("연구팀");
    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(toast.success).toHaveBeenCalledTimes(1);
  });
});

describe("자동 대화 선택과 노트북 떠나기(P3 게이트 3회차 경합)", () => {
  // 페이지는 실제 주소(window.location)와 링크 주소를 비교한다 — jsdom 주소를 이 노트북 경로로 맞춘다
  beforeEach(() => window.history.replaceState(null, "", "/notebooks/x1"));
  afterEach(() => window.history.replaceState(null, "", "/"));

  function prepareFreshNotebook() {
    routes["/api/v1/notebooks/x1/chat/sessions"] = [];
    const created = deferred<unknown>();
    api.post.mockImplementation((path: string) =>
      path === "/api/v1/notebooks/x1/chat/sessions" ? created.promise : never(),
    );
    return created;
  }
  const session = { id: "s-new", notebook_id: "x1", title: "새 대화", pinned: false, created_at: "2026-10-15T00:00:00Z", updated_at: "2026-10-15T00:00:00Z" };

  it("대화가 없는 노트북을 열면 첫 대화를 만들어 URL ?s=로 반영한다", async () => {
    const created = prepareFreshNotebook();
    renderPage();
    await screen.findByRole("heading", { level: 1, name: "커피 연구" });
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/x1/chat/sessions"));
    await act(async () => created.resolve(session));
    await waitFor(() => expect(nav.router.replace).toHaveBeenCalledWith("/notebooks/x1?s=s-new", { scroll: false }));
  });

  it("첫 대화를 만드는 중에 노트북을 떠나는 링크(브레드크럼 「노트북」)를 누르면, 늦게 끝난 생성이 이 노트북으로 되돌려 보내지 않는다", async () => {
    const created = prepareFreshNotebook();
    renderPage();
    await screen.findByRole("heading", { level: 1, name: "커피 연구" });
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/x1/chat/sessions"));
    // jsdom은 실제 페이지 이동을 하지 않는다 — 기본 동작만 막고(버블 단계), 페이지의 캡처 단계 감지는 그대로 둔다
    const stopNavigation = (e: MouseEvent) => e.preventDefault();
    document.addEventListener("click", stopNavigation);
    try {
      fireEvent.click(within(screen.getByRole("navigation", { name: "위치" })).getByRole("link", { name: "노트북" }));
      await act(async () => created.resolve(session));
      await settle(50);
      expect(nav.router.replace).not.toHaveBeenCalledWith("/notebooks/x1?s=s-new", expect.anything());
    } finally {
      document.removeEventListener("click", stopNavigation);
    }
  });

  it("같은 화면 안 링크·다운로드 링크는 떠나는 것이 아니다 — 자동 선택이 그대로 반영된다", async () => {
    const created = prepareFreshNotebook();
    renderPage();
    await screen.findByRole("heading", { level: 1, name: "커피 연구" });
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/x1/chat/sessions"));
    const stopNavigation = (e: MouseEvent) => e.preventDefault();
    document.addEventListener("click", stopNavigation);
    const same = document.createElement("a");
    same.href = "/notebooks/x1#anchor";
    const download = document.createElement("a");
    download.href = "/exports/file.md";
    download.setAttribute("download", "file.md");
    document.body.append(same, download);
    try {
      fireEvent.click(same);
      fireEvent.click(download);
      await act(async () => created.resolve(session));
      await waitFor(() => expect(nav.router.replace).toHaveBeenCalledWith("/notebooks/x1?s=s-new", { scroll: false }));
    } finally {
      document.removeEventListener("click", stopNavigation);
      same.remove();
      download.remove();
    }
  });
});

describe("대화 바·우측 탭·모바일 하단 탭(AC-3.3·AC-3.4)", () => {
  it("「새 대화」 실행 컨트롤은 화면에 1개이고, 대화 기록 트리거는 「대화: {제목}」이다", async () => {
    nav.search = "s=s1";
    routes["/api/v1/notebooks/x1/chat/sessions"] = [
      { id: "s1", notebook_id: "x1", title: "첫 대화", pinned: false, created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z" },
    ];
    renderPage();
    expect(await screen.findByRole("button", { name: "대화: 첫 대화" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "새 대화" })).toHaveLength(1);
  });

  it("데스크톱 우측 탭은 tablist 「소스·메모·에이전트」이고 화살표·Home·End로 옮기면 바로 선택된다", async () => {
    const user = userEvent.setup();
    renderPage();
    const tablist = await screen.findByRole("tablist", { name: "오른쪽 패널" });
    const tabs = within(tablist).getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual(["소스", "메모", "에이전트"]);
    expect(tabs.map((t) => t.dataset.testid)).toEqual(["right-tab-files", "right-tab-notes", "right-tab-agents"]);
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");
    expect(tabs[0]).toHaveAttribute("tabindex", "0");
    expect(tabs[1]).toHaveAttribute("tabindex", "-1");
    // 좁은 패널에서는 탭 줄만 가로 스크롤하고 접기 버튼은 줄지 않는다(1024 폭 촬영에서 잘림을 찾아 고침)
    expect(tablist).toHaveClass("min-w-0", "flex-1", "overflow-x-auto");
    expect(screen.getByTestId("toggle-right-panel")).toHaveClass("shrink-0");
    // 탭 ↔ 패널 연결
    const panel = document.getElementById(tabs[0].getAttribute("aria-controls") ?? "");
    expect(panel).toHaveAttribute("role", "tabpanel");
    expect(panel).toHaveAttribute("aria-labelledby", tabs[0].id);

    act(() => tabs[0].focus());
    await user.keyboard("{ArrowRight}");
    expect(tabs[1]).toHaveFocus();
    expect(tabs[1]).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{End}");
    expect(tabs[2]).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowRight}");
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowLeft}");
    expect(tabs[2]).toHaveFocus();
    await user.keyboard("{Home}");
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");
    // 선택한 탭은 저장값 형식(files|notes|agents)을 그대로 쓴다(§8.2)
    await user.click(tabs[1]);
    expect(JSON.parse(localStorage.getItem("nb-right-panel") ?? "{}")).toEqual({ open: true, tab: "notes" });
  });

  it("모바일 하단 탭은 「채팅·소스·메모·에이전트」 4개(각 44px 이상 클래스)이고 「내보내기」 탭은 없다", async () => {
    stubMedia(false);
    const user = userEvent.setup();
    renderPage();
    const bar = await screen.findByRole("navigation", { name: "패널 전환" });
    const buttons = within(bar).getAllByRole("button");
    expect(buttons.map((b) => b.textContent)).toEqual(["채팅", "소스", "메모", "에이전트"]);
    for (const b of buttons) expect(b).toHaveClass("min-h-11", "min-w-11");
    expect(screen.queryByTestId("mobile-tab-export")).toBeNull();
    expect(screen.queryByRole("tablist", { name: "오른쪽 패널" })).toBeNull();

    await user.click(within(bar).getByRole("button", { name: "소스" }));
    expect(within(bar).getByRole("button", { name: "소스" })).toHaveAttribute("aria-pressed", "true");
    // 모바일에서도 헤더(h1·더보기)는 같다
    expect(screen.getByRole("heading", { level: 1, name: "커피 연구" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "노트북 동작 더보기" })).toBeInTheDocument();
  });
});

describe("소스 상세 — 목록 아래가 아니라 우측 패널(모바일은 소스 화면) 전체", () => {
  const SOURCE = {
    id: "s1",
    notebook_id: "x1",
    type: "text",
    title: "원두 메모",
    origin_url: null,
    mime_type: null,
    size_bytes: null,
    status: "ready",
    error_message: null,
    token_count: 42,
    chunk_count: 2,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
  };

  beforeEach(() => {
    routes["/api/v1/notebooks/x1/sources"] = [SOURCE];
    routes["/api/v1/sources/s1"] = { ...SOURCE, parsed_preview: "원두 본문", summary: null, keywords: null };
  });
  afterEach(() => act(() => useSourceFocus.getState().clear()));

  it("데스크톱: 소스를 누르면 소스 탭 패널 전체가 상세가 되고, 「목록」으로 돌아온다", async () => {
    const user = userEvent.setup();
    renderPage();
    const filesPanel = await screen.findByRole("tabpanel", { name: "소스" });
    await user.click(await within(filesPanel).findByRole("button", { name: "원두 메모" }));

    const view = within(filesPanel).getByTestId("source-detail-view");
    expect(within(filesPanel).queryByTestId("source-list")).toBeNull();
    await waitFor(() =>
      expect(within(view).getByRole("heading", { level: 2, name: "원두 메모" })).toHaveFocus(),
    );
    expect(await within(view).findByText("원두 본문")).toBeInTheDocument();
    // 채팅은 그대로 있고 대화상자는 열리지 않는다
    expect(screen.getByTestId("chat-input")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();

    await user.click(within(view).getByTestId("source-detail-back"));
    await waitFor(() =>
      expect(within(filesPanel).getByRole("button", { name: "원두 메모" })).toHaveFocus(),
    );
  });

  it("데스크톱: 다른 탭(메모)에 있을 때 인용 요청이 오면 소스 탭으로 바뀌고 그 소스 상세가 열린다", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId("right-tab-notes"));
    expect(screen.getByTestId("right-tab-notes")).toHaveAttribute("aria-selected", "true");
    await screen.findByRole("button", { name: "원두 메모" });

    act(() => useSourceFocus.getState().request("s1"));

    expect(await screen.findByTestId("source-detail-view")).toBeInTheDocument();
    expect(screen.getByTestId("right-tab-files")).toHaveAttribute("aria-selected", "true");
    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 2, name: "원두 메모" })).toHaveFocus(),
    );
    expect(useSourceFocus.getState().requestedSourceId).toBeNull();
  });

  it("모바일: 하단 탭 「소스」 화면에서도 같게 열고 돌아오며, 채팅 화면에서 인용 요청이 오면 소스 화면으로 바뀌어 상세가 열린다", async () => {
    stubMedia(false);
    const user = userEvent.setup();
    renderPage();
    const bar = await screen.findByRole("navigation", { name: "패널 전환" });
    await user.click(within(bar).getByRole("button", { name: "소스" }));
    await user.click(await screen.findByRole("button", { name: "원두 메모" }));

    const view = screen.getByTestId("source-detail-view");
    expect(screen.queryByTestId("source-list")).toBeNull();
    await waitFor(() =>
      expect(within(view).getByRole("heading", { level: 2, name: "원두 메모" })).toHaveFocus(),
    );
    await user.click(within(view).getByTestId("source-detail-back"));
    await waitFor(() => expect(screen.getByRole("button", { name: "원두 메모" })).toHaveFocus());

    // 채팅 화면에 있을 때 인용 요청 → 소스 화면으로 바뀌고 상세가 열린다
    await user.click(within(bar).getByRole("button", { name: "채팅" }));
    act(() => useSourceFocus.getState().request("s1"));
    expect(await screen.findByTestId("source-detail-view")).toBeInTheDocument();
    expect(within(bar).getByRole("button", { name: "소스" })).toHaveAttribute("aria-pressed", "true");
    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 2, name: "원두 메모" })).toHaveFocus(),
    );
  });
});

describe("오류 화면", () => {
  it("없는 노트북(404)은 「노트북을 찾을 수 없습니다.」와 「노트북 홈으로」 링크를 보인다(「대시보드」 말 없음)", async () => {
    api.get.mockImplementation((path: string) =>
      path === "/api/v1/notebooks/x1" ? Promise.reject(new ApiError(404, "not found")) : never(),
    );
    renderPage();
    expect(await screen.findByText("노트북을 찾을 수 없습니다.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "노트북 홈으로" })).toHaveAttribute("href", "/");
    expect(screen.queryByText(/대시보드/)).toBeNull();
    expect(screen.queryByRole("button", { name: "다시 시도" })).toBeNull();
  });

  it("그 밖의 실패는 「다시 시도」로 다시 읽고, 성공하면 포커스가 페이지 맨 앞(브레드크럼 「노트북」)에 온다", async () => {
    const user = userEvent.setup();
    let fail = true;
    api.get.mockImplementation((path: string) => {
      if (path !== "/api/v1/notebooks/x1") return never();
      return fail ? Promise.reject(new ApiError(500, "boom")) : Promise.resolve(detail);
    });
    renderPage();
    expect(await screen.findByText("노트북을 불러오지 못했습니다")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    expect(await screen.findByRole("heading", { level: 1, name: "커피 연구" })).toBeInTheDocument();
    // P2 HCI-P2-2 — 오류 화면 전체가 작업 화면으로 바뀌므로 살아남은 경계(셸 main — 여기서는 렌더 컨테이너)의
    // 첫 요소인 브레드크럼 「노트북」으로 포커스가 온다(body로 떨어지지 않는다 — 새 페이지를 연 것과 같은 출발점)
    await waitFor(() =>
      expect(
        within(screen.getByRole("navigation", { name: "위치" })).getByRole("link", { name: "노트북" }),
      ).toHaveFocus(),
    );
  });
});

describe("마인드맵 노드 질문 → 모바일 채팅 화면 전환(mindmap-agent-skill AC-2.7)", () => {
  afterEach(() => act(() => useChatPrompt.setState({ requests: {}, busyNonce: null })));

  it("mindmap_prompt_mobile: 모바일에서 이 노트북 요청이 생기면 채팅 화면으로 옮긴다(다른 노트북 요청은 무시)", async () => {
    stubMedia(false);
    const user = userEvent.setup();
    renderPage();
    const bar = await screen.findByRole("navigation", { name: "패널 전환" });
    await user.click(within(bar).getByRole("button", { name: "에이전트" }));
    expect(within(bar).getByRole("button", { name: "에이전트" })).toHaveAttribute("aria-pressed", "true");

    act(() => {
      useChatPrompt.getState().request({ notebookId: "other", text: "다른 곳", sourceIds: null, origin: "mindmap" });
    });
    expect(within(bar).getByRole("button", { name: "에이전트" })).toHaveAttribute("aria-pressed", "true");

    act(() => {
      useChatPrompt.getState().request({ notebookId: "x1", text: "질문", sourceIds: null, origin: "mindmap" });
    });
    await waitFor(() =>
      expect(within(bar).getByRole("button", { name: "채팅" })).toHaveAttribute("aria-pressed", "true"),
    );
  });
});
