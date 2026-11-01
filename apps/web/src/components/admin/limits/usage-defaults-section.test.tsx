import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { UsageDefaultsSection } from "@/components/admin/limits/usage-defaults-section";
import { ApiError } from "@/lib/api-client";
import type { UsageDefaults } from "@/lib/admin-types";
import { APP_QUERY_DEFAULTS } from "@/lib/query-defaults";
import { qk } from "@/lib/query-keys";

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

const PATH = "/api/v1/admin/usage-defaults";
const SAVED: UsageDefaults = { daily_tokens: null, daily_agent_runs: 5 };

function renderSection(data: UsageDefaults = SAVED) {
  api.get.mockImplementation((p: string) =>
    p === PATH ? Promise.resolve(data) : Promise.reject(new Error(`unexpected GET ${p}`)),
  );
  const queryClient = new QueryClient({
    defaultOptions: { queries: { ...APP_QUERY_DEFAULTS, retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <UsageDefaultsSection />
    </QueryClientProvider>,
  );
  return { queryClient };
}

const section = () => screen.getByRole("region", { name: "일일 한도 기본값" });
const saveButton = () => within(section()).getByRole("button", { name: "기본값 저장" });

afterEach(() => {
  vi.resetAllMocks();
});

describe("UsageDefaultsSection — 일일 한도 기본값(코어 #387, #228 AC-12)", () => {
  it("저장된 기본값을 보이고, 기본값이 사용자별 한도 없는 사용자에게 적용된다는 동작 변화를 알린다", async () => {
    renderSection();
    expect(await screen.findByLabelText("에이전트 실행/일")).toHaveValue(5);
    expect(screen.getByLabelText("LLM 토큰/일")).toHaveValue(null);
    expect(screen.getByLabelText("LLM 토큰/일")).toHaveAttribute("placeholder", "무제한");
    expect(screen.getByLabelText("LLM 토큰/일")).toHaveAccessibleDescription(
      expect.stringContaining("범위 0~2,147,483,647"),
    );
    const notice = screen.getByTestId("usage-defaults-notice");
    expect(notice).toHaveTextContent("기본값을 두면 사용자별 한도가 없는 사용자도 이 한도를 받습니다");
    expect(notice).toHaveTextContent("지금까지 이 사용자들은 무제한이었습니다");
    expect(saveButton()).toBeDisabled();
  });

  it("저장은 바꾼 필드만 보내고, 사용자별 한도 조회(적용값)를 무효화한다", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderSection();
    queryClient.setQueryData(qk.adminUserLimits.user("u1"), { daily_tokens: null });
    api.put.mockResolvedValue({ ...SAVED, daily_tokens: 1000 });

    await user.type(await screen.findByLabelText("LLM 토큰/일"), "1000");
    await user.click(saveButton());

    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
    expect(api.put).toHaveBeenCalledWith(PATH, { daily_tokens: 1000 });
    expect(toast.success).toHaveBeenCalledWith("일일 한도 기본값을 저장했습니다", expect.anything());
    expect(queryClient.getQueryState(qk.adminUserLimits.user("u1"))?.isInvalidated).toBe(true);
    await waitFor(() => expect(saveButton()).toBeDisabled());
    expect(screen.getByLabelText("LLM 토큰/일")).toHaveValue(1000);
  });

  it("저장 중에 더 고친 필드는 저장 응답이 덮지 않는다", async () => {
    const user = userEvent.setup();
    renderSection();
    let resolve!: (saved: UsageDefaults) => void;
    api.put.mockReturnValue(new Promise<UsageDefaults>((r) => (resolve = r)));
    const tokens = await screen.findByLabelText("LLM 토큰/일");
    await user.type(tokens, "1000");
    await user.click(saveButton());
    await waitFor(() => expect(api.put).toHaveBeenCalledWith(PATH, { daily_tokens: 1000 }));

    const runs = screen.getByLabelText("에이전트 실행/일");
    await user.clear(runs);
    await user.type(runs, "30");
    await act(async () => resolve({ ...SAVED, daily_tokens: 1000 }));

    await waitFor(() => expect(toast.success).toHaveBeenCalled());
    expect(tokens).toHaveValue(1000);
    expect(runs).toHaveValue(30);
    expect(saveButton()).toBeEnabled();
  });

  it("「지우기」 뒤 저장하면 그 필드에 null을 보내 기본값을 지운다(무제한)", async () => {
    const user = userEvent.setup();
    renderSection();
    api.put.mockResolvedValue({ ...SAVED, daily_agent_runs: null });
    await screen.findByLabelText("에이전트 실행/일");

    await user.click(screen.getByRole("button", { name: "에이전트 실행/일 기본값 지우기" }));
    await user.click(saveButton());

    await waitFor(() => expect(api.put).toHaveBeenCalledWith(PATH, { daily_agent_runs: null }));
    await waitFor(() => expect(screen.getByLabelText("에이전트 실행/일")).toHaveValue(null));
  });

  it("0은 보낼 수 있다(사용을 막는 값), 음수는 보내지 않는다", async () => {
    const user = userEvent.setup();
    renderSection();
    api.put.mockResolvedValue({ ...SAVED, daily_tokens: 0 });
    const input = await screen.findByLabelText("LLM 토큰/일");

    await user.type(input, "-1");
    await user.click(saveButton());
    expect(api.put).not.toHaveBeenCalled();

    await user.clear(input);
    await user.type(input, "0");
    await user.click(saveButton());
    await waitFor(() => expect(api.put).toHaveBeenCalledWith(PATH, { daily_tokens: 0 }));
  });

  it("422는 서버가 준 범위로 항목별 문구를 보이고 오류 토스트를 띄우지 않는다", async () => {
    const user = userEvent.setup();
    renderSection();
    api.put.mockRejectedValue(
      new ApiError(422, "[]", {
        detail: [
          { type: "less_than_equal", loc: ["body", "daily_tokens"], msg: "Input should be…", ctx: { le: 1_000_000 } },
        ],
      }),
    );
    await user.type(await screen.findByLabelText("LLM 토큰/일"), "2000000");
    await user.click(saveButton());

    expect(await within(section()).findByRole("alert")).toHaveTextContent("LLM 토큰/일: 1,000,000 이하여야 합니다");
    expect(toast.error).not.toHaveBeenCalled();
  });
});
