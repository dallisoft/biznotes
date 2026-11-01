import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { Toaster, toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type SecretRevealContent, SecretRevealDialog } from "@/components/ui/secret-reveal-dialog";
import {
  PASSWORD_RESET_LOST_NOTICE,
  RESET_BUSY_REASON,
  SELF_DEACTIVATE_REASON,
  usePasswordResetSecret,
} from "@/hooks/use-admin-users";
import { useLogout } from "@/hooks/use-auth";
import { stubNarrowViewport } from "@/hooks/use-narrow-viewport.test-kit";
import { FIRST_LOGIN_CHANGE_NOTICE, PASSWORD_ISSUE_MESSAGE } from "@/lib/password-policy";
import { type SecretMutation, useSecretScope } from "@/lib/secret-mutation";
import {
  type ConsoleSpy,
  type FakeNetwork,
  findSecret,
  flushTasks,
  installFakeNetwork,
  type NetworkCall,
  spyConsole,
} from "@/lib/secret-mutation.test-kit";
import AdminLayout from "../layout";
import AdminUsersPage from "./page";

// 사용자·그룹(plan §6.6.2, AC-5.1·5.2·5.3, §8.4 종료 경로 행렬 「임시 비밀번호 초기화」 열) — 페이지·훅·다이얼로그·epoch·
// useLogout은 실경로이고 네트워크 경계(global fetch)만 대체한다. 비밀값은 모두 더미다(실계정·실비밀번호 없음).

const router = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
  prefetch: vi.fn(),
  back: vi.fn(),
  refresh: vi.fn(),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => router,
  usePathname: () => "/admin/users",
}));

const TEMP_1 = "dummy-temp-pass-1111";
const TEMP_2 = "dummy-temp-pass-2222";

const ME = {
  id: "admin-1",
  email: "admin@example.com",
  name: "Admin",
  role: "admin",
  is_active: true,
  created_at: "2026-06-14T00:00:00Z",
};
const NEW_API_USERS = [
  { ...ME, must_change_password: false },
  {
    id: "user-2",
    email: "new@example.com",
    name: "New",
    role: "user",
    is_active: true,
    must_change_password: true,
    created_at: "2026-10-10T00:00:00Z",
  },
  {
    id: "user-3",
    email: "demo@example.com",
    name: "Demo",
    role: "user",
    is_active: true,
    must_change_password: false,
    created_at: "2026-10-11T00:00:00Z",
  },
  {
    id: "user-4",
    email: "off@example.com",
    name: "Off",
    role: "user",
    is_active: false,
    must_change_password: false,
    created_at: "2026-10-12T00:00:00Z",
  },
];
// 구 API(코어 v0.10.x) 행 — must_change_password 키가 없다
const OLD_API_USERS = NEW_API_USERS.map((u) => {
  const row: Record<string, unknown> = { ...u };
  delete row.must_change_password;
  return row;
});

const nav: { go?: (route: "users" | "other") => void; logout?: () => void } = {};

function LogoutProbe() {
  const logout = useLogout();
  nav.logout = () => logout.mutate();
  return null;
}

function App({ client }: { client: QueryClient }) {
  const [route, setRoute] = useState<"users" | "other">("users");
  nav.go = setRoute;
  return (
    <QueryClientProvider client={client}>
      <Toaster />
      <LogoutProbe />
      <main id="main-content" tabIndex={-1}>
        <AdminLayout>{route === "users" ? <AdminUsersPage /> : <p>LLM 게이트웨이 화면</p>}</AdminLayout>
      </main>
    </QueryClientProvider>
  );
}

interface Setup {
  users?: unknown[];
  groups?: unknown[];
  extra?: (method: string, path: string, body: unknown) => { status: number; body?: unknown } | undefined;
}

let consoleSpy: ConsoleSpy | undefined;

afterEach(async () => {
  await flushTasks(1);
  consoleSpy?.restore();
  consoleSpy = undefined;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  router.push.mockReset();
  router.replace.mockReset();
});

function setup({ users = NEW_API_USERS, groups = [], extra }: Setup = {}) {
  const net = installFakeNetwork((method, path, body) => {
    const custom = extra?.(method, path, body);
    if (custom) return custom;
    if (method === "GET" && path === "/api/v1/auth/me") return { status: 200, body: ME };
    if (method === "GET" && path === "/api/v1/admin/users") return { status: 200, body: users };
    if (method === "GET" && path === "/api/v1/admin/groups") return { status: 200, body: groups };
    if (method === "POST" && /\/password-reset$/.test(path)) return "hold";
    if (method === "PATCH" && path.startsWith("/api/v1/admin/users/")) {
      const id = path.split("/").pop();
      const row = (users as { id: string }[]).find((u) => u.id === id);
      return { status: 200, body: { ...row, ...(body as object) } };
    }
    if (method === "POST" && path === "/api/v1/auth/logout") return { status: 204 };
    return { status: 200, body: [] };
  });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  consoleSpy = spyConsole();
  const view = render(<App client={client} />);
  const user = userEvent.setup();
  return { net, client, view, user };
}

type Ctx = ReturnType<typeof setup>;

const moreButton = (email: string) => screen.getByRole("button", { name: `${email} 동작 더보기` });

async function chooseResetItem(ctx: Ctx, email: string) {
  await ctx.user.click(moreButton(email));
  const menu = await screen.findByRole("menu");
  await ctx.user.click(within(menu).getByRole("menuitem", { name: /^비밀번호 초기화…/ }));
  return screen.findByRole("alertdialog");
}

