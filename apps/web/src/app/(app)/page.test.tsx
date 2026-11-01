import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DASHBOARD_VIEW_STORAGE_KEY } from "@/components/dashboard/view-mode-toggle";
import type { Notebook } from "@/lib/types";
import DashboardPage from "./page";

// 대시보드 배선(plan Phase 1) — 필터·뷰 모드·선택이 함께 동작하는지 실제 훅·컴포넌트로 본다.
// ui-ux-redesign P3(노트북 홈 — plan §6.6.2, AC-3.6·3.7): PageHeader·Toolbar·선택 바·카드·빈/오류/로딩 상태·생성 후 이동.
// 네트워크 경계(api)·App Router·토스트만 대체한다.

const { api, toast, router } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
  toast: { success: vi.fn(), error: vi.fn() },
  router: { push: vi.fn(), replace: vi.fn(), prefetch: vi.fn(), back: vi.fn(), refresh: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("sonner", () => ({ toast }));
vi.mock("next/navigation", () => ({ useRouter: () => router }));

function nb(id: string, overrides: Partial<Notebook> = {}): Notebook {
  return {
    id,
    owner_id: "me",
    title: `노트북 ${id}`,
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
    ...overrides,
  };
}

let lists: Record<string, Notebook[]>;

beforeEach(() => {
  localStorage.clear();
  lists = {
    all: [
      nb("c", { tags: ["research"], is_favorite: true }),
      nb("s", { owner_id: "other", is_owner: false, permission: "read" }),
      nb("a", { tags: ["research"] }),
      nb("b"),
    ],
    mine: [nb("c", { tags: ["research"], is_favorite: true }), nb("a", { tags: ["research"] }), nb("b")],
    shared: [nb("s", { owner_id: "other", is_owner: false, permission: "read" })],
  };
  api.get.mockImplementation((path: string) => {
    const match = /^\/api\/v1\/notebooks\?filter=(all|mine|shared)$/.exec(path);
    if (match) return Promise.resolve(lists[match[1]]);
    if (path === "/api/v1/tags") return Promise.resolve([]);
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
});

afterEach(() => vi.resetAllMocks());

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <DashboardPage />
    </QueryClientProvider>,
  );
  return client;
}

const count = () => screen.queryByTestId("bulk-selected-count")?.textContent ?? "0개";
const box = (title: string) => screen.getByRole("checkbox", { name: `${title} 선택` });
const selectAll = () => screen.getByRole("checkbox", { name: "표시된 노트북 모두 선택" });
const view = (name: "카드" | "표") => screen.getByRole("radio", { name });
const bulkBar = () => screen.getByRole("group", { name: "노트북 선택" });
/** 위험 색을 입히는 클래스(입력 오류 표시용 aria-invalid 변형은 버튼 기본 클래스라 뺀다). */
const dangerTokens = (el: HTMLElement) =>
  el.className.split(/\s+/).filter((c) => c.includes("destructive") && !c.includes("aria-invalid"));

/** 툴바의 태그 필터(Select)에서 항목을 고른다 — 「전체 태그」는 필터 해제. */
async function chooseTag(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(screen.getByRole("combobox", { name: "태그 필터" }));
  await user.click(await screen.findByRole("option", { name }));
}

describe("DashboardPage — 테이블 뷰·선택 모델", () => {
  it("AC-1.1 카드와 테이블이 같은 visibleNotebooks를 같은 순서로 그리고 기본은 카드다", async () => {
    const user = userEvent.setup();
    renderPage();
    const cards = await screen.findAllByTestId("notebook-card");
    const cardTitles = cards.map((c) => within(c).getByRole("heading").textContent);
    expect(cardTitles).toEqual(["노트북 c", "노트북 s", "노트북 a", "노트북 b"]);
    expect(screen.queryByTestId("notebook-table")).not.toBeInTheDocument();

    await user.click(view("표"));
    const rowTitles = screen
      .getAllByTestId("notebook-row")
      .map((r) => within(r).getAllByRole("link")[0].textContent?.replace("📓", ""));
    expect(rowTitles).toEqual(cardTitles);
    expect(screen.queryByTestId("notebook-card")).not.toBeInTheDocument();

    // 필터(태그) 적용 결과도 두 뷰가 같다
    await chooseTag(user, "research");
    expect(screen.getAllByTestId("notebook-row")).toHaveLength(2);
    await user.click(view("카드"));
    expect(
      screen.getAllByTestId("notebook-card").map((c) => within(c).getByRole("heading").textContent),
    ).toEqual(["노트북 c", "노트북 a"]);
  });

  it("AC-1.2 저장된 table 보기를 페이지 진입 때 복원한다", async () => {
    localStorage.setItem(DASHBOARD_VIEW_STORAGE_KEY, "table");
    renderPage();
    expect(await screen.findByTestId("notebook-table")).toBeInTheDocument();
    expect(view("표")).toHaveAttribute("aria-checked", "true");
  });

  it("AC-1.5 뷰 모드 토글은 선택을 유지한다(카드 ↔ 테이블 왕복 후 같은 개수)", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByTestId("notebook-card");
    await user.click(box("노트북 a"));
    await user.click(box("노트북 b"));
    expect(count()).toBe("2개 선택됨");

    await user.click(view("표"));
    expect(count()).toBe("2개 선택됨");
    expect(box("노트북 a")).toHaveAttribute("aria-checked", "true");
    expect(selectAll()).toHaveAttribute("aria-checked", "mixed");

    await user.click(view("카드"));
    expect(count()).toBe("2개 선택됨");
    expect(box("노트북 b")).toHaveAttribute("aria-checked", "true");
  });

  it("AC-1.5 탭 필터를 바꾸면 선택이 0이 된다(새 필터에 같은 항목이 남아도)", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByTestId("notebook-card");
    await user.click(selectAll());
    expect(count()).toBe("3개 선택됨");

    await user.click(screen.getByRole("tab", { name: "내 노트북" }));
    await waitFor(() => expect(screen.getAllByTestId("notebook-card")).toHaveLength(3));
    expect(count()).toBe("0개");
    expect(box("노트북 a")).toHaveAttribute("aria-checked", "false");
  });

  it("AC-1.5 즐겨찾기만을 켜거나 끄면 선택이 0이 된다", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByTestId("notebook-card");
    await user.click(box("노트북 c"));
    await user.click(screen.getByTestId("favorites-only-toggle"));
    expect(screen.getAllByTestId("notebook-card")).toHaveLength(1);
    expect(count()).toBe("0개");

    await user.click(box("노트북 c"));
    expect(count()).toBe("1개 선택됨");
    await user.click(screen.getByTestId("favorites-only-toggle"));
    expect(count()).toBe("0개");
  });

  it("AC-1.5 태그 필터를 바꾸면 선택이 0이 된다", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByTestId("notebook-card");
    await user.click(box("노트북 a"));
    await chooseTag(user, "research");
    expect(count()).toBe("0개");

    await user.click(box("노트북 a"));
    expect(count()).toBe("1개 선택됨");
    await chooseTag(user, "전체 태그");
    expect(count()).toBe("0개");
    expect(screen.getAllByTestId("notebook-card")).toHaveLength(4);
  });

  it("AC-1.4 선택한 뒤 refetch로 항목이 사라지면 개수·모두 선택 상태에서 즉시 빠진다", async () => {
    const user = userEvent.setup();
    const client = renderPage();
    await screen.findAllByTestId("notebook-card");
    await user.click(box("노트북 a"));
    await user.click(box("노트북 b"));
    expect(count()).toBe("2개 선택됨");
    expect(selectAll()).toHaveAttribute("aria-checked", "mixed");

    lists.all = lists.all.filter((n) => n.id !== "b" && n.id !== "c");
    await act(() => client.invalidateQueries({ queryKey: ["notebooks"] }));
    await waitFor(() => expect(screen.getAllByTestId("notebook-card")).toHaveLength(2));
    expect(count()).toBe("1개 선택됨");
    expect(selectAll()).toHaveAttribute("aria-checked", "true");
  });

  it("AC-1.3·AC-1.6 공유 탭처럼 선택 가능 항목이 없으면 모두 선택이 비활성이다", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByTestId("notebook-card");
    await user.click(screen.getByRole("tab", { name: "공유받은 노트북" }));
    await waitFor(() => expect(screen.getAllByTestId("notebook-card")).toHaveLength(1));
    expect(selectAll()).toHaveAttribute("aria-disabled", "true");
    await user.click(selectAll());
    expect(count()).toBe("0개");
  });
});

