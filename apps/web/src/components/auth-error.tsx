"use client";

import { Button } from "@/components/ui/button";

/** 인증 확인이 401 외 사유(API 다운, 네트워크 단절 등)로 실패했을 때의 폴백 화면. */
export function AuthErrorScreen({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-4">
      <p className="text-muted-foreground">서버에 연결하지 못했습니다: {message}</p>
      <Button variant="outline" onClick={onRetry} data-testid="auth-retry">
        다시 시도
      </Button>
    </main>
  );
}