/** 「비밀번호 초기화…」 → 확인 「초기화」 → 붙잡힌 요청을 돌려준다 */
async function requestReset(ctx: Ctx, email: string, id: string): Promise<NetworkCall> {
  const confirm = await chooseResetItem(ctx, email);
  expect(ctx.net.count("POST", `/api/v1/admin/users/${id}/password-reset`)).toBe(0);
  await ctx.user.click(within(confirm).getByRole("button", { name: "초기화" }));
  await waitFor(() => expect(ctx.net.held("POST", `/api/v1/admin/users/${id}/password-reset`)).toHaveLength(1));
  return ctx.net.held("POST", `/api/v1/admin/users/${id}/password-reset`)[0];
}

async function deliver(call: NetworkCall, secret: string) {
  await act(async () => {
    call.respond(200, { temporary_password: secret });
    await flushTasks();
  });
}

/** 결과 다이얼로그가 비밀값을 보이고 「복사」에 초기 포커스가 있다(AC-5.2 ①) */
async function expectRevealed(secret: string) {
  const dialog = await screen.findByRole("dialog", { name: "임시 비밀번호를 발급했습니다" });
  expect(within(dialog).getByTestId("temporary-password")).toHaveTextContent(secret);
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "임시 비밀번호 복사" })).toHaveFocus());
  // 닫히는 다른 팝업(입력 다이얼로그·메뉴)의 뒤늦은 포커스 복귀가 「복사」를 빼앗지 않는다
  await act(() => flushTasks(3));
  await new Promise((r) => requestAnimationFrame(() => r(undefined)));
  expect(within(dialog).getByRole("button", { name: "임시 비밀번호 복사" })).toHaveFocus();
  return dialog;
}

/** §8.4 행렬 공통 단언: 종료 뒤 DOM·React 상태·mutation 캐시(state·variables)·쿼리 캐시·토스트·저장소·URL·콘솔에 0건 */
async function expectGone(ctx: Ctx, secret: string) {
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "임시 비밀번호를 발급했습니다" })).toBeNull());
  await act(() => flushTasks());
  expect(
    findSecret(secret, { container: ctx.view.container, queryClient: ctx.client, console: consoleSpy }),
  ).toEqual([]);
}

/** 종료 뒤 도착한 응답이 다이얼로그를 다시 열지 않는다 */
async function expectStaysClosed(secret: string) {
  await act(() => flushTasks(5));
  expect(screen.queryByRole("dialog", { name: "임시 비밀번호를 발급했습니다" })).toBeNull();
  expect(document.body.textContent).not.toContain(secret);
}

async function ready() {
  await screen.findByText("new@example.com");
}

describe("AdminUsersPage — 화면 구조(AC-5.1·5.3, §6.6.2)", () => {
  it("PageHeader 브레드크럼 「관리 / 사용자·그룹」, h1 하나, 섹션 h2(text-lg) 「사용자」「그룹」", async () => {
    setup();
    await ready();
    const crumb = screen.getByRole("navigation", { name: "위치" });
    expect(within(crumb).getByRole("link", { name: "관리" })).toHaveAttribute("href", "/admin");
    expect(within(crumb).getByText("사용자·그룹")).toHaveAttribute("aria-current", "page");
    expect(screen.getAllByRole("heading", { level: 1 }).map((h) => h.textContent)).toEqual(["사용자·그룹"]);
    const h2 = screen.getAllByRole("heading", { level: 2 });
    expect(h2.map((h) => h.textContent)).toEqual(["사용자", "그룹"]);
    for (const h of h2) expect(h).toHaveClass("text-lg");
    expect(screen.getByRole("button", { name: "사용자 추가" })).toBeInTheDocument();
    expect(screen.queryByText(/^\+/)).toBeNull();
  });

  it("표 머리글은 「이메일 · 이름 · 역할 · 상태 · 동작」, 역할·상태는 라벨 배지이고 행 스위치는 없다(활성은 메뉴로 — §6.6.2)", async () => {
    setup();
    await ready();
    const table = screen.getByRole("table", { name: "사용자" });
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "이메일",
      "이름",
      "역할",
      "상태",
      "동작",
    ]);
    const adminRow = within(table).getByText("admin@example.com").closest("tr")!;
    expect(within(adminRow).getByText("관리자")).toHaveAttribute("data-tone", "info");
    expect(within(adminRow).getByText("활성")).toHaveAttribute("data-tone", "success");
    const offRow = within(table).getByText("off@example.com").closest("tr")!;
    expect(within(offRow).getByText("사용자")).toBeInTheDocument();
    expect(within(offRow).getByText("비활성")).toBeInTheDocument();
    expect(within(table).queryAllByRole("switch")).toHaveLength(0);
    // 행 동작 = 주 「사용 한도」 + ⋯ (4행 ≤ 10이라 페이지네이션 없음)
    expect(within(table).getAllByRole("button", { name: "사용 한도" })).toHaveLength(4);
    expect(screen.queryByRole("combobox", { name: "페이지당 개수" })).toBeNull();
  });

  it("검색(이메일·이름)과 역할 필터가 목록을 좁히고, 결과가 없으면 「필터 지우기」로 되돌린다", async () => {
    const { user } = setup();
    await ready();
    const table = () => screen.getByRole("table", { name: "사용자" });
    await user.type(screen.getByRole("searchbox", { name: "사용자 검색" }), "demo");
    expect(within(table()).getAllByRole("row")).toHaveLength(2);
    await user.clear(screen.getByRole("searchbox", { name: "사용자 검색" }));
    await user.click(screen.getByRole("radio", { name: "관리자" }));
    expect(within(table()).getAllByRole("row")).toHaveLength(2);
    await user.type(screen.getByRole("searchbox", { name: "사용자 검색" }), "nobody");
    expect(screen.queryByRole("table", { name: "사용자" })).toBeNull();
    expect(screen.getByText("조건에 맞는 항목이 없습니다")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "필터 지우기" }));
    expect(within(table()).getAllByRole("row")).toHaveLength(5);
    expect(screen.getByRole("radio", { name: "전체" })).toHaveAttribute("aria-checked", "true");
  });

  it("목록을 못 불러오면 ErrorState, 「다시 시도」가 성공하면 포커스는 「사용자」 섹션 안(검색)에 남는다(HCI-P2-2)", async () => {
    let failUsers = true;
    const { user } = setup({
      extra: (method, path) =>
        method === "GET" && path === "/api/v1/admin/users" && failUsers ? { status: 500, body: { detail: "boom" } } : undefined,
    });
    expect(await screen.findByText("목록을 불러오지 못했습니다")).toBeInTheDocument();
    failUsers = false;
    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    await ready();
    await waitFor(() => expect(screen.getByRole("searchbox", { name: "사용자 검색" })).toHaveFocus());
  });

  it("생성 다이얼로그의 역할 스위치는 이름(「관리자 권한」)을 가진다", async () => {
    const { user } = setup();
    await ready();
    await user.click(screen.getByRole("button", { name: "사용자 추가" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("switch", { name: "관리자 권한" })).toBeInTheDocument();
  });
});

