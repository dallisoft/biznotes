"use client";

/**
 * 비밀값 mutation 수명주기 — plan §6.5 「SecretRevealDialog + 비밀값 mutation 규칙」 ①~⑨, D-22·D-28, HCI-5.
 *
 * 비밀값(임시 비밀번호·웹훅 시크릿)을 돌려주는 요청은 이 모듈로만 보낸다.
 * ① `mutationFn` 안에서 응답을 받자마자 비밀값을 화면의 결과 다이얼로그 상태로 직접 넘기고, `mutationFn`의 반환값에는
 *   비밀값을 넣지 않는다(`{ ok, delivery }`만 돌려준다 — TanStack은 반환값을 `Mutation.state.data`에 둔다, §15.3 재현).
 * ② mutation `gcTime: 0`.
 * ③ mutation `variables`는 `{ vars, ticket }` — 대상 식별자와 요청 표만 담고 비밀값은 담지 않는다.
 * ④ 결과 다이얼로그 상태는 닫기·Esc·바깥 클릭({@link SecretScope.close}), 경로 이탈(언마운트), 로그아웃에서 `null`이 된다.
 * ⑤ 응답 전에 화면을 떠났으면 늦게 온 비밀값을 버리고 비밀값 없는 안내 토스트만 보인다.
 * ⑥ 쿼리 캐시·localStorage·URL·로그·토스트에 비밀값을 쓰지 않는다(이 모듈은 비밀값을 React 상태 하나에만 둔다).
 * ⑦ 화면마다 `secretEpoch`(닫기·로그아웃·언마운트 때 1 증가)와 요청마다 `requestId`를 둔다. 응답은 `requestId`가 최신이고
 *   epoch가 요청 시점과 같을 때만 다이얼로그에 들어간다 — 종료 뒤 늦은 응답이 상태를 다시 채우지 않는다.
 * ⑧ 단일 in-flight: 같은 요청 훅은 대기 중에 다시 시작되지 않고({@link SecretMutation.start}가 `false`), 화면은
 *   {@link SecretScope.busy}로 모든 비밀값 컨트롤을 비활성화한다.
 * ⑨ 로그아웃: `useLogout`은 `queryClient.clear()`로 세션 쿼리(`["me"]`)를 지운다 — 그 제거를 세션 종료로 보고 ⑦의 epoch를
 *   올려 상태를 지운다(화면이 아직 마운트되어 있어도).
 *
 * 보장 범위: 앱이 관리하는 저장소(React 상태·TanStack 쿼리/mutation 캐시·DOM)에서 참조를 없애는 것까지다. JS 엔진 메모리의
 * 물리적 소거·브라우저 확장·개발자 도구 관찰은 범위 밖이다.
 */
import { useMutation, useQueryClient, type QueryKey } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { ERROR_DURATION_MS, notifyError } from "@/lib/notify";

/** 세션 사용자 쿼리 키 — `useMe`(`hooks/use-auth.ts`)와 같다. 로그아웃의 `queryClient.clear()`가 이 쿼리를 지운다. */
export const SESSION_QUERY_KEY: QueryKey = ["me"];

/** 결과 다이얼로그 상태가 끝난 이유 — epoch를 올린 사건. */
export type SecretEndReason = "closed" | "session-ended" | "unmounted";

/**
 * 응답 처리 결과. `shown` = 다이얼로그에 넣음, `superseded` = 더 최근 요청이 있어 버림, `dropped` = 다이얼로그를 닫은 뒤라
 * 버림, `lost` = 화면을 떠났거나(언마운트) 로그아웃한 뒤라 버림(비밀값 없는 안내 대상).
 */
export type SecretDelivery = "shown" | "superseded" | "dropped" | "lost";

/** 요청 표 — 요청 시점의 epoch와 요청 번호. 비밀값이 아니다. */
export interface SecretTicket {
  readonly requestId: number;
  readonly epoch: number;
}

/** 다이얼로그에 보일 비밀값과 그 요청 번호. */
export interface RevealedSecret<T> {
  readonly requestId: number;
  readonly value: T;
}

