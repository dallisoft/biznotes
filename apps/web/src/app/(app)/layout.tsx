"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect } from "react";
import { AuthErrorScreen } from "@/components/auth-error";
import { AppShell } from "@/components/shell/app-shell";
import { Skeleton } from "@/components/ui/skeleton";
import { useMe } from "@/hooks/use-auth";
import { changePasswordHref } from "@/lib/safe-redirect";

// 인증 가드 + 글로벌 앱 셸 — (app) 그룹의 모든 페이지에 적용
export default function AppLayout({ children }: { children: React.ReactNode }) {
  const me = useMe();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  // 관리자가 만들거나 재설정한 계정 — 앱 셸을 렌더하지 않고 변경 화면으로 보낸다(AC-3.2).
  // 구 API 응답에는 키가 없어 falsy다.
  const mustChangePassword = me.data?.must_change_password === true;

  useEffect(() => {
    // 로그인 성공 후 원래 있던 페이지로 복귀 — phase-22-design.md §1.2
    const target = `${pathname}${searchParams.size ? `?${searchParams.toString()}` : ""}`;
    if (mustChangePassword) {
      router.replace(changePasswordHref(target));
      return;
    }
    if (!me.isError || me.error.status !== 401) return;
    const redirect = target === "/" ? "" : `?redirect=${encodeURIComponent(target)}`;
    router.replace(`/login${redirect}`);
  }, [mustChangePassword, me.isError, me.error, router, pathname, searchParams]);

  // 401 외 에러(API 다운 등)는 리다이렉트되지 않으므로 재시도 UI 노출
  if (me.isError && me.error.status !== 401) {
    return <AuthErrorScreen message={me.error.message} onRetry={() => me.refetch()} />;
  }

  if (me.isPending || me.isError || mustChangePassword) {
    return (
      <div className="flex h-screen">
        <div className="w-14 shrink-0 border-r bg-sidebar" />
        <main className="flex-1 p-8">
          <Skeleton className="h-10 w-48" />
        </main>
      </div>
    );
  }

  return <AppShell me={me.data}>{children}</AppShell>;
}
