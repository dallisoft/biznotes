"use client";

import { Bot, FileSearch, NotebookText, Zap } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useLogin } from "@/hooks/use-auth";
import { displayYear } from "@/lib/format";
import { loginErrorMessage } from "@/lib/login-error";
import { ERROR_DURATION_MS, failureTitle } from "@/lib/notify";

const FEATURES = [
  { icon: FileSearch, text: "문서·웹·텍스트 소스를 모아 근거 있는 답변을 받으세요" },
  { icon: NotebookText, text: "AI 답변을 메모로 저장하고 다시 소스로 활용하세요" },
  { icon: Bot, text: "보고서·브리핑·퀴즈를 에이전트가 대신 만들어 드립니다" },
];

export default function LoginPage() {
  const login = useLogin();
  const searchParams = useSearchParams();
  const redirect = searchParams.get("redirect");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    login.mutate(
      { email, password, redirect },
      {
        // 서버 메시지·422 검증 본문(입력값 포함)은 보이지 않는다 — 상태별 고정 문구(#93)
        onError: (err) =>
          toast.error(failureTitle("로그인"), {
            description: loginErrorMessage(err),
            duration: ERROR_DURATION_MS,
          }),
      },
    );
  }

  return (
    <main className="flex min-h-screen">
      {/* 브랜드 패널 (lg 이상) */}
      <section className="relative hidden flex-1 flex-col justify-between overflow-hidden bg-primary p-10 text-primary-foreground lg:flex">
        <div className="pointer-events-none absolute -top-32 -right-32 size-96 rounded-full bg-white/10 blur-3xl" />
        <div className="pointer-events-none absolute -bottom-40 -left-24 size-96 rounded-full bg-black/15 blur-3xl" />

        <div className="relative flex items-center gap-2.5">
          <div className="flex size-9 items-center justify-center rounded-xl bg-white/15">
            <Zap className="size-5" />
          </div>
          <span className="text-lg font-bold tracking-tight">biznotes</span>
        </div>

        <div className="relative max-w-md">
          <h2 className="text-3xl leading-snug font-bold">
            소스 기반 AI 노트북과
            <br />
            에이전트 실행 플랫폼
          </h2>
          <ul className="mt-8 flex flex-col gap-4">
            {FEATURES.map((f) => (
              <li key={f.text} className="flex items-start gap-3 text-sm/6 text-primary-foreground/90">
                <f.icon className="mt-0.5 size-4.5 shrink-0" />
                {f.text}
              </li>
            ))}
          </ul>
        </div>

        {/* 12px 글자 — primary 면 위 4.5:1 이상(/60은 3.0~3.2:1로 미달, /90은 인디고·틸·뉴트럴 모두 4.8:1 이상) */}
        <p className="relative text-xs text-primary-foreground/90">
          © {displayYear()} biznotes — RAG 기반 노트북 서비스
        </p>
      </section>

      {/* 로그인 폼 */}
      <section className="flex flex-1 items-center justify-center bg-background p-6">
        <div className="w-full max-w-sm">
          <div className="mb-8 flex flex-col items-center gap-2 text-center lg:items-start lg:text-left">
            <div className="mb-1 flex size-10 items-center justify-center rounded-xl bg-primary text-primary-foreground lg:hidden">
              <Zap className="size-5" />
            </div>
            <h1 className="text-2xl font-bold tracking-tight">로그인</h1>
            <p className="text-sm text-muted-foreground">
              biznotes 계정으로 계속하세요
            </p>
          </div>

          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="email">이메일</Label>
              <Input
                id="email"
                type="email"
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                autoFocus
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="password">비밀번호</Label>
              <Input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
            </div>
            <Button type="submit" disabled={login.isPending} className="mt-2">
              {login.isPending ? "로그인 중..." : "로그인"}
            </Button>
          </form>
        </div>
      </section>
    </main>
  );
}
