import { act, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Providers } from "@/components/providers";
import { api, PASSWORD_CHANGE_REQUIRED } from "@/lib/api-client";

// 프레임워크 경계: jsdom에는 App Router가 없다 — 라우터·경로만 대역으로 둔다.
// api-client는 실제 모듈이고 네트워크(fetch)만 대역이다.
const nav = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn(), pathname: "/" }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: nav.replace, push: nav.push }),
  usePathname: () => nav.pathname,
}));

const forced = () =>
  new Response(
    JSON.stringify({ detail: { code: PASSWORD_CHANGE_REQUIRED, detail: "비밀번호를 변경해야 합니다" } }),
    { status: 403, headers: { "Content-Type": "application/json" } },
  );

function stubFetch(make: () => Response) {
  const fn = vi.fn(async () => make());
  vi.stubGlobal("fetch", fn);
  return fn;
}

function goTo(path: string) {
  window.history.pushState({}, "", path);
  nav.pathname = new URL(path, window.location.origin).pathname;
}

beforeEach(() => {
  // next-themes(ThemeProvider enableSystem)가 matchMedia를 부른다 — jsdom에 없어 최소 대역
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
    })),
  );
  goTo("/notebooks/nb1?s=s1");
});

afterEach(() => {
  nav.replace.mockReset();
  nav.push.mockReset();
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

describe("Providers — 403 password_change_required 구독 (AC-3.1)", () => {
  it("403 code를 받으면 현재 경로를 redirect로 실어 변경 화면으로 replace한다", async () => {
    stubFetch(forced);
    render(<Providers>본문</Providers>);

    await act(async () => {
      await api.get("/api/v1/notebooks").catch(() => undefined);
    });

    expect(nav.replace).toHaveBeenCalledTimes(1);
    expect(nav.replace).toHaveBeenCalledWith(
      `/change-password?redirect=${encodeURIComponent("/notebooks/nb1?s=s1")}`,
    );
  });

  it("React Query 밖의 직접 getBlob(다운로드) 403 code도 이동한다", async () => {
    stubFetch(forced);
    render(<Providers>본문</Providers>);

    await act(async () => {
      await api.getBlob("/api/v1/exports/1/download").catch(() => undefined);
    });

    expect(nav.replace).toHaveBeenCalledTimes(1);
    expect(nav.replace).toHaveBeenCalledWith(
      `/change-password?redirect=${encodeURIComponent("/notebooks/nb1?s=s1")}`,
    );
  });

  it("같은 화면에서 연속 403이 와도 이동은 한 번이다(루프 없음)", async () => {
    stubFetch(forced);
    render(<Providers>본문</Providers>);

    await act(async () => {
      await Promise.all([
        api.get("/api/v1/notebooks").catch(() => undefined),
        api.get("/api/v1/models").catch(() => undefined),
        api.getBlob("/api/v1/exports/1/download").catch(() => undefined),
      ]);
    });

    expect(nav.replace).toHaveBeenCalledTimes(1);
  });

  it("변경 화면에 있으면 이동하지 않는다", async () => {
    goTo("/change-password?redirect=%2Fx");
    stubFetch(forced);
    render(<Providers>본문</Providers>);

    await act(async () => {
      await api.get("/api/v1/notebooks").catch(() => undefined);
    });

    expect(nav.replace).not.toHaveBeenCalled();
  });

  it("화면이 바뀐 뒤 다시 403이 오면 다시 이동한다", async () => {
    stubFetch(forced);
    const view = render(<Providers>본문</Providers>);
    await act(async () => {
      await api.get("/api/v1/notebooks").catch(() => undefined);
    });
    expect(nav.replace).toHaveBeenCalledTimes(1);

    goTo("/workflows");
    view.rerender(<Providers>본문</Providers>);
    await act(async () => {
      await api.get("/api/v1/workflows").catch(() => undefined);
    });

    await waitFor(() => expect(nav.replace).toHaveBeenCalledTimes(2));
    expect(nav.replace).toHaveBeenLastCalledWith(
      `/change-password?redirect=${encodeURIComponent("/workflows")}`,
    );
  });

  it("code 없는 403·다른 code는 이동하지 않는다", async () => {
    const responses = [
      new Response(JSON.stringify({ detail: "권한 없음" }), { status: 403 }),
      new Response(JSON.stringify({ detail: { code: "other", detail: "x" } }), { status: 403 }),
    ];
    stubFetch(() => responses.shift() as Response);
    render(<Providers>본문</Providers>);

    await act(async () => {
      await api.get("/api/v1/a").catch(() => undefined);
      await api.get("/api/v1/b").catch(() => undefined);
    });

    expect(nav.replace).not.toHaveBeenCalled();
  });

  it("언마운트하면 구독을 해제한다", async () => {
    stubFetch(forced);
    const view = render(<Providers>본문</Providers>);
    view.unmount();

    await api.get("/api/v1/notebooks").catch(() => undefined);

    expect(nav.replace).not.toHaveBeenCalled();
  });
});
