import { QueryClient, QueryClientProvider, useMutation, useQuery } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { Toaster } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api-client";
import {
  type SecretMutation,
  type SecretScope,
  SecretLifecycle,
  useSecretMutation,
  useSecretScope,
} from "./secret-mutation";
import {
  type FakeNetwork,
  findSecret,
  flushTasks,
  installFakeNetwork,
  spyConsole,
} from "./secret-mutation.test-kit";

// plan §6.5 비밀값 규칙 ①~⑨ · D-22 · D-28 — 훅·epoch·요청 표 로직은 실경로, 네트워크 경계(global fetch)만 대체한다.
// 비밀값은 모두 더미다(실계정·실비밀번호 없음).

const SECRET_A = "dummy-secret-AAAA-0001";
const SECRET_B = "dummy-secret-BBBB-0002";
const LOST = "비밀값을 받았지만 화면을 떠나 표시하지 못했습니다";

afterEach(async () => {
  await flushTasks(1);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

type Revealed = { label: string; secret: string };

function network(): FakeNetwork {
  return installFakeNetwork((method, path) => {
    if (method === "POST" && path.startsWith("/secret/")) return "hold";
    if (method === "GET" && path === "/api/v1/auth/me") return { status: 200, body: { id: "admin-1" } };
    return { status: 200, body: [] };
  });
}

/** 테스트 화면: scope 하나에 비밀값 요청 훅 `hooks`개(2개면 「강제로 두 요청을 만든 픽스처」 — 같은 flow 훅 두 벌). */
function makeScreen(hooks = 1, onSuccess = vi.fn()) {
  const handle: {
    scope?: SecretScope<Revealed>;
    mutations: SecretMutation<string>[];
  } = { mutations: [] };

  function useOne(scope: SecretScope<Revealed>) {
    return useSecretMutation<string, { secret: string }, Revealed>(scope, {
      request: (key) => api.post<{ secret: string }>(`/secret/${key}`),
      reveal: (res, key) => ({ label: key, secret: res.secret }),
      onSuccess,
      lostNotice: LOST,
      failureAction: "비밀값을 발급",
    });
  }

  function Screen() {
    // 세션 쿼리(useMe와 같은 키)가 캐시에 있는 실제 화면 조건을 만든다
    useQuery({ queryKey: ["me"], queryFn: () => api.get("/api/v1/auth/me") });
    const scope = useSecretScope<Revealed>();
    const m1 = useOne(scope);
    const m2 = useOne(scope);
    handle.scope = scope;
    handle.mutations = hooks === 2 ? [m1, m2] : [m1];
    const value = scope.revealed?.value;
    return createElement(
      "div",
      null,
      createElement("p", { "data-testid": "busy" }, scope.busy ? "busy" : "idle"),
      value
        ? createElement("output", { "data-testid": "revealed" }, `${value.label}:${value.secret}`)
        : null,
    );
  }

  return { handle, Screen, onSuccess };
}

function setup(hooks = 1) {
  const net = network();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const consoleSpy = spyConsole();
  const screenDef = makeScreen(hooks);
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children, createElement(Toaster));
  const view = render(createElement(screenDef.Screen), { wrapper });
  return { net, client, consoleSpy, view, ...screenDef };
}

const post = (net: FakeNetwork, key: string) => {
  const [call] = net.held("POST", `/secret/${key}`);
  if (!call) throw new Error(`POST /secret/${key} 요청 없음`);
  return call;
};

async function respond(net: FakeNetwork, key: string, secret: string) {
  await act(async () => {
    post(net, key).respond(200, { secret });
    await flushTasks();
  });
}

describe("SecretLifecycle — 요청 표·epoch(⑦, D-28)", () => {
  it("같은 epoch의 최신 요청만 보이고, 이전 요청·닫기·언마운트·세션 종료 뒤 응답은 버린다", () => {
    const setRevealed = vi.fn();
    const setBusy = vi.fn();
    const life = new SecretLifecycle<string>(setRevealed, setBusy);

    const t1 = life.begin();
    const t2 = life.begin();
    expect(setBusy).toHaveBeenLastCalledWith(true);
    expect(life.deliver(t1, "old")).toBe("superseded");
    expect(life.deliver(t2, "new")).toBe("shown");
    expect(setRevealed).toHaveBeenLastCalledWith({ requestId: t2.requestId, value: "new" });
    life.settle();
    expect(setBusy).not.toHaveBeenLastCalledWith(false);
    life.settle();
    expect(setBusy).toHaveBeenLastCalledWith(false);

    const t3 = life.begin();
    life.end("closed");
    expect(setRevealed).toHaveBeenLastCalledWith(null);
    expect(life.endReason(t3)).toBe("closed");
    expect(life.deliver(t3, "late")).toBe("dropped");

    const t4 = life.begin();
    life.end("unmounted");
    expect(life.deliver(t4, "late")).toBe("lost");

    const t5 = life.begin();
    life.end("session-ended");
    expect(life.endReason(t5)).toBe("session-ended");
    expect(life.deliver(t5, "late")).toBe("lost");
    expect(life.endReason(life.begin())).toBeUndefined();
    // 버린 응답은 상태를 다시 채우지 않았다 — 마지막 setRevealed는 여전히 null(종료)
    expect(setRevealed).toHaveBeenLastCalledWith(null);
  });
});

describe("useSecretMutation — 반환값·variables·캐시에 비밀값 없음(①②③⑥)", () => {
  it("응답의 비밀값은 다이얼로그 상태로만 가고, mutation data는 {ok, delivery}, variables는 대상·요청 표뿐, gcTime 0이다", async () => {
    const { net, client, consoleSpy, view, handle, onSuccess } = setup();
    act(() => {
      expect(handle.mutations[0].start("alpha")).toBe(true);
    });
    await flushTasks();
    expect(screen.getByTestId("busy")).toHaveTextContent("busy");
    await respond(net, "alpha", SECRET_A);

    expect(screen.getByTestId("revealed")).toHaveTextContent(`alpha:${SECRET_A}`);
    expect(onSuccess).toHaveBeenCalledWith("alpha");
    const [mutation] = client.getMutationCache().getAll();
    expect(mutation.options.gcTime).toBe(0);
    expect(mutation.state.data).toEqual({ ok: true, delivery: "shown" });
    expect(mutation.state.variables).toEqual({ vars: "alpha", ticket: { requestId: 1, epoch: 0 } });
    // 표시 중에는 DOM·React 상태에만 있다(검색 도구가 실제로 찾는다는 양성 대조)
    expect(findSecret(SECRET_A, { container: view.container, queryClient: client, console: consoleSpy })).toEqual(
      ["DOM", "React 상태"],
    );

    act(() => handle.scope!.close());
    expect(screen.queryByTestId("revealed")).toBeNull();
    expect(findSecret(SECRET_A, { container: view.container, queryClient: client, console: consoleSpy })).toEqual(
      [],
    );
    expect(screen.getByTestId("busy")).toHaveTextContent("idle");
  });

  it("대조(§15.3 재현): 비밀값을 반환하는 일반 mutation은 reset() 뒤에도 mutation 캐시에 남고, 검색 도구가 그것을 찾는다", async () => {
    const net = network();
    const client = new QueryClient();
    let naive: ReturnType<typeof useMutation<{ secret: string }>> | undefined;
    function Naive() {
      naive = useMutation({ mutationFn: () => api.post<{ secret: string }>("/secret/naive") });
      return null;
    }
    const view = render(createElement(QueryClientProvider, { client }, createElement(Naive)));
    act(() => naive!.mutate());
    await flushTasks();
    await respond(net, "naive", SECRET_A);
    act(() => naive!.reset());
    await flushTasks();
    expect(findSecret(SECRET_A, { container: view.container, queryClient: client })).toContain(
      "mutation 캐시(state)",
    );
  });

  it("같은 훅은 대기 중에 다시 시작하지 않는다(이중 클릭 — 단일 in-flight ⑧)", async () => {
    const { net, handle } = setup();
    act(() => {
      expect(handle.mutations[0].start("alpha")).toBe(true);
      expect(handle.mutations[0].start("alpha")).toBe(false);
    });
    await flushTasks();
    expect(net.count("POST", "/secret/alpha")).toBe(1);
    await respond(net, "alpha", SECRET_A);
    act(() => {
      expect(handle.mutations[0].start("beta")).toBe(true);
    });
    await flushTasks();
    expect(net.count("POST", "/secret/beta")).toBe(1);
  });
});

describe("응답 역순 도착 — 강제로 두 요청을 만든 픽스처(⑦, D-28)", () => {
  it.each([
    ["이전 요청이 먼저 도착", ["first", "second"] as const],
    ["최신 요청이 먼저 도착", ["second", "first"] as const],
  ])("%s해도 최신 requestId의 비밀값만 보이고 이전 응답은 버린다", async (_name, order) => {
    const { net, client, consoleSpy, view, handle } = setup(2);
    act(() => {
      handle.mutations[0].start("first");
      handle.mutations[1].start("second");
    });
    await flushTasks();
    const secrets = { first: SECRET_A, second: SECRET_B };
    for (const key of order) await respond(net, key, secrets[key]);

    expect(screen.getByTestId("revealed")).toHaveTextContent(`second:${SECRET_B}`);
    expect(findSecret(SECRET_A, { container: view.container, queryClient: client, console: consoleSpy })).toEqual(
      [],
    );
    const deliveries = client
      .getMutationCache()
      .getAll()
      .map((m) => (m.state.data as { delivery: string } | undefined)?.delivery)
      .sort();
    expect(deliveries).toEqual(["shown", "superseded"]);
    expect(screen.getByTestId("busy")).toHaveTextContent("idle");
  });
});

describe("종료 뒤 늦은 응답(④⑤⑦⑨)", () => {
  it("로그아웃(queryClient.clear — 세션 쿼리 제거) 뒤 도착한 비밀값은 버리고, 비밀값 없는 안내만 보인다", async () => {
    const { net, client, consoleSpy, view, handle, onSuccess } = setup();
    act(() => handle.mutations[0].start("alpha"));
    await flushTasks();
    act(() => client.clear());
    await respond(net, "alpha", SECRET_A);

    expect(screen.queryByTestId("revealed")).toBeNull();
    expect(onSuccess).toHaveBeenCalledWith("alpha");
    await waitFor(() => expect(screen.getByText(LOST)).toBeInTheDocument());
    expect(findSecret(SECRET_A, { container: view.container, queryClient: client, console: consoleSpy })).toEqual(
      [],
    );
  });

  it("표시 중 세션 쿼리(['me'])만 지워져도 세션 종료로 보고 다이얼로그 상태를 지운다", async () => {
    const { net, client, view, handle } = setup();
    act(() => handle.mutations[0].start("alpha"));
    await flushTasks();
    await respond(net, "alpha", SECRET_A);
    expect(screen.getByTestId("revealed")).toBeInTheDocument();
    act(() => client.removeQueries({ queryKey: ["me"], exact: true }));
    expect(screen.queryByTestId("revealed")).toBeNull();
    expect(findSecret(SECRET_A, { container: view.container, queryClient: client })).toEqual([]);
  });

  it("다른 쿼리가 지워지는 것은 세션 종료가 아니다", async () => {
    const { net, client, handle } = setup();
    client.setQueryData(["unrelated"], 1);
    act(() => handle.mutations[0].start("alpha"));
    await flushTasks();
    await respond(net, "alpha", SECRET_A);
    act(() => client.removeQueries({ queryKey: ["unrelated"] }));
    expect(screen.getByTestId("revealed")).toBeInTheDocument();
  });

  it("응답 전에 화면을 떠나면(언마운트) 늦은 비밀값은 버리고 안내 토스트만, 후속 처리(목록 무효화)는 실행한다", async () => {
    const net = network();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const consoleSpy = spyConsole();
    const def = makeScreen();
    const view = render(
      createElement(
        QueryClientProvider,
        { client },
        createElement(Toaster),
        createElement("div", { id: "screen-slot" }),
      ),
    );
    const screenView = render(createElement(QueryClientProvider, { client }, createElement(def.Screen)));
    act(() => def.handle.mutations[0].start("alpha"));
    await flushTasks();
    screenView.unmount();
    await respond(net, "alpha", SECRET_A);

    expect(def.onSuccess).toHaveBeenCalledWith("alpha");
    await waitFor(() => expect(screen.getByText(LOST)).toBeInTheDocument());
    expect(findSecret(SECRET_A, { container: view.container, queryClient: client, console: consoleSpy })).toEqual(
      [],
    );
  });

  it("실패는 비밀값 없이 실패 토스트로 알리고, 로그아웃 뒤의 실패는 알리지 않는다", async () => {
    const { net, client, handle } = setup();
    act(() => handle.mutations[0].start("alpha"));
    await flushTasks();
    await act(async () => {
      post(net, "alpha").respond(500, { detail: "서버 오류" });
      await flushTasks();
    });
    await waitFor(() => expect(screen.getByText("비밀값을 발급하지 못했습니다")).toBeInTheDocument());

    act(() => handle.mutations[0].start("beta"));
    await flushTasks();
    act(() => client.clear());
    await act(async () => {
      // 401은 api-client가 토큰 갱신 뒤 재시도하므로, 세션 종료 뒤의 실패는 500으로 만든다
      post(net, "beta").respond(500, { detail: "세션 종료 뒤 실패" });
      await flushTasks();
    });
    expect(screen.getAllByText("비밀값을 발급하지 못했습니다")).toHaveLength(1);
  });
});

describe("다이얼로그 닫기(④)", () => {
  it("닫은 뒤 이전 요청의 응답이 와도 다시 열리지 않는다", async () => {
    const { net, client, view, handle } = setup(2);
    act(() => handle.mutations[0].start("first"));
    await flushTasks();
    await respond(net, "first", SECRET_A);
    act(() => handle.mutations[1].start("second"));
    await flushTasks();
    act(() => handle.scope!.close());
    await respond(net, "second", SECRET_B);
    expect(screen.queryByTestId("revealed")).toBeNull();
    expect(findSecret(SECRET_B, { container: view.container, queryClient: client })).toEqual([]);
    expect(findSecret(SECRET_A, { container: view.container, queryClient: client })).toEqual([]);
  });
});
