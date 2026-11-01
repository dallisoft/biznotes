"use client";

import { useQueryClient } from "@tanstack/react-query";
import { KeyRound, LogOut } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useChangePassword, useLogout } from "@/hooks/use-auth";
import { api, ApiError } from "@/lib/api-client";
import { notifySuccess } from "@/lib/notify";
import {
  checkPasswordChange,
  describePasswordChangeError,
  PASSWORD_CHANGE_UNSUPPORTED_MESSAGE,
  PASSWORD_ISSUE_MESSAGE,
  PASSWORD_MAX_BYTES,
  PASSWORD_MIN_LENGTH,
  PASSWORD_SUPPORT_PROBE_FAILED_MESSAGE,
} from "@/lib/password-policy";
import { changePasswordHref, safeInternalPath } from "@/lib/safe-redirect";
import type { User } from "@/lib/types";

/**
 * 변경 성공 안내(BL-172, AC-4.9) — 코어는 변경 성공 시 그 전에 발급된 모든 토큰(다른 기기 포함)을 무효화한다(`pwv`).
 * 강제 변경·본인 변경 모두 같은 서버 동작이므로 둘 다 알린다. 판정·리다이렉트는 바꾸지 않는다.
 */
const PASSWORD_CHANGED_MESSAGE = "비밀번호를 변경했습니다. 다른 기기에서는 다시 로그인해야 합니다";

/**
 * 비밀번호 변경 화면(앱 셸 밖, `/login`과 같은 레벨 — HCI-11 ①).
 *
 * 능력 판정(AC-3.4, D-37′): 진입마다 React Query 캐시와 독립된 `GET /auth/me` 요청 하나로
 * 판정한다. 응답 객체에 `must_change_password` 키가 있어야 지원이다(구 API에는 없다). 언마운트·
 * 재진입·다시 시도 때 이전 요청을 `abort`하고, 진입 토큰으로 응답·거부를 그 진입에만 묶는다
 * (abort 뒤 도착분·Strict Mode 이중 effect도 버린다). 상태는 셋이다:
 *  - 지원: 200 + 키 있음 → 제출 가능
 *  - 미지원: 200 + 키 없음, 프로브 404·405, 제출 404·405 → 안내만(POST를 부르지 않는다)
 *  - 판정 실패: 네트워크 오류·5xx 등 → 「확인하지 못했습니다」 + 다시 시도, 제출 비활성
 * 취소(AbortError·교체된 진입의 거부)는 어떤 상태로도 승격하지 않는다. 401은 로그인으로 보낸다.
 */
type Probe =
  | { state: "checking" }
  | { state: "supported"; forced: boolean }
  | { state: "unsupported" }
  | { state: "failed" };

function isAbortError(err: unknown): boolean {
  return typeof err === "object" && err !== null && (err as { name?: unknown }).name === "AbortError";
}