describe("DashboardPage — 일괄 삭제(Phase 2)", () => {
  const deleteButton = () => within(bulkBar()).getByRole("button", { name: "삭제" });
  const deletedIds = () => api.delete.mock.calls.map(([p]) => String(p).replace("/api/v1/notebooks/", ""));

  /** 서버처럼 동작하는 DELETE — 지정한 오류를 던지고, 성공이면 목록에서 뺀다. */
  function serverDeletes(errors: Record<string, unknown> = {}) {
    api.delete.mockImplementation(async (path: string) => {
      const id = path.replace("/api/v1/notebooks/", "");
      if (id in errors) throw errors[id];
      for (const key of Object.keys(lists)) lists[key] = lists[key].filter((n) => n.id !== id);
    });
  }

  it("AC-1.7 재확인 — 실제 일괄 「삭제」 버튼은 0개면 비활성, 1개 이상이면 활성이다", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByTestId("notebook-card");
    expect(deleteButton()).toBeDisabled();
    await user.click(box("노트북 a"));
    expect(count()).toBe("1개 선택됨");
    expect(deleteButton()).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "선택 해제" }));
    expect(deleteButton()).toBeDisabled();

    await user.click(view("표"));
    await user.click(box("노트북 b"));
    expect(deleteButton()).toBeEnabled();
  });

  it("AC-2.6 모두 선택 뒤 삭제해도 DELETE 대상은 is_owner=true 항목뿐이고 확인 목록도 같다", async () => {
    const user = userEvent.setup();
    serverDeletes();
    const client = renderPage();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await screen.findAllByTestId("notebook-card");
    await user.click(screen.getByRole("checkbox", { name: /노트북 s 선택 불가/ })); // 비활성 — 무시된다
    await user.click(selectAll());
    expect(count()).toBe("3개 선택됨");

    await user.click(deleteButton());
    const dialog = screen.getByRole("alertdialog");
    expect(
      within(within(dialog).getByRole("list", { name: "삭제할 노트북" }))
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual(["노트북 c", "노트북 a", "노트북 b"]); // 표시 순서, 비소유 s 없음
    await user.click(within(dialog).getByRole("button", { name: "노트북 3개 영구 삭제" }));
    await within(dialog).findByTestId("bulk-result-summary");

    expect(deletedIds()).toEqual(["c", "a", "b"]); // 모두 is_owner=true
    expect(deletedIds()).not.toContain("s");
    expect(invalidate).toHaveBeenCalledTimes(1);
    expect(toast.success).toHaveBeenCalledWith("삭제 3 · 실패 0 · 확인 불가 0 · 미실행 0");

    await user.click(within(dialog).getByRole("button", { name: "닫기" }));
    await waitFor(() => expect(screen.getAllByTestId("notebook-card")).toHaveLength(1));
    expect(count()).toBe("0개");
  });

  it("AC-2.5·AC-2.6 서버 403·500·응답 손실 항목은 실패로 보고되고 선택에 남으며 성공 항목만 빠진다", async () => {
    const user = userEvent.setup();
    const { ApiError } = await import("@/lib/api-client");
    serverDeletes({
      a: new ApiError(403, "Only the owner can delete"), // 권한이 바뀐 경우를 시뮬레이션
      b: new TypeError("Failed to fetch"),
    });
    lists.all.push(nb("d"));
    const client = renderPage();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await screen.findAllByTestId("notebook-card");
    await user.click(selectAll());
    expect(count()).toBe("4개 선택됨");

    await user.click(deleteButton());
    await user.click(screen.getByRole("button", { name: "노트북 4개 영구 삭제" }));
    const summary = await screen.findByTestId("bulk-result-summary");
    expect(summary).toHaveTextContent("삭제 2 · 실패 1 · 확인 불가 1 · 미실행 0");
    expect(deletedIds()).toEqual(["c", "a", "b", "d"]); // 403 뒤에도 나머지를 계속 처리
    const rows = screen.getAllByTestId("bulk-result-item");
    expect(rows.map((r) => r.dataset.status)).toEqual(["succeeded", "failed", "failed", "succeeded"]);
    expect(rows[1]).toHaveTextContent("권한 없음 — 소유자만 할 수 있습니다");
    expect(rows[2]).toHaveTextContent("처리 여부를 확인할 수 없습니다");
    expect(toast.error).toHaveBeenCalledWith("삭제 2 · 실패 1 · 확인 불가 1 · 미실행 0");
    expect(invalidate).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "닫기" }));
    await waitFor(() => expect(screen.getAllByTestId("notebook-card")).toHaveLength(3));
    expect(count()).toBe("2개 선택됨");
    expect(box("노트북 a")).toHaveAttribute("aria-checked", "true");
    expect(box("노트북 b")).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByText("노트북 c")).not.toBeInTheDocument();

    // 같은 버튼으로 실패 항목만 다시 실행할 수 있다
    await user.click(deleteButton());
    expect(screen.getByRole("heading", { name: "노트북 2개를 영구 삭제할까요?" })).toBeInTheDocument();
  });

  it("취소하면 DELETE 0건이고 선택이 그대로다", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByTestId("notebook-card");
    await user.click(box("노트북 a"));
    await user.click(deleteButton());
    await user.click(screen.getByRole("button", { name: "취소" }));
    expect(api.delete).not.toHaveBeenCalled();
    expect(count()).toBe("1개 선택됨");
    expect(deleteButton()).toBeEnabled();
  });
});

