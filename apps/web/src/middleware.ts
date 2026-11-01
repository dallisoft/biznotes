import { type NextRequest, NextResponse } from "next/server";

// CSP enforce (OPS_CATCHUP_PLAN §3c) — report-only에서 전환.
// 요청마다 nonce를 발급해 인라인 스크립트(FOUC 방지 color-theme-init + Next 부트스트랩)에
// 적용하고 'strict-dynamic'으로 청크 로딩을 허용한다. 이로써 script-src의 'unsafe-inline'을 제거.
// nonce는 요청 헤더(x-nonce)로 전달 → layout.tsx가 읽어 <Script nonce>에 주입,
// Next는 CSP 헤더의 nonce를 감지해 자체 스크립트에도 자동 적용한다.
export function middleware(request: NextRequest) {
  const nonce = btoa(crypto.randomUUID());

  const apiOrigin = process.env.NEXT_PUBLIC_API_BASE_URL || "";
  const connectSrc = ["'self'", apiOrigin].filter(Boolean).join(" ");
  // dev: Next HMR/React Refresh가 eval 사용 → unsafe-eval 허용. prod 빌드에는 불필요.
  const isDev = process.env.NODE_ENV === "development";
  const scriptSrc = ["'self'", `'nonce-${nonce}'`, "'strict-dynamic'", isDev ? "'unsafe-eval'" : ""]
    .filter(Boolean)
    .join(" ");

  const csp = [
    "default-src 'self'",
    `script-src ${scriptSrc}`,
    // style-src: Tailwind/Next가 주입하는 인라인 스타일 때문에 unsafe-inline 유지(스크립트와 달리 위험 낮음)
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "media-src 'self' blob:",
    "font-src 'self' data:",
    `connect-src ${connectSrc}`,
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
  ].join("; ");

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  // Next가 nonce를 감지하도록 요청 헤더에도 CSP를 실어 보낸다
  requestHeaders.set("Content-Security-Policy", csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  // 정적 자산/이미지/파비콘과 dev API 프록시(/api/*)는 제외 — HTML 문서 응답에만 nonce CSP 적용
  matcher: [
    {
      source: "/((?!api|_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
