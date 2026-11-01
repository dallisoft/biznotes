/**
 * 비밀값 수명주기 테스트 도구(plan §8.4 종료 경로 테스트 행렬 — AC-5.2·5.2b). 테스트 전용 모듈이다(앱 코드가 부르지 않는다).
 *
 * - {@link installFakeNetwork}: 네트워크 경계(global `fetch`)만 대체한다. `api` 클라이언트·훅·다이얼로그·epoch 로직은 실경로로
 *   돈다(§3.3-3 R6). 비밀값 요청은 테스트가 응답 시점을 정할 수 있게 붙잡아 둔다(지연·역순 응답).
 * - {@link findSecret}: 비밀값 문자열이 앱이 관리하는 저장소 어디에 남았는지 찾는다 — DOM(토스트 포함), React 상태(커밋된
 *   파이버 트리의 hook 상태·props), TanStack mutation 캐시(`state`·`variables` 직렬화), 쿼리 캐시, localStorage·
 *   sessionStorage, URL·history, 콘솔 출력. 빈 배열이면 0건이다.
 *
 * 범위(plan §6.5 「보장 범위」): React가 다음 렌더를 위해 잡아 두는 이전 렌더 버퍼(alternate 파이버)와 클로저 안의 값,
 * JS 엔진 메모리는 검사하지 않는다 — 앱이 관리하는 저장소에서 참조가 사라졌는지까지만 본다.
 */
import type { QueryClient } from "@tanstack/react-query";
import { vi } from "vitest";
import { API_BASE_URL } from "@/lib/api";

// ---- 네트워크 경계 ----

export interface NetworkCall {
  method: string;
  path: string;
  body: unknown;
  /** 응답을 보낸다(204면 본문 없음). 이미 응답했으면 무시한다. */
  respond: (status: number, body?: unknown) => void;
  /** 응답 없이 실패시킨다(네트워크 오류). */
  fail: (error: unknown) => void;
  settled: boolean;
}

/** 경로 처리 — 응답(`{status, body}`)을 돌려주면 바로 응답하고, `"hold"`면 테스트가 응답할 때까지 붙잡는다. */
export type Route = (
  method: string,
  path: string,
  body: unknown,
) => { status: number; body?: unknown } | "hold";

export interface FakeNetwork {
  calls: NetworkCall[];
  /** 아직 응답하지 않은 요청 중 조건에 맞는 것 */
  held: (method: string, path: string | RegExp) => NetworkCall[];
  /** 조건에 맞는 요청 수(응답 여부 무관) */
  count: (method: string, path: string | RegExp) => number;
}

