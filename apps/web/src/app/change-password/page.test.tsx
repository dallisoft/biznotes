import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { Toaster } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api-client";
import {
  INVALID_CURRENT_PASSWORD_MESSAGE,
  PASSWORD_CHANGE_UNSUPPORTED_MESSAGE,
  PASSWORD_ISSUE_MESSAGE,
  PASSWORD_SUPPORT_PROBE_FAILED_MESSAGE,
  RATE_LIMITED_MESSAGE,
} from "@/lib/password-policy";
import type { User } from "@/lib/types";
import ChangePasswordPage from "./page";

// API 경계만 mock(AC-3.7) — api 객체만 대체하고 ApiError는 실제 모듈.
// 지연은 수동 resolve로 결정적으로 주입한다(실제 시간 대기 없음, v5 PR4-02).
const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
// 프레임워크 경계: App Router 대역
const nav = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn(), search: "" }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: nav.replace, push: nav.push }),
  useSearchParams: () => new URLSearchParams(nav.search),
}));

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (err: unknown) => void;
}
function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** `GET /auth/me` 프로브 호출 기록 — 실제 fetch처럼 signal이 취소되면 AbortError로 거부한다. */
let probes: { signal: AbortSignal | undefined; d: Deferred<unknown> }[] = [];

const NEW_ME: User = {
  id: "u1",
  email: "u@example.com",
  name: "U",
  role: "user",
  is_active: true,
  must_change_password: true,
  created_at: "2026-10-10T00:00:00Z",
};
const OLD_ME: Record<string, unknown> = { ...NEW_ME };
delete OLD_ME.must_change_password;

function makeClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
}

function renderPage(client = makeClient(), strict = false) {
  const tree = (
    <QueryClientProvider client={client}>
      <ChangePasswordPage />
    </QueryClientProvider>
  );
  const view = render(strict ? <StrictMode>{tree}</StrictMode> : tree);
  return { client, ...view };
}

const submitButton = () => screen.getByRole("button", { name: "비밀번호 변경" });

async function fillForm(current = "old-password-1", next = "new-password-1", confirm = next) {
  await userEvent.type(screen.getByLabelText("현재 비밀번호"), current);
  await userEvent.type(screen.getByLabelText("새 비밀번호"), next);
  await userEvent.type(screen.getByLabelText("새 비밀번호 확인"), confirm);
}

const apiErr = (status: number, detail: unknown, code?: string) =>
  new ApiError(status, "서버 메시지", { detail }, code);

beforeEach(() => {
  probes = [];
  nav.search = "redirect=%2Fworkflows";
  api.get.mockImplementation((path: string, init?: { signal?: AbortSignal }) => {
    if (path !== "/api/v1/auth/me") return Promise.reject(new Error(`예상 밖 GET ${path}`));
    const d = deferred<unknown>();
    init?.signal?.addEventListener("abort", () =>
      d.reject(new DOMException("The operation was aborted.", "AbortError")),
    );
    probes.push({ signal: init?.signal, d });
    return d.promise;
  });
});

afterEach(() => {
  vi.resetAllMocks();
});

