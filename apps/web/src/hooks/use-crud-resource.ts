"use client";

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type ApiError } from "@/lib/api-client";
import { notifyError } from "@/lib/notify";

/**
 * admin 목록 CRUD의 보일러플레이트 흡수 (R4-2 §4).
 *
 * 페이지마다 반복되던 `mutationFn → onSuccess: toast + invalidateQueries → onError: toast`
 * 3단 세트를 한 곳에 모은다. **테이블·페이지네이션은 건드리지 않는다** — `ui/table.tsx`와
 * `ui/table-pagination.tsx`가 이미 공용이고, 그 위에 제네릭 래퍼를 얹으면 커스텀 셀 때문에
 * 곧 새는 추상화가 된다(HCI-R4-3 승인: `<AdminTable>` 폐기).
 *
 * 성공/실패 토스트는 뮤테이션마다 다르므로 문구를 인자로 받는다. 문구를 생략하면
 * 성공 토스트를 띄우지 않는다(무음 토글처럼 UI가 이미 상태를 보여주는 경우).
 *
 * 기본 실패 토스트는 `lib/notify`의 규칙을 따른다(plan §6.5 「토스트」): 제목은
 * 「추가/변경/삭제하지 못했습니다」, 설명은 알려진 코드·상태면 한국어 문구, 그 밖은
 * 「잠시 후 다시 시도해 주세요」 + 서버 메시지(둘째 줄)다. 서버 원문을 제목으로 쓰지 않는다.
 *
 * ## ⚠️ `mutate(vars, { onError })`와 함께 쓰지 말 것
 *
 * react-query v5는 훅 레벨 `onError`와 `mutate` 레벨 `onError`를 **둘 다** 호출한다
 * (`mutation.js:159`가 전자, `mutationObserver.js:109`가 후자 — 대체가 아니라 누적).
 * 따라서 호출부에서 `mutate(v, { onError: toast.error })`를 넘기면 **에러 토스트가 두 번**
 * 뜬다. 호출부가 에러를 직접 처리해야 하면 `toastErrors: false`로 기본 핸들러를 끈다.
 */

export interface CrudMessages {
  /** 생략 시 성공 토스트 없음. */
  create?: string;
  update?: string;
  remove?: string;
}

export interface CrudResource<T, TCreate, TUpdate> {
  list: UseQueryResult<T[], ApiError>;
  create: UseMutationResult<T, ApiError, TCreate>;
  /** `id`를 제외한 나머지 필드가 PATCH 본문이 된다. */
  update: UseMutationResult<T, ApiError, TUpdate & { id: string }>;
  remove: UseMutationResult<void, ApiError, string>;
  /** 목록을 수동 무효화 — 페이지 고유 뮤테이션(헬스체크 등)의 onSuccess에서 쓴다. */
  invalidate: () => void;
}

export function useCrudResource<
  T,
  TCreate = Partial<T>,
  TUpdate = Partial<T>,
>({
  queryKey,
  basePath,
  messages = {},
  /**
   * 이 리소스가 바뀌면 함께 무효화할 키들. 예: `admin-agents` 변경 시
   * 사용자향 `agents` 목록도 새로 고쳐야 한다.
   */
  alsoInvalidate = [],
  /** 호출부가 `mutate`에 자체 `onError`를 넘긴다면 `false`로 끈다(위 주석). */
  toastErrors = true,
}: {
  queryKey: readonly unknown[];
  basePath: string;
  messages?: CrudMessages;
  alsoInvalidate?: readonly (readonly unknown[])[];
  toastErrors?: boolean;
}): CrudResource<T, TCreate, TUpdate> {
  const qc = useQueryClient();

  const invalidate = () => {
    qc.invalidateQueries({ queryKey });
    for (const k of alsoInvalidate) qc.invalidateQueries({ queryKey: k });
  };

  /** 모든 뮤테이션이 공유하는 성공/실패 처리. `failedAction`은 실패 제목의 동작(「추가」 등). */
  const settle = (successMessage: string | undefined, failedAction: string) => ({
    onSuccess: () => {
      invalidate();
      if (successMessage) toast.success(successMessage);
    },
    ...(toastErrors ? { onError: (e: ApiError) => notifyError(failedAction, e) } : {}),
  });

  const list = useQuery<T[], ApiError>({
    queryKey,
    queryFn: () => api.get<T[]>(basePath),
  });

  const create = useMutation<T, ApiError, TCreate>({
    mutationFn: (body) => api.post<T>(basePath, body),
    ...settle(messages.create, "추가"),
  });

  const update = useMutation<T, ApiError, TUpdate & { id: string }>({
    mutationFn: ({ id, ...body }) => api.patch<T>(`${basePath}/${id}`, body),
    ...settle(messages.update, "변경"),
  });

  const remove = useMutation<void, ApiError, string>({
    mutationFn: (id) => api.delete<void>(`${basePath}/${id}`),
    ...settle(messages.remove, "삭제"),
  });

  return { list, create, update, remove, invalidate };
}