function jsonResponse(status: number, body: unknown): Response {
  if (status === 204) return new Response(null, { status });
  return new Response(JSON.stringify(body ?? null), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function matches(call: NetworkCall, method: string, path: string | RegExp): boolean {
  if (call.method !== method) return false;
  return typeof path === "string" ? call.path === path : path.test(call.path);
}

export function installFakeNetwork(route: Route): FakeNetwork {
  const calls: NetworkCall[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(
      (url: string, init: RequestInit = {}) =>
        new Promise<Response>((resolve, reject) => {
          const method = init.method ?? "GET";
          const path = String(url).replace(API_BASE_URL, "");
          const body = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
          const call: NetworkCall = {
            method,
            path,
            body,
            settled: false,
            respond: (status, resBody) => {
              if (call.settled) return;
              call.settled = true;
              resolve(jsonResponse(status, resBody));
            },
            fail: (error) => {
              if (call.settled) return;
              call.settled = true;
              reject(error);
            },
          };
          calls.push(call);
          const result = route(method, path, body);
          if (result !== "hold") call.respond(result.status, result.body);
        }),
    ),
  );
  return {
    calls,
    held: (method, path) => calls.filter((c) => !c.settled && matches(c, method, path)),
    count: (method, path) => calls.filter((c) => matches(c, method, path)).length,
  };
}

// ---- 비밀값 검색 ----

// 파이버 구조 링크·개발용 메타는 따라가지 않는다(트리는 child/sibling으로 따로 돈다).
const SKIP_KEYS = new Set([
  "return",
  "child",
  "sibling",
  "alternate",
  "stateNode",
  "_owner",
  "_debugOwner",
  "_debugStack",
  "_debugTask",
  "_debugInfo",
  "_store",
]);

/** 값 안에 문자열 `needle`이 있는가(문자열·배열·평범한 객체·Map·Set을 따라간다. DOM 노드·함수는 보지 않는다). */
export function deepIncludes(value: unknown, needle: string, seen = new WeakSet<object>()): boolean {
  if (typeof value === "string") return value.includes(needle);
  if (value === null || typeof value !== "object") return false;
  if (seen.has(value)) return false;
  seen.add(value);
  if (typeof Node !== "undefined" && value instanceof Node) return false;
  if (value instanceof Map) {
    for (const [k, v] of value) {
      if (deepIncludes(k, needle, seen) || deepIncludes(v, needle, seen)) return true;
    }
    return false;
  }
  if (value instanceof Set) {
    for (const v of value) if (deepIncludes(v, needle, seen)) return true;
    return false;
  }
  for (const key of Object.keys(value)) {
    if (SKIP_KEYS.has(key)) continue;
    let child: unknown;
    try {
      child = (value as Record<string, unknown>)[key];
    } catch {
      continue;
    }
    if (deepIncludes(child, needle, seen)) return true;
  }
  return false;
}

interface Fiber {
  child: Fiber | null;
  sibling: Fiber | null;
  memoizedState: unknown;
  memoizedProps: unknown;
  pendingProps: unknown;
  updateQueue: unknown;
  stateNode: unknown;
}

/** 커밋된(현재) 파이버 트리의 루트 — RTL `render`의 container에서 찾는다. */
function currentRootFiber(container: Element): Fiber | null {
  const key = Object.keys(container).find((k) => k.startsWith("__reactContainer$"));
  if (!key) return null;
  const created = (container as unknown as Record<string, Fiber>)[key];
  const fiberRoot = created?.stateNode as { current?: Fiber } | undefined;
  return fiberRoot?.current ?? created ?? null;
}

/** React 상태(커밋된 트리의 모든 컴포넌트 hook 상태·props·업데이트 큐)에 `needle`이 있는가. */
export function reactStateIncludes(container: Element, needle: string): boolean {
  const root = currentRootFiber(container);
  if (!root) throw new Error("React 루트를 찾지 못했습니다");
  const stack: Fiber[] = [root];
  const seen = new WeakSet<object>();
  while (stack.length > 0) {
    const fiber = stack.pop()!;
    if (
      deepIncludes(fiber.memoizedState, needle, seen) ||
      deepIncludes(fiber.memoizedProps, needle, seen) ||
      deepIncludes(fiber.pendingProps, needle, seen) ||
      deepIncludes(fiber.updateQueue, needle, seen)
    ) {
      return true;
    }
    if (fiber.sibling) stack.push(fiber.sibling);
    if (fiber.child) stack.push(fiber.child);
  }
  return false;
}

export interface ConsoleSpy {
  output: () => string;
  restore: () => void;
}

/** console.* 출력 수집(원래 출력은 막지 않는다). */
export function spyConsole(): ConsoleSpy {
  const methods = ["log", "info", "warn", "error", "debug"] as const;
  const spies = methods.map((m) => vi.spyOn(console, m));
  return {
    output: () =>
      spies
        .flatMap((s) => s.mock.calls)
        .map((args) => args.map((a) => (typeof a === "string" ? a : safeStringify(a))).join(" "))
        .join("\n"),
    restore: () => spies.forEach((s) => s.mockRestore()),
  };
}

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}

function storageText(storage: Storage | undefined): string {
  if (!storage) return "";
  const parts: string[] = [];
  for (let i = 0; i < storage.length; i += 1) {
    const key = storage.key(i);
    if (key !== null) parts.push(`${key}=${storage.getItem(key)}`);
  }
  return parts.join("\n");
}

/**
 * `secret`이 남은 곳의 목록. 빈 배열이면 0건이다.
 * mutation 캐시는 각 mutation의 `state`(data·variables·error·context 포함)를 직렬화해 본다(plan §8.4).
 */
export function findSecret(
  secret: string,
  {
    container,
    queryClient,
    console: consoleSpy,
  }: { container: Element; queryClient: QueryClient; console?: ConsoleSpy },
): string[] {
  const where: string[] = [];
  if (document.body.innerHTML.includes(secret) || (document.body.textContent ?? "").includes(secret)) {
    where.push("DOM");
  }
  if (reactStateIncludes(container, secret)) where.push("React 상태");
  for (const m of queryClient.getMutationCache().getAll()) {
    if (safeStringify(m.state).includes(secret) || deepIncludes(m.state, secret)) {
      where.push("mutation 캐시(state)");
    }
    if (safeStringify(m.state.variables).includes(secret)) where.push("mutation 캐시(variables)");
  }
  for (const q of queryClient.getQueryCache().getAll()) {
    if (safeStringify(q.state).includes(secret) || deepIncludes(q.state, secret)) {
      where.push(`쿼리 캐시 ${safeStringify(q.queryKey)}`);
    }
  }
  if (storageText(globalThis.localStorage).includes(secret)) where.push("localStorage");
  if (storageText(globalThis.sessionStorage).includes(secret)) where.push("sessionStorage");
  if (window.location.href.includes(secret) || safeStringify(window.history.state).includes(secret)) {
    where.push("URL");
  }
  if (consoleSpy && consoleSpy.output().includes(secret)) where.push("콘솔");
  return where;
}

/** 다음 매크로 태스크까지 기다린다(응답 처리·React 커밋·sonner 렌더). */
export function flushTasks(times = 3): Promise<void> {
  return (async () => {
    for (let i = 0; i < times; i += 1) await new Promise<void>((r) => setTimeout(r, 0));
  })();
}
