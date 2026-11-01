import type { NextConfig } from "next";

// CSP는 nonce 기반 enforce로 전환 (OPS_CATCHUP_PLAN §3c) — src/middleware.ts가 요청마다
// nonce를 발급해 Content-Security-Policy 헤더를 설정한다. 정적 보안 헤더만 여기서 일괄 적용.
const nextConfig: NextConfig = {
  output: "standalone", // Docker multi-stage 배포용
  // 로컬 dev에서 NEXT_PUBLIC_API_BASE_URL="" 로 띄우면 same-origin 프록시로 API 호출
  // (브라우저 CORS 우회 — 프로덕션 standalone 빌드에는 영향 없음)
  async rewrites() {
    if (process.env.NODE_ENV !== "development") return [];
    const target = process.env.DEV_API_PROXY_TARGET ?? "http://localhost:28000";
    return [{ source: "/api/v1/:path*", destination: `${target}/api/v1/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
};

export default nextConfig;