/**
 * 화면 하나의 비밀값 수명 상태. 렌더와 무관하게 읽고 써야 하는 값(epoch·최신 요청 번호·대기 수)만 담고, 비밀값은
 * React 상태(`setRevealed`)로만 넘긴다 — 이 객체는 비밀값을 필드에 담지 않는다.
 */
export class SecretLifecycle<T> {
  private epoch = 0;
  private latest = 0;
  private inFlight = 0;
  /** epoch → 그 epoch를 끝낸 사건. 늦은 응답의 처리(안내 여부)를 정한다. */
  private readonly endedBy = new Map<number, SecretEndReason>();

  constructor(
    private readonly setRevealed: (value: RevealedSecret<T> | null) => void,
    private readonly setBusy: (busy: boolean) => void,
  ) {}

  /** 요청을 시작한다 — 요청 번호를 올리고 대기 수를 센다. */
  begin(): SecretTicket {
    this.inFlight += 1;
    this.latest += 1;
    this.setBusy(true);
    return { requestId: this.latest, epoch: this.epoch };
  }

  /** 요청이 끝났다(성공·실패 무관). */
  settle(): void {
    this.inFlight = Math.max(0, this.inFlight - 1);
    if (this.inFlight === 0) this.setBusy(false);
  }

  /** 응답의 비밀값을 넘긴다 — epoch가 같고 최신 요청일 때만 다이얼로그 상태에 넣는다(⑦). */
  deliver(ticket: SecretTicket, value: T): SecretDelivery {
    if (ticket.epoch !== this.epoch) {
      return this.endedBy.get(ticket.epoch) === "closed" ? "dropped" : "lost";
    }
    if (ticket.requestId !== this.latest) return "superseded";
    this.setRevealed({ requestId: ticket.requestId, value });
    return "shown";
  }

  /** 이 요청 표의 epoch를 끝낸 사건(아직 같은 epoch면 undefined). */
  endReason(ticket: SecretTicket): SecretEndReason | undefined {
    return ticket.epoch === this.epoch ? undefined : this.endedBy.get(ticket.epoch);
  }

  /** 다이얼로그 상태를 지우고 epoch를 올린다(④·⑦·⑨). */
  end(reason: SecretEndReason): void {
    this.endedBy.set(this.epoch, reason);
    this.epoch += 1;
    this.setRevealed(null);
  }
}

export interface SecretScope<T> {
  /** 결과 다이얼로그에 보일 비밀값(없으면 null). */
  revealed: RevealedSecret<T> | null;
  /** 이 화면의 비밀값 요청이 하나라도 대기 중인가 — 모든 비밀값 컨트롤을 비활성화한다(⑧). */
  busy: boolean;
  /** 결과 다이얼로그를 닫는다(닫기·Esc·바깥 클릭) — 상태를 지우고 epoch를 올린다. */
  close: () => void;
  /** {@link useSecretMutation}이 쓰는 수명 상태. */
  lifecycle: SecretLifecycle<T>;
}

function isSessionKey(key: QueryKey): boolean {
  return key.length === SESSION_QUERY_KEY.length && key.every((part, i) => part === SESSION_QUERY_KEY[i]);
}

/**
 * 비밀값을 여는 화면(사용자·그룹, 자동화)마다 하나 둔다. 화면의 모든 비밀값 요청 훅이 같은 scope를 공유해 epoch·요청 번호·
 * 대기 상태를 함께 쓴다.
 */
export function useSecretScope<T>(): SecretScope<T> {
  const [revealed, setRevealed] = useState<RevealedSecret<T> | null>(null);
  const [busy, setBusy] = useState(false);
  const [lifecycle] = useState(() => new SecretLifecycle<T>(setRevealed, setBusy));
  const queryClient = useQueryClient();

  // ⑨ 로그아웃 — 세션 쿼리가 지워지거나 쿼리 캐시가 통째로 비면(`queryClient.clear()`) 세션이 끝난 것으로 본다.
  // 캐시 이벤트는 clear() 안에서 동기로 오므로, 로그아웃 뒤 경로 이동 전에 이미 상태가 지워진다.
  useEffect(() => {
    const cache = queryClient.getQueryCache();
    return cache.subscribe((event) => {
      if (event.type !== "removed") return;
      if (isSessionKey(event.query.queryKey) || cache.getAll().length === 0) {
        lifecycle.end("session-ended");
      }
    });
  }, [queryClient, lifecycle]);

  // ④·⑤ 경로 이탈(언마운트) — epoch를 올려 늦은 응답을 버린다.
  useEffect(() => () => lifecycle.end("unmounted"), [lifecycle]);

  const close = useCallback(() => lifecycle.end("closed"), [lifecycle]);

  return { revealed, busy, close, lifecycle };
}

