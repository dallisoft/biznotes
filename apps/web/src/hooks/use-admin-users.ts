"use client";

import type { components } from "@biznotes/shared-types";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { SecretRevealContent } from "@/components/ui/secret-reveal-dialog";
import { api, type ApiError } from "@/lib/api-client";
import type { AdminUser } from "@/lib/admin-types";
import { FIRST_LOGIN_CHANGE_NOTICE } from "@/lib/password-policy";
import { type SecretScope, useSecretMutation } from "@/lib/secret-mutation";

type PasswordResetOut = components["schemas"]["PasswordResetOut"];

export const ADMIN_USERS_KEY = ["admin-users"] as const;

/**
 * 자기 계정 비활성화 차단 사유(plan §8.4). 서버 `PATCH /admin/users/{id}`도 코어 v0.15.1(#219)부터 같은
 * 문구의 422로 거부한다 — UI 차단은 요청 전에 막는 안전 측으로 유지한다.
 */
export const SELF_DEACTIVATE_REASON = "자기 계정은 비활성화할 수 없습니다";
/** 다른 비밀번호 초기화가 대기 중일 때 「비밀번호 초기화…」 항목의 사유(단일 in-flight — §6.5 ⑧). */
export const RESET_BUSY_REASON = "진행 중인 비밀번호 초기화가 끝나면 다시 시도할 수 있습니다";

export function useAdminUsers() {
  return useQuery<AdminUser[], ApiError>({
    queryKey: ADMIN_USERS_KEY,
    queryFn: () => api.get<AdminUser[]>("/api/v1/admin/users"),
  });
}

/** 계정 활성·비활성(`PATCH /admin/users/{id}`). 비활성화는 화면이 확인을 받은 뒤에만 부른다(D-10). */
export function useSetUserActive() {
  const queryClient = useQueryClient();
  return useMutation<AdminUser, unknown, { id: string; is_active: boolean }>({
    mutationFn: ({ id, is_active }) =>
      api.patch<AdminUser>(`/api/v1/admin/users/${id}`, { is_active }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ADMIN_USERS_KEY }),
  });
}

/** 비밀번호 초기화 대상 — 식별자와 표시용 정보만(비밀값 없음). mutation `variables`에 그대로 남는다. */
export interface PasswordResetTarget {
  id: string;
  email: string;
  /** 강제 변경을 지원하는 API의 행인가(`must_change_password` 키 존재 — AC-3.4) */
  forcedChange: boolean;
}

export const PASSWORD_RESET_LOST_NOTICE =
  "비밀번호가 초기화되었지만 화면을 떠나 표시하지 못했습니다 — 다시 초기화하세요";

/**
 * 비밀번호 초기화(`POST /admin/users/{id}/password-reset`) — 임시 비밀번호는 D-22 규칙으로 결과 다이얼로그에만 간다
 * (토스트·mutation 반환값·캐시에 없음). 요청 경로와 강제 변경 안내 조건은 그대로다(§8.4).
 */
export function usePasswordResetSecret(scope: SecretScope<SecretRevealContent>) {
  const queryClient = useQueryClient();
  return useSecretMutation<PasswordResetTarget, PasswordResetOut, SecretRevealContent>(scope, {
    request: (target) =>
      api.post<PasswordResetOut>(`/api/v1/admin/users/${target.id}/password-reset`),
    reveal: (response, target) => ({
      title: "임시 비밀번호를 발급했습니다",
      description: `‘${target.email}’ 계정의 임시 비밀번호입니다.`,
      fields: [
        {
          label: "임시 비밀번호",
          value: response.temporary_password,
          secret: true,
          testId: "temporary-password",
        },
      ],
      notice: target.forcedChange
        ? `${FIRST_LOGIN_CHANGE_NOTICE} — 이 비밀번호로 로그인하면 바로 새 비밀번호로 바꾸게 됩니다.`
        : undefined,
    }),
    // 강제 변경 표시(「변경 필요」 배지)가 바뀌므로 목록을 다시 읽는다.
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ADMIN_USERS_KEY }),
    lostNotice: PASSWORD_RESET_LOST_NOTICE,
    failureAction: "비밀번호를 초기화",
  });
}
