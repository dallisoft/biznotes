import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LogoutButton, UserAccountList, UserMenu } from "@/components/shell/user-menu";
import type { User } from "@/lib/types";

// API 경계만 mock(AC-3.7)
const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
// 프레임워크 경계: App Router 대역
const nav = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => nav,
  usePathname: () => "/notebooks/nb1",
  useSearchParams: () => new URLSearchParams("s=s1"),
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

function withClient(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function legacyMe(): User {
  const legacy: Record<string, unknown> = { ...ME };
  delete legacy.must_change_password;
  return legacy as User;
}

afterEach(() => {
  vi.resetAllMocks();
});

describe("UserMenu — 항목 개편 (plan §6.2.2·§6.2.4, AC-2.8 사용자 메뉴 부분)", () => {
  it("이름·이메일 머리글 / 비밀번호 변경 / 로그아웃 순서이고 링크 대상이 맞다", async () => {
    withClient(<UserMenu me={ME} />);
    await userEvent.click(screen.getByTestId("user-menu"));

    const menu = await screen.findByRole("menu");
    expect(within(menu).getByText("사용자")).toBeInTheDocument();
    expect(within(menu).getByText("user@example.com")).toBeInTheDocument();
    const items = within(menu).getAllByRole("menuitem");
    expect(items.map((el) => el.textContent?.trim())).toEqual(["비밀번호 변경", "로그아웃"]);
    expect(items[0]).toHaveAttribute("data-testid", "user-menu-change-password");
    // 옛 라벨 「연동 설정」은 남지 않는다(§6.3.1 쓰지 않는 말)
    expect(within(menu).queryByText("연동 설정")).not.toBeInTheDocument();
  });

  it("트리거는 아바타 + 표시 이름이고, 이름이 비면 이메일을 보인다", async () => {
    const { rerender } = withClient(<UserMenu me={ME} />);
    expect(screen.getByTestId("user-menu")).toHaveTextContent("사용자");
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <UserMenu me={{ ...ME, name: "" }} />
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("user-menu")).toHaveTextContent("user@example.com");
    expect(screen.getByTestId("user-menu")).toHaveAccessibleName("사용자 메뉴");
  });
});

describe("UserMenu — 본인 비밀번호 변경 진입점 (AC-3.4, HCI-11 ②·⑥, §11.4 능력 감지 유지)", () => {
  it("me에 must_change_password 키가 있으면(새 API) 「비밀번호 변경」이 현재 경로를 redirect로 싣는다", async () => {
    withClient(<UserMenu me={ME} />);
    await userEvent.click(screen.getByTestId("user-menu"));

    const item = await screen.findByRole("menuitem", { name: "비밀번호 변경" });
    expect(item).toHaveAttribute(
      "href",
      `/change-password?redirect=${encodeURIComponent("/notebooks/nb1?s=s1")}`,
    );
    expect(item).toHaveAttribute("data-testid", "user-menu-change-password");
    expect(screen.getByRole("menuitem", { name: "로그아웃" })).toBeInTheDocument();
  });

  it("키가 없으면(구 API) 메뉴 항목을 숨긴다 — 값이 false가 아니라 키 존재로 판정", async () => {
    withClient(<UserMenu me={legacyMe()} />);
    await userEvent.click(screen.getByTestId("user-menu"));

    expect(await screen.findByRole("menuitem", { name: "로그아웃" })).toBeInTheDocument();
    expect(screen.getAllByRole("menuitem")).toHaveLength(1);
    expect(screen.queryByRole("menuitem", { name: "비밀번호 변경" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("user-menu-change-password")).not.toBeInTheDocument();
  });

  it("로그아웃은 그대로 동작한다", async () => {
    api.post.mockResolvedValue(undefined);
    withClient(<UserMenu me={ME} />);
    await userEvent.click(screen.getByTestId("user-menu"));
    await userEvent.click(await screen.findByRole("menuitem", { name: "로그아웃" }));

    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/auth/logout"));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/login"));
  });
});

describe("UserAccountList — 드로어용 펼친 목록 변형 (plan §6.2.3 ② 「계정」 섹션)", () => {
  it("드롭다운과 같은 항목을 링크로 펼치고(로그아웃 제외), 누르면 onNavigate를 부른다", async () => {
    const onNavigate = vi.fn();
    withClient(<UserAccountList me={ME} onNavigate={onNavigate} />);

    const group = screen.getByRole("group", { name: "계정" });
    const links = within(group).getAllByRole("link");
    expect(links.map((a) => [a.textContent?.trim(), a.getAttribute("href")])).toEqual([
      ["비밀번호 변경", `/change-password?redirect=${encodeURIComponent("/notebooks/nb1?s=s1")}`],
    ]);
    expect(within(group).queryByText("로그아웃")).not.toBeInTheDocument();
    // 「계정」 머리글은 heading이 아니다(§9 공통 규칙)
    expect(screen.queryByRole("heading")).not.toBeInTheDocument();

    links[0].addEventListener("click", (e) => e.preventDefault()); // jsdom 탐색 없이 콜백만 본다
    await userEvent.click(links[0]);
    expect(onNavigate).toHaveBeenCalledTimes(1);
  });

  it("구 API(키 없음)면 항목이 없어 드로어 「계정」 섹션을 그리지 않는다", () => {
    withClient(<UserAccountList me={legacyMe()} />);
    expect(screen.queryByRole("group", { name: "계정" })).toBeNull();
    expect(screen.queryByTestId("drawer-account-section")).toBeNull();
  });
});

describe("LogoutButton — 드로어 고정 바닥의 로그아웃", () => {
  it("누르면 logout API 후 /login으로 간다(드롭다운과 같은 useLogout 경로)", async () => {
    api.post.mockResolvedValue(undefined);
    withClient(<LogoutButton />);
    await userEvent.click(screen.getByRole("button", { name: "로그아웃" }));

    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/auth/logout"));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/login"));
  });
});
