import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { headers } from "next/headers";
import Script from "next/script";
import { Providers } from "@/components/providers";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "biznotes",
  description: "RAG 기반 노트북 서비스 + 에이전트 실행 플랫폼",
  manifest: "/manifest.json",
};

// 페인트 전에 저장된 컬러 프리셋을 html[data-theme]에 적용 (FOUC 방지)
// 다크 모드 클래스는 next-themes가 동일한 방식으로 처리한다
const colorThemeScript = `(function(){try{var t=localStorage.getItem("color-theme");if(t==="teal"||t==="neutral"){document.documentElement.dataset.theme=t;}}catch(e){}})();`;

export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // CSP enforce(§3c): middleware가 발급한 요청별 nonce를 인라인 스크립트에 주입
  const nonce = (await headers()).get("x-nonce") ?? undefined;
  return (
    // 글꼴 변수 클래스는 <html>에 둔다 — globals.css의 html 글꼴 스택(--font-sans)이
    // var(--font-geist-sans)를 html에서 해석하므로 <body>에만 있으면 변수를 보지 못한다(D-08).
    <html
      lang="ko"
      className={`${geistSans.variable} ${geistMono.variable}`}
      suppressHydrationWarning
    >
      <body className="antialiased">
        <Script id="color-theme-init" strategy="beforeInteractive" nonce={nonce}>
          {colorThemeScript}
        </Script>
        <Providers nonce={nonce}>{children}</Providers>
      </body>
    </html>
  );
}