describe("변경 화면 — 진입별 fresh 능력 판정 (AC-3.4, D-37′)", () => {
  it("로그인 setQueryData만 있는 직후 진입해도 독립 프로브를 부르고, 판정 중에는 제출이 비활성이다", async () => {
    const client = makeClient();
    client.setQueryData(["me"], NEW_ME); // 로그인 onSuccess가 남긴 캐시
    renderPage(client);

    expect(probes).toHaveLength(1);
    expect(probes[0].signal).toBeInstanceOf(AbortSignal);
    expect(submitButton()).toBeDisabled();

    await act(async () => probes[0].d.resolve(NEW_ME));
    expect(submitButton()).toBeEnabled();
    expect(screen.getByTestId("change-password-forced")).toBeInTheDocument();
  });

  it("빈 QueryClient로 직접 진입해도 프로브 응답으로 지원을 판정한다", async () => {
    renderPage();
    await act(async () => probes[0].d.resolve({ ...NEW_ME, must_change_password: false }));
    expect(submitButton()).toBeEnabled();
    // 본인 변경(강제 아님)에는 취소 링크가 있다
    expect(screen.getByRole("link", { name: "취소" })).toHaveAttribute("href", "/workflows");
  });

  it("진입 전에 시작된 me 요청이 늦게 새 형태로 와도, 진입 요청이 구 형태면 미지원 안내이고 POST 0회", async () => {
    const client = makeClient();
    const early = deferred<User>();
    // 진입 전에 시작된 캐시 요청(예: 레이아웃 useMe) — 프로브와 다른 요청이다
    void client.fetchQuery({ queryKey: ["me"], queryFn: () => early.promise });
    renderPage(client);

    await act(async () => probes[0].d.resolve(OLD_ME));
    await act(async () => early.resolve(NEW_ME));

    expect(screen.getByText(PASSWORD_CHANGE_UNSUPPORTED_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "비밀번호 변경" })).not.toBeInTheDocument();
    expect(client.getQueryData(["me"])).toEqual(NEW_ME); // 캐시는 바뀌어도 판정 근거가 아니다
    expect(api.post).not.toHaveBeenCalled();
  });

  it("재진입하면 이전 요청은 signal.aborted이고, 이전 진입의 AbortError가 「확인하지 못했습니다」를 렌더하지 않는다", async () => {
    const first = renderPage();
    const firstSignal = probes[0].signal as AbortSignal;
    first.unmount();
    expect(firstSignal.aborted).toBe(true);

    renderPage();
    expect(probes).toHaveLength(2);
    // 이전 진입의 거부(AbortError)가 처리될 시간을 준다
    await act(async () => {
      await probes[0].d.promise.catch(() => undefined);
    });
    expect(screen.queryByText(PASSWORD_SUPPORT_PROBE_FAILED_MESSAGE)).not.toBeInTheDocument();
    expect(submitButton()).toBeDisabled();

    await act(async () => probes[1].d.resolve(NEW_ME));
    expect(submitButton()).toBeEnabled();
  });

  it("Strict Mode 이중 effect — 첫 effect의 취소는 상태를 바꾸지 않고 두 번째 진입 응답만 쓴다", async () => {
    renderPage(makeClient(), true);
    expect(probes.length).toBe(2);
    expect(probes[0].signal?.aborted).toBe(true);
    await act(async () => {
      await probes[0].d.promise.catch(() => undefined);
    });
    expect(screen.queryByText(PASSWORD_SUPPORT_PROBE_FAILED_MESSAGE)).not.toBeInTheDocument();

    // 교체된 진입이 (취소가 아니라) 늦게 성공해도 버린다
    await act(async () => probes[1].d.resolve(OLD_ME));
    expect(screen.getByText(PASSWORD_CHANGE_UNSUPPORTED_MESSAGE)).toBeInTheDocument();
  });

  it("로그인 캐시(새 형태)가 있어도 진입 프로브가 구 형태면 미지원 — 근거는 캐시가 아니라 이 진입 응답", async () => {
    const client = makeClient();
    client.setQueryData(["me"], { ...NEW_ME, must_change_password: false });
    renderPage(client);

    await act(async () => probes[0].d.resolve(OLD_ME));

    expect(screen.getByText(PASSWORD_CHANGE_UNSUPPORTED_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "비밀번호 변경" })).not.toBeInTheDocument();
  });

  it("진입 전에 시작된 me 요청이 프로브보다 먼저 새 형태로 도착해도, 프로브가 구 형태면 미지원", async () => {
    const client = makeClient();
    const early = deferred<User>();
    void client.fetchQuery({ queryKey: ["me"], queryFn: () => early.promise });
    renderPage(client);

    await act(async () => early.resolve(NEW_ME)); // 캐시가 먼저 새 형태가 된다
    expect(submitButton()).toBeDisabled(); // 판정 중
    await act(async () => probes[0].d.resolve(OLD_ME));

    expect(screen.getByText(PASSWORD_CHANGE_UNSUPPORTED_MESSAGE)).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
  });

  it("abort에도 늦게 도착한 이전 진입의 성공 응답은 버린다(Strict Mode — 진입 토큰)", async () => {
    // 이미 전송된 응답처럼 abort로 거부되지 않는 프로브
    api.get.mockImplementation((path: string, init?: { signal?: AbortSignal }) => {
      const d = deferred<unknown>();
      probes.push({ signal: init?.signal, d });
      return path === "/api/v1/auth/me" ? d.promise : Promise.reject(new Error(path));
    });
    renderPage(makeClient(), true);
    expect(probes).toHaveLength(2);

    await act(async () => probes[1].d.resolve(OLD_ME)); // 현재 진입: 구 형태
    await act(async () => probes[0].d.resolve(NEW_ME)); // 교체된 진입: 늦게 새 형태

    expect(screen.getByText(PASSWORD_CHANGE_UNSUPPORTED_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "비밀번호 변경" })).not.toBeInTheDocument();
  });

  it.each([
    ["네트워크 오류", new TypeError("Failed to fetch")],
    ["500", new ApiError(500, "Internal Server Error")],
  ])("%s → 판정 실패(제출 비활성) → 다시 시도 1회 뒤 지원", async (_label, error) => {
    renderPage();
    await act(async () => probes[0].d.reject(error));

    expect(screen.getByText(PASSWORD_SUPPORT_PROBE_FAILED_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByText(PASSWORD_CHANGE_UNSUPPORTED_MESSAGE)).not.toBeInTheDocument();
    expect(submitButton()).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "다시 시도" }));
    expect(probes).toHaveLength(2);
    await act(async () => probes[1].d.resolve(NEW_ME));

    expect(screen.queryByText(PASSWORD_SUPPORT_PROBE_FAILED_MESSAGE)).not.toBeInTheDocument();
    expect(submitButton()).toBeEnabled();
  });

  it("401 → 로그인으로 보낸다(미지원 안내가 아니다)", async () => {
    renderPage();
    await act(async () => probes[0].d.reject(new ApiError(401, "Unauthorized")));

    expect(nav.replace).toHaveBeenCalledWith(
      `/login?redirect=${encodeURIComponent("/change-password?redirect=%2Fworkflows")}`,
    );
    expect(screen.queryByText(PASSWORD_CHANGE_UNSUPPORTED_MESSAGE)).not.toBeInTheDocument();
  });

  it.each([[404], [405]])("프로브 %i → 미지원 안내", async (status) => {
    renderPage();
    await act(async () => probes[0].d.reject(new ApiError(status, "x")));
    expect(screen.getByText(PASSWORD_CHANGE_UNSUPPORTED_MESSAGE)).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
  });
});

