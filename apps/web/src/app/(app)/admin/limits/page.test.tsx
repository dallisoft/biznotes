import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
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
// 코어 항목 섹션(레이트 리밋·일일 한도 기본값·업로드 최대 크기)은 `components/admin/limits/*.test.tsx`가 다룬다
vi.mock("@/components/admin/limits/core-limit-sections", () => ({ CoreLimitSections: () => null }));

const LIMITS_PATH = "/api/v1/admin/limits";

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { ...APP_QUERY_DEFAULTS, retry: false }, mutations: { retry: false } },
  });
  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <AdminLimitsPage />
      </QueryClientProvider>,
    ),
  };
}

const saveBar = () => screen.queryByRole("region", { name: "저장되지 않은 변경 사항" });

// 서버 기본값(`app/services/limits.py` `LIMIT_BOUNDS`)
const DEFAULTS = {
  chat_token_budget: 100_000,
  chat_cost_budget_usd: 0.5,
  chat_max_iters: 6,
  chat_max_tokens: 4096,
  chat_history_messages: 6,
  source_load_max_sources: 5,
  source_load_max_chars: 60_000,
  source_load_budget_ratio: 0.3,
  approval_timeout_minutes: 10,
};

function mockLimits(budget = 100_000) {
  api.get.mockImplementation((p: string) =>
    p === LIMITS_PATH
      ? Promise.resolve({ ...DEFAULTS, chat_token_budget: budget })
      : Promise.resolve([]),
  );
}

afterEach(() => {
  vi.resetAllMocks();
});

