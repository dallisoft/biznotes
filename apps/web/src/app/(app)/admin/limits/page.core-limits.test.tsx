import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { APP_QUERY_DEFAULTS } from "@/lib/query-defaults";
import AdminLimitsPage from "./page";

const { api, toast } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));

// API 경계만 mock — `api` 객체만 대체하고 ApiError 등은 실제 모듈을 쓴다.
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("sonner", () => ({ toast }));

// 「사용 제한」 화면의 코어 항목 섹션 배치(이슈 #228 B, 코어 v0.26.0 #386·#387·#388) — 「요청당 한도」 아래에 세 섹션이 있고,
// 저장 단위가 서로 독립이다(요청당 한도의 저장 막대와 묶이지 않는다). 섹션 동작은 `components/admin/limits/*.test.tsx`.

const MIB = 1024 * 1024;
const RESPONSES: Record<string, unknown> = {
  "/api/v1/admin/limits": { chat_token_budget: 100_000 },
  "/api/v1/admin/rate-limits": {
    enabled: true,
    cache_ttl_seconds: 30,
    min_value: 1,
    max_value: 100_000,
    items: [{ key: "rate_limit_chat", env_value: 30, override: null, effective: 30 }],
  },
  "/api/v1/admin/usage-defaults": {
    daily_tokens: null,
    daily_agent_runs: null,
  },
  "/api/v1/admin/source-upload": {
    max_upload_bytes: 50 * MIB,
    env_max_upload_bytes: 50 * MIB,
    override: null,
    hard_max_upload_bytes: 500 * MIB,
  },
  // 노트북당 소스 상한(코어 v0.30.0 #445, #281)
  "/api/v1/admin/notebook-source-limit": {
    max_sources_per_notebook: 500,
    env_max_sources_per_notebook: 500,
    override: null,
  },
};

function renderPage() {
  api.get.mockImplementation((p: string) =>
    p in RESPONSES ? Promise.resolve(RESPONSES[p]) : Promise.reject(new Error(`unexpected GET ${p}`)),
  );
  const queryClient = new QueryClient({
    defaultOptions: { queries: { ...APP_QUERY_DEFAULTS, retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AdminLimitsPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.resetAllMocks();
});

describe("AdminLimitsPage — 코어 항목 섹션(#228 AC-12)", () => {
  it("「요청당 한도」 아래에 분당 레이트 리밋·일일 한도 기본값·업로드 최대 크기·노트북당 소스 수를 차례로 둔다", async () => {
    renderPage();
    await screen.findByLabelText("채팅 질문 재정의");
    await screen.findByLabelText("LLM 토큰/일");
    await screen.findByLabelText("업로드 최대 크기 재정의");
    await screen.findByLabelText("노트북당 소스 수 재정의");
    expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual([
      "요청당 한도",
      "분당 레이트 리밋",
      "일일 한도 기본값",
      "업로드 최대 크기",
      "노트북당 소스 수",
    ]);
    expect(api.get.mock.calls.map((c) => c[0]).sort()).toEqual(Object.keys(RESPONSES).sort());
  });

  it("코어 섹션의 저장은 그 섹션 경로만 부르고 「요청당 한도」 저장 막대를 띄우지 않는다", async () => {
    const user = userEvent.setup();
    renderPage();
    api.put.mockResolvedValue({
      ...(RESPONSES["/api/v1/admin/rate-limits"] as object),
      items: [{ key: "rate_limit_chat", env_value: 30, override: 40, effective: 40 }],
    });
    await user.type(await screen.findByLabelText("채팅 질문 재정의"), "40");
    expect(screen.queryByRole("region", { name: "저장되지 않은 변경 사항" })).toBeNull();
    const rateSection = screen.getByRole("region", { name: "분당 레이트 리밋" });
    await user.click(within(rateSection).getByRole("button", { name: "레이트 리밋 저장" }));

    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
    expect(api.put).toHaveBeenCalledWith("/api/v1/admin/rate-limits", { rate_limit_chat: 40 });
  });
});