describe("변경 화면 — 제출·오류 매핑·redirect (AC-3.3)", () => {
  async function renderSupported(forced = true) {
    const view = renderPage();
    await act(async () => probes[0].d.resolve({ ...NEW_ME, must_change_password: forced }));
    return view;
  }

  it("성공하면 응답 user로 me 캐시를 갱신하고 redirect(내부 경로)로 replace한다", async () => {
    const changed = { ...NEW_ME, must_change_password: false };
    api.post.mockResolvedValue({ access_token: "a", refresh_token: "r", token_type: "bearer", user: changed });
    const { client } = await renderSupported();

    await fillForm();
    await userEvent.click(submitButton());

    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/workflows"));
    expect(api.post).toHaveBeenCalledWith("/api/v1/auth/password", {
      current_password: "old-password-1",
      new_password: "new-password-1",
    });
    expect(client.getQueryData(["me"])).toEqual(changed);
  });

  it.each([
    ["redirect=%2F%2Fattacker.example%2Fp"],
    ["redirect=https%3A%2F%2Fattacker.example"],
    ["redirect=%2Fa%2F..%2F%2Fattacker.example%2Fp"],
    [""],
  ])("외부·누락 redirect(%s)는 /로 간다", async (search) => {
    nav.search = search;
    api.post.mockResolvedValue({ user: { ...NEW_ME, must_change_password: false } });
    await renderSupported();
    await fillForm();
    await userEvent.click(submitButton());
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/"));
  });

  it.each([
    ["400 invalid_current_password", apiErr(400, { code: "invalid_current_password" }, "invalid_current_password"), INVALID_CURRENT_PASSWORD_MESSAGE],
    ["422 too_short", apiErr(422, { code: "invalid_new_password", reason: "too_short" }, "invalid_new_password"), PASSWORD_ISSUE_MESSAGE.too_short],
    ["422 too_long_bytes", apiErr(422, { code: "invalid_new_password", reason: "too_long_bytes" }, "invalid_new_password"), PASSWORD_ISSUE_MESSAGE.too_long_bytes],
    ["422 same_as_current", apiErr(422, { code: "invalid_new_password", reason: "same_as_current" }, "invalid_new_password"), PASSWORD_ISSUE_MESSAGE.same_as_current],
    ["429", apiErr(429, "Too Many Requests"), RATE_LIMITED_MESSAGE],
  ])("서버 %s → 사전 문구(입력값 없음)", async (_label, error, message) => {
    api.post.mockRejectedValue(error);
    await renderSupported();
    await fillForm("old-password-1", "new-password-1");
    await userEvent.click(submitButton());

    const alert = await screen.findByTestId("change-password-error");
    expect(alert).toHaveTextContent(message);
    expect(alert.textContent).not.toContain("old-password-1");
    expect(alert.textContent).not.toContain("new-password-1");
    expect(alert.textContent).not.toContain("서버 메시지");
    expect(nav.replace).not.toHaveBeenCalled();
  });

  it("제출 401 → 로그인으로 보낸다", async () => {
    api.post.mockRejectedValue(new ApiError(401, "Token invalidated by password change"));
    await renderSupported();
    await fillForm();
    await userEvent.click(submitButton());
    await waitFor(() =>
      expect(nav.replace).toHaveBeenCalledWith(
        `/login?redirect=${encodeURIComponent("/change-password?redirect=%2Fworkflows")}`,
      ),
    );
  });

  it.each([[404], [405]])("제출 %i → 미지원 안내로 바꾸고 재시도하지 않는다", async (status) => {
    api.post.mockRejectedValue(new ApiError(status, "x"));
    await renderSupported();
    await fillForm();
    await userEvent.click(submitButton());

    expect(await screen.findByText(PASSWORD_CHANGE_UNSUPPORTED_MESSAGE)).toBeInTheDocument();
    expect(api.post).toHaveBeenCalledTimes(1);
  });

  it.each([
    [["old-password-1", "short7!", "short7!"], PASSWORD_ISSUE_MESSAGE.too_short],
    [["old-password-1", "a".repeat(73), "a".repeat(73)], PASSWORD_ISSUE_MESSAGE.too_long_bytes],
    [["old-password-1", "new-password-1", "new-password-2"], PASSWORD_ISSUE_MESSAGE.mismatch],
    [["same-password-1", "same-password-1", "same-password-1"], PASSWORD_ISSUE_MESSAGE.same_as_current],
  ])("클라이언트 사전 검사 %j → 문구, POST 없음", async (values, message) => {
    await renderSupported();
    await fillForm(...(values as [string, string, string]));
    await userEvent.click(submitButton());
    expect(screen.getByTestId("change-password-error")).toHaveTextContent(message);
    expect(api.post).not.toHaveBeenCalled();
  });

  it("강제 상태에서는 「나중에」·취소 경로가 없고 로그아웃은 할 수 있다", async () => {
    api.post.mockResolvedValue(undefined);
    await renderSupported(true);
    expect(screen.queryByRole("link", { name: "취소" })).not.toBeInTheDocument();
    expect(screen.queryByText("나중에")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "로그아웃" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/auth/logout"));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/login"));
  });
});