describe("AdminLimitsPage — 채팅 1회 토큰 한도", () => {
  it("적용 중인 값을 보여 주고 범위·단위를 안내한다", async () => {
    mockLimits(150_000);
    renderPage();
    expect(screen.getByRole("heading", { level: 1, name: "사용 제한" })).toBeInTheDocument();
    const input = await screen.findByLabelText("1회 토큰 한도");
    expect(input).toHaveValue(150_000);
    expect(input).toHaveAccessibleDescription(expect.stringContaining("범위 10,000~200,000"));
    const field = input.closest<HTMLElement>('[data-slot="form-field"]');
    expect(within(field!).getByText("토큰")).toBeInTheDocument();
    expect(saveBar()).toBeNull();
  });

  it("바꾸면 저장 막대가 보이고, 저장은 PUT 1회 뒤 막대를 닫는다", async () => {
    const user = userEvent.setup();
    mockLimits();
    api.put.mockImplementation((_path: string, body: unknown) => Promise.resolve(body));
    renderPage();
    const input = await screen.findByLabelText("1회 토큰 한도");

    await user.clear(input);
    await user.type(input, "180000");
    expect(saveBar()).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "변경 사항 저장" }));

    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
    expect(api.put).toHaveBeenCalledWith(LIMITS_PATH, { chat_token_budget: 180_000 });
    await waitFor(() => expect(saveBar()).toBeNull());
    expect(toast.success).toHaveBeenCalledWith("사용 제한을 저장했습니다", expect.anything());
  });

  it("저장 중에 더 고친 값은 저장 뒤 재조회가 덮지 않는다(저장 막대도 남는다)", async () => {
    const user = userEvent.setup();
    let stored = 100_000;
    api.get.mockImplementation((p: string) =>
      p === LIMITS_PATH
        ? Promise.resolve({ ...DEFAULTS, chat_token_budget: stored })
        : Promise.resolve([]),
    );
    let finishPut: (() => void) | undefined;
    api.put.mockImplementation(
      (_path: string, body: { chat_token_budget: number }) =>
        new Promise((resolve) => {
          finishPut = () => {
            stored = body.chat_token_budget;
            resolve(body);
          };
        }),
    );
    renderPage();
    const input = await screen.findByLabelText("1회 토큰 한도");

    await user.clear(input);
    await user.type(input, "120000");
    await user.click(screen.getByRole("button", { name: "변경 사항 저장" }));
    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
    await user.clear(input);
    await user.type(input, "130000");
    finishPut?.();

    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2)); // 저장 뒤 재조회
    await waitFor(() => expect(toast.success).toHaveBeenCalled());
    expect(input).toHaveValue(130_000);
    expect(saveBar()).not.toBeNull();
  });

  it("되돌리기는 원래 값으로 돌리고 저장하지 않는다", async () => {
    const user = userEvent.setup();
    mockLimits();
    renderPage();
    const input = await screen.findByLabelText("1회 토큰 한도");
    await user.clear(input);
    await user.type(input, "50000");
    await user.click(screen.getByRole("button", { name: "되돌리기" }));
    expect(input).toHaveValue(100_000);
    expect(saveBar()).toBeNull();
    expect(api.put).not.toHaveBeenCalled();
  });

  it.each(["5000", "250000"])("범위를 벗어난 값(%s)은 저장하지 않는다", async (value) => {
    const user = userEvent.setup();
    mockLimits();
    renderPage();
    const input = await screen.findByLabelText("1회 토큰 한도");
    await user.clear(input);
    await user.type(input, value);
    await user.click(screen.getByRole("button", { name: "변경 사항 저장" }));
    expect(api.put).not.toHaveBeenCalled();
    expect(saveBar()).not.toBeNull();
  });

  it("저장이 실패하면 오류 토스트를 띄우고 막대를 남긴다", async () => {
    const user = userEvent.setup();
    mockLimits();
    api.put.mockRejectedValue(new Error("boom"));
    renderPage();
    const input = await screen.findByLabelText("1회 토큰 한도");
    await user.clear(input);
    await user.type(input, "120000");
    await user.click(screen.getByRole("button", { name: "변경 사항 저장" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(toast.error.mock.calls[0][0]).toContain("사용 제한을 저장");
    expect(saveBar()).not.toBeNull();
  });

  it("불러오기에 실패하면 오류와 다시 시도를 보여 준다", async () => {
    const user = userEvent.setup();
    let fail = true;
    api.get.mockImplementation(() =>
      fail ? Promise.reject(new Error("down")) : Promise.resolve(DEFAULTS),
    );
    renderPage();
    expect(await screen.findByText("사용 제한을 불러오지 못했습니다")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    expect(await screen.findByLabelText("1회 토큰 한도")).toHaveValue(100_000);
  });
});

describe("AdminLimitsPage — 채팅·원문 읽기·승인 대기 한도(#228)", () => {
  // [라벨, 기본값, 범위·기본값 문구, 단위] — 서버 `tests/test_admin_limits.py::test_limit_bounds_match_web_form`과 같은 숫자
  const FIELDS: Array<[string, number, string, string | null]> = [
    ["1회 토큰 한도", 100_000, "범위 10,000~200,000 · 기본값 100,000", "토큰"],
    ["1회 비용 한도", 0.5, "범위 0.05~2.00 · 기본값 0.50", "USD"],
    ["최대 도구 반복", 6, "범위 1~12 · 기본값 6", "회"],
    ["턴당 출력 토큰", 4096, "범위 512~16,384 · 기본값 4,096", "토큰"],
    ["대화 이력 길이", 6, "범위 0~20 · 기본값 6", "개"],
    ["1회 최대 소스 수", 5, "범위 1~20 · 기본값 5", "개"],
    ["1회 최대 글자 수", 60_000, "범위 5,000~200,000 · 기본값 60,000", "자"],
    ["남은 토큰 한도 비율", 0.3, "범위 0~1 · 기본값 0.3", null],
    ["승인 대기 시간", 10, "범위 1~1,440 · 기본값 10", "분"],
  ];

  it.each(FIELDS)("%s — 적용 값과 범위·기본값·단위를 보인다", async (label, value, range, unit) => {
    mockLimits();
    renderPage();
    const input = await screen.findByLabelText(label);
    expect(input).toHaveValue(value);
    expect(input).toHaveAccessibleDescription(expect.stringContaining(range));
    const field = input.closest<HTMLElement>('[data-slot="form-field"]');
    expect(field).not.toBeNull();
    if (unit) expect(within(field!).getByText(unit)).toBeInTheDocument();
  });

  it("항목을 쓰임새별 소제목으로 묶는다", async () => {
    mockLimits();
    renderPage();
    await screen.findByLabelText("1회 토큰 한도");
    const headings = screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(headings).toEqual(["채팅 1회 한도", "채팅 답변", "원문 읽기", "에이전트 승인"]);
  });

  it("여러 항목을 바꾸면 바꾼 항목만 PUT에 싣는다", async () => {
    const user = userEvent.setup();
    mockLimits();
    api.put.mockImplementation((_path: string, body: unknown) =>
      Promise.resolve({ ...DEFAULTS, ...(body as object) }),
    );
    renderPage();

    const cost = await screen.findByLabelText("1회 비용 한도");
    await user.clear(cost);
    await user.type(cost, "1.25");
    const history = screen.getByLabelText("대화 이력 길이");
    await user.clear(history);
    await user.type(history, "0");
    const approval = screen.getByLabelText("승인 대기 시간");
    await user.clear(approval);
    await user.type(approval, "30");
    await user.click(screen.getByRole("button", { name: "변경 사항 저장" }));

    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
    expect(api.put).toHaveBeenCalledWith(LIMITS_PATH, {
      chat_cost_budget_usd: 1.25,
      chat_history_messages: 0,
      approval_timeout_minutes: 30,
    });
    await waitFor(() => expect(saveBar()).toBeNull());
  });

  it("편집 중 재조회되면 편집하지 않은 항목만 새 서버 값으로 바뀌고, PUT은 편집한 항목만 싣는다", async () => {
    const user = userEvent.setup();
    let server = { ...DEFAULTS };
    api.get.mockImplementation((p: string) =>
      p === LIMITS_PATH ? Promise.resolve(server) : Promise.resolve([]),
    );
    api.put.mockImplementation((_path: string, body: unknown) =>
      Promise.resolve({ ...server, ...(body as object) }),
    );
    const { queryClient } = renderPage();
    const cost = await screen.findByLabelText("1회 비용 한도");
    await user.clear(cost);
    await user.type(cost, "1.25");

    // 그사이 다른 관리자가 최대 도구 반복을 2로 바꿨다
    server = { ...DEFAULTS, chat_max_iters: 2 };
    await act(() => queryClient.invalidateQueries());
    await waitFor(() => expect(screen.getByLabelText("최대 도구 반복")).toHaveValue(2));
    expect(cost).toHaveValue(1.25);
    expect(saveBar()).not.toBeNull();

    await user.click(screen.getByRole("button", { name: "변경 사항 저장" }));
    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
    expect(api.put).toHaveBeenCalledWith(LIMITS_PATH, { chat_cost_budget_usd: 1.25 });
  });

  it("값을 바꿨다가 원래 값으로 돌리면 저장 막대가 사라진다", async () => {
    const user = userEvent.setup();
    mockLimits();
    renderPage();
    const ratio = await screen.findByLabelText("남은 토큰 한도 비율");
    await user.clear(ratio);
    await user.type(ratio, "0.5");
    expect(saveBar()).not.toBeNull();
    await user.clear(ratio);
    await user.type(ratio, "0.3");
    expect(saveBar()).toBeNull();
  });

  it.each([
    ["1회 비용 한도", "2.5"],
    ["1회 비용 한도", "0.01"],
    ["최대 도구 반복", "13"],
    ["턴당 출력 토큰", "256"],
    ["대화 이력 길이", "21"],
    ["1회 최대 소스 수", "0"],
    ["1회 최대 글자 수", "4000"],
    ["남은 토큰 한도 비율", "1.5"],
    ["승인 대기 시간", "1441"],
  ])("%s의 범위 밖 값(%s)은 저장하지 않는다", async (label, value) => {
    const user = userEvent.setup();
    mockLimits();
    renderPage();
    const input = await screen.findByLabelText(label);
    await user.clear(input);
    await user.type(input, value);
    await user.click(screen.getByRole("button", { name: "변경 사항 저장" }));
    expect(input).toBeInvalid();
    expect(api.put).not.toHaveBeenCalled();
    expect(saveBar()).not.toBeNull();
  });
});