export interface SecretMutationOptions<TVars, TResponse, T> {
  /** 네트워크 요청 — 응답에 비밀값이 들어 있다. */
  request: (vars: TVars) => Promise<TResponse>;
  /** 응답 → 결과 다이얼로그 값(비밀값 포함). `mutationFn` 안에서만 부른다. */
  reveal: (response: TResponse, vars: TVars) => T;
  /** 성공 뒤 할 일(비밀값 없이) — 목록 무효화 등. 화면을 떠난 뒤에도 실행된다. */
  onSuccess?: (vars: TVars) => void;
  /** 화면을 떠났거나 로그아웃한 뒤 도착한 성공 응답의 안내(⑤ — 비밀값 없음). */
  lostNotice: string;
  /** 실패 토스트의 동작 — `notifyError(failureAction, error)` → 「{failureAction}하지 못했습니다」. */
  failureAction: string;
}

/** 비밀값을 뺀 mutation 결과 — `Mutation.state.data`에 남는 값은 이것뿐이다. */
export interface SecretOutcome {
  ok: true;
  delivery: SecretDelivery;
}

/** mutation 변수 — 대상 식별자와 요청 표(비밀값 없음). */
export interface SecretVariables<TVars> {
  vars: TVars;
  ticket: SecretTicket;
}

export interface SecretStartCallbacks {
  /** 화면이 마운트된 동안의 성공 후속 처리(예: 입력 다이얼로그 닫기). 비밀값을 받지 않는다. */
  onSuccess?: () => void;
}

export interface SecretMutation<TVars> {
  /** 요청을 시작한다. 이 훅의 요청이 이미 대기 중이면 시작하지 않고 `false`(단일 in-flight — 이중 클릭 방지). */
  start: (vars: TVars, callbacks?: SecretStartCallbacks) => boolean;
  /** 이 훅의 요청이 대기 중인가. */
  pending: boolean;
}

/** 비밀값을 돌려주는 요청 하나 — {@link useSecretScope}의 scope에 붙는다. */
export function useSecretMutation<TVars, TResponse, T>(
  scope: SecretScope<T>,
  options: SecretMutationOptions<TVars, TResponse, T>,
): SecretMutation<TVars> {
  const { lifecycle } = scope;
  const runningRef = useRef(false);

  const mutation = useMutation<SecretOutcome, unknown, SecretVariables<TVars>>({
    gcTime: 0,
    mutationFn: async ({ vars, ticket }) => {
      try {
        const response = await options.request(vars);
        // 비밀값은 여기서 다이얼로그 상태로만 넘어가고, 반환값에는 처리 결과만 남는다(①).
        return { ok: true, delivery: lifecycle.deliver(ticket, options.reveal(response, vars)) };
      } finally {
        runningRef.current = false;
        lifecycle.settle();
      }
    },
    onSuccess: ({ delivery }, { vars }) => {
      options.onSuccess?.(vars);
      if (delivery === "lost") toast.warning(options.lostNotice, { duration: ERROR_DURATION_MS });
    },
    onError: (error, { ticket }) => {
      // 로그아웃 뒤의 실패(401 등)는 알리지 않는다 — 이미 세션이 끝났다.
      if (lifecycle.endReason(ticket) === "session-ended") return;
      notifyError(options.failureAction, error);
    },
  });

  const { mutate } = mutation;
  const start = useCallback(
    (vars: TVars, callbacks?: SecretStartCallbacks): boolean => {
      if (runningRef.current) return false;
      runningRef.current = true;
      mutate(
        { vars, ticket: lifecycle.begin() },
        callbacks?.onSuccess ? { onSuccess: () => callbacks.onSuccess?.() } : undefined,
      );
      return true;
    },
    [mutate, lifecycle],
  );

  return { start, pending: mutation.isPending };
}