describe("DashboardPage — 일괄 공유(Phase 3, core-v0.20.0 그룹판)", () => {
  const G = "group-research";
  const GROUPS = [
    { id: "group-alpha", name: "가나 그룹" },
    { id: G, name: "연구팀" },
  ];
  const shareButton = () => within(bulkBar()).getByRole("button", { name: "공유" });
  const touched = (method: "get" | "post" | "delete") =>
    api[method].mock.calls
      .map(([p]) => String(p))
      .filter((p) => /\/api\/v1\/notebooks\/[^/?]+\/share/.test(p))
      .map((p) => p.split("/")[4]);

  /** 서버처럼 동작하는 공유 API — 내 그룹, 기존 그룹 공유 행, 노트북별 POST 오류, 소유자 비소속 그룹(404)을 흉내 낸다. */
  async function serverShares(
    opts: {
      groups?: { id: string; name: string }[];
      existing?: Record<string, { permission: "read" | "write" }[]>;
      failPost?: Record<string, unknown>;
      notMember?: boolean;
    } = {},
  ) {
    const { ApiError } = await import("@/lib/api-client");
    const listNotebooks = api.get.getMockImplementation();
    api.get.mockImplementation((path: string) => {
      if (path === "/api/v1/me/groups") return Promise.resolve(opts.groups ?? GROUPS);
      const m = /^\/api\/v1\/notebooks\/([^/]+)\/shares$/.exec(path);
      if (m) {
        const rows = (opts.existing?.[m[1]] ?? []).map((r, i) => ({
          id: `old-${m[1]}-${i}`,
          notebook_id: m[1],
          user_id: null,
          group_id: G,
          permission: r.permission,
        }));
        return Promise.resolve(rows);
      }
      return listNotebooks?.(path);
    });
    let seq = 0;
    api.post.mockImplementation(async (path: string, body: { group_id: string; permission: string }) => {
      const id = path.split("/")[4];
      if (opts.failPost && id in opts.failPost) throw opts.failPost[id];
      if (opts.notMember) throw new ApiError(404, "Target group not found");
      seq += 1;
      return { id: `new-${seq}`, notebook_id: id, user_id: null, group_id: body.group_id, permission: body.permission };
    });
    api.delete.mockResolvedValue(undefined);
  }

  async function submitShare(user: ReturnType<typeof userEvent.setup>, groupName: string) {
    await user.click(shareButton());
    const dialog = await screen.findByRole("dialog");
    await user.click(await within(dialog).findByTestId("bulk-share-group-select"));
    await user.click(await screen.findByRole("option", { name: groupName }));
    await waitFor(() => expect(within(dialog).getByTestId("bulk-share-submit")).toBeEnabled());
    await user.click(within(dialog).getByTestId("bulk-share-submit"));
    return dialog;
  }

  it("AC-1.7 재확인 — 일괄 「공유」 버튼은 0개면 비활성, 1개 이상이면 활성이다", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByTestId("notebook-card");
    expect(shareButton()).toBeDisabled();
    await user.click(box("노트북 a"));
    expect(shareButton()).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "선택 해제" }));
    expect(shareButton()).toBeDisabled();
  });

  it("AC-3.8 모두 선택 뒤 공유 — 소유 노트북만 요청하고 본문은 정확히 {group_id, permission}, 공유·건너뜀은 선택에서 빠지며 실패는 남는다", async () => {
    const user = userEvent.setup();
    const { ApiError } = await import("@/lib/api-client");
    await serverShares({
      existing: { a: [{ permission: "write" }] }, // a는 이미 그 그룹 write → read 요청은 건너뜀
      failPost: { b: new ApiError(403, "Only the owner can share") }, // 권한이 바뀐 경우를 시뮬레이션
    });
    const client = renderPage();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await screen.findAllByTestId("notebook-card");
    await user.click(selectAll());
    expect(count()).toBe("3개 선택됨");

    const dialog = await submitShare(user, "연구팀");
    expect(
      await within(dialog).findByTestId("bulk-result-summary", {}, { timeout: 2_000 }),
    ).toHaveTextContent("공유 1 · 건너뜀 1 · 실패 1 · 확인 불가 0 · 미실행 0");
    expect(touched("get")).toEqual(["c", "a", "b"]); // 표시 순서, 비소유 s 0건
    expect(touched("post")).toEqual(["c", "b"]);
    expect(api.post.mock.calls.map(([, body]) => body)).toStrictEqual([
      { group_id: G, permission: "read", mode: "upgrade_only" },
      { group_id: G, permission: "read", mode: "upgrade_only" },
    ]);
    expect(api.delete).not.toHaveBeenCalled();
    expect(invalidate.mock.calls).toEqual([[{ queryKey: ["notebooks"] }], [{ queryKey: ["notebook-shares"] }]]);
    expect(toast.error).toHaveBeenCalledWith("공유 1 · 건너뜀 1 · 실패 1 · 확인 불가 0 · 미실행 0");

    await user.click(within(dialog).getByRole("button", { name: "닫기" }));
    expect(count()).toBe("1개 선택됨");
    expect(box("노트북 b")).toHaveAttribute("aria-checked", "true");
    expect(box("노트북 a")).toHaveAttribute("aria-checked", "false");
    expect(box("노트북 c")).toHaveAttribute("aria-checked", "false");
  });

  it("AC-3.4 소유자가 속하지 않은 그룹(404)이면 대상 중단 안내 — 공유된 항목 0이고 선택 전체가 유지된다", async () => {
    const user = userEvent.setup();
    await serverShares({ notMember: true });
    renderPage();
    await screen.findAllByTestId("notebook-card");
    await user.click(selectAll());
    const dialog = await submitShare(user, "연구팀");
    const summary = await within(dialog).findByTestId("bulk-result-summary");
    expect(summary).toHaveTextContent(/^공유 0 · 건너뜀 0 · 실패 [1-3] · 확인 불가 0 · 미실행 [0-2]$/);
    expect(within(dialog).getByTestId("bulk-result-notice")).toHaveTextContent("이 그룹에는 공유할 수 없어 중단했습니다");
    expect(api.delete).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "닫기" }));
    expect(count()).toBe("3개 선택됨");
  });

  it("그룹이 0개면 페이지에서도 공유 요청 0건이고 선택은 그대로다 — 관리자에게 요청하라는 안내", async () => {
    const user = userEvent.setup();
    await serverShares({ groups: [] });
    renderPage();
    await screen.findAllByTestId("notebook-card");
    await user.click(box("노트북 a"));
    await user.click(shareButton());
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByTestId("bulk-share-no-groups")).toHaveTextContent(
      "공유할 그룹이 없습니다 — 관리자에게 그룹을 요청하세요",
    );
    expect(within(dialog).getByTestId("bulk-share-submit")).toBeDisabled();
    expect(touched("get")).toEqual([]);
    expect(api.post).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "취소" }));
    expect(count()).toBe("1개 선택됨");
  });
});

