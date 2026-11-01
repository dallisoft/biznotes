"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { usePathname, useRouter } from "next/navigation";
import { ThemeProvider } from "next-themes";
import { useEffect, useRef, useState } from "react";
import { ColorThemeProvider } from "@/components/theme/color-theme-provider";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { onPasswordChangeRequired } from "@/lib/api-client";
import { installObjectUrlCleanup } from "@/lib/object-url-cache";
import { APP_QUERY_DEFAULTS } from "@/lib/query-defaults";
import { CHANGE_PASSWORD_PATH, changePasswordHref } from "@/lib/safe-redirect";

/**
 * api-client의 403 `password_change_required` 통지를 구독해 변경 화면으로 보낸다(AC-3.1, D-33).
 *
 * 현재 경로(쿼리 포함)를 `redirect`로 싣는다. 같은 화면에서 403이 여러 번 와도 이동은 한 번이고
 * (화면이 바뀌면 다시 허용), 변경 화면 자신에서는 이동하지 않는다(루프 방지).
 */
function PasswordChangeRedirect() {
  const router = useRouter();
  const pathname = usePathname();
  const redirectedRef = useRef(false);

  useEffect(() => {
    redirectedRef.current = false;
  }, [pathname]);

  useEffect(
    () =>
      onPasswordChangeRequired(() => {
        const { pathname: current, search } = window.location;
        if (redirectedRef.current || current === CHANGE_PASSWORD_PATH) return;
        redirectedRef.current = true;
        router.replace(changePasswordHref(`${current}${search}`));
      }),
    [router],
  );

  return null;
}

export function Providers({
  children,
  nonce,
}: {
  children: React.ReactNode;
  nonce?: string;
}) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { ...APP_QUERY_DEFAULTS },
        },
      }),
  );
  // 캐시가 버리는 산출물 object URL을 해제한다(삽화·오디오)
  useEffect(() => installObjectUrlCleanup(queryClient), [queryClient]);

  return (
    // nonce: next-themes의 FOUC 방지 인라인 스크립트에 CSP nonce 적용 (§3c enforce 전환)
    <ThemeProvider
      attribute="class"
      defaultTheme="system"
      enableSystem
      disableTransitionOnChange
      nonce={nonce}
    >
      <ColorThemeProvider>
        <QueryClientProvider client={queryClient}>
          <PasswordChangeRedirect />
          <TooltipProvider>{children}</TooltipProvider>
          <Toaster position="top-center" richColors />
        </QueryClientProvider>
      </ColorThemeProvider>
    </ThemeProvider>
  );
}
