"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { PageContainer } from "@/components/layout/page-container";
import { Skeleton } from "@/components/ui/skeleton";
import { useMe } from "@/hooks/use-auth";

// 인증(401)은 (app)/layout.tsx 셸이 처리 — 여기서는 admin 권한만 검사한다(plan §8.3 — 가드 무변경).
// 관리자가 아니면 children(관리 개요·관리 화면)을 렌더하지 않으므로 관리 API 요청도 0건이다.
// 랜드마크 `main`은 앱 셸이 하나만 둔다(§6.8-3) — 여기서는 스크롤 영역 + PageContainer(div)만 둔다.
export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const me = useMe();
  const router = useRouter();

  useEffect(() => {
    if (me.data && me.data.role !== "admin") router.replace("/");
  }, [me.data, router]);

  if (!me.data || me.data.role !== "admin") {
    return (
      <div className="p-8">
        <Skeleton className="h-10 w-48" />
      </div>
    );
  }

  return (
    <div data-slot="admin-scroll" className="h-full min-w-0 overflow-y-auto">
      <PageContainer width="wide">{children}</PageContainer>
    </div>
  );
}
