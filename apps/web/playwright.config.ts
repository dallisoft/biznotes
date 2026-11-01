import { defineConfig } from "@playwright/test";

// 실행 전제: docker compose 스택 기동 (web :23000, api :28000) + 시드 계정
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  workers: 1, // 상태 공유(시드 계정) 충돌 방지
  retries: 1,
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:23000",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "setup", testMatch: /auth\.setup\.ts/ },
    {
      name: "chromium",
      testIgnore: /auth\.setup\.ts/,
      dependencies: ["setup"],
      // 로그인 1회 재사용 — 로그인 레이트 리밋(10/분) 회피
      use: { storageState: "playwright/.auth/admin.json" },
    },
  ],
  reporter: [["list"]],
});
