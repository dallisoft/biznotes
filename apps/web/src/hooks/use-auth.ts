"use client";

import type { components } from "@biznotes/shared-types";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api-client";
import { changePasswordHref, safeInternalPath } from "@/lib/safe-redirect";
import type { User } from "@/lib/types";

type TokenResponse = components["schemas"]["TokenResponse"];
type PasswordChangeRequest = components["schemas"]["PasswordChangeRequest"];

export function useMe() {
  return useQuery<User, ApiError>({
    queryKey: ["me"],
    queryFn: () => api.get<User>("/api/v1/auth/me"),
    retry: false,
  });
}

export function useLogin() {
  const queryClient = useQueryClient();
  const router = useRouter();
  return useMutation({
    mutationFn: (body: { email: string; password: string; redirect?: string | null }) =>
      api.post<{ user: User }>("/api/v1/auth/login", {
        email: body.email,
        password: body.password,
      }),
    onSuccess: (data, variables) => {
      queryClient.setQueryData(["me"], data.user);
      // 오픈 리다이렉트 방지 — 앱 내부 경로만(AC-3.3, safeInternalPath)
      const target = safeInternalPath(variables.redirect ?? null);
      // 관리자가 만들거나 재설정한 계정은 먼저 비밀번호를 바꾼다(AC-3.2). 구 API 응답에는 키가
      // 없어 falsy다.
      router.push(data.user.must_change_password ? changePasswordHref(target) : target);
    },
  });
}

/**
 * 본인 비밀번호 변경(`POST /auth/password`, 코어 v0.11.0 #165). 성공하면 서버가 새 쿠키를
 * 발급하고 다른 기기의 토큰을 끊는다 — 응답의 사용자로 `["me"]` 캐시를 갱신한다.
 * 이동은 화면이 정한다(redirect 판정은 화면 몫).
 */
export function useChangePassword() {
  const queryClient = useQueryClient();
  return useMutation<TokenResponse, ApiError, PasswordChangeRequest>({
    mutationFn: (body) => api.post<TokenResponse>("/api/v1/auth/password", body),
    onSuccess: (data) => {
      queryClient.setQueryData(["me"], data.user);
    },
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  const router = useRouter();
  return useMutation({
    mutationFn: () => api.post<void>("/api/v1/auth/logout"),
    onSettled: () => {
      queryClient.clear();
      router.push("/login");
    },
  });
}