describe("AdminUsersPage — 비밀번호 초기화(AC-5.2)", () => {
  it("「비밀번호 초기화…」는 확인 전 요청 0회, 확인 후 1회. 결과 다이얼로그에 비밀번호·「복사」(초기 포커스), 토스트 없음, 닫으면 그 행 ⋯로 복귀", async () => {
    const ctx = setup();
    await ready();
    const confirm = await chooseResetItem(ctx, "new@example.com");
    expect(confirm).toHaveTextContent("‘new@example.com’ 계정의 비밀번호를 초기화할까요?");
    // 확인 버튼은 위험 동작 빨강이 아니다(일반 구획 항목 — §6.6.2 메뉴 순서)
    expect(within(confirm).getByRole("button", { name: "초기화" }).className).not.toMatch(/bg-destructive/);
    await ctx.user.click(within(confirm).getByRole("button", { name: "취소" }));
    await act(() => flushTasks());
    expect(ctx.net.count("POST", /password-reset$/)).toBe(0);

    const call = await requestReset(ctx, "new@example.com", "user-2");
    expect(ctx.net.count("POST", /password-reset$/)).toBe(1);
    await deliver(call, TEMP_1);
    const dialog = await expectRevealed(TEMP_1);
    // 강제 변경 지원 API의 행이면 안내를 싣는다(요청 경로·조건 그대로 — §8.4)
    expect(dialog).toHaveTextContent(FIRST_LOGIN_CHANGE_NOTICE);
    expect(dialog).toHaveTextContent("이 창을 닫으면 다시 볼 수 없습니다.");
    // 토스트 노출 제거 — 토스트 영역에 비밀값 없음
    const toaster = document.querySelector("section[aria-label^='Notifications']");
    expect(toaster?.textContent ?? "").not.toContain(TEMP_1);

    await ctx.user.click(within(dialog).getByRole("button", { name: "닫기" }));
    await expectGone(ctx, TEMP_1);
    await waitFor(() => expect(moreButton("new@example.com")).toHaveFocus());
  });

  it("구 API 행(키 없음)의 결과에는 강제 변경 안내를 싣지 않는다", async () => {
    const ctx = setup({ users: OLD_API_USERS });
    await ready();
    const call = await requestReset(ctx, "demo@example.com", "user-3");
    await deliver(call, TEMP_1);
    const dialog = await expectRevealed(TEMP_1);
    expect(dialog).not.toHaveTextContent(FIRST_LOGIN_CHANGE_NOTICE);
  });

  it("실패하면 비밀값 없는 실패 토스트를 보이고 결과 다이얼로그는 열지 않는다", async () => {
    const ctx = setup();
    await ready();
    const call = await requestReset(ctx, "new@example.com", "user-2");
    await act(async () => {
      call.respond(422, { detail: { code: "password_policy_rejected_temporary" } });
      await flushTasks();
    });
    expect(await screen.findByText("비밀번호를 초기화하지 못했습니다")).toBeInTheDocument();
    expect(screen.getByText("임시 비밀번호를 만들지 못했습니다. 다시 시도해 주세요.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("§8.4 종료 경로 행렬 — 임시 비밀번호 초기화 열(AC-5.2 ②)", () => {
  it("① 표시 후 「닫기」 → 0건, 다시 열리지 않음", async () => {
    const ctx = setup();
    await ready();
    await deliver(await requestReset(ctx, "new@example.com", "user-2"), TEMP_1);
    const dialog = await expectRevealed(TEMP_1);
    // 양성 대조: 표시 중에는 검색 도구가 DOM·React 상태에서 찾는다
    expect(findSecret(TEMP_1, { container: ctx.view.container, queryClient: ctx.client })).toEqual([
      "DOM",
      "React 상태",
    ]);
    await ctx.user.click(within(dialog).getByRole("button", { name: "닫기" }));
    await expectGone(ctx, TEMP_1);
    await expectStaysClosed(TEMP_1);
  });

  it("② 표시 후 Esc → 0건, 그 행 ⋯로 복귀", async () => {
    const ctx = setup();
    await ready();
    await deliver(await requestReset(ctx, "demo@example.com", "user-3"), TEMP_1);
    await expectRevealed(TEMP_1);
    await ctx.user.keyboard("{Escape}");
    await expectGone(ctx, TEMP_1);
    await expectStaysClosed(TEMP_1);
    await waitFor(() => expect(moreButton("demo@example.com")).toHaveFocus());
  });

  it("③ 표시 후 바깥 클릭 → 0건, 그 행 ⋯로 복귀", async () => {
    const ctx = setup();
    await ready();
    await deliver(await requestReset(ctx, "demo@example.com", "user-3"), TEMP_1);
    await expectRevealed(TEMP_1);
    await ctx.user.click(document.querySelector<HTMLElement>("[data-slot='dialog-overlay']")!);
    await expectGone(ctx, TEMP_1);
    await expectStaysClosed(TEMP_1);
    await waitFor(() => expect(moreButton("demo@example.com")).toHaveFocus());
  });

  it("④ 표시 중 라우트 이동(언마운트) → 0건", async () => {
    const ctx = setup();
    await ready();
    await deliver(await requestReset(ctx, "new@example.com", "user-2"), TEMP_1);
    await expectRevealed(TEMP_1);
    act(() => nav.go!("other"));
    expect(await screen.findByText("LLM 게이트웨이 화면")).toBeInTheDocument();
    await expectGone(ctx, TEMP_1);
    await expectStaysClosed(TEMP_1);
  });

  it("⑤ 표시 중 로그아웃(useLogout 실경로: queryClient.clear → /login) → 화면이 남아 있어도 0건", async () => {
    const ctx = setup();
    await ready();
    await deliver(await requestReset(ctx, "new@example.com", "user-2"), TEMP_1);
    await expectRevealed(TEMP_1);
    await act(async () => {
      nav.logout!();
      await flushTasks();
    });
    expect(ctx.net.count("POST", "/api/v1/auth/logout")).toBe(1);
    expect(router.push).toHaveBeenCalledWith("/login");
    await expectGone(ctx, TEMP_1);
    await expectStaysClosed(TEMP_1);
  });

  it("⑥ 응답 대기 중 로그아웃 → 늦은 응답은 버리고 다이얼로그를 열지 않는다(안내만, 비밀값 없음)", async () => {
    const ctx = setup();
    await ready();
    const call = await requestReset(ctx, "new@example.com", "user-2");
    await act(async () => {
      nav.logout!();
      await flushTasks();
    });
    expect(router.push).toHaveBeenCalledWith("/login");
    await deliver(call, TEMP_1);
    await expectStaysClosed(TEMP_1);
    expect(await screen.findByText(PASSWORD_RESET_LOST_NOTICE)).toBeInTheDocument();
    await expectGone(ctx, TEMP_1);
  });

  it("⑦ 응답 대기 중 화면 이탈(언마운트) → 늦은 응답은 버리고, 비밀값 없는 안내 토스트를 보인다", async () => {
    const ctx = setup();
    await ready();
    const call = await requestReset(ctx, "new@example.com", "user-2");
    act(() => nav.go!("other"));
    await screen.findByText("LLM 게이트웨이 화면");
    await deliver(call, TEMP_1);
    const notice = await screen.findByText(PASSWORD_RESET_LOST_NOTICE);
    expect(notice.closest("[data-sonner-toast]")?.textContent ?? "").not.toContain(TEMP_1);
    await expectStaysClosed(TEMP_1);
    await expectGone(ctx, TEMP_1);
  });

  it("⑧ 대기 중 다른 행의 「비밀번호 초기화…」는 비활성(사유 표시)이고 선택해도 요청이 없다 — 확인 버튼 이중 클릭도 1회", async () => {
    const ctx = setup();
    await ready();
    // 확인 버튼을 한 렌더 안에서 두 번 눌러도(이중 클릭) 요청은 1회다
    const confirm = await chooseResetItem(ctx, "new@example.com");
    const action = within(confirm).getByRole("button", { name: "초기화" });
    act(() => {
      fireEvent.click(action);
      fireEvent.click(action);
    });
    await waitFor(() => expect(ctx.net.count("POST", /password-reset$/)).toBe(1));

    await ctx.user.click(moreButton("demo@example.com"));
    const menu = await screen.findByRole("menu");
    const item = within(menu).getByRole("menuitem", { name: /^비밀번호 초기화…/ });
    expect(item).toHaveAttribute("aria-disabled", "true");
    expect(item).toHaveTextContent(RESET_BUSY_REASON);
    await ctx.user.click(item);
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(ctx.net.count("POST", /password-reset$/)).toBe(1);
    await ctx.user.keyboard("{Escape}");
    await waitFor(() => expect(moreButton("demo@example.com")).toHaveFocus());

    // 응답이 오면 다시 쓸 수 있다
    await deliver(ctx.net.held("POST", /password-reset$/)[0], TEMP_1);
    const dialog = await expectRevealed(TEMP_1);
    await ctx.user.click(within(dialog).getByRole("button", { name: "닫기" }));
    await expectGone(ctx, TEMP_1);
    await ctx.user.click(moreButton("demo@example.com"));
    expect(
      within(await screen.findByRole("menu")).getByRole("menuitem", { name: "비밀번호 초기화…" }),
    ).not.toHaveAttribute("aria-disabled", "true");
  });

  it("대기 중 다른 행 메뉴를 Esc로 닫자마자 응답이 와도 초기 포커스는 「복사」다(메뉴의 늦은 포커스 복귀가 빼앗지 않음 — secure R-1)", async () => {
    const ctx = setup();
    await ready();
    const call = await requestReset(ctx, "new@example.com", "user-2");
    // 다른 행의 ⋯ 메뉴를 열었다가 Esc로 닫고, 메뉴의 포커스 복귀가 끝나기 전에 응답이 도착한다
    await ctx.user.click(moreButton("demo@example.com"));
    await screen.findByRole("menu");
    await ctx.user.keyboard("{Escape}");
    await deliver(call, TEMP_1);
    const dialog = await expectRevealed(TEMP_1);
    // 결과 다이얼로그를 닫으면 요청한 행(new@…)의 ⋯로 돌아간다(메뉴를 연 다른 행이 아니라)
    await ctx.user.click(within(dialog).getByRole("button", { name: "닫기" }));
    await expectGone(ctx, TEMP_1);
    await waitFor(() => expect(moreButton("new@example.com")).toHaveFocus());
  });

  it("⑨ 응답 역순 도착(같은 scope에 초기화 훅 두 벌 — 강제로 두 요청을 만든 픽스처) → 최신 requestId만 표시, 이전 응답 폐기", async () => {
    const net = installFakeNetwork((method, path) =>
      method === "POST" && /password-reset$/.test(path) ? "hold" : { status: 200, body: [] },
    );
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const probe: { a?: SecretMutation<{ id: string; email: string; forcedChange: boolean }>; b?: typeof probe.a } =
      {};
    function Fixture() {
      const scope = useSecretScope<SecretRevealContent>();
      probe.a = usePasswordResetSecret(scope);
      probe.b = usePasswordResetSecret(scope);
      return (
        <SecretRevealDialog
          content={scope.revealed?.value ?? null}
          onClose={scope.close}
          returnFocusTo={() => null}
        />
      );
    }
    // 행렬 공통 단언의 관찰자를 다른 칸과 같게 둔다 — 실제 토스트 렌더러(DOM)와 콘솔 관찰자(P5 리뷰 P5-03)
    consoleSpy = spyConsole();
    const view = render(
      <QueryClientProvider client={client}>
        <Toaster />
        <Fixture />
      </QueryClientProvider>,
    );
    const observers = { container: view.container, queryClient: client, console: consoleSpy };
    for (const order of [["older", "newer"], ["newer", "older"]] as const) {
      act(() => {
        probe.a!.start({ id: "user-2", email: "new@example.com", forcedChange: true });
        probe.b!.start({ id: "user-3", email: "demo@example.com", forcedChange: true });
      });
      await act(() => flushTasks());
      const older = net.held("POST", "/api/v1/admin/users/user-2/password-reset")[0];
      const newer = net.held("POST", "/api/v1/admin/users/user-3/password-reset")[0];
      for (const which of order) await deliver(which === "older" ? older : newer, which === "older" ? TEMP_1 : TEMP_2);

      const dialog = await screen.findByRole("dialog", { name: "임시 비밀번호를 발급했습니다" });
      expect(within(dialog).getByTestId("temporary-password")).toHaveTextContent(TEMP_2);
      expect(dialog).toHaveTextContent("demo@example.com");
      // 양성 대조: 표시 중에는 같은 관찰자가 최신 비밀값을 찾는다(DOM·React 상태)
      expect(findSecret(TEMP_2, observers)).toEqual(expect.arrayContaining(["DOM", "React 상태"]));
      expect(findSecret(TEMP_1, observers)).toEqual([]);
      await userEvent.setup().keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      await act(() => flushTasks());
      expect(findSecret(TEMP_2, observers)).toEqual([]);
      expect(findSecret(TEMP_1, observers)).toEqual([]);
      await expectStaysClosed(TEMP_2);
    }
    // 관찰자 양성 대조: 이 픽스처의 토스트 렌더러·콘솔 관찰자는 실제로 새는 값을 잡는다(더미 탐침)
    const PROBE = "dummy-observer-probe-0001";
    act(() => {
      toast(`탐침 ${PROBE}`);
      console.info(`탐침 ${PROBE}`);
    });
    await waitFor(() => expect(findSecret(PROBE, observers)).toEqual(expect.arrayContaining(["DOM", "콘솔"])));
  });
});

describe("AdminUsersPage — 비활성화·활성화(AC-5.2, D-10)", () => {
  it("「비활성화…」는 확인 전 PATCH 0회, 확인 후 1회(is_active false) + 토스트", async () => {
    const ctx = setup();
    await ready();
    await ctx.user.click(moreButton("demo@example.com"));
    const menu = await screen.findByRole("menu");
    const items = within(menu).getAllByRole("menuitem").map((i) => i.textContent);
    expect(items).toEqual(["비밀번호 초기화…", "비활성화…"]);
    expect(within(menu).getByRole("separator")).toBeInTheDocument();
    await ctx.user.click(within(menu).getByRole("menuitem", { name: "비활성화…" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("‘demo@example.com’ 계정을 비활성화할까요?");
    expect(dialog).toHaveTextContent("로그인할 수 없게 됩니다");
    expect(ctx.net.count("PATCH", "/api/v1/admin/users/user-3")).toBe(0);
    await ctx.user.click(within(dialog).getByRole("button", { name: "비활성화" }));
    await waitFor(() => expect(ctx.net.count("PATCH", "/api/v1/admin/users/user-3")).toBe(1));
    expect(ctx.net.calls.find((c) => c.method === "PATCH")!.body).toEqual({ is_active: false });
    expect(await screen.findByText("계정을 비활성화했습니다")).toBeInTheDocument();
  });

  it("취소하면 PATCH 0회", async () => {
    const ctx = setup();
    await ready();
    await ctx.user.click(moreButton("demo@example.com"));
    await ctx.user.click(await screen.findByRole("menuitem", { name: "비활성화…" }));
    await ctx.user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "취소" }));
    await act(() => flushTasks());
    expect(ctx.net.count("PATCH", /\/api\/v1\/admin\/users\//)).toBe(0);
  });

  it("자기 계정의 「비활성화…」는 비활성이고 사유를 보이며 선택해도 아무 일도 없다", async () => {
    const ctx = setup();
    await ready();
    await ctx.user.click(moreButton("admin@example.com"));
    const item = await screen.findByRole("menuitem", { name: /^비활성화…/ });
    expect(item).toHaveAttribute("aria-disabled", "true");
    expect(item).toHaveTextContent(SELF_DEACTIVATE_REASON);
    await ctx.user.click(item);
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(ctx.net.count("PATCH", /\/api\/v1\/admin\/users\//)).toBe(0);
  });

  it("비활성 계정은 「활성화」(확인 없음)로 바로 되돌린다", async () => {
    const ctx = setup();
    await ready();
    await ctx.user.click(moreButton("off@example.com"));
    await ctx.user.click(await screen.findByRole("menuitem", { name: "활성화" }));
    await waitFor(() => expect(ctx.net.count("PATCH", "/api/v1/admin/users/user-4")).toBe(1));
    expect(ctx.net.calls.find((c) => c.method === "PATCH")!.body).toEqual({ is_active: true });
    expect(await screen.findByText("계정을 활성화했습니다")).toBeInTheDocument();
  });
});

describe("AdminUsersPage — 좁은 화면 표 규칙(plan §6.7, P5 리뷰 P5-04)", () => {

  it("두 표는 좁은 화면 규칙 래퍼(첫 열 고정·오른쪽 단서) 안에 있다", async () => {
    setup({ groups: [{ id: "g-1", name: "운영팀", member_count: 2 }] });
    await ready();
    await screen.findByText("운영팀");
    const tables = [screen.getByRole("table", { name: "사용자" }), screen.getByRole("table", { name: "그룹" })];
    expect(screen.getAllByRole("table")).toHaveLength(2);
    for (const t of tables) expect(t.closest('[data-slot="admin-table-frame"]')).not.toBeNull();
  });

  it("< 768: 행 동작은 ⋯ 하나 — 「사용 한도」는 메뉴 첫 항목이고, 연 다이얼로그를 닫으면 그 행 ⋯로 돌아간다", async () => {
    stubNarrowViewport();
    const ctx = setup({
      groups: [{ id: "g-1", name: "운영팀", member_count: 2 }],
      extra: (method, path) =>
        method === "GET" && path === "/api/v1/admin/users/user-3/limits"
          ? {
              status: 200,
              body: {
                daily_tokens: null,
                daily_agent_runs: null,
                today: { tokens: 0, agent_runs: 0 },
              },
            }
          : undefined,
    });
    await ready();
    await screen.findByText("운영팀");
    // 주 동작 버튼(「사용 한도」「멤버 관리」)은 표에 없고, 행마다 ⋯ 하나
    await waitFor(() => expect(screen.queryByRole("button", { name: "사용 한도" })).toBeNull());
    expect(screen.queryByRole("button", { name: "멤버 관리" })).toBeNull();
    const usersTable = screen.getByRole("table", { name: "사용자" });
    for (const row of within(usersTable).getAllByRole("row").slice(1)) {
      expect(within(row).getAllByRole("button").map((b) => b.getAttribute("aria-label"))).toEqual([
        expect.stringMatching(/ 동작 더보기$/),
      ]);
    }
    const more = moreButton("demo@example.com");
    act(() => more.focus());
    await ctx.user.keyboard("{Enter}");
    const menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem")[0]).toHaveTextContent("사용 한도");
    await ctx.user.click(within(menu).getByRole("menuitem", { name: "사용 한도" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByLabelText("에이전트 실행/일")).toBeInTheDocument();
    await act(() => new Promise((r) => setTimeout(r, 50)));
    expect(dialog.contains(document.activeElement)).toBe(true);
    await ctx.user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(more).toHaveFocus());

    // 그룹 표도 같다 — 「멤버 관리」는 ⋯ 메뉴 첫 항목
    await ctx.user.click(moreButton("운영팀"));
    const groupMenu = await screen.findByRole("menu");
    expect(within(groupMenu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["멤버 관리", "삭제…"]);
  });
});

describe("AdminUsersPage — 사용 한도", () => {
  it("에이전트 실행 한도를 표시하고 저장 payload에 포함한다", async () => {
    const ctx = setup({
      extra: (method, path) => {
        if (method === "GET" && path === "/api/v1/admin/users/user-3/limits") {
          return {
            status: 200,
            body: {
              daily_tokens: 1000,
              daily_agent_runs: 3,
              today: { tokens: 7, agent_runs: 2 },
            },
          };
        }
        if (method === "PUT") return { status: 200, body: {} };
        return undefined;
      },
    });
    await ready();
    const row = screen.getByText("demo@example.com").closest("tr")!;
    await ctx.user.click(within(row).getByRole("button", { name: "사용 한도" }));
    const runsInput = await screen.findByLabelText("에이전트 실행/일");
    expect(runsInput).toHaveValue(3);
    expect(screen.getByText("오늘 2")).toBeInTheDocument();
    // 「오늘」은 표시 시간대 하루다(코어 v0.24.0 #352, #213) — UTC 자정 안내가 남지 않는다
    const dialog = screen.getByRole("dialog", { name: /사용 한도/ });
    expect(dialog).toHaveTextContent("사용량은 매일 한국 표준시(KST) 자정에 초기화됩니다.");
    expect(dialog).not.toHaveTextContent("UTC");
    await ctx.user.clear(runsInput);
    await ctx.user.type(runsInput, "5");
    await ctx.user.click(screen.getByRole("button", { name: "저장" }));
    await waitFor(() =>
      expect(ctx.net.calls.find((c) => c.method === "PUT")?.body).toEqual({
        daily_tokens: 1000,
        daily_agent_runs: 5,
      }),
    );
    expect(ctx.net.calls.find((c) => c.method === "PUT")?.path).toBe("/api/v1/admin/users/user-3/limits");
    expect(await screen.findByText("사용 한도를 저장했습니다")).toBeInTheDocument();
  });

  function limitsWith(body: Record<string, unknown>) {
    return setup({
      extra: (method, path) =>
        method === "GET" && path === "/api/v1/admin/users/user-3/limits"
          ? { status: 200, body: { today: { tokens: 7, agent_runs: 0 }, ...body } }
          : undefined,
    });
  }

  async function openLimits(ctx: ReturnType<typeof setup>) {
    await ready();
    const row = screen.getByText("demo@example.com").closest("tr")!;
    await ctx.user.click(within(row).getByRole("button", { name: "사용 한도" }));
    return screen.findByLabelText("LLM 토큰/일");
  }

  it("필드마다 적용 한도와 출처를 보인다 — 사용자 지정, 0 기본값은 「막음」(코어 v0.26.0 #387, #228 AC-14)", async () => {
    const ctx = limitsWith({
      daily_tokens: 1000,
      daily_agent_runs: null,
      effective: {
        daily_tokens: { value: 1000, source: "user" },
        daily_agent_runs: { value: 0, source: "default" },
      },
    });
    const tokens = await openLimits(ctx);
    expect(tokens).toHaveValue(1000);
    expect(screen.getByTestId("limit-effective-daily_tokens")).toHaveTextContent("지금 적용: 1,000 · 사용자 지정");
    expect(tokens).toHaveAccessibleDescription("지금 적용: 1,000 · 사용자 지정");
    // 0 기본값은 「막음」이다 — 무제한으로 보이지 않는다
    expect(screen.getByTestId("limit-effective-daily_agent_runs")).toHaveTextContent("지금 적용: 0 · 기본값");
    const dialog = screen.getByRole("dialog", { name: /사용 한도/ });
    expect(dialog).toHaveTextContent("일일 한도 기본값을 따르고, 기본값도 없으면 무제한입니다.");
  });

  it("사용자별 값이 없으면 기본값을 따르고(입력 안내도 기본값), 기본값도 없으면 무제한이다", async () => {
    const ctx = limitsWith({
      daily_tokens: null,
      daily_agent_runs: null,
      effective: {
        daily_tokens: { value: 5000, source: "default" },
        daily_agent_runs: { value: null, source: "unlimited" },
      },
    });
    const tokens = await openLimits(ctx);
    expect(tokens).toHaveValue(null);
    expect(tokens).toHaveAttribute("placeholder", "기본값 5,000");
    expect(screen.getByTestId("limit-effective-daily_tokens")).toHaveTextContent("지금 적용: 5,000 · 기본값");
    expect(screen.getByLabelText("에이전트 실행/일")).toHaveAttribute("placeholder", "무제한");
    expect(screen.getByTestId("limit-effective-daily_agent_runs")).toHaveTextContent("지금 적용: 무제한");
  });

  it("구 API 응답(effective 없음)이면 적용 한도 줄을 그리지 않는다", async () => {
    const ctx = setup({
      extra: (method, path) =>
        method === "GET" && path === "/api/v1/admin/users/user-3/limits"
          ? { status: 200, body: { daily_tokens: null, today: {} } }
          : undefined,
    });
    await ready();
    const row = screen.getByText("demo@example.com").closest("tr")!;
    await ctx.user.click(within(row).getByRole("button", { name: "사용 한도" }));
    expect(await screen.findByLabelText("LLM 토큰/일")).toHaveAttribute("placeholder", "무제한");
    expect(screen.queryByTestId("limit-effective-daily_tokens")).toBeNull();
  });
});

describe("AdminUsersPage — 비밀번호 강제 변경 표시(AC-3.5, AC-3.4 키 기준 — 기존 동작 유지)", () => {
  async function openCreateDialog(ctx: Ctx) {
    await ctx.user.click(screen.getByRole("button", { name: "사용자 추가" }));
    return screen.findByRole("dialog");
  }
  async function fillCreate(ctx: Ctx, dialog: HTMLElement) {
    const { getByLabelText } = within(dialog);
    await ctx.user.type(getByLabelText("이메일"), "x@example.com");
    await ctx.user.type(getByLabelText("이름"), "X");
    await ctx.user.type(getByLabelText("초기 비밀번호 (8자 이상)"), "temp-pass-1");
  }

  it("must_change_password=true 행에만 「변경 필요」 배지(warning)를 붙인다", async () => {
    setup();
    await ready();
    expect(screen.getByTestId("must-change-badge-user-2")).toHaveTextContent("변경 필요");
    expect(screen.getByTestId("must-change-badge-user-2")).toHaveAttribute("data-tone", "warning");
    expect(screen.queryByTestId("must-change-badge-admin-1")).not.toBeInTheDocument();
  });

  it("행에 키가 없으면(구 API) 배지·생성 폼 안내를 숨긴다", async () => {
    const ctx = setup({ users: OLD_API_USERS });
    await ready();
    expect(screen.queryByText("변경 필요")).not.toBeInTheDocument();
    const dialog = await openCreateDialog(ctx);
    expect(within(dialog).queryByTestId("create-user-change-notice")).not.toBeInTheDocument();
  });

  it("키가 있으면 생성 폼에 첫 로그인 변경 안내를 보이고, 추가 성공 토스트에도 싣는다", async () => {
    const ctx = setup({
      extra: (method, path) =>
        method === "POST" && path === "/api/v1/admin/users"
          ? { status: 201, body: { ...NEW_API_USERS[1], id: "user-9", email: "x@example.com" } }
          : undefined,
    });
    await ready();
    const dialog = await openCreateDialog(ctx);
    expect(within(dialog).getByTestId("create-user-change-notice")).toHaveTextContent(FIRST_LOGIN_CHANGE_NOTICE);
    await fillCreate(ctx, dialog);
    await ctx.user.click(within(dialog).getByRole("button", { name: "추가" }));
    expect(await screen.findByText("사용자를 추가했습니다")).toBeInTheDocument();
    expect(screen.getByText(FIRST_LOGIN_CHANGE_NOTICE)).toBeInTheDocument();
    expect(ctx.net.calls.find((c) => c.method === "POST" && c.path === "/api/v1/admin/users")?.body).toEqual({
      email: "x@example.com",
      name: "X",
      password: "temp-pass-1",
      role: "user",
    });
  });

  it("추가 422 invalid_new_password는 reason 문구로 보이고 JSON detail·입력 비밀번호를 노출하지 않는다", async () => {
    const detail = { code: "invalid_new_password", reason: "too_long_bytes", detail: "서버 문구" };
    const ctx = setup({
      extra: (method, path) =>
        method === "POST" && path === "/api/v1/admin/users" ? { status: 422, body: { detail } } : undefined,
    });
    await ready();
    const dialog = await openCreateDialog(ctx);
    await fillCreate(ctx, dialog);
    await ctx.user.click(within(dialog).getByRole("button", { name: "추가" }));
    const toast = await screen.findByText(PASSWORD_ISSUE_MESSAGE.too_long_bytes);
    const text = toast.closest("[data-sonner-toast]")?.textContent ?? "";
    expect(text).not.toContain("{");
    expect(text).not.toContain("temp-pass-1");
  });

  it("추가의 다른 오류(409 등)는 기존처럼 서버 메시지를 보인다", async () => {
    const ctx = setup({
      extra: (method, path) =>
        method === "POST" && path === "/api/v1/admin/users"
          ? { status: 409, body: { detail: "이미 존재하는 이메일입니다" } }
          : undefined,
    });
    await ready();
    const dialog = await openCreateDialog(ctx);
    await fillCreate(ctx, dialog);
    await ctx.user.click(within(dialog).getByRole("button", { name: "추가" }));
    expect(await screen.findByText("이미 존재하는 이메일입니다")).toBeInTheDocument();
  });
});

describe("AdminUsersPage — 그룹 섹션(§6.6.2)", () => {
  it("그룹이 없으면 EmptyState 「아직 그룹이 없습니다」(표·페이지네이션 없음)", async () => {
    setup();
    await ready();
    expect(await screen.findByText("아직 그룹이 없습니다")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "그룹" })).toBeNull();
  });

  it("「그룹 추가」는 다이얼로그로 만들고 성공 토스트를 보인다", async () => {
    const ctx = setup({
      extra: (method, path) =>
        method === "POST" && path === "/api/v1/admin/groups" ? { status: 201, body: { id: "g9" } } : undefined,
    });
    await ready();
    await ctx.user.click(screen.getByRole("button", { name: "그룹 추가" }));
    const dialog = await screen.findByRole("dialog");
    await ctx.user.type(within(dialog).getByLabelText("그룹 이름"), "연구팀");
    await ctx.user.click(within(dialog).getByRole("button", { name: "추가" }));
    await waitFor(() =>
      expect(ctx.net.calls.find((c) => c.method === "POST" && c.path === "/api/v1/admin/groups")?.body).toEqual({
        name: "연구팀",
      }),
    );
    expect(await screen.findByText("그룹을 추가했습니다")).toBeInTheDocument();
  });

  it("그룹 행: 주 「멤버 관리」 + ⋯ 「삭제…」 — 확인 전 DELETE 0회, 확인 후 1회", async () => {
    const ctx = setup({
      groups: [{ id: "g1", name: "연구팀", member_count: 3 }],
      extra: (method) => (method === "DELETE" ? { status: 204 } : undefined),
    });
    await ready();
    const table = await screen.findByRole("table", { name: "그룹" });
    expect(within(table).getByRole("button", { name: "멤버 관리" })).toBeInTheDocument();
    expect(within(table).getByText("3명")).toBeInTheDocument();
    await ctx.user.click(screen.getByRole("button", { name: "연구팀 동작 더보기" }));
    await ctx.user.click(await screen.findByRole("menuitem", { name: "삭제…" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("‘연구팀’ 그룹을 삭제할까요?");
    expect(ctx.net.count("DELETE", "/api/v1/admin/groups/g1")).toBe(0);
    await ctx.user.click(within(dialog).getByRole("button", { name: "삭제" }));
    await waitFor(() => expect(ctx.net.count("DELETE", "/api/v1/admin/groups/g1")).toBe(1));
    expect(await screen.findByText("그룹을 삭제했습니다")).toBeInTheDocument();
  });
});

describe("네트워크 기록", () => {
  it("관리 API 경로만 부른다(경로 회귀 방지)", async () => {
    const ctx: { net: FakeNetwork } = setup();
    await ready();
    const paths = new Set(ctx.net.calls.map((c) => `${c.method} ${c.path}`));
    expect(paths).toEqual(
      new Set(["GET /api/v1/auth/me", "GET /api/v1/admin/users", "GET /api/v1/admin/groups"]),
    );
  });
});