describe("DashboardPage — 단건 공유 관리(core-v0.20.0 AC-3.1·3.2 — 카드 메뉴 진입)", () => {
  /** 카드 메뉴 「공유」 — 대화상자는 목록 캐시에서 그 노트북을 찾아 연다(토글이 visibility·is_owner를 쓴다). */
  async function openShare(user: ReturnType<typeof userEvent.setup>, title: string) {
    await user.click(screen.getByRole("button", { name: `${title} 동작 더보기` }));
    await user.click(await screen.findByRole("menuitem", { name: "공유" }));
    return screen.findByRole("dialog", { name: "노트북 공유" });
  }

  beforeEach(() => {
    const listNotebooks = api.get.getMockImplementation();
    api.get.mockImplementation((path: string) => {
      if (path === "/api/v1/me/groups") return Promise.resolve([]);
      if (/^\/api\/v1\/notebooks\/[^/]+\/shares$/.test(path)) return Promise.resolve([]);
      return listNotebooks?.(path);
    });
  });

  it("카드 메뉴 「공유」는 그 노트북의 공유 관리 대화상자를 열고, 전체 공개 토글은 목록의 visibility를 보이며 켜면 본문이 정확히 {visibility}다", async () => {
    const user = userEvent.setup();
    lists.all = lists.all.map((n) => (n.id === "a" ? { ...n, visibility: "public" as const } : n));
    api.patch.mockImplementation(async (_path: string, body: { visibility: string }) => ({ ...nb("b"), ...body }));
    renderPage();
    await screen.findAllByTestId("notebook-card");

    let dialog = await openShare(user, "노트북 a");
    expect(dialog).toHaveAttribute("data-testid", "share-dialog");
    expect(within(dialog).getByRole("switch", { name: "전체 공개" })).toHaveAttribute("aria-checked", "true");
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/api/v1/notebooks/a/shares"));
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "노트북 공유" })).toBeNull());

    dialog = await openShare(user, "노트북 b");
    const toggle = within(dialog).getByRole("switch", { name: "전체 공개" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    await user.click(toggle);
    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    expect(api.patch.mock.calls[0]).toStrictEqual(["/api/v1/notebooks/b", { visibility: "public" }]);
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("전체 공개를 켰습니다", expect.anything()));
    expect(toggle).toHaveAttribute("aria-checked", "true");
  });

  it("열린 동안 그 노트북이 목록에서 사라지면(다른 곳에서 삭제) 대화상자를 닫는다", async () => {
    const user = userEvent.setup();
    const client = renderPage();
    await screen.findAllByTestId("notebook-card");
    await openShare(user, "노트북 a");
    lists.all = lists.all.filter((n) => n.id !== "a");
    await act(async () => {
      await client.invalidateQueries({ queryKey: ["notebooks"] });
    });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "노트북 공유" })).toBeNull());
  });
});

describe("DashboardPage — 홈 헤더 진입점 단일화(ui-ux-redesign AC-2.8 홈 부분, plan §6.2.4)", () => {
  it("헤더에 검색 버튼이 없고, 헤더 버튼은 「새 노트북」 하나다", async () => {
    renderPage();
    await screen.findAllByTestId("notebook-card");

    // 전역 검색은 사이드바 검색 트리거·⌘K로 단일화됐다.
    expect(screen.queryByTestId("dashboard-search-trigger")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /검색/ })).not.toBeInTheDocument();

    // 헤더는 공용 PageHeader(제목 h1 하나 + 액션 영역) — 액션은 주 버튼 「새 노트북」 하나(plan §6.6.2)
    expect(screen.getAllByRole("heading", { level: 1 }).map((h) => h.textContent)).toEqual(["노트북"]);
    const actions = document.querySelector<HTMLElement>("[data-slot=page-header-actions]");
    expect(actions).not.toBeNull();
    const headerButtons = within(actions as HTMLElement).getAllByRole("button");
    expect(headerButtons.map((b) => b.textContent?.trim())).toEqual(["새 노트북"]);
    expect(headerButtons[0]).toHaveAttribute("data-testid", "create-notebook");
  });

  it("「새 노트북」은 생성 다이얼로그를 연다", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByTestId("notebook-card");
    await user.click(screen.getByRole("button", { name: "새 노트북" }));
    expect(await screen.findByRole("dialog", { name: "새 노트북 만들기" })).toBeInTheDocument();
  });
});

