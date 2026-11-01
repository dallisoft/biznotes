// 서버 컴포넌트: 컨테이너 내부 네트워크 주소 우선, 없으면 공개 주소
export const API_INTERNAL_URL =
  process.env.API_INTERNAL_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://localhost:28000";

// 클라이언트 컴포넌트: 브라우저에서 접근 가능한 공개 주소
export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:28000";