export default function ChangePasswordPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const change = useChangePassword();
  const logout = useLogout();

  const target = safeInternalPath(searchParams.get("redirect"));
  const loginHref = `/login?redirect=${encodeURIComponent(changePasswordHref(target))}`;

  const [probe, setProbe] = useState<Probe>({ state: "checking" });
  const [attempt, setAttempt] = useState(0);
  const entryRef = useRef(0);

  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    const entry = ++entryRef.current;
    const controller = new AbortController();
    setProbe({ state: "checking" });
    api.get<User>("/api/v1/auth/me", { signal: controller.signal }).then(
      (me) => {
        if (entryRef.current !== entry) return;
        if (typeof me !== "object" || me === null) {
          setProbe({ state: "failed" });
          return;
        }
        // 캐시 갱신은 허용하되, 제출 가능 여부의 근거는 이 진입의 응답뿐이다
        queryClient.setQueryData(["me"], me);
        setProbe(
          "must_change_password" in me
            ? { state: "supported", forced: me.must_change_password === true }
            : { state: "unsupported" },
        );
      },
      (err: unknown) => {
        if (entryRef.current !== entry || isAbortError(err)) return;
        if (err instanceof ApiError && err.status === 401) {
          router.replace(loginHref);
          return;
        }
        if (err instanceof ApiError && (err.status === 404 || err.status === 405)) {
          setProbe({ state: "unsupported" });
          return;
        }
        setProbe({ state: "failed" });
      },
    );
    return () => {
      // 이 진입은 끝났다 — 늦게 도착한 응답·거부를 버린다
      entryRef.current += 1;
      controller.abort();
    };
    // loginHref·router는 진입 판정의 입력이 아니다(진입마다 1회 — attempt만 재판정 트리거)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attempt]);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (probe.state !== "supported" || change.isPending) return;
    const issue = checkPasswordChange({ current, next, confirm });
    if (issue) {
      setFormError(PASSWORD_ISSUE_MESSAGE[issue]);
      return;
    }
    setFormError(null);
    change.mutate(
      { current_password: current, new_password: next },
      {
        onSuccess: () => {
          // 토스트는 루트 Providers의 Toaster가 그리므로 셸 밖 → 셸 안 이동 뒤에도 남는다
          notifySuccess(PASSWORD_CHANGED_MESSAGE);
          router.replace(target);
        },
        onError: (err) => {
          const failure = describePasswordChangeError(err);
          if (failure.kind === "login") router.replace(loginHref);
          else if (failure.kind === "unsupported") setProbe({ state: "unsupported" });
          else setFormError(failure.message);
        },
      },
    );
  }

  const supported = probe.state === "supported";
  const forced = supported && probe.forced;

  return (
    <main className="flex min-h-screen items-center justify-center bg-background p-6">
      <div className="w-full max-w-sm" data-testid="change-password-page">
        <div className="mb-6 flex flex-col gap-2">
          <div className="flex size-10 items-center justify-center rounded-xl bg-primary text-primary-foreground">
            <KeyRound className="size-5" />
          </div>
          <h1 className="text-2xl font-bold tracking-tight">비밀번호 변경</h1>
          {forced && (
            <p className="text-sm text-muted-foreground" data-testid="change-password-forced">
              관리자가 정한 임시 비밀번호로 로그인했습니다. 계속하려면 새 비밀번호로 바꾸세요.
            </p>
          )}
        </div>

        {probe.state === "unsupported" ? (
          <div className="flex flex-col gap-4" data-testid="change-password-unsupported">
            <p className="rounded-md border bg-muted/40 p-3 text-sm">{PASSWORD_CHANGE_UNSUPPORTED_MESSAGE}</p>
            <Button variant="outline" render={<Link href={target} />}>
              돌아가기
            </Button>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="flex flex-col gap-4" noValidate>
            {probe.state === "checking" && (
              <p className="text-sm text-muted-foreground" role="status">
                서버 지원 여부를 확인하는 중…
              </p>
            )}
            {probe.state === "failed" && (
              <div
                className="flex items-center justify-between gap-3 rounded-md bg-destructive/10 p-2.5 text-sm text-destructive-text"
                role="alert"
              >
                <span>{PASSWORD_SUPPORT_PROBE_FAILED_MESSAGE}</span>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => setAttempt((n) => n + 1)}
                >
                  다시 시도
                </Button>
              </div>
            )}
            <div className="flex flex-col gap-2">
              <Label htmlFor="current-password">현재 비밀번호</Label>
              <Input
                id="current-password"
                type="password"
                autoComplete="current-password"
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
                autoFocus
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="new-password">새 비밀번호</Label>
              <Input
                id="new-password"
                type="password"
                autoComplete="new-password"
                value={next}
                onChange={(e) => setNext(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                {PASSWORD_MIN_LENGTH}자 이상, UTF-8 {PASSWORD_MAX_BYTES}바이트 이하. 현재 비밀번호와
                달라야 합니다.
              </p>
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="confirm-password">새 비밀번호 확인</Label>
              <Input
                id="confirm-password"
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
              />
            </div>
            {formError && (
              <p
                className="rounded-md bg-destructive/10 p-2 text-sm text-destructive-text"
                role="alert"
                data-testid="change-password-error"
              >
                {formError}
              </p>
            )}
            <Button type="submit" disabled={!supported || change.isPending} className="mt-1">
              {change.isPending ? "변경 중..." : "비밀번호 변경"}
            </Button>
            {supported && !forced && (
              <Button variant="ghost" render={<Link href={target} />}>
                취소
              </Button>
            )}
          </form>
        )}

        <div className="mt-6 flex justify-end">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => logout.mutate()}
            disabled={logout.isPending}
          >
            <LogOut className="size-4" />
            로그아웃
          </Button>
        </div>
      </div>
    </main>
  );
}