describe("변경 화면 — 성공 안내 토스트(BL-172, AC-4.9)", () => {
  const SUCCESS = "비밀번호를 변경했습니다. 다른 기기에서는 다시 로그인해야 합니다";

  async function renderSupportedWithToaster(forced: boolean) {
    render(<Toaster />); // 실제 sonner — 앱은 루트 Providers의 Toaster가 그린다
    const view = renderPage();
    await act(async () => probes[0].d.resolve({ ...NEW_ME, must_change_password: forced }));
    return view;
  }

  it.each([
    ["본인 변경(강제 아님)", false],
    ["강제 변경", true],
  ])("%s 성공 → 「비밀번호를 변경했습니다. 다른 기기에서는 다시 로그인해야 합니다」 토스트, 판정·리다이렉트는 그대로", async (_label, forced) => {
    api.post.mockResolvedValue({ user: { ...NEW_ME, must_change_password: false } });
    await renderSupportedWithToaster(forced as boolean);
    await fillForm("old-password-1", "new-password-1");
    await userEvent.click(submitButton());

    const toast = await screen.findByText(SUCCESS);
    expect(nav.replace).toHaveBeenCalledWith("/workflows");
    expect(nav.replace).toHaveBeenCalledTimes(1);
    // 비밀값은 토스트에 없다
    expect(toast.closest("[data-sonner-toast]")?.textContent).not.toMatch(/old-password-1|new-password-1/);
  });

  it.each([
    ["400 invalid_current_password", apiErr(400, { code: "invalid_current_password" }, "invalid_current_password")],
    ["제출 404(미지원)", new ApiError(404, "x")],
    ["제출 401", new ApiError(401, "Unauthorized")],
  ])("실패(%s)에는 성공 토스트가 없다", async (_label, error) => {
    api.post.mockRejectedValue(error);
    await renderSupportedWithToaster(false);
    await fillForm();
    await userEvent.click(submitButton());
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    await act(async () => {});
    expect(screen.queryByText(SUCCESS)).not.toBeInTheDocument();
  });

  it("클라이언트 사전 검사에 걸리면 요청도 토스트도 없다", async () => {
    await renderSupportedWithToaster(false);
    await fillForm("old-password-1", "new-password-1", "new-password-2");
    await userEvent.click(submitButton());
    expect(screen.getByTestId("change-password-error")).toHaveTextContent(PASSWORD_ISSUE_MESSAGE.mismatch);
    expect(api.post).not.toHaveBeenCalled();
    expect(screen.queryByText(SUCCESS)).not.toBeInTheDocument();
  });
});
