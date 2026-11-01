import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    globals: true,
    // 커버리지 ratchet 게이트 (리팩토링 계획 R0-1): `vitest run --coverage`에서만 적용.
    // thresholds는 "하향 금지" 베이스라인 — 미달 시 종료 코드 1. 상향은 자유,
    // 하향 조정은 인간 승인 필요.
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/**/*.test.{ts,tsx}", "src/**/*.d.ts"],
      reporter: ["text-summary", "json-summary"],
      // 실측 (2026-11-01, biznotes Phase 2b — 뺀 기능의 코드·테스트를 걷어낸 뒤 `make ci`):
      // statements 90.67 / branches 87.53 / functions 87.62 / lines 91.94
      // — 소수 첫째 자리 내림에서 0.1 여유로 다시 잠금(이전 하한 87.6 / 84.7 / 83.6 / 88.8).
      thresholds: {
        statements: 90.5,
        branches: 87.4,
        functions: 87.5,
        lines: 91.8,
      },
    },
  },
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
});