describe("DashboardPage — 「공유받은 노트북」 탭 = 내 것이 아닌 볼 수 있는 노트북 전부", () => {
  it("공유 기록 없이 공개된 노트북(샘플 등)도 공유받은 탭에 보이고, 카드에서 「공개」로 구분한다", async () => {
    const user = userEvent.setup();
    // 실제 API처럼 공개 노트북은 `all`에만 있고 `shared`(공유 기록 기준)에는 없다
    lists.all.push(nb("p", { title: "샘플 노트북", owner_id: "other", is_owner: false, permission: "read", visibility: "public" }));
    renderPage();
    await screen.findAllByTestId("notebook-card");
    await user.click(screen.getByRole("tab", { name: "공유받은 노트북" }));

    await waitFor(() => expect(screen.getAllByTestId("notebook-card")).toHaveLength(2));
    const badgeOf = (title: string) => {
      const card = screen.getAllByTestId("notebook-card").find((c) => c.textContent?.includes(title));
      return card?.querySelector("[data-slot=status-badge]")?.textContent;
    };
    expect(badgeOf("샘플 노트북")).toBe("공개");
    expect(badgeOf("노트북 s")).toBe("공유받음");
    // 내 노트북은 이 탭에 없다
    expect(screen.queryByText("노트북 c")).toBeNull();
    // 코어 `shared`(공유 기록만)가 아니라 `all`을 받아 고른다
    const paths = api.get.mock.calls.map(([p]) => String(p));
    expect(paths).not.toContain("/api/v1/notebooks?filter=shared");
  });

  it("탭 줄은 가로만 스크롤한다 — 활성 탭 밑줄이 세로 스크롤바를 만들지 않는다", async () => {
    renderPage();
    await screen.findAllByTestId("notebook-card");
    const tablist = screen.getByRole("tablist", { name: "노트북 범위" });
    expect(tablist.className).toMatch(/\boverflow-x-auto\b/);
    expect(tablist.className).toMatch(/\boverflow-y-hidden\b/);
  });
});

describe("DashboardPage — 노트북 홈 재설계(ui-ux-redesign P3, plan §6.6.2·AC-3.6·AC-3.7)", () => {
  it("AC-3.7 탭은 「전체 · 내 노트북 · 공유받은 노트북」이고, 표 보기 「권한」 칸은 「소유자/편집 가능/읽기 전용」이다", async () => {
    const user = userEvent.setup();
    lists.all.push(nb("w", { owner_id: "other", is_owner: false, permission: "write" }));
    renderPage();
    await screen.findAllByTestId("notebook-card");
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["전체", "내 노트북", "공유받은 노트북"]);
    expect(screen.getByRole("tablist", { name: "노트북 범위" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "공유 노트북" })).toBeNull();

    await user.click(view("표"));
    const permissionOf = (title: string) => {
      const row = screen.getAllByTestId("notebook-row").find((r) => r.textContent?.includes(title));
      return row ? within(row).getAllByRole("cell")[3].textContent : null;
    };
    expect(permissionOf("노트북 c")).toBe("소유자");
    expect(permissionOf("노트북 s")).toBe("읽기 전용");
    expect(permissionOf("노트북 w")).toBe("편집 가능");
    expect(screen.getByTestId("notebook-table")).not.toHaveTextContent(/\b(owner|write|read)\b/);
  });

  it("툴바 오른쪽은 「즐겨찾기만」 · 태그 필터 · 보기 전환 순서이고, 태그가 없으면 태그 필터를 그리지 않는다", async () => {
    renderPage();
    await screen.findAllByTestId("notebook-card");
    const toolbar = document.querySelector<HTMLElement>("[data-slot=toolbar]");
    expect(toolbar).not.toBeNull();
    const favorites = within(toolbar as HTMLElement).getByTestId("favorites-only-toggle");
    const tagFilter = within(toolbar as HTMLElement).getByRole("combobox", { name: "태그 필터" });
    const viewToggle = within(toolbar as HTMLElement).getByRole("radiogroup", { name: "보기 방식" });
    expect(favorites.compareDocumentPosition(tagFilter) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(tagFilter.compareDocumentPosition(viewToggle) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(tagFilter).toHaveTextContent("전체 태그");
    // 예전 태그 칩 줄은 없다(e2e 사용 0건 — 착수 시 grep 확인)
    expect(screen.queryByTestId("tag-filter-bar")).toBeNull();
  });

  it("태그가 하나도 없으면 태그 필터가 없다", async () => {
    for (const key of Object.keys(lists)) lists[key] = lists[key].map((n) => ({ ...n, tags: [] }));
    renderPage();
    await screen.findAllByTestId("notebook-card");
    expect(screen.queryByRole("combobox", { name: "태그 필터" })).toBeNull();
  });

  it("카드 그리드는 콘텐츠 폭 기준 auto-fill(최소 260px) 목록이고 카드 하나가 항목 하나다(T1)", async () => {
    renderPage();
    const cards = await screen.findAllByTestId("notebook-card");
    const list = screen.getByRole("list", { name: "노트북 목록" });
    expect(list.className).toContain("grid-cols-[repeat(auto-fill,minmax(min(260px,100%),1fr))]");
    expect(list.className).toContain("gap-4");
    // 뷰포트별 고정 열 수(sm:grid-cols-2 …)가 없다
    expect(list.className).not.toMatch(/(sm|md|lg|xl):grid-cols-/);
    expect(within(list).getAllByRole("listitem")).toHaveLength(cards.length);
  });

  describe("AC-3.6 선택 바 — 0개면 일괄 버튼이 outline·비활성이고 위험 틴트가 아니다", () => {
    it("0개: 「표시된 노트북 모두 선택」과 비활성 「공유」「삭제」만 — 개수·선택 해제 없음, 빨강 없음", async () => {
      renderPage();
      await screen.findAllByTestId("notebook-card");
      const bar = bulkBar();
      expect(within(bar).getByRole("checkbox", { name: "표시된 노트북 모두 선택" })).toBeInTheDocument();
      expect(within(bar).queryByTestId("bulk-selected-count")).toBeNull();
      expect(within(bar).queryByRole("button", { name: "선택 해제" })).toBeNull();
      const buttons = within(bar).getAllByRole("button");
      expect(buttons.map((b) => b.textContent?.trim())).toEqual(["공유", "삭제"]);
      for (const b of buttons) {
        expect(b).toBeDisabled();
        expect(dangerTokens(b)).toEqual([]); // 틴트 배경·위험 글자 모두 없음
        expect(b.className).toMatch(/(^|\s)border-border(\s|$)/); // outline 변형
      }
    });

    it("1개 이상: 「N개 선택됨 · 선택 해제 · 공유 · 삭제」, 삭제만 위험 글자(틴트 배경 없음)", async () => {
      const user = userEvent.setup();
      renderPage();
      await screen.findAllByTestId("notebook-card");
      await user.click(box("노트북 a"));
      const bar = bulkBar();
      expect(within(bar).getByTestId("bulk-selected-count")).toHaveTextContent("1개 선택됨");
      const share = within(bar).getByRole("button", { name: "공유" });
      const del = within(bar).getByRole("button", { name: "삭제" });
      expect(within(bar).getByRole("button", { name: "선택 해제" })).toBeEnabled();
      expect(share).toBeEnabled();
      expect(del).toBeEnabled();
      expect(dangerTokens(share)).toEqual([]);
      expect(del).toHaveClass("text-destructive-text");
      // 위험 글자만, 틴트 배경은 hover 때만(변형은 outline 그대로)
      expect(dangerTokens(del).filter((c) => c.startsWith("bg-"))).toEqual([]);
      expect(del.className).toMatch(/(^|\s)border-border(\s|$)/);
    });
  });

  describe("AC-3.6 생성 후 이동(HCI-3)", () => {
    it("「새 노트북」으로 만들면 성공 토스트 뒤 /notebooks/{새 id}로 이동하고 다이얼로그가 닫힌다", async () => {
      const user = userEvent.setup();
      api.post.mockResolvedValue({ ...nb("new-1"), title: "시장 조사" });
      renderPage();
      await screen.findAllByTestId("notebook-card");
      await user.click(screen.getByTestId("create-notebook"));
      const dialog = await screen.findByRole("dialog", { name: "새 노트북 만들기" });
      await user.type(within(dialog).getByLabelText("제목"), "시장 조사");
      await user.click(within(dialog).getByRole("button", { name: "만들기" }));

      await waitFor(() => expect(router.push).toHaveBeenCalledWith("/notebooks/new-1"));
      expect(router.push).toHaveBeenCalledTimes(1);
      expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks", { title: "시장 조사", emoji: "📓" });
      expect(toast.success).toHaveBeenCalledWith("노트북을 만들었습니다", expect.anything());
      await waitFor(() => expect(screen.queryByRole("dialog", { name: "새 노트북 만들기" })).toBeNull());
    });

    it("만들기에 실패하면 이동하지 않고 다이얼로그·입력을 그대로 두며 실패 토스트를 띄운다", async () => {
      const user = userEvent.setup();
      const { ApiError } = await import("@/lib/api-client");
      api.post.mockRejectedValue(new ApiError(500, "boom"));
      renderPage();
      await screen.findAllByTestId("notebook-card");
      await user.click(screen.getByTestId("create-notebook"));
      const dialog = await screen.findByRole("dialog", { name: "새 노트북 만들기" });
      await user.type(within(dialog).getByLabelText("제목"), "실패할 노트북");
      await user.click(within(dialog).getByRole("button", { name: "만들기" }));

      await waitFor(() =>
        expect(toast.error).toHaveBeenCalledWith("노트북을 생성하지 못했습니다", expect.anything()),
      );
      expect(router.push).not.toHaveBeenCalled();
      expect(screen.getByRole("dialog", { name: "새 노트북 만들기" })).toBeInTheDocument();
      expect(within(dialog).getByLabelText("제목")).toHaveValue("실패할 노트북");
    });
  });

  describe("빈·오류·로딩 상태(EmptyState·ErrorState·LoadingState)", () => {
    it("노트북이 없으면 「아직 노트북이 없습니다」와 「새 노트북」 — 누르면 생성 다이얼로그가 열린다", async () => {
      const user = userEvent.setup();
      lists = { all: [], mine: [], shared: [] };
      renderPage();
      expect(await screen.findByText("아직 노트북이 없습니다")).toBeInTheDocument();
      expect(screen.queryByRole("group", { name: "노트북 선택" })).toBeNull(); // 선택 바 없음
      const empty = document.querySelector<HTMLElement>("[data-slot=empty-state]");
      await user.click(within(empty as HTMLElement).getByRole("button", { name: "새 노트북" }));
      expect(await screen.findByRole("dialog", { name: "새 노트북 만들기" })).toBeInTheDocument();
    });

    it("공유받은 노트북이 없으면 그 탭은 안내만 보이고 생성 버튼을 두지 않는다", async () => {
      const user = userEvent.setup();
      // 공유받은 탭은 `all`에서 소유하지 않은 것을 고른다 — 남의 노트북이 하나도 없는 상태
      lists.all = lists.all.filter((n) => n.is_owner);
      lists.shared = [];
      renderPage();
      await screen.findAllByTestId("notebook-card");
      await user.click(screen.getByRole("tab", { name: "공유받은 노트북" }));
      expect(await screen.findByText("공유받은 노트북이 없습니다")).toBeInTheDocument();
      const empty = document.querySelector<HTMLElement>("[data-slot=empty-state]");
      expect(within(empty as HTMLElement).queryByRole("button")).toBeNull();
    });

    it("필터 결과가 없으면 「조건에 맞는 항목이 없습니다」 + 「필터 지우기」로 즐겨찾기만·태그를 함께 푼다", async () => {
      const user = userEvent.setup();
      renderPage();
      await screen.findAllByTestId("notebook-card");
      await chooseTag(user, "research"); // c·a
      await user.click(screen.getByTestId("favorites-only-toggle")); // c
      await user.click(box("노트북 c"));
      lists.all = lists.all.map((n) => ({ ...n, is_favorite: false }));
      // 즐겨찾기를 끈 서버 상태로 다시 읽으면 조건에 맞는 항목이 없다
      await user.click(screen.getByRole("tab", { name: "내 노트북" }));
      await user.click(screen.getByRole("tab", { name: "전체" }));
      expect(await screen.findByText("조건에 맞는 항목이 없습니다")).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "필터 지우기" }));
      await waitFor(() => expect(screen.getAllByTestId("notebook-card")).toHaveLength(4));
      expect(screen.getByTestId("favorites-only-toggle")).toHaveAttribute("aria-pressed", "false");
      expect(screen.getByRole("combobox", { name: "태그 필터" })).toHaveTextContent("전체 태그");
    });

    it("목록을 불러오지 못하면 ErrorState(「다시 시도」)를 보이고, 다시 시도가 성공하면 카드를 그리며 포커스는 목록 맨 앞(모두 선택)에 온다", async () => {
      const user = userEvent.setup();
      const { ApiError } = await import("@/lib/api-client");
      const listOk = api.get.getMockImplementation();
      api.get.mockImplementation((path: string) =>
        path.startsWith("/api/v1/notebooks?") ? Promise.reject(new ApiError(500, "boom")) : listOk?.(path),
      );
      renderPage();
      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent("노트북 목록을 불러오지 못했습니다");
      api.get.mockImplementation((path: string) => listOk?.(path));
      await user.click(within(alert).getByRole("button", { name: "다시 시도" }));
      expect(await screen.findAllByTestId("notebook-card")).toHaveLength(4);
      // P2 HCI-P2-2 — 사라진 「다시 시도」 대신 목록 경계 안 첫 요소로(페이지 맨 위 「새 노트북」·body가 아니다)
      await waitFor(() => expect(screen.getByRole("checkbox", { name: "표시된 노트북 모두 선택" })).toHaveFocus());
      expect(screen.getByTestId("create-notebook")).not.toHaveFocus();
    });

    it("불러오는 동안 카드 모양 스켈레톤과 「노트북 목록을 불러오는 중」 상태를 알린다", async () => {
      api.get.mockImplementation(() => new Promise(() => {}));
      renderPage();
      expect(screen.getByRole("status")).toHaveTextContent("노트북 목록을 불러오는 중");
      expect(screen.getAllByTestId("loading-skeleton").length).toBeGreaterThan(0);
    });
  });

  describe("카드 소유자 동작 — 단건 삭제(홈은 이동 없음, §6.6.2 PR-09)", () => {
    it("확인하면 DELETE 후 목록에서 사라지고 성공 토스트를 띄우며(이동 없음) 포커스는 다음 카드의 ⋯로 간다", async () => {
      const user = userEvent.setup();
      api.delete.mockImplementation(async (path: string) => {
        const id = path.replace("/api/v1/notebooks/", "");
        for (const key of Object.keys(lists)) lists[key] = lists[key].filter((n) => n.id !== id);
      });
      renderPage();
      await screen.findAllByTestId("notebook-card");
      await user.click(screen.getByRole("button", { name: "노트북 a 동작 더보기" }));
      await user.click(await screen.findByRole("menuitem", { name: "삭제" }));
      const dialog = await screen.findByRole("alertdialog", { name: "‘노트북 a’ 노트북을 삭제할까요?" });
      await user.click(within(dialog).getByRole("button", { name: "삭제" }));

      await waitFor(() => expect(screen.getAllByTestId("notebook-card")).toHaveLength(3));
      expect(api.delete).toHaveBeenCalledWith("/api/v1/notebooks/a");
      expect(router.push).not.toHaveBeenCalled();
      expect(router.replace).not.toHaveBeenCalled();
      await waitFor(() =>
        expect(toast.success).toHaveBeenCalledWith("노트북을 삭제했습니다", expect.anything()),
      );
      await waitFor(() => expect(screen.getByRole("button", { name: "노트북 b 동작 더보기" })).toHaveFocus());
    });
  });

  describe("단건 다이얼로그 — 키보드로 열 때 포커스(P3 리뷰 수정 중 발견)", () => {
    it("카드 더보기를 키보드로 열어 「공유」를 고르면 포커스가 공유 관리 대화상자의 「그룹에 공유」 제목에 남고(전체 공개 스위치가 아니다), 닫으면 ⋯로 돌아온다", async () => {
      const user = userEvent.setup();
      renderPage();
      await screen.findAllByTestId("notebook-card");
      const more = screen.getByRole("button", { name: "노트북 a 동작 더보기" });
      act(() => more.focus());
      await user.keyboard("{Enter}");
      const target = await screen.findByRole("menuitem", { name: "공유" });
      act(() => target.focus());
      await user.keyboard("{Enter}");
      const dialog = await screen.findByRole("dialog", { name: "노트북 공유" });
      const heading = within(dialog).getByRole("heading", { name: "그룹에 공유" });
      await waitFor(() => expect(heading).toHaveFocus());
      await act(async () => {
        await new Promise((r) => setTimeout(r, 100));
      });
      expect(heading).toHaveFocus();
      expect(within(dialog).getByRole("switch", { name: "전체 공개" })).not.toHaveFocus();
      await user.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("dialog", { name: "노트북 공유" })).toBeNull());
      await waitFor(() => expect(more).toHaveFocus());
      expect(api.patch).not.toHaveBeenCalled();
    });

    it.each([
      ["이름 변경", "노트북 이름 변경", "이름"],
      ["태그 관리", "태그 관리", "새 태그 이름"],
    ])("카드 더보기를 키보드로 열어 「%s」을 고르면 포커스가 다이얼로그 안(%s · %s 칸)에 남고, 닫으면 ⋯로 돌아온다", async (item, dialogName, field) => {
      const user = userEvent.setup();
      renderPage();
      await screen.findAllByTestId("notebook-card");
      const more = screen.getByRole("button", { name: "노트북 a 동작 더보기" });
      act(() => more.focus());
      await user.keyboard("{Enter}");
      const target = await screen.findByRole("menuitem", { name: item });
      act(() => target.focus());
      await user.keyboard("{Enter}");
      const dialog = await screen.findByRole("dialog", { name: dialogName });
      const input = within(dialog).getByLabelText(field);
      await waitFor(() => expect(input).toHaveFocus());
      await act(async () => {
        await new Promise((r) => setTimeout(r, 100));
      });
      expect(input).toHaveFocus();
      await user.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("dialog", { name: dialogName })).toBeNull());
      await waitFor(() => expect(more).toHaveFocus());
    });
  });

  describe("단건 다이얼로그 — 늦은 응답(P3 리뷰 P3-02)", () => {
    /** 공유 관리 대화상자가 읽는 그룹·공유 목록(노트북 a·b 모두 공유 없음). */
    function serveShareReads() {
      const listNotebooks = api.get.getMockImplementation();
      api.get.mockImplementation((path: string) => {
        if (path === "/api/v1/me/groups") {
          return Promise.resolve([
            { id: "group-alpha", name: "가나 그룹" },
            { id: "group-research", name: "연구팀" },
          ]);
        }
        if (/^\/api\/v1\/notebooks\/[ab]\/shares$/.test(path)) return Promise.resolve([]);
        return listNotebooks?.(path);
      });
    }

    async function chooseGroup(user: ReturnType<typeof userEvent.setup>, dialog: HTMLElement, name: string) {
      await user.click(await within(dialog).findByTestId("share-group-select"));
      await user.click(await screen.findByRole("option", { name }));
    }

    it("노트북 a 공유 요청 중에 닫고 노트북 b 공유를 열어 그룹을 고르면, a의 공유가 늦게 성공해도 b의 공유 창과 입력이 그대로다(P3-02·P3-R01)", async () => {
      const user = userEvent.setup();
      serveShareReads();
      let resolveA!: (value: unknown) => void;
      api.post.mockImplementation((path: string) =>
        path === "/api/v1/notebooks/a/share"
          ? new Promise((r) => {
              resolveA = r;
            })
          : new Promise(() => {}),
      );
      renderPage();
      await screen.findAllByTestId("notebook-card");

      await user.click(screen.getByRole("button", { name: "노트북 a 동작 더보기" }));
      await user.click(await screen.findByRole("menuitem", { name: "공유" }));
      let share = await screen.findByRole("dialog", { name: "노트북 공유" });
      await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
      await act(async () => {
        await new Promise((r) => setTimeout(r, 50));
      });
      await chooseGroup(user, share, "연구팀");
      await user.click(within(share).getByTestId("share-submit"));
      await waitFor(() =>
        expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/a/share", {
          group_id: "group-research",
          permission: "read",
        }),
      );
      await user.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("dialog", { name: "노트북 공유" })).toBeNull());

      await user.click(screen.getByRole("button", { name: "노트북 b 동작 더보기" }));
      await user.click(await screen.findByRole("menuitem", { name: "공유" }));
      share = await screen.findByRole("dialog", { name: "노트북 공유" });
      // b의 창은 빈 입력으로 시작하고 a의 요청을 기다리는 표시가 없다
      await waitFor(() => expect(share.contains(document.activeElement)).toBe(true));
      const group = await within(share).findByTestId("share-group-select");
      expect(group).toHaveTextContent("그룹 선택");
      expect(within(share).getByTestId("share-submit")).toHaveTextContent(/^공유$/);
      await chooseGroup(user, share, "가나 그룹");

      await act(async () =>
        resolveA({ id: "s1", notebook_id: "a", user_id: null, group_id: "group-research", permission: "read" }),
      );
      await waitFor(() =>
        expect(toast.success).toHaveBeenCalledWith("「연구팀」 그룹에 읽기 전용 권한으로 공유했습니다", expect.anything()),
      );
      await act(async () => {
        await new Promise((r) => setTimeout(r, 250));
      });
      const still = screen.getByRole("dialog", { name: "노트북 공유" });
      expect(within(still).getByTestId("share-group-select")).toHaveTextContent("가나 그룹");
      // a의 응답 행은 a의 공유 목록 캐시에만 들어간다 — b의 목록은 비어 있다
      expect(within(still).queryAllByTestId("share-row")).toHaveLength(0);
    });

    it("노트북 a 공유가 b의 창을 연 뒤 「그룹 404」로 끝나도 b의 창에 인라인 오류가 새지 않는다(P3-R01)", async () => {
      const user = userEvent.setup();
      serveShareReads();
      const { ApiError } = await import("@/lib/api-client");
      let rejectA!: (reason: unknown) => void;
      api.post.mockImplementation((path: string) =>
        path === "/api/v1/notebooks/a/share"
          ? new Promise((_, rej) => {
              rejectA = rej;
            })
          : new Promise(() => {}),
      );
      renderPage();
      await screen.findAllByTestId("notebook-card");
      await user.click(screen.getByRole("button", { name: "노트북 a 동작 더보기" }));
      await user.click(await screen.findByRole("menuitem", { name: "공유" }));
      let share = await screen.findByRole("dialog", { name: "노트북 공유" });
      await waitFor(() => expect(share.contains(document.activeElement)).toBe(true));
      await chooseGroup(user, share, "연구팀");
      await user.click(within(share).getByTestId("share-submit"));
      await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/a/share", expect.anything()));
      await user.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("dialog", { name: "노트북 공유" })).toBeNull());

      await user.click(screen.getByRole("button", { name: "노트북 b 동작 더보기" }));
      await user.click(await screen.findByRole("menuitem", { name: "공유" }));
      share = await screen.findByRole("dialog", { name: "노트북 공유" });
      await waitFor(() => expect(share.contains(document.activeElement)).toBe(true));
      await chooseGroup(user, share, "가나 그룹");

      await act(async () => rejectA(new ApiError(404, "Target group not found")));
      await waitFor(() =>
        expect(toast.error).toHaveBeenCalledWith("노트북을 공유하지 못했습니다", expect.anything()),
      );
      await act(async () => {
        await new Promise((r) => setTimeout(r, 250));
      });
      const still = screen.getByRole("dialog", { name: "노트북 공유" });
      expect(within(still).queryByRole("alert")).toBeNull();
      expect(within(still).getByTestId("share-group-select")).toHaveTextContent("가나 그룹");
      expect(within(still).getByTestId("share-group-select")).not.toHaveAttribute("aria-invalid");
    });
  });
});
